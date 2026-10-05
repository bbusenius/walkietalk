//! The local control socket of a running operator-mode `talk`.

use std::fs::File;
use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context, bail};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::mpsc;

use super::{Action, Command, Request, Response, Shared, lock};
use crate::messaging::text::normalize;

const MAX_REQUEST: u64 = 128 * 1024;
pub const MAX_EDIT_CHARS: usize = 8000;

/// Holds the single-instance lock and serves the socket.
pub struct Server {
    path: PathBuf,
    _lock: File,
    task: tokio::task::JoinHandle<()>,
}

/// Reserve this config: only one operator-mode `talk` per config file.
pub fn reserve(config: &Path) -> anyhow::Result<(PathBuf, File)> {
    let path = super::socket_path(config)?;
    let lock_path = path.with_extension("lock");
    let file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&lock_path)?;
    if file.try_lock().is_err() {
        bail!("operator mode is already running for this config");
    }
    Ok((path, file))
}

impl Server {
    pub fn start(
        reserved: (PathBuf, File),
        shared: Shared,
        commands: mpsc::Sender<Command>,
    ) -> anyhow::Result<Server> {
        use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
        let (path, lock) = reserved;
        if let Ok(meta) = std::fs::symlink_metadata(&path) {
            // The lock proves any existing socket is stale.
            anyhow::ensure!(
                meta.file_type().is_socket() && meta.uid() == crate::sys::current_uid(),
                "unexpected file at {}",
                path.display()
            );
            std::fs::remove_file(&path)?;
        }
        let listener = UnixListener::bind(&path)
            .with_context(|| format!("cannot create {}", path.display()))?;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))?;
        let task = tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                tokio::spawn(serve(stream, shared.clone(), commands.clone()));
            }
        });
        Ok(Server {
            path,
            _lock: lock,
            task,
        })
    }

    /// Whether the server stopped unexpectedly.
    pub fn failed(&self) -> bool {
        self.task.is_finished()
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        self.task.abort();
        let _ = std::fs::remove_file(&self.path);
    }
}

async fn send(
    stream: &mut (impl AsyncWriteExt + Unpin),
    response: &Response,
) -> std::io::Result<()> {
    let mut line = serde_json::to_vec(response).expect("responses serialize");
    line.push(b'\n');
    stream.write_all(&line).await
}

fn done(ok: bool, message: &str) -> Response {
    Response::Done {
        done: true,
        ok,
        message: Some(message.into()),
    }
}

async fn serve(stream: UnixStream, shared: Shared, commands: mpsc::Sender<Command>) {
    let (read, mut write) = stream.into_split();
    let snapshot = {
        let board = lock(&shared);
        board.review.snapshot(board.conversation.clone())
    };
    if send(
        &mut write,
        &Response::Snapshot {
            snapshot: Box::new(snapshot.clone()),
        },
    )
    .await
    .is_err()
    {
        return;
    }
    let mut reader = BufReader::new(read.take(MAX_REQUEST));
    let mut line = String::new();
    match tokio::time::timeout(Duration::from_secs(30), reader.read_line(&mut line)).await {
        Ok(Ok(n)) if n > 0 && line.ends_with('\n') => {}
        _ => return,
    }
    let Ok(request) = serde_json::from_str::<Request>(&line) else {
        let _ = send(&mut write, &done(false, "invalid request")).await;
        return;
    };
    let reply = match validate(&request, &snapshot) {
        Err(message) => Some(done(false, &message)),
        Ok(_) if request.action == Action::Status => Some(Response::Done {
            done: true,
            ok: true,
            message: None,
        }),
        Ok(_) => None,
    };
    if let Some(reply) = reply {
        let _ = send(&mut write, &reply).await;
        return;
    }
    let revision = request.revision.unwrap_or(if request.approved {
        snapshot.approved_revision
    } else {
        snapshot.revision
    });
    let text = request.text.as_deref().map(normalize);
    let (command, mut replies, cancelled) =
        Command::new(request.action, request.approved, revision, text);
    if commands.send(command).await.is_err() {
        let _ = send(&mut write, &done(false, "talk stopped")).await;
        return;
    }
    let _ = send(
        &mut write,
        &Response::Line {
            kind: "status".into(),
            message: "Command queued; waiting for the radio to be idle.".into(),
        },
    )
    .await;
    let mut hangup = reader.into_inner().into_inner();
    let mut probe = [0u8; 1];
    loop {
        tokio::select! {
            reply = replies.recv() => {
                let Some(reply) = reply else {
                    let _ = send(&mut write, &done(false, "talk stopped")).await;
                    return;
                };
                let finished = matches!(reply, Response::Done { .. });
                if send(&mut write, &reply).await.is_err() || finished {
                    cancelled.store(true, std::sync::atomic::Ordering::SeqCst);
                    return;
                }
            }
            // A client that disconnects withdraws a command that has not started.
            _ = hangup.read(&mut probe) => {
                cancelled.store(true, std::sync::atomic::Ordering::SeqCst);
                return;
            }
        }
    }
}

