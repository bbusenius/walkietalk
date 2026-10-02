//! Energy-based utterance detection.
//!
//! Speech starts when a 20 ms frame's RMS reaches the threshold and ends
//! after a run of quieter frames (the hangover). Bursts with less than a
//! quarter second of voiced audio are treated as noise and discarded.

use std::collections::VecDeque;

use super::clip::rms;

pub const FRAME_MS: u32 = 20;
const MIN_VOICED_MS: u32 = 250;

pub fn frame_len(rate: u32) -> usize {
    (rate * FRAME_MS / 1000).max(1) as usize
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum VadEvent {
    /// No speech yet.
    Waiting {
        level: f64,
    },
    /// Speech began with this frame.
    Started {
        level: f64,
    },
    Speaking {
        level: f64,
    },
    /// A short burst ended before it counted as speech; it was dropped.
    Discarded {
        level: f64,
    },
    /// The utterance is complete.
    Finished {
        reason: EndReason,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EndReason {
    Silence,
    MaxLength,
}

#[derive(Debug, Clone)]
pub struct Vad {
    threshold: f64,
    hangover_frames: usize,
    max_frames: usize,
    min_voiced: usize,
    speaking: bool,
    quiet_left: usize,
    frames: usize,
    voiced: usize,
    peak: f64,
    captured: Vec<i16>,
    preroll: VecDeque<Vec<i16>>,
}

impl Vad {
    pub fn new(threshold: f64, hangover_ms: u32, max_seconds: f64) -> Vad {
        let per_second = 1000 / FRAME_MS as usize;
        let hangover_frames = (hangover_ms / FRAME_MS).max(1) as usize;
        Vad {
            threshold,
            hangover_frames,
            max_frames: ((max_seconds * per_second as f64).round() as usize).max(1),
            min_voiced: (MIN_VOICED_MS / FRAME_MS) as usize,
            speaking: false,
            quiet_left: 0,
            frames: 0,
            voiced: 0,
            peak: 0.0,
            captured: Vec::new(),
            preroll: VecDeque::with_capacity(hangover_frames),
        }
    }

    pub fn speaking(&self) -> bool {
        self.speaking
    }

    /// Loudest frame level seen so far.
    pub fn peak(&self) -> f64 {
        self.peak
    }

    /// Audio of the utterance in progress, including a little lead-in.
    pub fn captured(&self) -> &[i16] {
        &self.captured
    }

    pub fn take_captured(&mut self) -> Vec<i16> {
        std::mem::take(&mut self.captured)
    }

    /// Whether enough voiced audio was heard to count as speech.
    pub fn has_speech(&self) -> bool {
        self.speaking && self.voiced >= self.min_voiced
    }

    pub fn push(&mut self, frame: &[i16]) -> VadEvent {
        let level = rms(frame);
        self.peak = self.peak.max(level);
        let loud = level >= self.threshold;
        if !self.speaking {
            if !loud {
                if self.preroll.len() == self.hangover_frames {
                    self.preroll.pop_front();
                }
                self.preroll.push_back(frame.to_vec());
                return VadEvent::Waiting { level };
            }
            self.speaking = true;
            for prior in self.preroll.drain(..) {
                self.captured.extend_from_slice(&prior);
            }
            self.captured.extend_from_slice(frame);
            self.frames = 1;
            self.voiced = 1;
            self.quiet_left = self.hangover_frames;
            return VadEvent::Started { level };
        }
        self.captured.extend_from_slice(frame);
        self.frames += 1;
        if self.frames >= self.max_frames {
            return VadEvent::Finished {
                reason: EndReason::MaxLength,
            };
        }
        if loud {
            self.voiced += 1;
            self.quiet_left = self.hangover_frames;
            return VadEvent::Speaking { level };
        }
        self.quiet_left -= 1;
        if self.quiet_left > 0 {
            return VadEvent::Speaking { level };
        }
        if self.voiced < self.min_voiced {
            self.speaking = false;
            self.captured.clear();
            self.frames = 0;
            self.voiced = 0;
            return VadEvent::Discarded { level };
        }
        VadEvent::Finished {
            reason: EndReason::Silence,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const RATE: u32 = 8000;

    fn frame(amplitude: i16) -> Vec<i16> {
        (0..frame_len(RATE))
            .map(|i| if i % 2 == 0 { amplitude } else { -amplitude })
            .collect()
    }

    fn run(vad: &mut Vad, frames: &[(i16, usize)]) -> Vec<VadEvent> {
        frames
            .iter()
            .flat_map(|&(amp, n)| std::iter::repeat_n(amp, n))
            .map(|amp| vad.push(&frame(amp)))
            .collect()
    }

    #[test]
    fn speech_ends_after_hangover_and_keeps_lead_in() {
        let mut vad = Vad::new(0.1, 100, 10.0);
        let events = run(&mut vad, &[(0, 10), (10_000, 20), (0, 5)]);
        assert!(matches!(events[10], VadEvent::Started { .. }));
        assert_eq!(
            events.last(),
            Some(&VadEvent::Finished {
                reason: EndReason::Silence
            })
        );
        // 5 frames of lead-in, 20 loud, 5 quiet.
        assert_eq!(vad.captured().len(), 30 * frame_len(RATE));
    }

    #[test]
    fn short_noise_is_discarded() {
        let mut vad = Vad::new(0.1, 60, 10.0);
        let events = run(&mut vad, &[(10_000, 3), (0, 3)]);
        assert!(matches!(events.last(), Some(VadEvent::Discarded { .. })));
        assert!(!vad.speaking());
        assert!(vad.captured().is_empty());
    }

    #[test]
    fn long_speech_stops_at_the_maximum() {
        let mut vad = Vad::new(0.1, 100, 1.0);
        let events = run(&mut vad, &[(10_000, 100)]);
        let end = events
            .iter()
            .position(|e| matches!(e, VadEvent::Finished { .. }))
            .unwrap();
        assert_eq!(
            events[end],
            VadEvent::Finished {
                reason: EndReason::MaxLength
            }
        );
        assert_eq!(end, 49);
    }
}
