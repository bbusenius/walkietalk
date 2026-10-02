//! The operator panel: live radio log above fixed review controls, in the
//! terminal running `talk`.
//!
//! While the panel is open, standard output and error are redirected into
//! its log, so native libraries and helper programs cannot corrupt the
//! screen. The terminal itself is drawn through `/dev/tty`.

use std::collections::VecDeque;
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{Context, bail};
use ratatui::crossterm::event::{self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use ratatui::crossterm::execute;
use ratatui::crossterm::terminal::{self, EnterAlternateScreen, LeaveAlternateScreen};
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};
use ratatui::{Frame, Terminal};
use tokio::sync::mpsc;

use crate::operator::review::{Direction, ItemView, Snapshot};
use crate::operator::{Action, Command, Response, Shared, lock};
use crate::ui::{self, Kind};

const MIN_COLS: u16 = 60;
const MIN_ROWS: u16 = 20;
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
}

impl Log {
    fn push(&mut self, kind: Kind, message: &str) {
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

/// An action in progress, bound to the item it was aimed at.
struct Pending {
    replies: mpsc::UnboundedReceiver<Response>,
}

struct State {
    approved_view: bool,
    scroll: u16,
    editor: Option<Editor>,
    status: (Kind, String),
    pending: Option<Pending>,
}

/// A one-line editor.
struct Editor {
    text: Vec<char>,
    cursor: usize,
    /// The revision of the item being edited; the edit applies only to it.
    revision: u64,
}

impl Editor {
    fn new(text: &str, revision: u64) -> Editor {
        let text: Vec<char> = text.chars().collect();
        Editor {
            cursor: text.len(),
            text,
            revision,
        }
    }

    /// Returns `Some(true)` to save, `Some(false)` to cancel.
    fn key(&mut self, key: KeyEvent) -> Option<bool> {
        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
        match key.code {
            KeyCode::Enter => return Some(true),
            KeyCode::Esc => return Some(false),
            KeyCode::Left => self.cursor = self.cursor.saturating_sub(1),
            KeyCode::Right => self.cursor = (self.cursor + 1).min(self.text.len()),
            KeyCode::Home => self.cursor = 0,
            KeyCode::End => self.cursor = self.text.len(),
            KeyCode::Char('a') if ctrl => self.cursor = 0,
            KeyCode::Char('e') if ctrl => self.cursor = self.text.len(),
            KeyCode::Char('u') if ctrl => {
                self.text.clear();
                self.cursor = 0;
            }
            KeyCode::Backspace if self.cursor > 0 => {
                self.cursor -= 1;
                self.text.remove(self.cursor);
            }
            KeyCode::Delete if self.cursor < self.text.len() => {
                self.text.remove(self.cursor);
            }
            KeyCode::Tab => self.insert(' '),
            KeyCode::Char(c) if !ctrl => self.insert(if c.is_whitespace() { ' ' } else { c }),
            _ => {}
        }
        None
    }

    fn insert(&mut self, c: char) {
        self.text.insert(self.cursor, c);
        self.cursor += 1;
    }

    fn text(&self) -> String {
        self.text.iter().collect()
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
        let mut state = State {
            approved_view: false,
            scroll: 0,
            editor: None,
            status: (Kind::Status, "Ready.".into()),
            pending: None,
        };
        while !stop.load(Ordering::SeqCst) {
            if let Some(pending) = state.pending.as_mut() {
                while let Ok(reply) = pending.replies.try_recv() {
                    match reply {
                        Response::Line { kind, message } => {
                            state.status = (kind_of(&kind), message)
                        }
                        Response::Done { ok, message, .. } => {
                            if let Some(message) = message {
                                state.status =
                                    (if ok { Kind::Status } else { Kind::Warn }, message);
                            }
                            state.pending = None;
                            break;
                        }
                        Response::Snapshot { .. } => {}
                    }
                }
            }
            let snapshot = {
                let board = lock(&shared);
                (
                    board.review.snapshot(board.conversation.clone()),
                    board.delivery.clone(),
                )
            };
            let lines: Vec<(Kind, String)> = log
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .lines
                .iter()
                .cloned()
                .collect();
            term.draw(|frame| draw(frame, &snapshot.0, &snapshot.1, &lines, &state))?;
            if !event::poll(Duration::from_millis(100))? {
                continue;
            }
            let Event::Key(key) = event::read()? else {
                continue;
            };
            if key.kind != KeyEventKind::Press {
                continue;
            }
            if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
                crate::signals::request_stop();
                continue;
            }
            let (cols, rows) = terminal::size()?;
            if cols < MIN_COLS || rows < MIN_ROWS {
                continue;
            }
            handle_key(key, &mut state, &snapshot.0, &commands);
        }
        Ok(())
    })();
    drop(redirect);
    let _ = execute!(writer, LeaveAlternateScreen);
    let _ = terminal::disable_raw_mode();
    result
}

