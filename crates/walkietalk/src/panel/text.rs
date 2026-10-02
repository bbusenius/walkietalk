//! Fitting text into terminal columns by display width, so wide characters
//! (emoji, CJK) and combining marks line up.

use unicode_width::UnicodeWidthChar;

/// Columns a character takes on screen (0 for combining marks).
pub fn width(c: char) -> usize {
    c.width().unwrap_or(0)
}

/// Wrap text into rows of at most `columns`, breaking at spaces where
/// possible and inside words only when a word is longer than a row. No
/// characters are dropped. Newlines start new rows.
pub fn wrap(text: &str, columns: usize) -> Vec<String> {
    let columns = columns.max(1);
    let mut rows = Vec::new();
    for paragraph in text.split('\n') {
        let mut rest: Vec<char> = paragraph.chars().filter(|c| !c.is_control()).collect();
        if rest.is_empty() {
            rows.push(String::new());
            continue;
        }
        while !rest.is_empty() {
            // How many characters fit.
            let mut used = 0;
            let mut fit = 0;
            for &c in &rest {
                if fit > 0 && used + width(c) > columns {
                    break;
                }
                used += width(c);
                fit += 1;
            }
            // Prefer to break after the last space that fits.
            let take = if fit < rest.len() {
                match rest[..fit].iter().rposition(|&c| c == ' ') {
                    Some(space) if space > 0 => space + 1,
                    _ => fit,
                }
            } else {
                fit
            };
            let row: String = rest.drain(..take).collect();
            rows.push(row.trim_end().to_string());
            while rest.first() == Some(&' ') {
                rest.remove(0);
            }
        }
    }
    rows
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wraps_at_spaces_and_splits_long_words() {
        assert_eq!(wrap("alpha beta gamma", 11), ["alpha beta", "gamma"]);
        assert_eq!(wrap("abcdefghij", 4), ["abcd", "efgh", "ij"]);
        assert_eq!(wrap("one\n\ntwo", 10), ["one", "", "two"]);
    }

    #[test]
    fn wide_characters_count_double() {
        // Each of these takes two columns.
        assert_eq!(wrap("日本語テキスト", 6), ["日本語", "テキス", "ト"]);
        assert!(
            wrap("🙂🙂🙂", 4)
                .iter()
                .all(|row| row.chars().map(width).sum::<usize>() <= 4)
        );
    }
}