/// Checks that can be answered from the snapshot the client saw.
fn validate(request: &Request, snapshot: &super::review::Snapshot) -> Result<(), String> {
    if request.action == Action::Edit {
        if request.approved {
            return Err(super::review::APPROVED_EDIT_BLOCK.into());
        }
        let text = request.text.as_deref().map(normalize).unwrap_or_default();
        if text.is_empty() || text.chars().count() > MAX_EDIT_CHARS {
            return Err(format!(
                "edit needs text of 1 to {MAX_EDIT_CHARS} characters"
            ));
        }
    } else if request.text.is_some() {
        return Err("--text applies only to edit".into());
    }
    if matches!(request.action, Action::Status | Action::Sleep) {
        return Ok(());
    }
    let (item, revision) = if request.approved {
        (&snapshot.approved_item, snapshot.approved_revision)
    } else {
        (&snapshot.item, snapshot.revision)
    };
    if request.revision.is_some_and(|r| r != revision) {
        return Err("the displayed item changed; review it again".into());
    }
    let Some(item) = item else {
        return Err(if request.approved {
            "no approved incoming message is waiting"
        } else {
            "no message is waiting for review"
        }
        .into());
    };
    if request.action == Action::Edit
        && let Some(block) = &item.edit_block
    {
        return Err(block.clone());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{Config, Service};
    use crate::operator::client::exchange;
    use crate::operator::review::Review;
    use crate::operator::{Board, lock};

    fn shared() -> Shared {
        let text = format!(
            "{}\n[messaging]\noperator_mode = true\n[messaging.signal]\nwake_phrase = \"grandma\"\nto = \"+1555\"\n",
            crate::config::tests_support::MINIMAL
        );
        let config = Config::parse(&text, "/".into()).unwrap();
        std::sync::Arc::new(std::sync::Mutex::new(Board {
            review: Review::new(&config),
            conversation: "asleep".into(),
            delivery: String::new(),
        }))
    }

    async fn serve(
        shared: Shared,
    ) -> (tempfile::TempDir, PathBuf, mpsc::Receiver<Command>, Server) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.sock");
        let lock = File::create(dir.path().join("test.lock")).unwrap();
        lock.try_lock().unwrap();
        let (tx, rx) = mpsc::channel(4);
        let server = Server::start((path.clone(), lock), shared, tx).unwrap();
        (dir, path, rx, server)
    }

    #[tokio::test]
    async fn commands_reach_the_loop_and_report_back() {
        let board = shared();
        lock(&board)
            .review
            .add_incoming(Service::Signal, "1", false, "hello");
        let (_dir, path, mut rx, _server) = serve(board).await;
        let loop_task = tokio::spawn(async move {
            let command = rx.recv().await.unwrap();
            assert_eq!(command.action, Action::Approve);
            command.say("status", "Message approved.");
            command.finish(true);
        });
        let stream = UnixStream::connect(&path).await.unwrap();
        assert!(
            exchange(
                stream,
                Action::Approve,
                false,
                None,
                None,
                Duration::from_secs(5)
            )
            .await
            .unwrap()
        );
        loop_task.await.unwrap();
    }

    #[tokio::test]
    async fn status_needs_no_loop_and_empty_queue_is_refused() {
        let (_dir, path, _rx, _server) = serve(shared()).await;
        let stream = UnixStream::connect(&path).await.unwrap();
        assert!(
            exchange(
                stream,
                Action::Status,
                false,
                None,
                None,
                Duration::from_secs(5)
            )
            .await
            .unwrap()
        );
        let stream = UnixStream::connect(&path).await.unwrap();
        assert!(
            !exchange(
                stream,
                Action::Approve,
                false,
                None,
                None,
                Duration::from_secs(5)
            )
            .await
            .unwrap()
        );
    }

    #[tokio::test]
    async fn edits_are_validated_before_queueing() {
        let board = shared();
        lock(&board)
            .review
            .add_incoming(Service::Signal, "v", true, "");
        let (_dir, path, mut rx, _server) = serve(board).await;
        let stream = UnixStream::connect(&path).await.unwrap();
        // An unread voice note cannot be edited.
        assert!(
            !exchange(
                stream,
                Action::Edit,
                false,
                Some("words".into()),
                None,
                Duration::from_secs(5)
            )
            .await
            .unwrap()
        );
        assert!(rx.try_recv().is_err(), "nothing was queued");
    }

    #[tokio::test]
    async fn the_socket_is_private_and_a_second_instance_is_refused() {
        use std::os::unix::fs::PermissionsExt;
        let (dir, path, _rx, server) = serve(shared()).await;
        let mode = std::fs::metadata(&path).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600);
        let again = File::options()
            .append(true)
            .open(dir.path().join("test.lock"))
            .unwrap();
        assert!(again.try_lock().is_err(), "the instance lock is held");
        drop(server);
        assert!(!path.exists(), "the socket is removed on exit");
    }
}
