//! WhatsApp through `wacli`.
//!
//! `wacli sync --follow` keeps the local store current; new messages are
//! read from its SQLite database. History from before startup is ignored.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};

use anyhow::{Context, bail};
use tokio::sync::mpsc;

use super::{Content, Inbound, same_contact};
use crate::config::{ContactConfig, Service};
use crate::exec::{self, Daemon, Job};
use crate::ui;

const VOICE_MEDIA: &[&str] = &["audio", "ptt", "voice"];
const MEDIA_WAIT: Duration = Duration::from_secs(60);
const SEND_TIMEOUT: Duration = Duration::from_secs(60);

pub struct WhatsApp {
    store: PathBuf,
    to: String,
    daemon: Option<Daemon>,
    poller: tokio::task::JoinHandle<()>,
}

/// `WACLI_STORE_DIR`, else `~/.wacli` if it exists, else the XDG state dir.
pub fn store_dir() -> PathBuf {
    if let Some(dir) = std::env::var_os("WACLI_STORE_DIR") {
        return crate::paths::expand_home(Path::new(&dir));
    }
    let legacy = crate::paths::home().join(".wacli");
    if legacy.exists() {
        return legacy;
    }
    crate::paths::state_home().join("wacli")
}

fn env() -> HashMap<String, String> {
    exec::inherit(&[
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "TZ",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    ])
}

struct Row {
    rowid: i64,
    msg_id: String,
    chat: String,
    from_me: bool,
    text: String,
    media: String,
    local_path: String,
    ts: i64,
}

struct Poller {
    db: PathBuf,
    to: String,
    started: i64,
    last_rowid: i64,
    waiting: HashMap<i64, Instant>,
    seen: HashSet<String>,
}

impl Poller {
    fn query(&self) -> rusqlite::Result<Vec<Row>> {
        let conn = rusqlite::Connection::open_with_flags(
            &self.db,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
        )?;
        conn.busy_timeout(Duration::from_millis(500))?;
        let columns = "rowid, msg_id, chat_jid, from_me, coalesce(text, display_text, ''), coalesce(media_type, ''), coalesce(local_path, ''), ts";
        let map = |r: &rusqlite::Row| {
            Ok(Row {
                rowid: r.get(0)?,
                msg_id: r.get(1)?,
                chat: r.get(2)?,
                from_me: r.get::<_, i64>(3)? != 0,
                text: r.get(4)?,
                media: r.get(5)?,
                local_path: r.get(6)?,
                ts: r.get(7)?,
            })
        };
        let mut rows: Vec<Row> = conn
            .prepare(&format!(
                "SELECT {columns} FROM messages WHERE rowid > ?1 ORDER BY rowid"
            ))?
            .query_map([self.last_rowid], map)?
            .collect::<Result<_, _>>()?;
        let mut again =
            conn.prepare(&format!("SELECT {columns} FROM messages WHERE rowid = ?1"))?;
        for rowid in self.waiting.keys() {
            rows.extend(
                again
                    .query_map([rowid], map)?
                    .collect::<Result<Vec<_>, _>>()?,
            );
        }
        rows.sort_by_key(|r| (r.ts, r.rowid));
        Ok(rows)
    }

    /// New messages from the contact since startup.
    fn poll(&mut self) -> Vec<Inbound> {
        if !self.db.is_file() {
            return Vec::new();
        }
        let Ok(rows) = self.query() else {
            return Vec::new();
        };
        let mut found = Vec::new();
        for row in rows {
            self.last_rowid = self.last_rowid.max(row.rowid);
            if row.from_me
                || row.msg_id.is_empty()
                || !same_contact(&self.to, &row.chat)
                || self.seen.contains(&row.msg_id)
            {
                continue;
            }
            // History can arrive in later batches; the cutoff applies to all of it.
            if row.ts < self.started {
                self.seen.insert(row.msg_id);
                continue;
            }
            let content = if VOICE_MEDIA.contains(&row.media.as_str()) {
                let path = PathBuf::from(&row.local_path);
                if !row.local_path.is_empty() && path.is_file() {
                    Some(Content::Voice(path))
                } else {
                    let first = *self.waiting.entry(row.rowid).or_insert_with(Instant::now);
                    if first.elapsed() < MEDIA_WAIT {
                        continue;
                    }
                    ui::error!(
                        "A WhatsApp voice message did not download within a minute; skipped."
                    );
                    None
                }
            } else if row.media.is_empty() && !row.text.trim().is_empty() {
                Some(Content::Text(row.text))
            } else {
                None
            };
            self.waiting.remove(&row.rowid);
            self.seen.insert(row.msg_id.clone());
            if let Some(content) = content {
                found.push(Inbound {
                    service: Service::WhatsApp,
                    id: row.msg_id,
                    content,
                });
            }
        }
        found
    }
}

