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
    /// Polls the store and keeps `wacli sync` running; its group is killed
    /// when the task stops.
    poller: tokio::task::JoinHandle<()>,
}

/// The process holding the store's lock, from wacli's `LOCK` file.
fn lock_holder(lock: &str) -> Option<u32> {
    lock.lines()
        .find_map(|line| line.strip_prefix("pid=")?.trim().parse().ok())
}

/// A `wacli` process (other than ours) that holds the store, if any.
fn external_sync(store: &std::path::Path) -> Option<u32> {
    let pid = lock_holder(&std::fs::read_to_string(store.join("LOCK")).ok()?)?;
    let cmdline = std::fs::read(format!("/proc/{pid}/cmdline")).ok()?;
    String::from_utf8_lossy(&cmdline)
        .contains("wacli")
        .then_some(pid)
}

/// Keeps a sync running: ours, or one that was already running for the
/// same store (wacli allows only one).
enum SyncState {
    Ours(Daemon),
    External(u32),
    WaitUntil(tokio::time::Instant),
}

struct SyncKeeper {
    program: std::path::PathBuf,
    store: std::path::PathBuf,
    state: SyncState,
}

impl SyncKeeper {
    fn start(program: std::path::PathBuf, store: std::path::PathBuf) -> SyncKeeper {
        let state = SyncState::WaitUntil(tokio::time::Instant::now());
        let mut keeper = SyncKeeper {
            program,
            store,
            state,
        };
        keeper.tick();
        keeper
    }

    fn launch(&mut self) -> SyncState {
        if let Some(pid) = external_sync(&self.store) {
            ui::status!("WhatsApp: using the wacli sync already running (pid {pid}).");
            return SyncState::External(pid);
        }
        match sync(&self.program, &self.store) {
            Ok(daemon) => SyncState::Ours(daemon),
            Err(err) => {
                ui::error!("Cannot start wacli sync: {err:#}; retrying in 30 s.");
                SyncState::WaitUntil(tokio::time::Instant::now() + Duration::from_secs(30))
            }
        }
    }

    fn tick(&mut self) {
        let ours_exited = match &mut self.state {
            SyncState::Ours(daemon) => !matches!(daemon.child.try_wait(), Ok(None)),
            _ => false,
        };
        let next = match &self.state {
            SyncState::Ours(_) if ours_exited => match external_sync(&self.store) {
                Some(pid) => {
                    ui::status!(
                        "WhatsApp: another wacli sync (pid {pid}) holds the store; using it."
                    );
                    Some(SyncState::External(pid))
                }
                None => {
                    ui::error!("wacli sync stopped; restarting it in 10 s.");
                    Some(SyncState::WaitUntil(
                        tokio::time::Instant::now() + Duration::from_secs(10),
                    ))
                }
            },
            SyncState::External(pid) if !std::path::Path::new(&format!("/proc/{pid}")).exists() => {
                ui::status!("WhatsApp: the other wacli sync stopped; starting our own.");
                Some(self.launch())
            }
            SyncState::WaitUntil(at) if tokio::time::Instant::now() >= *at => Some(self.launch()),
            _ => None,
        };
        if let Some(next) = next {
            self.state = next;
        }
    }
}

/// Start `wacli sync --follow`, which keeps the local store current.
fn sync(program: &std::path::Path, store: &std::path::Path) -> anyhow::Result<Daemon> {
    let mut command = Job::new(program.to_path_buf(), "wacli")
        .arg("--store")
        .arg(store.as_os_str())
        .args(["sync", "--follow", "--download-media"])
        .env(env())
        .command();
    command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    Ok(Daemon::spawn(command, "wacli")?)
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
        let mut keeper = SyncKeeper::start(program, store.clone());
        let poller = tokio::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_secs(1)).await;
                keeper.tick();
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

    pub async fn stop(self) {
        self.poller.abort();
        // Dropping the aborted task kills the sync's process group.
        let _ = self.poller.await;
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
    fn the_lock_names_its_holder() {
        assert_eq!(
            lock_holder("pid=2344946\nacquired_at=2026-09-28T17:11:55Z\n"),
            Some(2344946)
        );
        assert_eq!(lock_holder("garbage"), None);
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
