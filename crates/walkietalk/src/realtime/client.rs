//! The xAI realtime WebSocket: connecting, sending commands, and typed
//! server events. Nothing here touches audio devices or PTT.

use std::collections::HashMap;
use std::time::Duration;

use anyhow::{Context, bail};
use base64::Engine;
use futures_util::stream::SplitSink;
use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream};

use crate::credentials::Secret;

type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;

/// Events kept in memory before the turn is abandoned.
const INBOX: usize = 256;

/// Credentials were refused; retrying cannot help.
#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct AuthError(pub String);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    User,
    Assistant,
    Other,
}

/// A server event the bridge acts on.
#[derive(Debug, Clone, PartialEq)]
pub enum Event {
    SessionUpdated,
    Committed { item_id: String },
    InputTranscript { item_id: String, text: String, done: bool },
    InputTranscriptFailed,
    ItemDeleted { item_id: String },
    AudioDelta(Vec<i16>),
    AudioDone,
    OutputTranscriptDelta(String),
    OutputTranscriptDone(String),
    /// A tool call ended a stretch of speech.
    ToolCall,
    ResponseDone { status: String },
    Other(String),
}

pub struct Client {
    sink: SplitSink<Socket, Message>,
    inbox: mpsc::Receiver<anyhow::Result<(Event, Value)>>,
    reader: tokio::task::JoinHandle<()>,
    /// Conversation items the server holds, by ID.
    pub items: HashMap<String, Role>,
    /// Item order, oldest first.
    pub order: Vec<String>,
}

/// Add the model as a query parameter, keeping any existing ones.
pub fn session_url(base: &str, model: &str) -> String {
    let separator = if base.contains('?') { '&' } else { '?' };
    format!("{base}{separator}model={model}")
}

impl Client {
    pub async fn connect(url: &str, model: &str, key: &Secret, timeout: Duration) -> anyhow::Result<Client> {
        let mut request = session_url(url, model)
            .into_client_request()
            .context("invalid realtime URL")?;
        request.headers_mut().insert(
            "Authorization",
            format!("Bearer {}", key.expose()).parse().context("invalid realtime API key")?,
        );
        let connect = tokio_tungstenite::connect_async(request);
        let (socket, _) = match tokio::time::timeout(timeout, connect).await {
            Err(_) => bail!("realtime connection timed out"),
            Ok(Err(tokio_tungstenite::tungstenite::Error::Http(response))) => {
                let status = response.status().as_u16();
                if status == 401 || status == 403 {
                    bail!(AuthError(format!("realtime authentication failed (HTTP {status}); check the billed API key")));
                }
                bail!("realtime connection refused (HTTP {status})");
            }
            Ok(Err(err)) => bail!("realtime connection failed: {}", describe(&err)),
            Ok(Ok(pair)) => pair,
        };
        let (sink, mut stream) = socket.split();
        let (tx, inbox) = mpsc::channel(INBOX);
        let reader = tokio::spawn(async move {
            while let Some(message) = stream.next().await {
                let parsed = match message {
                    Ok(Message::Text(text)) => parse(&text),
                    Ok(Message::Binary(_)) => Err(anyhow::anyhow!("realtime sent an unexpected binary frame")),
                    Ok(Message::Close(_)) => Err(anyhow::anyhow!("realtime connection closed")),
                    Ok(_) => continue,
                    Err(err) => Err(anyhow::anyhow!("realtime connection lost: {}", describe(&err))),
                };
                let stop = parsed.is_err();
                match parsed {
                    Ok((Event::Other(kind), _)) if kind == "ping" => continue,
                    other => {
                        if tx.try_send(other).is_err() {
                            let _ = tx.try_send(Err(anyhow::anyhow!("realtime events arrived faster than handled; turn discarded")));
                            return;
                        }
                    }
                }
                if stop {
                    return;
                }
            }
            let _ = tx.try_send(Err(anyhow::anyhow!("realtime connection closed")));
        });
        Ok(Client {
            sink,
            inbox,
            reader,
            items: HashMap::new(),
            order: Vec::new(),
        })
    }

    pub async fn send(&mut self, command: Value) -> anyhow::Result<()> {
        self.sink
            .send(Message::Text(command.to_string().into()))
            .await
            .map_err(|err| anyhow::anyhow!("realtime send failed: {}", describe(&err)))
    }

