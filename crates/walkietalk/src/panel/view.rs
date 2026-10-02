//! What the panel shows, and drawing it.

use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Position, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Padding, Paragraph};

use super::editor::Editor;
use super::text::{width, wrap};
use crate::operator::Response;
use crate::operator::review::{Direction, ItemView, Snapshot};
use crate::ui::Kind;

pub const MIN_COLS: u16 = 60;
pub const MIN_ROWS: u16 = 20;
/// The log keeps at least this many rows.
const MIN_LOG_ROWS: u16 = 6;

/// An action sent to the talk loop and not yet finished.
pub struct Pending {
    pub replies: tokio::sync::mpsc::UnboundedReceiver<Response>,
}

/// The panel's own state, separate from the shared review queue.
pub struct State {
    pub approved_view: bool,
    pub editor: Option<Editor>,
    /// The latest action or delivery result.
    pub status: (Kind, String),
    pub pending: Option<Pending>,
    /// First wrapped body row on screen, and what it applies to.
    pub scroll: usize,
    pub scroll_key: Option<(bool, u64, usize)>,
    /// Rows of message text that fit, from the last drawing.
    pub body_rows: usize,
    /// The delivery result last shown, so a new one replaces the status.
    pub last_delivery: String,
}

impl State {
    pub fn new() -> State {
        State {
            approved_view: false,
            editor: None,
            status: (Kind::Status, String::new()),
            pending: None,
            scroll: 0,
            scroll_key: None,
            body_rows: 1,
            last_delivery: String::new(),
        }
    }
}

/// Everything drawn comes from here.
pub struct Input<'a> {
    pub snapshot: &'a Snapshot,
    /// The newest log lines, oldest first.
    pub log: &'a [(Kind, String)],
}

fn style(kind: Kind) -> Style {
    match kind {
        Kind::Status => Style::new(),
        Kind::Meter => Style::new().fg(Color::DarkGray),
        Kind::Event => Style::new().fg(Color::Cyan),
        Kind::Transcript => Style::new().fg(Color::Cyan).add_modifier(Modifier::BOLD),
        Kind::Accepted => Style::new().fg(Color::Green).add_modifier(Modifier::BOLD),
        Kind::Ignored | Kind::Warn => Style::new().fg(Color::Yellow),
        Kind::Reply => Style::new().fg(Color::Magenta),
        Kind::Error => Style::new().fg(Color::Red).add_modifier(Modifier::BOLD),
    }
}

/// "#3 Incoming WhatsApp from Queen (voice) | Edited"
pub fn heading(item: &ItemView) -> String {
    let (direction, preposition) = match item.direction {
        Direction::Incoming => ("Incoming", "from"),
        Direction::Outgoing => ("Outgoing", "to"),
    };
    let kind = if item.voice { "voice" } else { "text" };
    let edited = if item.original.is_some() {
        " | Edited"
    } else {
        ""
    };
    format!(
        "#{} {direction} {} {preposition} {} ({kind}){edited}",
        item.number, item.service, item.alias
    )
}

/// The keys that work right now.
pub fn hints(state: &State) -> &'static [(&'static str, &'static str)] {
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
            ("Ctrl+C", "Stop"),
        ]
    }
}

/// Hints as rows of "[K] Action" that fit `columns`, keys in bold.
fn hint_rows(hints: &[(&str, &str)], columns: usize) -> Vec<Line<'static>> {
    let mut rows: Vec<Vec<Span<'static>>> = vec![Vec::new()];
    let mut used = 0;
    for (key, action) in hints {
        let text_width = key.len() + action.len() + 3;
        if used > 0 && used + 2 + text_width > columns {
            rows.push(Vec::new());
            used = 0;
        }
        let row = rows.last_mut().expect("at least one row");
        if used > 0 {
            row.push(Span::raw("  "));
            used += 2;
        }
        row.push(Span::styled(
            format!("[{key}]"),
            Style::new().add_modifier(Modifier::BOLD),
        ));
        row.push(Span::raw(format!(" {action}")));
        used += text_width;
    }
    rows.into_iter().map(Line::from).collect()
}

/// Cut a line to `columns` display columns.
fn clip(text: &str, columns: usize) -> String {
    let mut used = 0;
    text.chars()
        .take_while(|&c| {
            used += width(c);
            used <= columns
        })
        .collect()
}

