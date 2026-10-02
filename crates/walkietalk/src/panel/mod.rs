//! The operator panel: live radio log above fixed review controls, in the
//! terminal running `talk`.
//!
//! While the panel is open, standard output and error are redirected into
//! its log, so native libraries and helper programs cannot corrupt the
//! screen. The terminal itself is drawn through `/dev/tty`.

mod editor;
mod text;
mod view;

use std::collections::VecDeque;
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{Context, bail};
use ratatui::Terminal;
use ratatui::crossterm::event::{self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use ratatui::crossterm::execute;
use ratatui::crossterm::terminal::{self, EnterAlternateScreen, LeaveAlternateScreen};
use tokio::sync::mpsc;

use crate::operator::review::Snapshot;
use crate::operator::{Action, Command, Response, Shared, lock};
use crate::ui::{self, Kind};
use editor::{Editor, Outcome};
use view::{Input, MIN_COLS, MIN_ROWS, Pending, State};

const LOG_LINES: usize = 500;

/// Refuse to start the panel without a large enough interactive terminal.
pub fn check_terminal() -> anyhow::Result<()> {
    use std::io::IsTerminal;
    if !std::io::stdin().is_terminal() || !std::io::stdout().is_terminal() {
        bail!("--panel needs an interactive terminal");
    }
    let (cols, rows) = terminal::size().context("cannot read the terminal size")?;
    if cols < MIN_COLS || rows < MIN_ROWS {
        bail!(
            "--panel needs a terminal of at least {MIN_COLS}x{MIN_ROWS} (this one is {cols}x{rows})"
        );
    }
    Ok(())
}

/// The log pane's lines; consecutive meter readings share one line.
#[derive(Default)]
struct Log {
    lines: VecDeque<(Kind, String)>,
    /// Bumped on every change, so the panel redraws only when needed.
    revision: u64,
}

impl Log {
    fn push(&mut self, kind: Kind, message: &str) {
        self.revision += 1;
        for line in message.lines() {
            let meter = kind == Kind::Meter && line.starts_with("RMS ");
            if meter
                && self
                    .lines
                    .back()
                    .is_some_and(|(k, l)| *k == Kind::Meter && l.starts_with("RMS "))
            {
                self.lines.pop_back();
            }
            if self.lines.len() == LOG_LINES {
                self.lines.pop_front();
            }
            self.lines.push_back((kind, line.to_string()));
        }
    }
}

/// Saved standard output and error, restored when the panel closes.
struct Redirect {
    saved_out: OwnedFd,
    saved_err: OwnedFd,
}

impl Redirect {
    fn start(log: Arc<Mutex<Log>>) -> anyhow::Result<Redirect> {
        let mut fds = [0; 2];
        // SAFETY: pipe writes two new descriptors into the array.
        anyhow::ensure!(
            unsafe { libc::pipe(fds.as_mut_ptr()) } == 0,
            "cannot create a pipe"
        );
        // SAFETY: both descriptors were just created and are owned here.
        let (read, write) = unsafe { (File::from_raw_fd(fds[0]), OwnedFd::from_raw_fd(fds[1])) };
        // SAFETY: dup returns a new descriptor or -1, checked below.
        let (out, err) = unsafe { (libc::dup(1), libc::dup(2)) };
        anyhow::ensure!(out >= 0 && err >= 0, "cannot save the terminal output");
        // SAFETY: out/err are fresh descriptors owned by us.
        let saved = unsafe {
            Redirect {
                saved_out: OwnedFd::from_raw_fd(out),
                saved_err: OwnedFd::from_raw_fd(err),
            }
        };
        // SAFETY: dup2 replaces 1 and 2 with the pipe's write end.
        unsafe {
            libc::dup2(write.as_raw_fd(), 1);
            libc::dup2(write.as_raw_fd(), 2);
        }
        drop(write);
        std::thread::Builder::new()
            .name("panel-output".into())
            .spawn(move || {
                for line in BufReader::new(read).lines() {
                    let Ok(line) = line else { break };
                    log.lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .push(Kind::Warn, &line);
                }
            })?;
        Ok(saved)
    }
}

impl Drop for Redirect {
    fn drop(&mut self) {
        // SAFETY: restoring the descriptors saved at start.
        unsafe {
            libc::dup2(self.saved_out.as_raw_fd(), 1);
            libc::dup2(self.saved_err.as_raw_fd(), 2);
        }
    }
}

pub struct Panel {
    stop: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
    failed: Arc<Mutex<Option<String>>>,
}

impl Panel {
    pub fn start(shared: Shared, commands: mpsc::Sender<Command>) -> anyhow::Result<Panel> {
        let log = Arc::new(Mutex::new(Log::default()));
        let sink_log = log.clone();
        ui::set_sink(move |kind, message| {
            sink_log
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .push(kind, message)
        });
        let stop = Arc::new(AtomicBool::new(false));
        let failed = Arc::new(Mutex::new(None));
        let (s, f) = (stop.clone(), failed.clone());
        let thread = std::thread::Builder::new()
            .name("panel".into())
            .spawn(move || {
                if let Err(err) = run(shared, commands, log, s) {
                    *f.lock().unwrap_or_else(|e| e.into_inner()) = Some(format!("{err:#}"));
                }
                ui::clear_sink();
            })?;
        Ok(Panel {
            stop,
            thread: Some(thread),
            failed,
        })
    }

    /// Fail if the panel stopped unexpectedly.
    pub fn check(&self) -> anyhow::Result<()> {
        match self.failed.lock().unwrap_or_else(|e| e.into_inner()).take() {
            Some(err) => bail!("the operator panel failed: {err}"),
            None => Ok(()),
        }
    }

    pub fn close(mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

impl Drop for Panel {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

fn run(
    shared: Shared,
    commands: mpsc::Sender<Command>,
    log: Arc<Mutex<Log>>,
    stop: Arc<AtomicBool>,
) -> anyhow::Result<()> {
    let tty = File::options()
        .read(true)
        .write(true)
        .open("/dev/tty")
        .context("cannot open the terminal")?;
    terminal::enable_raw_mode()?;
    let mut writer = tty.try_clone()?;
    execute!(writer, EnterAlternateScreen)?;
    let redirect = Redirect::start(log.clone());
    let result = (|| -> anyhow::Result<()> {
        let mut term = Terminal::new(ratatui::backend::CrosstermBackend::new(tty))?;
        let mut state = State::new();
        let mut lines: Vec<(Kind, String)> = Vec::new();
        let mut drawn: Option<(u64, Snapshot, (u16, u16))> = None;
        let mut dirty = true;
        while !stop.load(Ordering::SeqCst) {
            dirty |= take_replies(&mut state);
            let (snapshot, delivery) = {
                let board = lock(&shared);
                (
                    board.review.snapshot(board.conversation.clone()),
                    board.delivery.clone(),
                )
            };
            // A new delivery result becomes the latest status.
            if delivery != state.last_delivery {
                if !delivery.is_empty() {
                    state.status = (Kind::Event, delivery.clone());
                }
                state.last_delivery = delivery;
                dirty = true;
            }
            let revision = {
                let log = log.lock().unwrap_or_else(|e| e.into_inner());
                if drawn.as_ref().is_none_or(|(r, _, _)| *r != log.revision) {
                    lines = log.lines.iter().cloned().collect();
                }
                log.revision
            };
            let size = terminal::size()?;
            let current = (revision, snapshot, size);
            // Redraw only when something on screen could have changed.
            if dirty || drawn.as_ref() != Some(&current) {
                term.draw(|frame| {
                    view::draw(
                        frame,
                        &Input {
                            snapshot: &current.1,
                            log: &lines,
                        },
                        &mut state,
                    )
                })?;
                drawn = Some(current);
                dirty = false;
            }
            if !event::poll(Duration::from_millis(100))? {
                continue;
            }
            let Event::Key(key) = event::read()? else {
                dirty = true; // resize and other events
                continue;
            };
            if key.kind != KeyEventKind::Press {
                continue;
            }
            dirty = true;
            if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
                crate::signals::request_stop();
                continue;
            }
            let (cols, rows) = terminal::size()?;
            if cols < MIN_COLS || rows < MIN_ROWS {
                continue;
            }
            let snapshot = drawn
                .as_ref()
                .map(|(_, s, _)| s.clone())
                .unwrap_or_default();
            handle_key(key, &mut state, &snapshot, &shared, &commands);
        }
        Ok(())
    })();
    drop(redirect);
    let _ = execute!(writer, LeaveAlternateScreen);
    let _ = terminal::disable_raw_mode();
    result
}

/// Apply progress from a running action. Returns whether anything changed.
fn take_replies(state: &mut State) -> bool {
    let Some(pending) = state.pending.as_mut() else {
        return false;
    };
    let mut changed = false;
    while let Ok(reply) = pending.replies.try_recv() {
        changed = true;
        match reply {
            Response::Line { kind, message } => state.status = (kind_of(&kind), message),
            Response::Done { ok, message, .. } => {
                if let Some(message) = message {
                    state.status = (if ok { Kind::Status } else { Kind::Warn }, message);
                }
                state.pending = None;
                return true;
            }
            Response::Snapshot { .. } => {}
        }
    }
    changed
}

fn handle_key(
    key: KeyEvent,
    state: &mut State,
    snapshot: &Snapshot,
    shared: &Shared,
    commands: &mpsc::Sender<Command>,
) {
    if let Some(editor) = state.editor.as_mut() {
        match editor.key(key) {
            Outcome::Save => {
                let (text, revision) = (editor.text(), editor.revision);
                state.editor = None;
                submit(
                    state,
                    snapshot,
                    shared,
                    commands,
                    Action::Edit,
                    Some(text),
                    Some(revision),
                );
            }
            Outcome::Cancel => {
                state.editor = None;
                state.status = (Kind::Status, "Edit cancelled.".into());
            }
            Outcome::Continue => {}
        }
        return;
    }
    let page = state.body_rows.max(1) as isize;
    match key.code {
        KeyCode::Up => view::scroll(state, Some(-1)),
        KeyCode::Down => view::scroll(state, Some(1)),
        KeyCode::PageUp => view::scroll(state, Some(-page)),
        KeyCode::PageDown => view::scroll(state, Some(page)),
        KeyCode::Home => state.scroll = 0,
        KeyCode::End => view::scroll(state, None),
        _ if state.pending.is_some() => {
            state.status = (
                Kind::Warn,
                "Waiting for the current action to finish.".into(),
            );
        }
        KeyCode::Tab => state.approved_view = !state.approved_view,
        KeyCode::Char(c) => {
            let action = match c.to_ascii_lowercase() {
                'r' => Action::Read,
                'a' => Action::Approve,
                't' => Action::Transmit,
                'd' => Action::Deny,
                's' => Action::Sleep,
                'e' => {
                    if state.approved_view {
                        state.status = (
                            Kind::Warn,
                            crate::operator::review::APPROVED_EDIT_BLOCK.into(),
                        );
                    } else if let Some(item) = &snapshot.item {
                        match &item.edit_block {
                            Some(block) => state.status = (Kind::Warn, block.clone()),
                            None => {
                                state.editor = Some(Editor::new(&item.content, snapshot.revision))
                            }
                        }
                    }
                    return;
                }
                _ => return,
            };
            if action == Action::Transmit && !state.approved_view && snapshot.item.is_none() {
                state.status = (
                    Kind::Warn,
                    "Nothing waits for review; press Tab to transmit an approved message.".into(),
                );
                return;
            }
            submit(state, snapshot, shared, commands, action, None, None);
        }
        _ => {}
    }
}

fn submit(
    state: &mut State,
    snapshot: &Snapshot,
    shared: &Shared,
    commands: &mpsc::Sender<Command>,
    action: Action,
    text: Option<String>,
    bound: Option<u64>,
) {
    let (item, shown) = if state.approved_view {
        (&snapshot.approved_item, snapshot.approved_revision)
    } else {
        (&snapshot.item, snapshot.revision)
    };
    if action != Action::Sleep && item.is_none() {
        state.status = (Kind::Warn, "No message is selected.".into());
        return;
    }
    let (command, replies, _) = Command::new(
        action,
        state.approved_view && action != Action::Sleep,
        bound.unwrap_or(shown),
        text,
    );
    match commands.try_send(command) {
        Ok(()) => {
            state.status = (
                Kind::Status,
                "Action queued; waiting for the radio to be idle.".into(),
            );
            state.pending = Some(Pending { replies });
            // An older delivery result is no longer the latest news.
            lock(shared).delivery.clear();
            state.last_delivery.clear();
        }
        Err(_) => {
            state.status = (
                Kind::Error,
                "The talk loop is not accepting commands.".into(),
            )
        }
    }
}

fn kind_of(kind: &str) -> Kind {
    match kind {
        "reply" => Kind::Reply,
        "warn" => Kind::Warn,
        "error" => Kind::Error,
        _ => Kind::Status,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn meter_lines_collapse_into_one() {
        let mut log = Log::default();
        log.push(Kind::Meter, "RMS 0.010");
        log.push(Kind::Meter, "RMS 0.020");
        log.push(Kind::Event, "Speech started");
        log.push(Kind::Meter, "RMS 0.030");
        let lines: Vec<&str> = log.lines.iter().map(|(_, l)| l.as_str()).collect();
        assert_eq!(lines, ["RMS 0.020", "Speech started", "RMS 0.030"]);
    }
}