    /// Configure the session and wait for the server to accept it.
    pub async fn configure(&mut self, session: Value, timeout: Duration) -> anyhow::Result<()> {
        self.send(json!({"type": "session.update", "session": session})).await?;
        self.wait_for(timeout, |e| matches!(e, Event::SessionUpdated)).await.map(|_| ())
    }

    pub async fn append_audio(&mut self, pcm: &[i16]) -> anyhow::Result<()> {
        let bytes: Vec<u8> = pcm.iter().flat_map(|s| s.to_le_bytes()).collect();
        let audio = base64::engine::general_purpose::STANDARD.encode(bytes);
        self.send(json!({"type": "input_audio_buffer.append", "audio": audio})).await
    }

    /// The next event, or an error after `timeout` of silence.
    pub async fn next(&mut self, timeout: Duration) -> anyhow::Result<Event> {
        let received = tokio::time::timeout(timeout, self.inbox.recv())
            .await
            .map_err(|_| anyhow::anyhow!("realtime server went quiet for {:.0}s", timeout.as_secs_f64()))?;
        let (event, raw) = received.context("realtime connection closed")??;
        self.track(&event, &raw);
        Ok(event)
    }

    /// Read events until one matches, within `timeout` in total.
    pub async fn wait_for(&mut self, timeout: Duration, matches: impl Fn(&Event) -> bool) -> anyhow::Result<Event> {
        let deadline = tokio::time::Instant::now() + timeout;
        loop {
            let left = deadline.saturating_duration_since(tokio::time::Instant::now());
            if left.is_zero() {
                bail!("realtime server did not respond in time");
            }
            let event = self.next(left).await?;
            if matches(&event) {
                return Ok(event);
            }
        }
    }

    /// Delete an item and wait for the server to confirm.
    pub async fn delete(&mut self, item_id: &str, timeout: Duration) -> anyhow::Result<()> {
        self.send(json!({"type": "conversation.item.delete", "item_id": item_id})).await?;
        let id = item_id.to_string();
        self.wait_for(timeout, |e| matches!(e, Event::ItemDeleted { item_id } if *item_id == id)).await.map(|_| ())
    }

    fn remember(&mut self, id: &str, role: Role) {
        if self.items.insert(id.to_string(), role).is_none() {
            self.order.push(id.to_string());
        }
    }

    /// Track which conversation items exist, so whole turns can be removed.
    fn track(&mut self, event: &Event, raw: &Value) {
        match event {
            Event::Committed { item_id } => self.remember(item_id, Role::User),
            Event::ItemDeleted { item_id } => {
                self.items.remove(item_id);
                self.order.retain(|id| id != item_id);
            }
            _ => {}
        }
        let mut items = Vec::new();
        match raw["type"].as_str() {
            Some("conversation.item.added" | "response.output_item.added" | "response.output_item.done") => {
                items.push(&raw["item"]);
            }
            Some("response.done") => {
                if let Some(output) = raw["response"]["output"].as_array() {
                    items.extend(output.iter());
                }
            }
            _ => {}
        }
        for item in items {
            if let Some(id) = item["id"].as_str().filter(|id| !id.is_empty()) {
                let role = match item["role"].as_str().or(item["type"].as_str()) {
                    Some("user") => Role::User,
                    Some("assistant") => Role::Assistant,
                    _ => Role::Other,
                };
                self.remember(id, role);
            }
        }
    }

    pub async fn close(mut self) {
        let _ = tokio::time::timeout(Duration::from_secs(2), self.sink.close()).await;
        self.reader.abort();
    }
}

impl Drop for Client {
    fn drop(&mut self) {
        self.reader.abort();
    }
}

fn describe(err: &tokio_tungstenite::tungstenite::Error) -> String {
    use tokio_tungstenite::tungstenite::Error as E;
    match err {
        E::ConnectionClosed | E::AlreadyClosed => "connection closed".into(),
        E::Io(io) => io.kind().to_string(),
        E::Tls(_) => "TLS error".into(),
        E::Url(_) => "invalid URL".into(),
        _ => "protocol error".into(),
    }
}

fn text(value: &Value) -> String {
    value.as_str().unwrap_or("").to_string()
}

