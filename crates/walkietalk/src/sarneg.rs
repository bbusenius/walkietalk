//! SARNEG (Search and Rescue Numerical Encryption Grid) codes.
//!
//! A shared ten-letter key with no repeated letters stands for the digits
//! 0 through 9, so a secret number is spoken as letters: with key
//! `AFTERSHOCK`, 762 is "O H T" or "oscar hotel tango". Reading a transcript
//! is deterministic: a NATO phonetic alphabet word stands for its first
//! letter, a single letter for itself, and every other word is ignored. A
//! single letter may be clarified ("A as in Andy", "M as in Mike"); the
//! clarification belongs to that letter.

use crate::phrases;

/// The NATO standard phonetic alphabet, A through Z: the official spelling
/// first, then common variants. "X-ray" is transcribed as the words "x" and
/// "ray", which already reads as X.
const PHONETIC: [&[&str]; 26] = [
    &["alfa", "alpha"],
    &["bravo"],
    &["charlie"],
    &["delta"],
    &["echo"],
    &["foxtrot"],
    &["golf"],
    &["hotel"],
    &["india"],
    &["juliett", "juliet"],
    &["kilo"],
    &["lima"],
    &["mike"],
    &["november"],
    &["oscar"],
    &["papa"],
    &["quebec"],
    &["romeo"],
    &["sierra"],
    &["tango"],
    &["uniform"],
    &["victor"],
    &["whiskey", "whisky"],
    &["xray"],
    &["yankee"],
    &["zulu"],
];

/// The phonetic words, as hints for speech recognizers.
pub fn phonetic_words() -> impl Iterator<Item = &'static str> {
    PHONETIC.iter().flat_map(|words| words.iter().copied())
}

/// The letter a lowercase word stands for, if any.
fn letter(word: &str) -> Option<char> {
    let mut chars = word.chars();
    match (chars.next(), chars.next()) {
        (Some(c), None) if c.is_ascii_alphabetic() => Some(c.to_ascii_uppercase()),
        (Some(c), Some(_)) if phonetic_words().any(|w| w == word) => Some(c.to_ascii_uppercase()),
        _ => None,
    }
}

/// A validated key: position `d` holds the letter that stands for digit `d`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Key([char; 10]);

impl Key {
    /// Ten ASCII letters, none repeated, in any case.
    pub fn parse(text: &str) -> Option<Key> {
        let letters: Vec<char> = text
            .trim()
            .chars()
            .map(|c| c.to_ascii_uppercase())
            .collect();
        let distinct = letters
            .iter()
            .enumerate()
            .all(|(i, c)| c.is_ascii_uppercase() && !letters[..i].contains(c));
        if !distinct {
            return None;
        }
        letters.try_into().ok().map(Key)
    }

    /// The digits `letters` stand for; None when a letter is not in the key.
    fn decode(&self, letters: &[char]) -> Option<String> {
        letters
            .iter()
            .map(|l| {
                let digit = self.0.iter().position(|k| k == l)?;
                char::from_digit(digit as u32, 10)
            })
            .collect()
    }
}

/// Whether a number is valid as a code: at least three digits. Shorter
/// codes would match ordinary words that are also letters ("Mike", "a").
pub fn valid_number(number: &str) -> bool {
    number.len() >= 3 && number.bytes().all(|b| b.is_ascii_digit())
}

/// The number of words after a single letter that clarify it: "as in",
/// then a word that starts with that letter. "For" is not accepted; it begins
/// too many requests ("T for the weather").
fn clarification(rest: &[String], l: char) -> usize {
    match rest {
        [a, b, example, ..]
            if a == "as" && b == "in" && example.starts_with(l.to_ascii_lowercase()) =>
        {
            3
        }
        _ => 0,
    }
}

/// The letters spoken in `words`, each with the index of the word after it
/// and its clarification.
fn spoken(words: &[String]) -> Vec<(char, usize)> {
    let mut letters = Vec::new();
    let mut index = 0;
    while index < words.len() {
        let word = &words[index];
        index += 1;
        let Some(l) = letter(word) else { continue };
        if word.len() == 1 {
            index += clarification(&words[index..], l);
        }
        letters.push((l, index));
    }
    letters
}

