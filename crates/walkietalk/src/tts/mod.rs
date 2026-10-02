//! Speech synthesis backends. They turn final text into radio-ready audio
//! and never touch playback or PTT.

pub mod grok;
pub mod hermes;
pub mod piper;

use std::time::Duration;

use async_trait::async_trait;

use crate::audio::{Clip, Fit};
use crate::config::Config;

/// Longest text a voice accepts in one request.
pub const MAX_TEXT_CHARS: usize = 2000;

#[async_trait]
pub trait Voice: Send + Sync {
    fn label(&self) -> String;
    /// Check the executable, model, or credentials before first use.
    async fn prepare(&self) -> anyhow::Result<()>;
    /// Speech at the radio rate that fits one transmission. With
    /// [`Fit::Strict`], audio that would need cropping fails with
    /// [`crate::audio::TooLong`].
    async fn synthesize(&self, text: &str, fit: Fit) -> anyhow::Result<Clip>;
}

/// Settings shared by every voice.
#[derive(Debug, Clone, Copy)]
pub struct Shaping {
    pub budget: Duration,
    pub peak_normalize: bool,
    pub timeout: Duration,
}

impl Shaping {
    pub fn from_config(config: &Config) -> Shaping {
        Shaping {
            budget: config.radio.speech_budget(),
            peak_normalize: config.tts.peak_normalize,
            timeout: config.tts.timeout(),
        }
    }

    /// Resample, fit, and level a synthesized clip.
    pub fn finish(&self, clip: Clip, fit: Fit) -> anyhow::Result<Clip> {
        let clip = clip.to_radio().fit(self.budget, fit)?;
        Ok(if self.peak_normalize { clip.peak_normalized() } else { clip })
    }

    /// The most audio worth decoding: anything far beyond the budget is refused.
    pub fn decode_limit(&self) -> Duration {
        self.budget * 2 + Duration::from_secs(1)
    }
}

/// Check text before sending it to any voice.
pub fn check_text(text: &str) -> anyhow::Result<String> {
    crate::agent::clean_reply(text, MAX_TEXT_CHARS)
}