pub fn draw(frame: &mut Frame, input: &Input, state: &mut State) {
    let area = frame.area();
    if area.width < MIN_COLS || area.height < MIN_ROWS {
        let message = format!(
            "Enlarge the terminal to at least {MIN_COLS}x{MIN_ROWS} for operator controls.\nThe radio keeps running. Ctrl+C stops."
        );
        frame.render_widget(Paragraph::new(message), area);
        return;
    }
    // The review area grows with the terminal: half the height, 14 to 20 rows.
    let review_height = (area.height / 2)
        .clamp(14, 20)
        .min(area.height - MIN_LOG_ROWS);
    let [log_area, review_area] = Layout::vertical([
        Constraint::Min(MIN_LOG_ROWS),
        Constraint::Length(review_height),
    ])
    .areas(area);
    draw_log(frame, log_area, input.log);
    draw_review(frame, review_area, input.snapshot, state);
}

fn draw_log(frame: &mut Frame, area: Rect, log: &[(Kind, String)]) {
    let block = Block::default()
        .borders(Borders::ALL)
        .padding(Padding::horizontal(1))
        .title(" Radio log ");
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let (rows, columns) = (inner.height as usize, inner.width as usize);
    // Wrap from the newest entry backwards until the pane is full.
    let mut lines: Vec<Line> = Vec::new();
    for (kind, text) in log.iter().rev() {
        for row in wrap(text, columns).into_iter().rev() {
            lines.push(Line::styled(row, style(*kind)));
        }
        if lines.len() >= rows {
            break;
        }
    }
    lines.truncate(rows);
    lines.reverse();
    frame.render_widget(Paragraph::new(lines), inner);
}

fn draw_review(frame: &mut Frame, area: Rect, snapshot: &Snapshot, state: &mut State) {
    let title = if state.approved_view {
        " Operator: Approved (Tab: Review) "
    } else {
        " Operator: Review (Tab: Approved) "
    };
    let block = Block::default()
        .borders(Borders::ALL)
        .padding(Padding::horizontal(1))
        .title(title);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let columns = inner.width as usize;

    let busy = state.pending.is_some();
    let hints = if busy {
        vec![Line::from(vec![
            Span::raw("Working; shortcuts paused  "),
            Span::styled("[Ctrl+C]", Style::new().add_modifier(Modifier::BOLD)),
            Span::raw(" Stop"),
        ])]
    } else {
        hint_rows(hints(state), columns)
    };
    let [conversation, counts, head, _gap, body, ready, result, keys] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Min(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(hints.len() as u16),
    ])
    .areas(inner);
    let line =
        |text: String, kind: Kind| Paragraph::new(Line::styled(clip(&text, columns), style(kind)));
    let conversation_text = if snapshot.conversation.is_empty() {
        "waiting for wake"
    } else {
        &snapshot.conversation
    };
    frame.render_widget(
        line(format!("Conversation: {conversation_text}"), Kind::Status),
        conversation,
    );
    frame.render_widget(
        line(
            format!(
                "Waiting for review: {}    Approved for delivery: {}",
                snapshot.waiting, snapshot.approved
            ),
            Kind::Status,
        ),
        counts,
    );
    frame.render_widget(Paragraph::new(hints), keys);
    frame.render_widget(line(state.status.1.clone(), state.status.0), result);

    let item = if state.approved_view {
        &snapshot.approved_item
    } else {
        &snapshot.item
    };
    let body_rows = body.height as usize;
    state.body_rows = body_rows;

    // Editing: the real terminal cursor marks the editing point.
    if let Some(editor) = state.editor.as_mut() {
        let heading_text = snapshot.item.as_ref().map(heading).unwrap_or_default();
        frame.render_widget(
            Paragraph::new(Line::styled(
                clip(&heading_text, columns),
                Style::new().add_modifier(Modifier::BOLD),
            )),
            head,
        );
        let layout = editor.layout(columns);
        let top = editor.scroll_to_cursor(&layout, body_rows);
        let shown: Vec<Line> = layout.rows[top..]
            .iter()
            .take(body_rows)
            .map(|r| Line::styled(r.clone(), style(Kind::Reply)))
            .collect();
        frame.render_widget(Paragraph::new(shown), body);
        let x = body.x + layout.cursor_column as u16;
        let y = body.y + (layout.cursor_row - top) as u16;
        frame.set_cursor_position(Position::new(x, y));
        let mut ready_text = "Editing; Enter saves, Esc cancels.".to_string();
        if layout.rows.len() > body_rows {
            ready_text += &format!(
                " | Lines {}-{}/{}",
                top + 1,
                (top + body_rows).min(layout.rows.len()),
                layout.rows.len()
            );
        }
        if snapshot.revision != editor.revision {
            frame.render_widget(
                line(
                    "Message changed while editing; Enter will be rejected.".into(),
                    Kind::Warn,
                ),
                ready,
            );
        } else {
            frame.render_widget(line(ready_text, Kind::Status), ready);
        }
        return;
    }

    let (heading_text, text, mut ready_text) = match item {
        Some(item) => {
            let mut text = if item.readable {
                item.content.clone()
            } else {
                "Voice message. Press R to read its transcript.".into()
            };
            if let Some(original) = &item.original {
                text += &format!("\n\nOriginal: {original}");
            }
            let ready = if state.approved_view {
                "Approved; T transmits"
            } else if item.readable {
                "Ready for review"
            } else {
                "Read the voice transcript before approving"
            };
            (heading(item), text, ready.to_string())
        }
        None => {
            let blocked = state.approved_view && snapshot.approved > 0;
            let (heading, text) = if blocked {
                (
                    "Approved delivery blocked",
                    "Earlier messages need review. Press Tab for Review.",
                )
            } else if state.approved_view {
                ("Approved queue empty", "No approved messages waiting.")
            } else {
                ("Review queue empty", "No messages waiting for review.")
            };
            (heading.to_string(), text.to_string(), String::new())
        }
    };
    frame.render_widget(
        Paragraph::new(Line::styled(
            clip(&heading_text, columns),
            Style::new().add_modifier(Modifier::BOLD),
        )),
        head,
    );
    let rows = wrap(&text, columns);
    // A different item (or width) starts at the top.
    let key = (
        state.approved_view,
        if state.approved_view {
            snapshot.approved_revision
        } else {
            snapshot.revision
        },
        columns,
    );
    if state.scroll_key != Some(key) {
        state.scroll = 0;
        state.scroll_key = Some(key);
    }
    state.scroll = state.scroll.min(rows.len().saturating_sub(body_rows));
    let shown: Vec<Line> = rows
        .iter()
        .skip(state.scroll)
        .take(body_rows)
        .map(|r| {
            Line::styled(
                r.clone(),
                style(if item.is_some() {
                    Kind::Reply
                } else {
                    Kind::Status
                }),
            )
        })
        .collect();
    frame.render_widget(Paragraph::new(shown), body);
    if rows.len() > body_rows {
        let end = (state.scroll + body_rows).min(rows.len());
        ready_text += &format!(
            " | Lines {}-{end}/{}; Up/Down, PgUp/PgDn",
            state.scroll + 1,
            rows.len()
        );
    }
    if let Some(delivering) = &snapshot.delivering {
        ready_text += &format!(" | Delivering item {}", delivering.number);
    }
    frame.render_widget(
        line(
            ready_text.trim_start_matches(" | ").to_string(),
            Kind::Status,
        ),
        ready,
    );
}

