//! Signal through `signal-cli`.
//!
//! signal-cli locks its account, so sending and receiving share one
//! process: an already-running HTTP daemon when one answers on
//! 127.0.0.1:8080, otherwise a `signal-cli jsonRpc` child we start.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use anyhow::{Context, bail};
use base64::Engine;
use futures_util::StreamExt;
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::ChildStdin;
use tokio::sync::{Mutex, mpsc, oneshot};

use super::voice::MAX_UPLOAD_BYTES;
use super::{Content, Inbound, same_contact};
use crate::config::{ContactConfig, Service};
use crate::exec::{self, Daemon, Job};
use crate::ui;

pub const DAEMON: &str = "http://127.0.0.1:8080";
const SEND_TIMEOUT: Duration = Duration::from_secs(60);
const ATTACHMENT_WAIT: Duration = Duration::from_secs(60);
/// The daemon sends a keepalive every 15 s.
const STREAM_TIMEOUT: Duration = Duration::from_secs(30);

enum Link {
    Http {
        base: String,
        client: reqwest::Client,
    },
    Rpc(Box<Rpc>),
}

/// Our own `signal-cli jsonRpc` process.
struct Rpc {
    stdin: Mutex<ChildStdin>,
    pending: Arc<Mutex<HashMap<u64, oneshot::Sender<Value>>>>,
    daemon: Daemon,
}

pub struct Signal {
    link: Link,
    to: String,
    account: String,
    next_id: AtomicU64,
    tasks: Vec<tokio::task::JoinHandle<()>>,
}

pub fn attachments_dir(contact: &ContactConfig) -> PathBuf {
    match &contact.attachments_dir {
        Some(dir) => crate::paths::expand_home(dir),
        None => crate::paths::data_home().join("signal-cli/attachments"),
    }
}

/// Turn one event into an inbound message, if it is a text or voice note
/// from the configured contact. Calls, groups, reactions, and copies of our
/// own sent messages are ignored.
pub fn parse_event(
    payload: &Value,
    contact: &ContactConfig,
    attachments: &Path,
) -> Option<Inbound> {
    let params = if payload["method"] == "receive" {
        &payload["params"]
    } else {
        payload
    };
    if !contact.account.is_empty()
        && params
            .get("account")
            .is_some_and(|a| a != contact.account.as_str())
    {
        return None;
    }
    let envelope = params.get("envelope").unwrap_or(params);
    if envelope.get("callMessage").is_some() || envelope.get("typingMessage").is_some() {
        return None;
    }
    let data = envelope.get("dataMessage")?;
    let source = envelope["sourceNumber"]
        .as_str()
        .or(envelope["source"].as_str())
        .unwrap_or("");
    if !same_contact(&contact.to, source)
        || data.get("groupInfo").is_some()
        || data.get("reaction").is_some()
    {
        return None;
    }
    let stamp = envelope["timestamp"]
        .as_i64()
        .map(|t| t.to_string())
        .unwrap_or_else(|| source.to_string());
    for attachment in data["attachments"].as_array().into_iter().flatten() {
        let voice = attachment["isVoiceNote"] == true
            || attachment["contentType"]
                .as_str()
                .is_some_and(|t| t.starts_with("audio/"));
        let id = attachment["id"].as_str().unwrap_or("");
        let safe = !id.is_empty() && id != "." && id != ".." && !id.contains('/');
        if voice && safe {
            return Some(Inbound {
                service: Service::Signal,
                id: format!("{stamp}:{id}"),
                content: Content::Voice(attachments.join(id)),
            });
        }
    }
    let text = data["message"].as_str().unwrap_or("").trim();
    (!text.is_empty()).then(|| Inbound {
        service: Service::Signal,
        id: stamp,
        content: Content::Text(text.to_string()),
    })
}

/// A sentence for a refused send.
fn failure(error: &Value) -> String {
    let unregistered = error["data"]["response"]["results"]
        .as_array()
        .is_some_and(|r| r.iter().any(|x| x["type"] == "UNREGISTERED_FAILURE"));
    if unregistered {
        "Message not sent. That number is not a Signal account.".into()
    } else {
        "Message not sent.".into()
    }
}

/// Deliver a parsed event, waiting for a voice attachment file to appear.
fn forward(found: Inbound, inbox: &mpsc::Sender<Inbound>) {
    let inbox = inbox.clone();
    tokio::spawn(async move {
        if let Content::Voice(path) = &found.content {
            let deadline = tokio::time::Instant::now() + ATTACHMENT_WAIT;
            while !path.is_file() {
                if tokio::time::Instant::now() >= deadline {
                    ui::error!(
                        "A Signal voice message never appeared on disk; check messaging.signal.attachments_dir."
                    );
                    return;
                }
                tokio::time::sleep(Duration::from_millis(250)).await;
            }
        }
        let _ = inbox.send(found).await;
    });
}

impl Signal {
    pub async fn start(
        contact: &ContactConfig,
        inbox: mpsc::Sender<Inbound>,
    ) -> anyhow::Result<Signal> {
        Signal::start_with(contact, inbox, DAEMON).await
    }

