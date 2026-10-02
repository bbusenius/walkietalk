//! `walkietalk operator`: control a running operator-mode `talk`.

use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context, bail};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;

use super::review::{ItemView, Snapshot};
use super::{Action, Request, Response};
use crate::ui;

async fn connect(config: Option<&Path>) -> anyhow::Result<UnixStream> {
    let candidates: Vec<PathBuf> = match config {
        Some(config) => vec![super::socket_path(config)?],
        None => {
            let mut found: Vec<PathBuf> = std::fs::read_dir(super::socket_dir()?)?
                .flatten()
                .map(|e| e.path())
                .filter(|p| p.extension().is_some_and(|e| e == "sock"))
                .collect();
            found.sort();
            found
        }
    };
    let mut live = Vec::new();
    for path in candidates {
        if let Ok(stream) = UnixStream::connect(&path).await {
            live.push(stream);
        }
    }
    match live.len() {
        0 => bail!(
            "no operator-mode talk is running{}; start `talk --capture` with messaging.operator_mode = true",
            if config.is_some() {
                " for this config"
            } else {
                ""
            }
        ),
        1 => Ok(live.pop().expect("one")),
        _ => bail!("more than one operator-mode talk is running; choose one with --config"),
    }
}

fn show(item: &ItemView) {
    let kind = if item.voice { "voice" } else { "text" };
    let direction = match item.direction {
        super::review::Direction::Incoming => "incoming",
        super::review::Direction::Outgoing => "outgoing",
    };
    ui::status!(
        "Item {}: {direction} {}, {}, {kind}.",
        item.number,
        item.service,
        item.alias
    );
    match (&item.original, item.readable) {
        (Some(original), _) => {
            ui::reply!("Edited: {}", item.content);
            ui::status!("Original: {original}");
        }
        (None, true) => ui::reply!(
            "{}{}",
            if item.voice { "Voice transcript: " } else { "" },
            item.content
        ),
        (None, false) => {
            ui::status!("Voice message: run `operator read` for its transcript before approving.")
        }
    }
}

fn summary(snapshot: &Snapshot, action: Action, approved: bool) {
    ui::status!(
        "Review queue: {} waiting; {} approved for radio delivery.",
        snapshot.waiting,
        snapshot.approved
    );
    if !snapshot.conversation.is_empty() {
        ui::status!("Conversation: {}", snapshot.conversation);
    }
    if action == Action::Sleep {
        return;
    }
    let item = if approved {
        &snapshot.approved_item
    } else {
        &snapshot.item
    };
    match item {
        Some(item) => show(item),
        None => ui::status!(
            "{}",
            if approved {
                "No approved incoming message is waiting."
            } else {
                "The review queue is empty."
            }
        ),
    }
}

/// Send one request and print the progress. Returns whether it succeeded.
pub async fn request(
    config: Option<&Path>,
    action: Action,
    approved: bool,
    text: Option<String>,
    bound: Option<u64>,
    timeout: Duration,
) -> anyhow::Result<bool> {
    let stream = connect(config).await?;
    exchange(stream, action, approved, text, bound, timeout).await
}

/// Run one request over an open control connection.
/// `bound` pins the request to a revision seen earlier (an edit started on a
/// previous connection); otherwise it binds to this connection's snapshot.
pub async fn exchange(
    stream: UnixStream,
    action: Action,
    approved: bool,
    text: Option<String>,
    bound: Option<u64>,
    timeout: Duration,
) -> anyhow::Result<bool> {
    let work = async {
        let (read, mut write) = stream.into_split();
        let mut lines = BufReader::new(read).lines();
        let first = lines
            .next_line()
            .await?
            .context("operator controls closed the connection")?;
        let Ok(Response::Snapshot { snapshot }) = serde_json::from_str(&first) else {
            bail!("invalid response from the operator controls");
        };
        summary(&snapshot, action, approved);
        let shown = if approved {
            snapshot.approved_revision
        } else {
            snapshot.revision
        };
        let revision = (action != Action::Sleep).then_some(bound.unwrap_or(shown));
        let request = Request {
            action,
            approved,
            revision,
            text,
        };
        let mut line = serde_json::to_vec(&request)?;
        line.push(b'\n');
        write.write_all(&line).await?;
        while let Some(line) = lines.next_line().await? {
            match serde_json::from_str::<Response>(&line) {
                Ok(Response::Line { kind, message }) => ui::emit(kind_of(&kind), &message),
                Ok(Response::Done { ok, message, .. }) => {
                    if let Some(message) = message {
                        ui::emit(if ok { ui::Kind::Status } else { ui::Kind::Warn }, &message);
                    }
                    return Ok(ok);
                }
                _ => bail!("invalid response from the operator controls"),
            }
        }
        bail!("the operator connection closed; check `operator status` before retrying")
    };
    match tokio::time::timeout(timeout, work).await {
        Ok(result) => result,
        Err(_) => bail!(
            "the operator command timed out; it may have started, so check `operator status` before retrying (or use --timeout)"
        ),
    }
}

fn kind_of(kind: &str) -> ui::Kind {
    match kind {
        "reply" => ui::Kind::Reply,
        "warn" => ui::Kind::Warn,
        "error" => ui::Kind::Error,
        _ => ui::Kind::Status,
    }
}

/// The current review head and its revision, for prefilling the editor.
pub async fn current_text(config: Option<&Path>) -> anyhow::Result<(ItemView, u64)> {
    let stream = connect(config).await?;
    let (read, mut write) = stream.into_split();
    let mut lines = BufReader::new(read).lines();
    let first = lines
        .next_line()
        .await?
        .context("operator controls closed the connection")?;
    let Ok(Response::Snapshot { snapshot }) = serde_json::from_str(&first) else {
        bail!("invalid response from the operator controls");
    };
    let request = serde_json::to_vec(&Request {
        action: Action::Status,
        approved: false,
        revision: None,
        text: None,
    })?;
    write.write_all(&[request, b"\n".to_vec()].concat()).await?;
    let item = snapshot.item.context("the review queue is empty")?;
    if let Some(block) = &item.edit_block {
        bail!("{block}");
    }
    Ok((item, snapshot.revision))
}

/// Prompt for new words, prefilled with the current ones. `None` cancels.
pub fn prompt_edit(current: &str) -> anyhow::Result<Option<String>> {
    let mut editor = rustyline::DefaultEditor::new()?;
    match editor.readline_with_initial("New text: ", (current, "")) {
        Ok(line) => Ok(Some(line)),
        Err(
            rustyline::error::ReadlineError::Interrupted | rustyline::error::ReadlineError::Eof,
        ) => Ok(None),
        Err(err) => Err(err.into()),
    }
}
