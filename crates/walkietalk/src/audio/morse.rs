//! International Morse code station identification, generated locally.
//!
//! 20 words per minute (60 ms units) on an 800 Hz tone at half scale, with
//! short ramps so keying does not click.

use crate::config::RADIO_RATE;

use super::Clip;

const UNIT_SECONDS: f64 = 1.2 / 20.0;
const TONE_HZ: f64 = 800.0;
const AMPLITUDE: f64 = 0.5;
const RAMP_SECONDS: f64 = 0.005;

fn code(c: char) -> Option<&'static str> {
    Some(match c.to_ascii_uppercase() {
        'A' => ".-", 'B' => "-...", 'C' => "-.-.", 'D' => "-..", 'E' => ".", 'F' => "..-.",
        'G' => "--.", 'H' => "....", 'I' => "..", 'J' => ".---", 'K' => "-.-", 'L' => ".-..",
        'M' => "--", 'N' => "-.", 'O' => "---", 'P' => ".--.", 'Q' => "--.-", 'R' => ".-.",
        'S' => "...", 'T' => "-", 'U' => "..-", 'V' => "...-", 'W' => ".--", 'X' => "-..-",
        'Y' => "-.--", 'Z' => "--..", '0' => "-----", '1' => ".----", '2' => "..---",
        '3' => "...--", '4' => "....-", '5' => ".....", '6' => "-....", '7' => "--...",
        '8' => "---..", '9' => "----.",
        _ => return None,
    })
}

/// Tone (positive) and gap (negative) lengths in units.
fn units(text: &str) -> Result<Vec<i32>, char> {
    let mut out = Vec::new();
    for (w, word) in text.split_whitespace().enumerate() {
        if w > 0 {
            out.push(-7);
        }
        for (i, c) in word.chars().enumerate() {
            let pattern = code(c).ok_or(c)?;
            if i > 0 {
                out.push(-3);
            }
            for (j, element) in pattern.chars().enumerate() {
                if j > 0 {
                    out.push(-1);
                }
                out.push(if element == '-' { 3 } else { 1 });
            }
        }
    }
    Ok(out)
}

/// The call sign as a Morse clip at the radio rate.
pub fn morse(text: &str) -> anyhow::Result<Clip> {
    let runs = units(text).map_err(|c| anyhow::anyhow!("Morse cannot send {c:?}; use letters, digits, and spaces"))?;
    anyhow::ensure!(!runs.is_empty(), "Morse station ID needs a letter or digit");
    let unit = (UNIT_SECONDS * RADIO_RATE as f64).round() as usize;
    let ramp = (RAMP_SECONDS * RADIO_RATE as f64).round() as usize;
    let mut samples = Vec::new();
    for run in runs {
        let n = run.unsigned_abs() as usize * unit;
        if run < 0 {
            samples.extend(std::iter::repeat_n(0i16, n));
            continue;
        }
        let edge = ramp.min(n / 2);
        samples.extend((0..n).map(|i| {
            let envelope = if i < edge {
                i as f64 / edge as f64
            } else if i >= n - edge {
                (n - i) as f64 / edge as f64
            } else {
                1.0
            };
            let phase = std::f64::consts::TAU * TONE_HZ * i as f64 / RADIO_RATE as f64;
            (phase.sin() * envelope * AMPLITUDE * i16::MAX as f64).round() as i16
        }));
    }
    Ok(Clip::new(samples, RADIO_RATE))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn timing_follows_the_standard() {
        // S = "..." is 5 units; a letter gap is 3; O = "---" is 11.
        assert_eq!(units("SO").unwrap(), vec![1, -1, 1, -1, 1, -3, 3, -1, 3, -1, 3]);
        // A word space is 7 units, and repeated spaces collapse.
        assert_eq!(units("E  E").unwrap(), vec![1, -7, 1]);
    }

    #[test]
    fn clip_length_matches_units() {
        let clip = morse("e").unwrap();
        assert_eq!(clip.len(), (UNIT_SECONDS * 48_000.0).round() as usize);
        assert!(clip.samples().iter().all(|s| s.unsigned_abs() <= i16::MAX as u16 / 2 + 1));
    }

    #[test]
    fn punctuation_is_refused() {
        assert!(morse("TEST-123").is_err());
        assert!(morse("   ").is_err());
    }
}
