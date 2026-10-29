//! Listening between the transmissions of one long reply, so another
//! station can break in.

use tokio::time::{Instant, sleep, timeout_at};

use super::{Input, Source};
use crate::audio::capture::Capture;
use crate::audio::vad::Vad;
use crate::config::Config;
use crate::radio::BreakIn;
use crate::ui;

/// Speech that broke in, with the open source it came from. Listening
/// resumes on that source, starting with these frames.
pub(super) struct Carried {
    pub source: Source,
    pub frames: Vec<Vec<i16>>,
}

/// The pause between two transmissions: the post-transmit mute, then the
/// break-in gap spent listening.
pub(super) struct Gap<'a> {
    pub input: &'a mut Input,
    pub carried: &'a mut Option<Carried>,
    pub config: &'a Config,
}

#[async_trait::async_trait]
impl BreakIn for Gap<'_> {
    async fn listen(&mut self) -> bool {
        let mute = self.config.radio.post_tx_mute();
        if !mute.is_zero() {
            ui::meter!(
                "Post-transmit mute {:.1}s, then listening before the rest of the reply.",
                mute.as_secs_f64()
            );
            sleep(mute).await;
        }
        match self.heard().await {
            Ok(heard) => heard,
            Err(err) => {
                ui::warning!("Could not listen between transmissions ({err:#}); continuing.");
                false
            }
        }
    }
}

impl Gap<'_> {
    /// Listen for the break-in gap. Speech that has started by its end is
    /// followed until it proves to be speech or noise.
    async fn heard(&mut self) -> anyhow::Result<bool> {
        let gap = self.config.radio.break_in_gap();
        let mut source = match self.input {
            Input::Capture => Source::Device(Capture::open(&self.config.audio.input).await?),
            #[cfg(test)]
            Input::Frames(rx) => match rx.take() {
                Some(rx) => Source::Frames(rx),
                None => return Ok(false),
            },
            // A file holds one utterance; nobody can break in.
            Input::Wav(_) => {
                sleep(gap).await;
                return Ok(false);
            }
        };
        let vad = &self.config.vad;
        let mut detector = Vad::new(vad.threshold, vad.hangover_ms, vad.max_utterance_seconds);
        let mut frames = Vec::new();
        let deadline = Instant::now() + gap;
        let heard = loop {
            let next = if detector.speaking() {
                source.next_frame().await
            } else {
                match timeout_at(deadline, source.next_frame()).await {
                    Ok(next) => next,
                    Err(_) => break false,
                }
            };
            let frame = match next {
                Ok(Some(frame)) => frame,
                Ok(None) => break false,
                Err(err) => {
                    self.give_back(source);
                    return Err(err);
                }
            };
            detector.push(&frame);
            frames.push(frame);
            if detector.has_speech() {
                break true;
            }
        };
        if heard {
            ui::event!("Someone started talking between transmissions.");
            *self.carried = Some(Carried { source, frames });
        } else {
            self.give_back(source);
        }
        Ok(heard)
    }

    /// Return test input so the loop can read the rest of it; a device just
    /// closes.
    fn give_back(&mut self, source: Source) {
        #[cfg(test)]
        if let (Source::Frames(rx), Input::Frames(slot)) = (source, &mut *self.input) {
            *slot = Some(rx);
        }
        #[cfg(not(test))]
        drop(source);
    }
}
