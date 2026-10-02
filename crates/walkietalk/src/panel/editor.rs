//! The panel's message editor: one logical line, wrapped to the screen,
//! with a real terminal cursor.

use ratatui::crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

use super::text::width;
use crate::operator::server::MAX_EDIT_CHARS;

pub enum Outcome {
    Save,
    Cancel,
    Continue,
}

pub struct Editor {
    text: Vec<char>,
    cursor: usize,
    /// The revision of the item being edited; the edit applies only to it.
    pub revision: u64,
    /// First wrapped row on screen.
    scroll: usize,
}

/// The editor laid out for a given width.
pub struct Layout {
    pub rows: Vec<String>,
    pub cursor_row: usize,
    pub cursor_column: usize,
}

impl Editor {
    pub fn new(text: &str, revision: u64) -> Editor {
        let text: Vec<char> = text.chars().take(MAX_EDIT_CHARS).collect();
        Editor {
            cursor: text.len(),
            text,
            revision,
            scroll: 0,
        }
    }

    pub fn text(&self) -> String {
        self.text.iter().collect()
    }

    pub fn key(&mut self, key: KeyEvent) -> Outcome {
        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
        match key.code {
            KeyCode::Enter => return Outcome::Save,
            KeyCode::Esc => return Outcome::Cancel,
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
        Outcome::Continue
    }

    fn insert(&mut self, c: char) {
        if self.text.len() < MAX_EDIT_CHARS && !c.is_control() {
            self.text.insert(self.cursor, c);
            self.cursor += 1;
        }
    }

    /// Wrap by display width (character by character, so the cursor maps to
    /// one place) and find the cursor's row and column.
    pub fn layout(&self, columns: usize) -> Layout {
        let columns = columns.max(2);
        let (mut rows, mut row, mut used) = (Vec::new(), String::new(), 0);
        let (mut cursor_row, mut cursor_column) = (0, 0);
        for (index, &c) in self.text.iter().enumerate() {
            if !row.is_empty() && used + width(c) > columns {
                rows.push(std::mem::take(&mut row));
                used = 0;
            }
            if index == self.cursor {
                (cursor_row, cursor_column) = (rows.len(), used);
            }
            row.push(c);
            used += width(c);
        }
        if self.cursor == self.text.len() {
            // The cursor sits after the last character; start a new row if full.
            if used >= columns {
                rows.push(std::mem::take(&mut row));
                used = 0;
            }
            (cursor_row, cursor_column) = (rows.len(), used);
        }
        rows.push(row);
        Layout {
            rows,
            cursor_row,
            cursor_column,
        }
    }

    /// Keep the cursor's row within `visible` rows; returns the first row to show.
    pub fn scroll_to_cursor(&mut self, layout: &Layout, visible: usize) -> usize {
        let visible = visible.max(1);
        if layout.cursor_row < self.scroll {
            self.scroll = layout.cursor_row;
        } else if layout.cursor_row >= self.scroll + visible {
            self.scroll = layout.cursor_row + 1 - visible;
        }
        self.scroll = self.scroll.min(layout.rows.len().saturating_sub(visible));
        self.scroll
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    fn ctrl(c: char) -> KeyEvent {
        KeyEvent::new(KeyCode::Char(c), KeyModifiers::CONTROL)
    }

    #[test]
    fn supports_the_documented_keys() {
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
        assert!(matches!(e.key(key(KeyCode::Enter)), Outcome::Save));
        assert!(matches!(e.key(key(KeyCode::Esc)), Outcome::Cancel));
    }

    #[test]
    fn moving_the_cursor_never_changes_the_text_layout() {
        let mut e = Editor::new("see you at noon", 1);
        let before = e.layout(10).rows;
        for _ in 0..6 {
            e.key(key(KeyCode::Left));
            assert_eq!(e.layout(10).rows, before);
        }
    }

    #[test]
    fn cursor_position_follows_wrapping_and_wide_characters() {
        let mut e = Editor::new("abcdefgh", 1);
        let l = e.layout(5);
        assert_eq!(l.rows, ["abcde", "fgh"]);
        assert_eq!((l.cursor_row, l.cursor_column), (1, 3));
        e.key(key(KeyCode::Home));
        let l = e.layout(5);
        assert_eq!((l.cursor_row, l.cursor_column), (0, 0));
        // A full last row puts the end cursor on a new row.
        let full = Editor::new("abcde", 1).layout(5);
        assert_eq!((full.cursor_row, full.cursor_column), (1, 0));
        // Wide characters advance the cursor by two columns.
        let mut wide = Editor::new("日本", 1);
        wide.key(key(KeyCode::Left));
        let l = wide.layout(10);
        assert_eq!((l.cursor_row, l.cursor_column), (0, 2));
    }

    #[test]
    fn scrolling_keeps_the_cursor_in_view() {
        let mut e = Editor::new(&"word ".repeat(40), 1);
        let l = e.layout(10);
        let top = e.scroll_to_cursor(&l, 3);
        assert!(l.cursor_row >= top && l.cursor_row < top + 3);
        e.key(key(KeyCode::Home));
        let l = e.layout(10);
        assert_eq!(e.scroll_to_cursor(&l, 3), 0);
    }

    #[test]
    fn edits_are_capped() {
        let mut e = Editor::new(&"x".repeat(MAX_EDIT_CHARS), 1);
        e.key(key(KeyCode::Char('y')));
        assert_eq!(e.text().len(), MAX_EDIT_CHARS);
    }
}