/// Scroll the message by `delta` rows; `None` jumps to the end.
pub fn scroll(state: &mut State, delta: Option<isize>) {
    match delta {
        Some(delta) => state.scroll = state.scroll.saturating_add_signed(delta),
        // Clamped to the last page at the next drawing.
        None => state.scroll = usize::MAX,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Service;
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;
    use ratatui::crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

    fn item(content: &str) -> ItemView {
        ItemView {
            number: 3,
            direction: Direction::Incoming,
            service: Service::WhatsApp,
            alias: "Queen".into(),
            voice: false,
            content: content.into(),
            readable: true,
            original: None,
            edit_block: None,
        }
    }

    fn render(
        snapshot: &Snapshot,
        log: &[(Kind, String)],
        state: &mut State,
        size: (u16, u16),
    ) -> Terminal<TestBackend> {
        let mut term = Terminal::new(TestBackend::new(size.0, size.1)).unwrap();
        term.draw(|f| draw(f, &Input { snapshot, log }, state))
            .unwrap();
        term
    }

    fn screen(term: &Terminal<TestBackend>) -> String {
        let buffer = term.backend().buffer();
        (0..buffer.area.height)
            .map(|y| {
                (0..buffer.area.width)
                    .map(|x| buffer[(x, y)].symbol())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    fn snapshot_with(content: &str) -> Snapshot {
        Snapshot {
            waiting: 1,
            item: Some(item(content)),
            revision: 7,
            conversation: "asleep".into(),
            ..Default::default()
        }
    }

    #[test]
    fn shows_status_rows_heading_hints_and_readiness() {
        let mut state = State::new();
        state.status = (
            Kind::Status,
            "Message approved; waiting for radio delivery.".into(),
        );
        let term = render(
            &snapshot_with("Dinner at six"),
            &[],
            &mut state,
            (MIN_COLS, MIN_ROWS),
        );
        let text = screen(&term);
        for expected in [
            "Operator: Review (Tab: Approved)",
            "Conversation: asleep",
            "Waiting for review: 1",
            "#3 Incoming WhatsApp from Queen (text)",
            "Dinner at six",
            "Ready for review",
            "Message approved",
            "[R] Read",
        ] {
            assert!(text.contains(expected), "missing {expected:?} in\n{text}");
        }
    }

    #[test]
    fn the_editor_uses_the_terminal_cursor_and_text_never_shifts() {
        let snapshot = snapshot_with("see you at noon");
        let mut state = State::new();
        state.editor = Some(Editor::new("see you at noon", 7));
        let mut term = render(&snapshot, &[], &mut state, (MIN_COLS, MIN_ROWS));
        let first = screen(&term);
        assert!(first.contains("see you at noon"), "{first}");
        let end = term.get_cursor_position().unwrap();
        for _ in 0..4 {
            state
                .editor
                .as_mut()
                .unwrap()
                .key(KeyEvent::new(KeyCode::Left, KeyModifiers::NONE));
        }
        term.draw(|f| {
            draw(
                f,
                &Input {
                    snapshot: &snapshot,
                    log: &[],
                },
                &mut state,
            )
        })
        .unwrap();
        assert_eq!(screen(&term), first, "the text stays where it was");
        let moved = term.get_cursor_position().unwrap();
        assert_eq!((moved.x, moved.y), (end.x - 4, end.y));
    }

    #[test]
    fn editing_warns_when_the_message_changes() {
        let mut snapshot = snapshot_with("a");
        let mut state = State::new();
        state.editor = Some(Editor::new("a", 7));
        snapshot.revision = 8;
        let text = screen(&render(&snapshot, &[], &mut state, (MIN_COLS, MIN_ROWS)));
        assert!(text.contains("Message changed while editing"), "{text}");
    }

    #[test]
    fn end_shows_the_last_page_and_a_new_item_starts_at_the_top() {
        let long = (1..=40)
            .map(|n| format!("line{n}"))
            .collect::<Vec<_>>()
            .join("\n");
        let mut snapshot = snapshot_with(&long);
        let mut state = State::new();
        render(&snapshot, &[], &mut state, (MIN_COLS, MIN_ROWS));
        scroll(&mut state, None);
        let text = screen(&render(&snapshot, &[], &mut state, (MIN_COLS, MIN_ROWS)));
        assert!(text.contains("line40"), "End stays in view:\n{text}");
        assert!(text.contains("/40"), "{text}");
        snapshot.revision = 8;
        let text = screen(&render(&snapshot, &[], &mut state, (MIN_COLS, MIN_ROWS)));
        assert!(
            text.contains("line1 ") || text.contains("line1\n") || text.contains("line1│"),
            "{text}"
        );
        assert_eq!(state.scroll, 0);
    }

    #[test]
    fn long_log_lines_wrap_and_the_newest_stays_visible() {
        let log = vec![
            (Kind::Event, "old".to_string()),
            (Kind::Event, format!("{} END", "x".repeat(150))),
        ];
        let text = screen(&render(
            &Snapshot::default(),
            &log,
            &mut State::new(),
            (MIN_COLS, MIN_ROWS),
        ));
        assert!(text.contains("END"), "{text}");
    }

    #[test]
    fn busy_replaces_the_hints() {
        let mut state = State::new();
        let (_tx, rx) = tokio::sync::mpsc::unbounded_channel();
        state.pending = Some(Pending { replies: rx });
        let text = screen(&render(
            &snapshot_with("x"),
            &[],
            &mut state,
            (MIN_COLS, MIN_ROWS),
        ));
        assert!(text.contains("Working; shortcuts paused"));
        assert!(!text.contains("[R] Read"));
    }

    #[test]
    fn blocked_approvals_are_explained() {
        let mut state = State::new();
        state.approved_view = true;
        let snapshot = Snapshot {
            approved: 1,
            ..Default::default()
        };
        let text = screen(&render(&snapshot, &[], &mut state, (MIN_COLS, MIN_ROWS)));
        assert!(text.contains("Approved delivery blocked"), "{text}");
    }

    #[test]
    fn the_review_area_grows_on_tall_terminals() {
        let long = (1..=40)
            .map(|n| format!("line{n}"))
            .collect::<Vec<_>>()
            .join("\n");
        let mut small = State::new();
        render(&snapshot_with(&long), &[], &mut small, (80, MIN_ROWS));
        let mut tall = State::new();
        render(&snapshot_with(&long), &[], &mut tall, (80, 50));
        assert!(tall.body_rows > small.body_rows);
    }
}