/// Whether the spoken letters in `utterance` are exactly `number`.
pub fn is_code(key: &Key, utterance: &str, number: &str) -> bool {
    let letters: Vec<char> = spoken(&phrases::words(utterance))
        .into_iter()
        .map(|(l, _)| l)
        .collect();
    key.decode(&letters).as_deref() == Some(number)
}

/// The code among `numbers` spoken at the start of `utterance`, as (index
/// into `numbers`, words consumed). The first spoken letter must begin the
/// code; ignored words before or inside it are consumed with it.
pub fn longest_prefix(key: &Key, utterance: &str, numbers: &[String]) -> Option<(usize, usize)> {
    let mut letters = Vec::new();
    let mut best = None;
    for (l, end) in spoken(&phrases::words(utterance)) {
        letters.push(l);
        let Some(digits) = key.decode(&letters) else {
            break;
        };
        if let Some(code) = numbers.iter().position(|n| *n == digits) {
            best = Some((code, end));
        }
        if !numbers.iter().any(|n| n.starts_with(&digits)) {
            break;
        }
    }
    best
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(text: &str) -> Key {
        Key::parse(text).unwrap()
    }

    fn numbers(list: &[&str]) -> Vec<String> {
        list.iter().map(|n| n.to_string()).collect()
    }

    #[test]
    fn key_needs_ten_distinct_letters() {
        assert!(Key::parse("AFTERSHOCK").is_some());
        assert_eq!(Key::parse(" blackhorse "), Key::parse("BLACKHORSE"));
        assert!(Key::parse("AFTERSHOC").is_none(), "nine letters");
        assert!(Key::parse("AFTERSHOCKS").is_none(), "eleven letters");
        assert!(Key::parse("AFTERSHOCA").is_none(), "A repeats");
        assert!(Key::parse("AFTERSHOcA").is_none(), "repeats ignore case");
        assert!(Key::parse("AFTER SHOC").is_none());
        assert!(Key::parse("AFTERSH0CK").is_none(), "a digit");
        assert!(Key::parse("ÄFTERSHOCK").is_none());
    }

    #[test]
    fn numbers_are_at_least_three_digits() {
        assert!(valid_number("762"));
        assert!(valid_number("007"));
        assert!(valid_number("12345678901234567890"));
        assert!(!valid_number("76"));
        assert!(!valid_number(""));
        assert!(!valid_number("76 2"));
        assert!(!valid_number("seven"));
    }

    #[test]
    fn spaced_letters_and_phonetic_words_decode_alike() {
        let k = key("AFTERSHOCK");
        for spoken in [
            "O H T",
            "o. h. t.",
            "O-H-T",
            "Oscar, Hotel, Tango.",
            "oscar H tango",
        ] {
            assert!(is_code(&k, spoken, "762"), "{spoken}");
        }
        // From the MGRS example: 63380.
        assert!(is_code(&k, "hotel echo echo charlie alfa", "63380"));
        assert!(is_code(&k, "H E E C Alpha", "63380"));
    }

    #[test]
    fn other_words_are_ignored_but_letters_are_exact() {
        let k = key("AFTERSHOCK");
        assert!(is_code(&k, "um, oscar hotel tango, over", "762"));
        assert!(
            !is_code(&k, "oscar hotel tango echo", "762"),
            "extra letter"
        );
        assert!(!is_code(&k, "oscar hotel", "762"), "missing letter");
        assert!(!is_code(&k, "hotel oscar tango", "762"), "order matters");
        assert!(
            !is_code(&k, "OHT", "762"),
            "run-together letters are one word"
        );
        assert!(
            !is_code(&k, "oscar hotel bravo", "762"),
            "B is not in the key"
        );
        assert!(!is_code(&k, "", "762"));
    }

    #[test]
    fn the_same_number_under_a_new_key_is_new_letters() {
        let old = key("AFTERSHOCK");
        let new = key("BLACKHORSE");
        assert!(is_code(&old, "O H T", "762"));
        assert!(!is_code(&new, "O H T", "762"));
        assert!(is_code(&new, "R O A", "762"));
    }

    #[test]
    fn a_code_at_the_start_leaves_the_rest_as_traffic() {
        let k = key("AFTERSHOCK");
        let codes = numbers(&["762", "4518"]);
        let text = "Oscar hotel tango, what's a good joke?";
        let (code, words) = longest_prefix(&k, text, &codes).unwrap();
        assert_eq!(code, 0);
        assert_eq!(phrases::after_words(text, words), "what's a good joke?");
        // The traffic's own letters ("a", "I") are never read as code.
        assert_eq!(
            longest_prefix(&k, "R S F C I need a hand", &codes),
            Some((1, 4))
        );
        assert_eq!(
            longest_prefix(&k, "uh, O H T", &codes),
            Some((0, 4)),
            "filler before the code"
        );
    }

    #[test]
    fn a_clarified_letter_is_one_letter() {
        let k = key("AFTERSHOCK");
        // 012 is A F T; 763 is O H E.
        assert!(is_code(
            &k,
            "A as in Andy, F as in Frank, T as in Tom",
            "012"
        ));
        // The example is not read again, even when it is a phonetic word.
        assert!(is_code(
            &k,
            "O as in Oscar, H as in hotel, E as in echo",
            "763"
        ));
        assert!(is_code(&k, "O, hotel, E as in Echo", "763"));
        let codes = numbers(&["012"]);
        let text = "A as in Andy, F as in Frank, T as in Tom";
        let (_, words) = longest_prefix(&k, text, &codes).unwrap();
        assert_eq!(
            phrases::after_words(text, words),
            "",
            "nothing is left as traffic"
        );
        let text = "A F T as in Tom, what's the weather?";
        let (_, words) = longest_prefix(&k, text, &codes).unwrap();
        assert_eq!(phrases::after_words(text, words), "what's the weather?");
    }

    #[test]
    fn only_a_matching_as_in_clarifies() {
        let k = key("AFTERSHOCK");
        let codes = numbers(&["012"]);
        for (text, traffic) in [
            ("A F T for the weather", "for the weather"),
            ("A F T as in Bob", "as in Bob"),
            ("A F T as in", "as in"),
            // A phonetic word is never clarified.
            ("alfa foxtrot tango as in tango", "as in tango"),
        ] {
            let (_, words) = longest_prefix(&k, text, &codes).unwrap();
            assert_eq!(phrases::after_words(text, words), traffic, "{text}");
        }
    }

    #[test]
    fn the_first_spoken_letter_must_begin_the_code() {
        let k = key("AFTERSHOCK");
        let codes = numbers(&["762"]);
        assert_eq!(longest_prefix(&k, "I said O H T", &codes), None);
        assert_eq!(longest_prefix(&k, "oscar hotel echo", &codes), None);
        assert_eq!(
            longest_prefix(&k, "charlotte, what time is it", &codes),
            None
        );
    }

    #[test]
    fn the_alphabet_is_nato_a_through_z() {
        for (words, expected) in PHONETIC.iter().zip('A'..='Z') {
            for word in *words {
                assert_eq!(letter(word), Some(expected), "{word}");
            }
        }
        let official: Vec<&str> = PHONETIC.iter().map(|words| words[0]).collect();
        assert_eq!(
            official.join(" "),
            "alfa bravo charlie delta echo foxtrot golf hotel india juliett kilo lima mike \
             november oscar papa quebec romeo sierra tango uniform victor whiskey xray yankee zulu"
        );
        // The official "X-ray" spelling reads as X.
        assert!(is_code(&key("XYLOPHANES"), "X-ray, Yankee, Lima", "012"));
    }

    #[test]
    fn only_listed_words_are_letters() {
        assert_eq!(letter("x"), Some('X'));
        assert_eq!(letter("juliett"), Some('J'));
        assert_eq!(letter("xray"), Some('X'));
        assert_eq!(letter("oh"), None);
        assert_eq!(letter("see"), None);
        assert_eq!(letter("7"), None);
        assert_eq!(letter("é"), None);
    }
}
