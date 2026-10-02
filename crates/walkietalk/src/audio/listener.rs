//! Turning a stream of frames into utterances, with a level meter.

use std::time::Instant;

use super::Clip;
use super::vad::{EndReason, Vad, VadEvent, frame_len};
use crate::config::VadConfig;
use crate::ui;

/// Frames between meter lines (half a second).
const METER_EVERY: usize = 25;

#[derive(Debug, Clone)]
pub struct Utterance {
    pub audio: Clip,
    /// When speech began; follow-up windows are judged from this moment.
    pub started_at: Instant,
    pub reason: EndReason,
}

/// What one frame did.
#[derive(Debug)]
pub enum Heard {
    Waiting,
    /// Audio was added to the utterance in progress (`new` holds it).
    Speech { new: Vec<i16> },
    /// The utterance in progress turned out to be noise and was dropped.
    Discarded,
    Finished(Utterance),
}

pub struct Listener {
    vad: Vad,
    rate: u32,
    threshold: f64,
    frames: usize,
    started_at: Option<Instant>,
    meter: bool,
}

impl Listener {
    pub fn new(config: &VadConfig, rate: u32, meter: bool) -> Listener {
        Listener {
            vad: Vad::new(config.threshold, config.hangover_ms, config.max_utterance_seconds, rate),
            rate,
            threshold: config.threshold,
            frames: 0,
            started_at: None,
            meter,
        }
    }

    pub fn speaking(&self) -> bool {
        self.vad.speaking()
    }

    pub fn peak(&self) -> f64 {
        self.vad.peak()
    }

    pub fn push(&mut self, frame: &[i16]) -> Heard {
        let before = self.vad.captured().len();
        let event = self.vad.push(frame);
        self.frames += 1;
        match event {
            VadEvent::Waiting { level } => {
                if self.meter && self.frames % METER_EVERY == 0 {
                    ui::meter!("RMS {level:.3} (threshold {:.3})", self.threshold);
                }
                Heard::Waiting
            }
            VadEvent::Started { level } => {
                self.started_at = Some(Instant::now());
                ui::event!("Speech started (RMS {level:.3})");
                Heard::Speech { new: self.vad.captured()[before..].to_vec() }
            }
            VadEvent::Speaking { level } => {
                if self.meter && self.frames % METER_EVERY == 0 {
                    ui::meter!("RMS {level:.3} peak {:.3}", self.vad.peak());
                }
                Heard::Speech { new: self.vad.captured()[before..].to_vec() }
            }
            VadEvent::Discarded { level } => {
                self.started_at = None;
                ui::meter!("Ignored short noise (RMS {level:.3})");
                Heard::Discarded
            }
            VadEvent::Finished { reason } => {
                let audio = Clip::new(self.vad.take_captured(), self.rate);
                let why = match reason {
                    EndReason::Silence => "silence",
                    EndReason::MaxLength => "maximum length",
                };
                ui::event!("Speech ended after {:.1}s ({why}; peak RMS {:.3})", audio.seconds(), self.vad.peak());
                Heard::Finished(Utterance {
                    audio,
                    started_at: self.started_at.take().unwrap_or_else(Instant::now),
                    reason,
                })
            }
        }
    }

    /// End of input: a speech burst in progress counts if it was long enough.
    pub fn finish(&mut self) -> Option<Utterance> {
        if !self.vad.has_speech() {
            return None;
        }
        Some(Utterance {
            audio: Clip::new(self.vad.take_captured(), self.rate),
            started_at: self.started_at.take().unwrap_or_else(Instant::now),
            reason: EndReason::Silence,
        })
    }

    /// Run a whole clip (from a WAV file) through the detector.
    pub fn first_utterance(config: &VadConfig, clip: &Clip) -> Option<Utterance> {
        let mut listener = Listener::new(config, clip.rate(), false);
        let size = frame_len(clip.rate());
        for chunk in clip.samples().chunks(size) {
            let mut frame = chunk.to_vec();
            frame.resize(size, 0);
            if let Heard::Finished(utterance) = listener.push(&frame) {
                return Some(utterance);
            }
        }
        listener.finish()
    }
}