fn handle_key(
    key: KeyEvent,
    state: &mut State,
    snapshot: &Snapshot,
    commands: &mpsc::Sender<Command>,
) {
    if let Some(editor) = state.editor.as_mut() {
        match editor.key(key) {
            Some(true) => {
                let (text, revision) = (editor.text(), editor.revision);
                state.editor = None;
                submit(
                    state,
                    snapshot,
                    commands,
                    Action::Edit,
                    Some(text),
                    Some(revision),
                );
            }
            Some(false) => {
                state.editor = None;
                state.status = (Kind::Status, "Edit cancelled.".into());
            }
            None => {}
        }
        return;
    }
    match key.code {
        KeyCode::Up => state.scroll = state.scroll.saturating_sub(1),
        KeyCode::Down => state.scroll = state.scroll.saturating_add(1),
        KeyCode::PageUp => state.scroll = state.scroll.saturating_sub(5),
        KeyCode::PageDown => state.scroll = state.scroll.saturating_add(5),
        KeyCode::Home => state.scroll = 0,
        KeyCode::End => state.scroll = u16::MAX / 2,
        _ if state.pending.is_some() => {
            state.status = (
                Kind::Warn,
                "Waiting for the current action to finish.".into(),
            )
        }
        KeyCode::Tab => {
            state.approved_view = !state.approved_view;
            state.scroll = 0;
        }
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
            submit(state, snapshot, commands, action, None, None);
        }
        _ => {}
    }
}