impl WhatsApp {
    pub async fn start(
        contact: &ContactConfig,
        inbox: mpsc::Sender<Inbound>,
    ) -> anyhow::Result<WhatsApp> {
        let program = exec::find("wacli").context("WhatsApp messaging needs wacli on PATH")?;
        let store = store_dir();
        let started = jiff::Timestamp::now().as_second();
        let mut poller = Poller {
            db: store.join("wacli.db"),
            to: contact.to.clone(),
            started,
            last_rowid: 0,
            waiting: HashMap::new(),
            seen: HashSet::new(),
        };
        // Everything already stored is history.
        if poller.db.is_file()
            && let Ok(rows) = poller.query()
        {
            for row in rows {
                poller.last_rowid = poller.last_rowid.max(row.rowid);
                poller.seen.insert(row.msg_id);
            }
        }
        let mut command = Job::new(program, "wacli")
            .arg("--store")
            .arg(store.as_os_str())
            .args(["sync", "--follow", "--download-media"])
            .env(env())
            .command();
        command
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        let daemon = Daemon::spawn(command, "wacli")?;
        let poller = tokio::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_secs(1)).await;
                let (next, found) = tokio::task::spawn_blocking(move || {
                    let found = poller.poll();
                    (poller, found)
                })
                .await
                .expect("WhatsApp poller panicked");
                poller = next;
                for message in found {
                    if inbox.send(message).await.is_err() {
                        return;
                    }
                }
            }
        });
        Ok(WhatsApp {
            store,
            to: contact.to.clone(),
            daemon: Some(daemon),
            poller,
        })
    }

    async fn send(&self, args: &[&std::ffi::OsStr]) -> anyhow::Result<()> {
        let program = exec::find("wacli")?;
        let output = Job::new(program, "wacli")
            .arg("--store")
            .arg(self.store.as_os_str())
            .args(["--json", "send"])
            .args(args)
            .env(env())
            .timeout(SEND_TIMEOUT)
            .max_output(256 * 1024)
            .run()
            .await
            .map_err(|_| anyhow::anyhow!("Message not sent."))?;
        let reply: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap_or_default();
        if !output.success() || reply["success"] != true {
            bail!("Message not sent.");
        }
        Ok(())
    }

    pub async fn send_text(&self, text: &str) -> anyhow::Result<()> {
        self.send(&[
            "text".as_ref(),
            "--to".as_ref(),
            self.to.as_ref(),
            "--message".as_ref(),
            text.as_ref(),
        ])
        .await
    }

    pub async fn send_voice(&self, file: &Path) -> anyhow::Result<()> {
        self.send(&[
            "voice".as_ref(),
            "--to".as_ref(),
            self.to.as_ref(),
            "--file".as_ref(),
            file.as_os_str(),
            "--mime".as_ref(),
            "audio/ogg; codecs=opus".as_ref(),
        ])
        .await
    }

    pub async fn stop(mut self) {
        self.poller.abort();
        if let Some(daemon) = self.daemon.take() {
            daemon.stop().await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn db(dir: &Path) -> PathBuf {
        let path = dir.join("wacli.db");
        let conn = rusqlite::Connection::open(&path).unwrap();
        conn.execute_batch(
            "CREATE TABLE messages (rowid INTEGER PRIMARY KEY AUTOINCREMENT, chat_jid TEXT, msg_id TEXT, ts INTEGER, from_me INTEGER, text TEXT, display_text TEXT, media_type TEXT, local_path TEXT);",
        )
        .unwrap();
        path
    }

    /// (id, chat, ts, from_me, text, media, local_path)
    type TestRow<'a> = (&'a str, &'a str, i64, bool, &'a str, &'a str, &'a str);

    fn insert(path: &Path, (id, chat, ts, from_me, text, media, local): TestRow) {
        let conn = rusqlite::Connection::open(path).unwrap();
        conn.execute(
            "INSERT INTO messages (chat_jid, msg_id, ts, from_me, text, media_type, local_path) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            rusqlite::params![chat, id, ts, from_me as i64, text, media, local],
        )
        .unwrap();
    }

    fn poller(db: PathBuf, started: i64) -> Poller {
        Poller {
            db,
            to: "+1 (555) 123-4567".into(),
            started,
            last_rowid: 0,
            waiting: HashMap::new(),
            seen: HashSet::new(),
        }
    }

    #[test]
    fn only_new_messages_from_the_contact_are_delivered() {
        let dir = tempfile::tempdir().unwrap();
        let path = db(dir.path());
        insert(
            &path,
            (
                "old",
                "15551234567@s.whatsapp.net",
                50,
                false,
                "history",
                "",
                "",
            ),
        );
        insert(
            &path,
            (
                "mine",
                "15551234567@s.whatsapp.net",
                200,
                true,
                "from me",
                "",
                "",
            ),
        );
        insert(
            &path,
            (
                "other",
                "19998887777@s.whatsapp.net",
                200,
                false,
                "someone else",
                "",
                "",
            ),
        );
        insert(
            &path,
            (
                "new",
                "15551234567@s.whatsapp.net",
                200,
                false,
                "hello",
                "",
                "",
            ),
        );
        let mut p = poller(path.clone(), 100);
        let found = p.poll();
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].content, Content::Text("hello".into()));
        assert!(p.poll().is_empty(), "messages are delivered once");
    }

    #[test]
    fn voice_waits_for_its_download() {
        let dir = tempfile::tempdir().unwrap();
        let path = db(dir.path());
        let audio = dir.path().join("note.ogg");
        insert(
            &path,
            (
                "v",
                "15551234567@s.whatsapp.net",
                200,
                false,
                "",
                "ptt",
                audio.to_str().unwrap(),
            ),
        );
        let mut p = poller(path, 100);
        assert!(p.poll().is_empty());
        std::fs::write(&audio, b"ogg").unwrap();
        let found = p.poll();
        assert_eq!(found[0].content, Content::Voice(audio));
    }
}