/// Parse one server message into an event plus its raw JSON.
pub fn parse(message: &str) -> anyhow::Result<(Event, Value)> {
    let raw: Value = serde_json::from_str(message).map_err(|_| anyhow::anyhow!("realtime sent malformed JSON"))?;
    let kind = raw["type"].as_str().context("realtime event without a type")?;
    let event = match kind {
        "session.updated" => Event::SessionUpdated,
        "input_audio_buffer.committed" => Event::Committed {
            item_id: raw["item_id"].as_str().filter(|s| !s.is_empty()).context("commit without an item ID")?.into(),
        },
        "conversation.item.input_audio_transcription.completed" | "conversation.item.input_audio_transcription.updated" => {
            Event::InputTranscript {
                item_id: text(&raw["item_id"]),
                text: text(&raw["transcript"]).trim().to_string(),
                done: kind.ends_with(".completed"),
            }
        }
        "conversation.item.input_audio_transcription.failed" => Event::InputTranscriptFailed,
        "conversation.item.added" => {
            // Some servers deliver the input transcript on the item itself.
            let transcript: Vec<String> = raw["item"]["content"]
                .as_array()
                .map(|parts| parts.iter().filter_map(|p| p["transcript"].as_str()).map(|t| t.trim().to_string()).filter(|t| !t.is_empty()).collect())
                .unwrap_or_default();
            if transcript.is_empty() {
                Event::Other(kind.into())
            } else {
                Event::InputTranscript { item_id: text(&raw["item"]["id"]), text: transcript.join(" "), done: false }
            }
        }
        "conversation.item.deleted" => Event::ItemDeleted { item_id: text(&raw["item_id"]) },
        "response.output_audio.delta" => {
            let b64 = raw["delta"].as_str().or(raw["audio"].as_str()).unwrap_or("");
            let bytes = base64::engine::general_purpose::STANDARD
                .decode(b64)
                .map_err(|_| anyhow::anyhow!("realtime sent invalid audio"))?;
            anyhow::ensure!(bytes.len() % 2 == 0, "realtime sent an incomplete audio sample");
            Event::AudioDelta(bytes.chunks_exact(2).map(|b| i16::from_le_bytes([b[0], b[1]])).collect())
        }
        "response.output_audio.done" => Event::AudioDone,
        "response.output_audio_transcript.delta" => Event::OutputTranscriptDelta(text(raw.get("delta").unwrap_or(&raw["transcript"]))),
        "response.output_audio_transcript.done" => Event::OutputTranscriptDone(text(&raw["transcript"])),
        "response.function_call_arguments.done" => Event::ToolCall,
        "response.done" => Event::ResponseDone {
            status: raw["response"]["status"].as_str().unwrap_or("completed").to_string(),
        },
        "error" => {
            let detail = raw["error"]["message"]
                .as_str()
                .or(raw["error"]["code"].as_str())
                .or(raw["message"].as_str())
                .unwrap_or("unknown error")
                .chars()
                .take(200)
                .collect::<String>();
            let lower = detail.to_lowercase();
            if ["auth", "unauthorized", "forbidden", "api key", "invalid key"].iter().any(|w| lower.contains(w)) {
                bail!(AuthError(format!("realtime authentication failed: {detail}")));
            }
            bail!("realtime error: {detail}");
        }
        other => Event::Other(other.into()),
    };
    Ok((event, raw))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn audio_deltas_decode_to_samples() {
        let b64 = base64::engine::general_purpose::STANDARD.encode([1u8, 0, 0xff, 0xff]);
        let (event, _) = parse(&json!({"type": "response.output_audio.delta", "delta": b64}).to_string()).unwrap();
        assert_eq!(event, Event::AudioDelta(vec![1, -1]));
    }

    #[test]
    fn errors_are_classified() {
        let auth = parse(r#"{"type":"error","error":{"message":"Invalid API key"}}"#).unwrap_err();
        assert!(auth.downcast_ref::<AuthError>().is_some());
        let other = parse(r#"{"type":"error","error":{"message":"rate limited"}}"#).unwrap_err();
        assert!(other.downcast_ref::<AuthError>().is_none());
        assert!(parse("not json").is_err());
    }

    #[test]
    fn model_is_added_to_the_url() {
        assert_eq!(session_url("wss://x/v1/realtime", "m"), "wss://x/v1/realtime?model=m");
        assert_eq!(session_url("wss://x/rt?a=1", "m"), "wss://x/rt?a=1&model=m");
    }
}
