//! Word-level phrase matching for wake phrases and spoken controls.
//!
//! Speech recognizers vary in case and punctuation, so phrases are compared
//! as sequences of lowercase words. Matching is exact on those words; it is
//! never fuzzy. Mistranscriptions are handled with explicit aliases.

/// Byte spans of the words (runs of letters and digits) in `text`.
fn word_spans(text: &str) -> Vec<(usize, usize)> {
    let mut spans = Vec::new();
    let mut start = None;
    for (index, c) in text.char_indices() {
        match (c.is_alphanumeric(), start) {
            (true, None) => start = Some(index),
            (false, Some(s)) => {
                spans.push((s, index));
                start = None;
            }
            _ => {}
        }
    }
    if let Some(s) = start {
        spans.push((s, text.len()));
    }
    spans
}

/// The lowercase words of `text`.
pub fn words(text: &str) -> Vec<String> {
    word_spans(text)
        .into_iter()
        .map(|(s, e)| text[s..e].to_lowercase())
        .collect()
}

/// Lowercase words joined by single spaces; punctuation and spacing ignored.
pub fn normalize(text: &str) -> String {
    words(text).join(" ")
}

/// A configured phrase, pre-split into words.
#[derive(Debug, Clone)]
pub struct Phrase {
    text: String,
    words: Vec<String>,
}

impl Phrase {
    pub fn new(text: &str) -> Phrase {
        Phrase {
            text: text.to_string(),
            words: words(text),
        }
    }

    pub fn text(&self) -> &str {
        &self.text
    }

    /// Number of words.
    pub fn len(&self) -> usize {
        self.words.len()
    }

    pub fn is_empty(&self) -> bool {
        self.words.is_empty()
    }
}

/// The longest phrase that starts `utterance`, as (index into `phrases`,
/// words consumed). Ties go to the earlier phrase.
pub fn longest_prefix(utterance: &str, phrases: &[Phrase]) -> Option<(usize, usize)> {
    let words = words(utterance);
    phrases
        .iter()
        .enumerate()
        .filter(|(_, p)| !p.is_empty() && words.starts_with(&p.words))
        .fold(
            None,
            |best: Option<(usize, usize)>, (index, p)| match best {
                Some((_, len)) if len >= p.len() => best,
                _ => Some((index, p.len())),
            },
        )
}

/// The text after the first `count` words, without separating punctuation.
pub fn after_words(utterance: &str, count: usize) -> &str {
    if count == 0 {
        return utterance.trim();
    }
    let spans = word_spans(utterance);
    let Some(&(_, end)) = spans.get(count - 1) else {
        return "";
    };
    utterance[end..]
        .trim_start_matches(|c: char| {
            c.is_whitespace() || ",.?!:;-\u{2013}\u{2014}\u{2026}".contains(c)
        })
        .trim_end()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_ignores_case_punctuation_and_spacing() {
        assert_eq!(normalize("  Go, to   SLEEP! "), "go to sleep");
        assert_eq!(normalize("Ça va?"), "ça va");
        assert_eq!(normalize("!!!"), "");
    }

    #[test]
    fn longest_matching_phrase_wins() {
        let phrases = [
            Phrase::new("code"),
            Phrase::new("code one"),
            Phrase::new("charlotte"),
        ];
        assert_eq!(
            longest_prefix("Code one, hello nana", &phrases),
            Some((1, 2))
        );
        assert_eq!(longest_prefix("code two", &phrases), Some((0, 1)));
        assert_eq!(longest_prefix("Charlottesville is nice", &phrases), None);
    }

    #[test]
    fn traffic_keeps_original_text_after_the_phrase() {
        assert_eq!(after_words("Charlotte, what's 2+2?", 1), "what's 2+2?");
        assert_eq!(after_words("charlotte", 1), "");
        assert_eq!(
            after_words("Rain bow trout -- tell me a joke.", 3),
            "tell me a joke."
        );
    }
}