    pub async fn start_with(
        contact: &ContactConfig,
        inbox: mpsc::Sender<Inbound>,
        daemon_url: &str,
    ) -> anyhow::Result<Signal> {
        let attachments = attachments_dir(contact);
        let client = reqwest::Client::builder()
            .no_proxy()
            .connect_timeout(Duration::from_secs(5))
            .build()?;
        let daemon_up = client
            .get(format!("{daemon_url}/api/v1/check"))
            .timeout(Duration::from_millis(500))
            .send()
            .await
            .is_ok_and(|r| r.status().is_success());
        let mut tasks = Vec::new();
        let link = if daemon_up {
            ui::status!("Signal: using the signal-cli daemon at {daemon_url}.");
            let mut url = format!("{daemon_url}/api/v1/events");
            if !contact.account.is_empty() {
                url.push_str(&format!("?account={}", contact.account.replace('+', "%2B")));
            }
            let (contact, client2) = (contact.clone(), client.clone());
            tasks.push(tokio::spawn(async move {
                loop {
                    let connect =
                        tokio::time::timeout(Duration::from_secs(10), client2.get(&url).send());
                    if let Ok(Ok(response)) = connect.await {
                        let mut lines = response.bytes_stream();
                        let mut buffer = Vec::new();
                        // A missed keepalive, error, or end of stream reconnects.
                        while let Ok(Some(Ok(chunk))) =
                            tokio::time::timeout(STREAM_TIMEOUT, lines.next()).await
                        {
                            buffer.extend_from_slice(&chunk);
                            while let Some(pos) = buffer.iter().position(|&b| b == b'\n') {
                                let line: Vec<u8> = buffer.drain(..=pos).collect();
                                let line = String::from_utf8_lossy(&line);
                                let Some(data) = line.trim().strip_prefix("data:") else {
                                    continue;
                                };
                                if let Ok(payload) = serde_json::from_str::<Value>(data.trim())
                                    && let Some(found) =
                                        parse_event(&payload, &contact, &attachments)
                                {
                                    forward(found, &inbox);
                                }
                            }
                            if buffer.len() > 1024 * 1024 {
                                buffer.clear();
                            }
                        }
                    }
                    if inbox.is_closed() {
                        return;
                    }
                    tokio::time::sleep(Duration::from_secs(1)).await;
                }
            }));
            Link::Http {
                base: daemon_url.to_string(),
                client,
            }
        } else {
            let program =
                exec::find("signal-cli").context("Signal messaging needs signal-cli on PATH")?;
            let mut job = Job::new(program, "signal-cli").env(exec::inherit(&[
                "HOME",
                "PATH",
                "LANG",
                "LC_ALL",
                "TZ",
                "JAVA_HOME",
                "XDG_DATA_HOME",
                "XDG_CONFIG_HOME",
                "XDG_CACHE_HOME",
            ]));
            if !contact.account.is_empty() {
                job = job.args(["-a", &contact.account]);
            }
            let mut command = job
                .args([
                    "jsonRpc",
                    "--receive-mode",
                    "on-start",
                    "--ignore-stories",
                    "--ignore-avatars",
                    "--ignore-stickers",
                ])
                .command();
            command
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::null());
            let mut daemon = Daemon::spawn(command, "signal-cli")?;
            let stdin = daemon.child.stdin.take().context("signal-cli stdin")?;
            let stdout = daemon.child.stdout.take().context("signal-cli stdout")?;
            let pending: Arc<Mutex<HashMap<u64, oneshot::Sender<Value>>>> = Arc::default();
            let (waiters, contact) = (pending.clone(), contact.clone());
            tasks.push(tokio::spawn(async move {
                let mut lines = BufReader::new(stdout).lines();
                while let Ok(Some(line)) = lines.next_line().await {
                    let Ok(payload) = serde_json::from_str::<Value>(&line) else {
                        continue;
                    };
                    if let Some(id) = payload["id"].as_u64() {
                        if let Some(waiter) = waiters.lock().await.remove(&id) {
                            let _ = waiter.send(payload);
                        }
                    } else if let Some(found) = parse_event(&payload, &contact, &attachments) {
                        forward(found, &inbox);
                    }
                }
                ui::error!("signal-cli stopped; Signal messages are unavailable until restart.");
            }));
            ui::status!("Signal: started signal-cli jsonRpc.");
            Link::Rpc(Box::new(Rpc {
                stdin: Mutex::new(stdin),
                pending,
                daemon,
            }))
        };
        Ok(Signal {
            link,
            to: contact.to.clone(),
            account: contact.account.clone(),
            next_id: AtomicU64::new(1),
            tasks,
        })
    }

    fn params(&self, text: Option<&str>, attachment: Option<String>) -> Value {
        let mut params = json!({"recipient": [self.to]});
        if let Some(text) = text {
            params["message"] = json!(text);
        }
        if let Some(attachment) = attachment {
            params["attachments"] = json!([attachment]);
            params["voiceNote"] = json!(true);
        }
        if !self.account.is_empty() {
            params["account"] = json!(self.account);
        }
        params
    }

    async fn call(&self, params: Value) -> anyhow::Result<()> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let request = json!({"jsonrpc": "2.0", "method": "send", "params": params, "id": id});
        let reply = match &self.link {
            Link::Http { base, client } => {
                let response = client
                    .post(format!("{base}/api/v1/rpc"))
                    .json(&request)
                    .timeout(SEND_TIMEOUT)
                    .send()
                    .await
                    .map_err(|_| anyhow::anyhow!("Message not sent."))?;
                crate::http::json(response, 1024 * 1024, "signal-cli")
                    .await
                    .map_err(|_| anyhow::anyhow!("Message not sent."))?
            }
            Link::Rpc(rpc) => {
                let Rpc { stdin, pending, .. } = rpc.as_ref();
                let (tx, rx) = oneshot::channel();
                pending.lock().await.insert(id, tx);
                let line = format!("{request}\n");
                // Writing and waiting share one deadline; a stuck signal-cli
                // cannot hold up the radio.
                let exchange = async {
                    stdin.lock().await.write_all(line.as_bytes()).await.ok()?;
                    rx.await.ok()
                };
                match tokio::time::timeout(SEND_TIMEOUT, exchange).await {
                    Ok(Some(reply)) => reply,
                    _ => {
                        pending.lock().await.remove(&id);
                        bail!("Message not sent.");
                    }
                }
            }
        };
        if let Some(error) = reply.get("error").filter(|e| !e.is_null()) {
            bail!(failure(error));
        }
        if reply.get("result").is_none() {
            bail!("Message not sent.");
        }
        Ok(())
    }

    pub async fn send_text(&self, text: &str) -> anyhow::Result<()> {
        self.call(self.params(Some(text), None)).await
    }

    pub async fn send_voice(&self, file: &Path) -> anyhow::Result<()> {
        let attachment = match &self.link {
            // A separate daemon may not see our temporary files; embed the audio.
            Link::Http { .. } => {
                let bytes = std::fs::read(file)?;
                anyhow::ensure!(
                    !bytes.is_empty() && bytes.len() as u64 <= MAX_UPLOAD_BYTES,
                    "Message not sent. Voice message too large."
                );
                format!(
                    "data:audio/ogg;filename=voice.ogg;base64,{}",
                    base64::engine::general_purpose::STANDARD.encode(bytes)
                )
            }
            Link::Rpc(_) => file.display().to_string(),
        };
        self.call(self.params(None, Some(attachment))).await
    }

    pub async fn stop(self) {
        for task in &self.tasks {
            task.abort();
        }
        if let Link::Rpc(rpc) = self.link {
            rpc.daemon.stop().await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn contact() -> ContactConfig {
        toml::from_str("wake_phrase = \"grandma\"\nto = \"+15557654321\"\n").unwrap()
    }

    #[test]
    fn text_from_the_contact_is_accepted() {
        let event = json!({"envelope": {"sourceNumber": "+15557654321", "timestamp": 42, "dataMessage": {"message": "Hello"}}});
        let found = parse_event(&event, &contact(), Path::new("/a")).unwrap();
        assert_eq!(found.content, Content::Text("Hello".into()));
        assert_eq!(found.id, "42");
    }

    #[test]
    fn others_groups_syncs_and_calls_are_ignored() {
        let dir = Path::new("/a");
        let other = json!({"envelope": {"sourceNumber": "+19990000000", "timestamp": 1, "dataMessage": {"message": "x"}}});
        let group = json!({"envelope": {"sourceNumber": "+15557654321", "timestamp": 1, "dataMessage": {"message": "x", "groupInfo": {}}}});
        let sync = json!({"envelope": {"sourceNumber": "+15557654321", "timestamp": 1, "syncMessage": {"sentMessage": {"message": "x"}}}});
        let call = json!({"envelope": {"sourceNumber": "+15557654321", "timestamp": 1, "callMessage": {}}});
        for event in [other, group, sync, call] {
            assert!(parse_event(&event, &contact(), dir).is_none(), "{event}");
        }
    }

    #[test]
    fn voice_notes_point_into_the_attachments_dir() {
        let event = json!({"method": "receive", "params": {"envelope": {"sourceNumber": "+15557654321", "timestamp": 7,
            "dataMessage": {"attachments": [{"id": "abc.m4a", "contentType": "audio/aac"}]}}}});
        let found = parse_event(&event, &contact(), Path::new("/att")).unwrap();
        assert_eq!(found.content, Content::Voice(PathBuf::from("/att/abc.m4a")));
        let sneaky = json!({"envelope": {"sourceNumber": "+15557654321", "timestamp": 7,
            "dataMessage": {"attachments": [{"id": "../../etc/passwd", "contentType": "audio/aac"}]}}});
        assert!(parse_event(&sneaky, &contact(), Path::new("/att")).is_none());
    }

    #[test]
    fn unregistered_numbers_get_a_clear_failure() {
        let error = json!({"data": {"response": {"results": [{"type": "UNREGISTERED_FAILURE"}]}}});
        assert!(failure(&error).contains("not a Signal account"));
    }
}
