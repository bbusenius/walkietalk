//! Operator mode: local review of every message, controlled from another
//! terminal or the panel.

pub mod client;
pub mod review;
pub mod server;

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use anyhow::{Context, bail};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tokio::sync::mpsc;

use review::Review;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, clap::ValueEnum)]
#[serde(rename_all = "lowercase")]
pub enum Action {
    Status,
    Read,
    Edit,
    Approve,
    Transmit,
    Deny,
    Sleep,
}

/// What a client asks for.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Request {
    pub action: Action,
    #[serde(default)]
    pub approved: bool,
    /// The revision of the item the client showed; omitted means "whatever
    /// the server's snapshot showed".
    #[serde(default)]
    pub revision: Option<u64>,
    #[serde(default)]
    pub text: Option<String>,
}

/// One line back to the client.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(untagged)]
pub enum Response {
    Snapshot {
        snapshot: Box<review::Snapshot>,
    },
    Done {
        done: bool,
        ok: bool,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        message: Option<String>,
    },
    Line {
        kind: String,
        message: String,
    },
}

/// A command waiting for the talk loop.
pub struct Command {
    pub action: Action,
    pub approved_view: bool,
    pub revision: u64,
    pub text: Option<String>,
    replies: mpsc::UnboundedSender<Response>,
    cancelled: Arc<AtomicBool>,
}

impl Command {
    pub fn new(
        action: Action,
        approved_view: bool,
        revision: u64,
        text: Option<String>,
    ) -> (Command, mpsc::UnboundedReceiver<Response>, Arc<AtomicBool>) {
        let (replies, rx) = mpsc::unbounded_channel();
        let cancelled = Arc::new(AtomicBool::new(false));
        (
            Command {
                action,
                approved_view,
                revision,
                text,
                replies,
                cancelled: cancelled.clone(),
            },
            rx,
            cancelled,
        )
    }

    pub fn cancelled(&self) -> bool {
        self.cancelled.load(Ordering::SeqCst)
    }

    /// Progress for the client (also logged by the caller).
    pub fn say(&self, kind: &str, message: &str) {
        let _ = self.replies.send(Response::Line {
            kind: kind.into(),
            message: message.into(),
        });
    }

    pub fn finish(&self, ok: bool) {
        let _ = self.replies.send(Response::Done {
            done: true,
            ok,
            message: None,
        });
    }
}

/// State the talk loop shares with the control server and panel.
pub struct Board {
    pub review: Review,
    pub conversation: String,
    /// Latest delivery progress, for the panel.
    pub delivery: String,
}

pub type Shared = Arc<Mutex<Board>>;

pub fn lock(shared: &Shared) -> std::sync::MutexGuard<'_, Board> {
    shared.lock().unwrap_or_else(|e| e.into_inner())
}

/// The private directory holding control sockets.
pub fn socket_dir() -> anyhow::Result<PathBuf> {
    use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt};
    let uid = crate::sys::current_uid();
    let runtime = std::env::var_os("XDG_RUNTIME_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(format!("/run/user/{uid}")));
    let base = match std::fs::symlink_metadata(&runtime) {
        Ok(meta) if meta.is_dir() && meta.uid() == uid && meta.mode() & 0o077 == 0 => {
            runtime.join("walkietalk-operator")
        }
        Ok(_) if std::env::var_os("XDG_RUNTIME_DIR").is_some() => {
            bail!(
                "the runtime directory {} must be private and owned by you",
                runtime.display()
            )
        }
        _ => crate::paths::cache_home().join("walkietalk/operator"),
    };
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&base)?;
    let meta = std::fs::symlink_metadata(&base)?;
    anyhow::ensure!(
        meta.is_dir() && meta.uid() == uid,
        "{} is not a directory you own",
        base.display()
    );
    std::fs::set_permissions(&base, std::fs::Permissions::from_mode(0o700))?;
    Ok(base)
}

/// The control socket for one config file.
pub fn socket_path(config: &Path) -> anyhow::Result<PathBuf> {
    let absolute = config
        .canonicalize()
        .with_context(|| format!("cannot find {}", config.display()))?;
    let digest = Sha256::digest(absolute.as_os_str().as_encoded_bytes());
    let name: String = digest.iter().take(12).map(|b| format!("{b:02x}")).collect();
    Ok(socket_dir()?.join(format!("{name}.sock")))
}
