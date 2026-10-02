//! Agent guidance with placeholders filled from the radio settings.

use std::fmt;

/// Values a guidance template may mention.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Limits {
    pub max_reply_chars: usize,
    /// Seconds of speech that fit in one transmission.
    pub spoken_seconds: f64,
}

impl Limits {
    /// About two spoken words per second, never less than one.
    pub fn max_words(&self) -> usize {
        ((self.spoken_seconds * 2.0).floor() as usize).max(1)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Field {
    MaxReplyChars,
    SpokenSeconds,
    MaxWords,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum Piece {
    Text(String),
    Field(Field),
}

/// A parsed guidance template. `{{` and `}}` are literal braces.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Template(Vec<Piece>);

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum TemplateError {
    #[error(
        "unknown placeholder {{{0}}}; use {{max_reply_chars}}, {{spoken_seconds}}, or {{max_words}}"
    )]
    Unknown(String),
    #[error("unmatched brace; write {{{{ or }}}} for a literal brace")]
    Unmatched,
}

impl Template {
    pub fn parse(text: &str) -> Result<Template, TemplateError> {
        let mut pieces = Vec::new();
        let mut literal = String::new();
        let mut chars = text.chars().peekable();
        while let Some(c) = chars.next() {
            match c {
                '{' if chars.peek() == Some(&'{') => {
                    chars.next();
                    literal.push('{');
                }
                '}' if chars.peek() == Some(&'}') => {
                    chars.next();
                    literal.push('}');
                }
                '{' => {
                    let mut name = String::new();
                    loop {
                        match chars.next() {
                            Some('}') => break,
                            Some('{') | None => return Err(TemplateError::Unmatched),
                            Some(c) => name.push(c),
                        }
                    }
                    let field = match name.as_str() {
                        "max_reply_chars" => Field::MaxReplyChars,
                        "spoken_seconds" => Field::SpokenSeconds,
                        "max_words" => Field::MaxWords,
                        _ => return Err(TemplateError::Unknown(name)),
                    };
                    if !literal.is_empty() {
                        pieces.push(Piece::Text(std::mem::take(&mut literal)));
                    }
                    pieces.push(Piece::Field(field));
                }
                '}' => return Err(TemplateError::Unmatched),
                c => literal.push(c),
            }
        }
        if !literal.is_empty() {
            pieces.push(Piece::Text(literal));
        }
        Ok(Template(pieces))
    }

    pub fn render(&self, limits: Limits) -> String {
        let mut out = String::new();
        for piece in &self.0 {
            match piece {
                Piece::Text(text) => out.push_str(text),
                Piece::Field(Field::MaxReplyChars) => {
                    out.push_str(&limits.max_reply_chars.to_string())
                }
                Piece::Field(Field::SpokenSeconds) => {
                    out.push_str(&Seconds(limits.spoken_seconds).to_string())
                }
                Piece::Field(Field::MaxWords) => out.push_str(&limits.max_words().to_string()),
            }
        }
        out
    }
}

/// Seconds without a trailing `.0`.
struct Seconds(f64);

impl fmt::Display for Seconds {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let rounded = (self.0 * 10.0).round() / 10.0;
        if rounded.fract() == 0.0 {
            write!(f, "{}", rounded as i64)
        } else {
            write!(f, "{rounded:.1}")
        }
    }
}

const DEFAULT: &str = "Answer in short, plain, spoken-style sentences suitable for a family. \
Use at most {max_reply_chars} characters.";
const SPOKEN: &str = " This answer will be spoken on a radio. Aim for one short sentence, \
at most {max_words} words, to fit within {spoken_seconds} seconds. Use radio procedure words \
such as over, copy, roger, stand by, go ahead, and out when they fit.";

/// The guidance paragraph: the user's template, or the built-in default
/// (which mentions the radio when the reply will be spoken).
pub fn guidance(custom: &str, limits: Limits, spoken: bool) -> String {
    let template = if custom.is_empty() {
        let mut text = DEFAULT.to_string();
        if spoken {
            text.push_str(SPOKEN);
        }
        Template::parse(&text).expect("built-in guidance is valid")
    } else {
        Template::parse(custom).expect("instructions are validated with the config")
    };
    template.render(limits)
}

#[cfg(test)]
mod tests {
    use super::*;

    const LIMITS: Limits = Limits {
        max_reply_chars: 600,
        spoken_seconds: 9.8,
    };

    #[test]
    fn placeholders_are_filled() {
        let t =
            Template::parse("{max_reply_chars} chars, {max_words} words, {spoken_seconds}s {{x}}")
                .unwrap();
        assert_eq!(t.render(LIMITS), "600 chars, 19 words, 9.8s {x}");
    }

    #[test]
    fn bad_templates_are_rejected() {
        assert_eq!(
            Template::parse("{mood}"),
            Err(TemplateError::Unknown("mood".into()))
        );
        assert_eq!(Template::parse("{max_words"), Err(TemplateError::Unmatched));
        assert_eq!(Template::parse("a } b"), Err(TemplateError::Unmatched));
    }

    #[test]
    fn default_guidance_mentions_radio_only_when_spoken() {
        assert!(guidance("", LIMITS, true).contains("radio"));
        assert!(!guidance("", LIMITS, false).contains("radio"));
        assert_eq!(
            Limits {
                max_reply_chars: 1,
                spoken_seconds: 0.2
            }
            .max_words(),
            1
        );
    }
}