fn submit(
    state: &mut State,
    snapshot: &Snapshot,
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
    let revision = bound.unwrap_or(shown);
    if action != Action::Sleep && item.is_none() {
        state.status = (Kind::Warn, "No message is selected.".into());
        return;
    }
    let (command, replies, _) = Command::new(
        action,
        state.approved_view && action != Action::Sleep,
        revision,
        text,
    );
    match commands.try_send(command) {
        Ok(()) => {
            state.status = (
                Kind::Status,
                "Action queued; waiting for the radio to be idle.".into(),
            );
            state.pending = Some(Pending { replies });
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

fn color(kind: Kind) -> Style {
    match kind {
        Kind::Status => Style::new().add_modifier(Modifier::BOLD),
        Kind::Meter => Style::new().fg(Color::DarkGray),
        Kind::Event => Style::new().fg(Color::Cyan),
        Kind::Transcript => Style::new().fg(Color::Cyan).add_modifier(Modifier::BOLD),
        Kind::Accepted => Style::new().fg(Color::Green).add_modifier(Modifier::BOLD),
        Kind::Ignored | Kind::Warn => Style::new().fg(Color::Yellow),
        Kind::Reply => Style::new().fg(Color::Magenta),
        Kind::Error => Style::new().fg(Color::Red).add_modifier(Modifier::BOLD),
    }
}

fn describe(item: &ItemView) -> String {
    let direction = if item.direction == Direction::Incoming {
        "incoming"
    } else {
        "outgoing"
    };
    let kind = if item.voice { "voice" } else { "text" };
    format!(
        "#{} {direction} {} from/to {} ({kind})",
        item.number, item.service, item.alias
    )
}

fn draw(
    frame: &mut Frame,
    snapshot: &Snapshot,
    delivery: &str,
    lines: &[(Kind, String)],
    state: &State,
) {
    let area = frame.area();
    if area.width < MIN_COLS || area.height < MIN_ROWS {
        frame.render_widget(
            Paragraph::new(format!("Enlarge the terminal to at least {MIN_COLS}x{MIN_ROWS}; shortcuts are paused. The radio keeps running.")),
            area,
        );
        return;
    }
    let [log_area, review_area] =
        Layout::vertical([Constraint::Min(5), Constraint::Length(14)]).areas(area);
    let height = log_area.height.saturating_sub(2) as usize;
    let shown: Vec<Line> = lines
        .iter()
        .skip(lines.len().saturating_sub(height))
        .map(|(kind, text)| Line::styled(text.clone(), color(*kind)))
        .collect();
    frame.render_widget(
        Paragraph::new(shown).block(Block::default().borders(Borders::ALL).title(" Radio log ")),
        log_area,
    );
    draw_review(frame, review_area, snapshot, delivery, state);
}

/// The keys that work right now, always shown under the review controls.
fn hints(state: &State) -> &'static [(&'static str, &'static str)] {
    if state.editor.is_some() {
        &[
            ("Enter", "Save"),
            ("Esc", "Cancel"),
            ("Left/Right", "Move"),
            ("Home/End", "Jump"),
            ("Ctrl+U", "Clear"),
        ]
    } else if state.approved_view {
        &[
            ("R", "Read"),
            ("T", "Transmit"),
            ("D", "Deny"),
            ("S", "Sleep"),
            ("Tab", "Review view"),
            ("Up/Down", "Scroll"),
            ("Ctrl+C", "Stop"),
        ]
    } else {
        &[
            ("R", "Read"),
            ("E", "Edit"),
            ("A", "Approve"),
            ("T", "Transmit"),
            ("D", "Deny"),
            ("S", "Sleep"),
            ("Tab", "Approved view"),
            ("Up/Down", "Scroll"),
            ("Ctrl+C", "Stop"),
        ]
    }
}

/// "[R] Read  [E] Edit ...", with the keys in bold.
fn hint_line(hints: &[(&str, &str)]) -> Line<'static> {
    let mut spans = Vec::new();
    for (i, (key, action)) in hints.iter().enumerate() {
        if i > 0 {
            spans.push(Span::raw("  "));
        }
        spans.push(Span::styled(
            format!("[{key}]"),
            Style::new().add_modifier(Modifier::BOLD),
        ));
        spans.push(Span::raw(format!(" {action}")));
    }
    Line::from(spans)
}

fn draw_review(frame: &mut Frame, area: Rect, snapshot: &Snapshot, delivery: &str, state: &State) {
    let view = if state.approved_view {
        "Approved"
    } else {
        "Review"
    };
    let title = format!(
        " {view}: {} waiting, {} approved | {} ",
        snapshot.waiting, snapshot.approved, snapshot.conversation
    );
    let block = Block::default().borders(Borders::ALL).title(title);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let [head, body, foot, keys] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Min(1),
        Constraint::Length(2),
        Constraint::Length(2),
    ])
    .areas(inner);
    frame.render_widget(
        Paragraph::new(hint_line(hints(state))).wrap(Wrap { trim: true }),
        keys,
    );
    let item = if state.approved_view {
        &snapshot.approved_item
    } else {
        &snapshot.item
    };
    match item {
        None => frame.render_widget(
            Paragraph::new(if state.approved_view {
                "No approved incoming message is waiting."
            } else {
                "The review queue is empty."
            }),
            head,
        ),
        Some(item) => {
            frame.render_widget(
                Paragraph::new(describe(item)).style(Style::new().add_modifier(Modifier::BOLD)),
                head,
            );
            let mut text = vec![];
            if let Some(editor) = &state.editor {
                let (before, after): (String, String) = (
                    editor.text[..editor.cursor].iter().collect(),
                    editor.text[editor.cursor..].iter().collect(),
                );
                text.push(Line::from(vec![
                    Span::raw("Edit: "),
                    Span::raw(before),
                    Span::styled("|", Style::new().fg(Color::Yellow)),
                    Span::raw(after),
                ]));
            } else if !item.readable {
                text.push(Line::raw("Voice message: press R to read its transcript."));
            } else {
                let label = if item.original.is_some() {
                    "Edited: "
                } else if item.voice {
                    "Transcript: "
                } else {
                    ""
                };
                text.push(Line::styled(
                    format!("{label}{}", item.content),
                    color(Kind::Reply),
                ));
                if let Some(original) = &item.original {
                    text.push(Line::raw(format!("Original: {original}")));
                }
            }
            frame.render_widget(
                Paragraph::new(text)
                    .wrap(Wrap { trim: false })
                    .scroll((state.scroll, 0)),
                body,
            );
        }
    }
    let mut foot_lines = vec![Line::styled(state.status.1.clone(), color(state.status.0))];
    if !delivery.is_empty() {
        foot_lines.push(Line::styled(
            delivery.to_string(),
            Style::new().fg(Color::Cyan),
        ));
    }
    frame.render_widget(Paragraph::new(foot_lines), foot);
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

    #[test]
    fn key_hints_are_bracketed_and_follow_the_mode() {
        let mut state = State {
            approved_view: false,
            scroll: 0,
            editor: None,
            status: (Kind::Status, String::new()),
            pending: None,
        };
        let text = |state: &State| hint_line(hints(state)).to_string();
        assert!(
            text(&state).starts_with("[R] Read  [E] Edit  [A] Approve  [T] Transmit  [D] Deny")
        );
        state.approved_view = true;
        assert!(
            !text(&state).contains("[E]"),
            "approved messages can't be edited"
        );
        state.editor = Some(Editor::new("x", 1));
        assert!(text(&state).starts_with("[Enter] Save  [Esc] Cancel"));
    }

    #[test]
    fn hints_stay_visible_at_the_smallest_size_and_after_an_action() {
        let state = State {
            approved_view: false,
            scroll: 0,
            editor: None,
            status: (
                Kind::Status,
                "Message approved; waiting for radio delivery.".into(),
            ),
            pending: None,
        };
        let mut term =
            Terminal::new(ratatui::backend::TestBackend::new(MIN_COLS, MIN_ROWS)).unwrap();
        term.draw(|f| draw(f, &Snapshot::default(), "", &[], &state))
            .unwrap();
        let screen: String = term
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|c| c.symbol())
            .collect();
        assert!(screen.contains("[R] Read"), "{screen}");
        assert!(screen.contains("Message approved"));
    }

    #[test]
    fn editor_supports_the_documented_keys() {
        let key = |code| KeyEvent::new(code, KeyModifiers::NONE);
        let ctrl = |c| KeyEvent::new(KeyCode::Char(c), KeyModifiers::CONTROL);
        let mut e = Editor::new("hello", 1);
        e.key(key(KeyCode::Home));
        e.key(key(KeyCode::Char('X')));
        e.key(ctrl('e'));
        e.key(key(KeyCode::Tab));
        e.key(key(KeyCode::Char('y')));
        assert_eq!(e.text(), "Xhello y");
        e.key(key(KeyCode::Backspace));
        e.key(key(KeyCode::Left));
        e.key(key(KeyCode::Delete));
        assert_eq!(e.text(), "Xhello");
        e.key(ctrl('u'));
        assert_eq!(e.text(), "");
        assert_eq!(e.key(key(KeyCode::Enter)), Some(true));
        assert_eq!(e.key(key(KeyCode::Esc)), Some(false));
    }
}
