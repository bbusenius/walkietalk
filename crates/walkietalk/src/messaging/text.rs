//! Message text: cleaning what phones send, and splitting long text into
//! radio-sized pieces.

/// Keep phone text speakable: drop control and invisible format
/// characters, turn any whitespace into plain spaces.
pub fn normalize(text: &str) -> String {
    let cleaned: String = text
        .chars()
        .filter_map(|c| {
            if c.is_whitespace() {
                Some(' ')
            } else if c.is_control() || is_format(c) {
                None
            } else {
                Some(c)
            }
        })
        .collect();
    cleaned
        .split(' ')
        .filter(|w| !w.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

/// Unicode "format" characters that render as nothing (zero-width joiners,
/// direction marks, BOMs, and similar).
fn is_format(c: char) -> bool {
    matches!(c, '\u{00AD}' | '\u{061C}' | '\u{180E}' | '\u{200B}'..='\u{200F}' | '\u{202A}'..='\u{202E}' | '\u{2060}'..='\u{2064}' | '\u{2066}'..='\u{206F}' | '\u{FEFF}' | '\u{FFF9}'..='\u{FFFB}')
}

/// Split off at most `max` characters, preferring the end of a sentence,
/// then a word boundary, and cutting a word only as a last resort.
pub fn split(text: &str, max: usize) -> (String, String) {
    let max = max.max(1);
    if text.chars().count() <= max {
        return (text.to_string(), String::new());
    }
    let limit = text.char_indices().nth(max).map_or(text.len(), |(i, _)| i);
    let window = &text[..limit];
    let sentence = window
        .char_indices()
        .filter(|&(i, c)| {
            matches!(c, '.' | '!' | '?')
                && text[i + c.len_utf8()..].starts_with(char::is_whitespace)
        })
        .map(|(i, c)| i + c.len_utf8())
        .next_back();
    let cut = sentence
        .or_else(|| window.rfind(char::is_whitespace).filter(|&i| i > 0))
        .unwrap_or(limit);
    (
        text[..cut].trim_end().to_string(),
        text[cut..].trim_start().to_string(),
    )
}

/// The final piece of a text reply ends with "over".
pub fn with_over(body: &str) -> String {
    format!(
        "{}, over",
        body.trim().trim_end_matches([' ', ',', '.', '!', '?'])
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn controls_and_invisible_characters_are_removed() {
        assert_eq!(
            normalize("Hi\u{200B} there\n\tfriend\u{7}"),
            "Hi there friend"
        );
        assert_eq!(normalize("  \u{3000}  "), "");
    }

    #[test]
    fn splits_prefer_sentences_then_words() {
        let (a, b) = split("One two. Three four five.", 16);
        assert_eq!((a.as_str(), b.as_str()), ("One two.", "Three four five."));
        let (a, b) = split("alpha beta gamma", 12);
        assert_eq!((a.as_str(), b.as_str()), ("alpha beta", "gamma"));
        let (a, b) = split("abcdefghij", 4);
        assert_eq!((a.as_str(), b.as_str()), ("abcd", "efghij"));
        let (a, b) = split("short", 10);
        assert_eq!((a.as_str(), b.as_str()), ("short", ""));
    }

    #[test]
    fn over_replaces_final_punctuation() {
        assert_eq!(with_over("See you soon!"), "See you soon, over");
    }
}
