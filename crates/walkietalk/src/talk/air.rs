//! Putting speech on the air: confirmations, replies, and station IDs.

use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::Context;

use crate::audio::morse::morse;
use crate::audio::{Clip, Fit};
use crate::config::{Config, StationIdMethod};
use crate::radio::station_id::{self, StationId};
use crate::radio::{Radio, TxError};
use crate::tts::Voice;
use crate::ui;

/// Why a reply did not reach the air.
#[derive(Debug, thiserror::Error)]
pub enum AirError {
    /// The reply never went out; listening can continue.
    #[error("{0:#}")]
    NotSent(anyhow::Error),
    /// The transmitter was keyed but the audio failed; it has been released
    /// and part of the reply may have been heard.
    #[error("{0:#}")]
    Playback(anyhow::Error),
    /// Stop the program: the hardware or a due station ID failed.
    #[error("{message}")]
    Fatal {
        message: String,
        /// The reply itself was transmitted before the failure.
        reply_sent: bool,
    },
}

impl AirError {
    fn fatal(message: impl Into<String>, reply_sent: bool) -> AirError {
        AirError::Fatal {
            message: message.into(),
            reply_sent,
        }
    }
}

/// The transmitter plus what is needed to speak on it.
pub struct Air {
    radio: Arc<Radio>,
    voice: Option<Arc<dyn Voice>>,
    station_id: StationId,
    budget: Duration,
    mute: Duration,
    /// Playback failures in a row; a device that keeps failing stops `talk`
    /// rather than keying dead air again and again.
    playback_failures: u32,
}

/// Consecutive playback failures that stop `talk`.
const PLAYBACK_FAILURE_LIMIT: u32 = 3;

impl Air {
    pub fn new(radio: Radio, voice: Option<Arc<dyn Voice>>, config: &Config) -> Air {
        Air {
            radio: Arc::new(radio),
            voice,
            station_id: StationId::new(config.radio.station_id.clone()),
            budget: config.radio.speech_budget(),
            mute: config.radio.post_tx_mute(),
            playback_failures: 0,
        }
    }

    pub fn voice(&self) -> Option<&Arc<dyn Voice>> {
        self.voice.as_ref()
    }

    pub fn radio(&self) -> &Arc<Radio> {
        &self.radio
    }

    pub fn budget(&self) -> Duration {
        self.budget
    }

    pub fn station_id(&self) -> &StationId {
        &self.station_id
    }

    pub fn station_id_mut(&mut self) -> &mut StationId {
        &mut self.station_id
    }

    /// Synthesize text, cropping speech that would not fit.
    pub async fn synthesize(&self, text: &str) -> anyhow::Result<Clip> {
        let voice = self
            .voice
            .as_ref()
            .context("no voice is configured for transmission")?;
        voice.synthesize(text, Fit::Crop).await
    }

    /// Transmit one prepared clip off the async threads.
    pub async fn transmit(&self, clip: Clip) -> Result<(), TxError> {
        let radio = self.radio.clone();
        tokio::task::spawn_blocking(move || radio.transmit(&clip))
            .await
            .unwrap_or_else(|err| {
                Err(TxError::NotKeyed(anyhow::anyhow!(
                    "transmit worker failed: {err}"
                )))
            })
    }

    /// Prepare the station ID clip if one is due. Morse is generated here;
    /// voice IDs come from the voice and are never cropped.
    pub async fn due_id(&self) -> Result<Option<Clip>, AirError> {
        if !self.station_id.due(Instant::now()) {
            return Ok(None);
        }
        ui::meter!("Preparing station ID...");
        let callsign = self.station_id.callsign();
        let clip = match self.station_id.config().method {
            StationIdMethod::Morse => morse(callsign),
            StationIdMethod::Voice => match &self.voice {
                Some(voice) => voice.synthesize(callsign, Fit::Strict).await,
                None => Err(anyhow::anyhow!(
                    "a voice station ID needs a configured voice"
                )),
            },
        };
        clip.map(Some).map_err(|err| {
            AirError::fatal(
                format!("station ID could not be prepared: {err:#}; stopping"),
                false,
            )
        })
    }

    /// Transmit a reply, with the station ID when due, then the
    /// post-transmit mute. The ID is marked sent only after it airs.
    pub async fn send(&mut self, reply: Clip) -> Result<(), AirError> {
        self.send_with_id(reply, true).await
    }

    async fn send_with_id(&mut self, reply: Clip, id_allowed: bool) -> Result<(), AirError> {
        let id = if id_allowed {
            self.due_id().await?
        } else {
            None
        };
        let bursts = match &id {
            Some(id_clip) => {
                station_id::plan(reply, id_clip.clone(), self.budget).map_err(|err| {
                    AirError::fatal(
                        format!("station ID does not fit one transmission: {err}"),
                        false,
                    )
                })?
            }
            None => vec![reply],
        };
        let result = self.send_bursts(bursts, id.is_some()).await;
        match &result {
            Ok(()) => self.playback_failures = 0,
            Err(AirError::Playback(err)) => {
                self.playback_failures += 1;
                if self.playback_failures >= PLAYBACK_FAILURE_LIMIT {
                    return Err(AirError::fatal(
                        format!(
                            "audio playback failed {PLAYBACK_FAILURE_LIMIT} times in a row ({err:#}); stopping"
                        ),
                        false,
                    ));
                }
            }
            Err(_) => {}
        }
        // Part of a failed transmission may have been heard: mute as usual.
        if matches!(result, Ok(()) | Err(AirError::Playback(_))) {
            self.mute().await;
        }
        result
    }

    async fn send_bursts(&mut self, bursts: Vec<Clip>, with_id: bool) -> Result<(), AirError> {
        for (index, burst) in bursts.into_iter().enumerate() {
            if index > 0 {
                ui::status!("Station ID follows in its own transmission.");
                tokio::time::sleep(station_id::GAP).await;
            }
            match self.transmit(burst).await {
                Ok(()) => {}
                Err(err @ TxError::Ptt(_)) => {
                    return Err(AirError::fatal(err.to_string(), index > 0));
                }
                Err(err) if index > 0 => {
                    return Err(AirError::fatal(
                        format!("station ID failed: {err}; stopping"),
                        true,
                    ));
                }
                Err(TxError::NotKeyed(err)) => return Err(AirError::NotSent(err)),
                Err(TxError::Playback(err)) => return Err(AirError::Playback(err)),
            }
        }
        if with_id {
            self.station_id.mark_sent(Instant::now());
        }
        Ok(())
    }

    pub async fn mute(&self) {
        if !self.mute.is_zero() {
            ui::meter!("Post-transmit mute {:.1}s.", self.mute.as_secs_f64());
            tokio::time::sleep(self.mute).await;
        }
    }

    /// Speak a fixed phrase (a confirmation). Empty text stays silent.
    /// Synthesis problems are reported and return `Ok(false)`.
    pub async fn say(&mut self, text: &str, what: &str) -> Result<bool, AirError> {
        if text.is_empty() {
            return Ok(false);
        }
        let clip = match self.synthesize(text).await {
            Ok(clip) => clip,
            Err(err) => {
                ui::error!("{what} could not be synthesized: {err:#}");
                return Ok(false);
            }
        };
        // Confirmations carry an ID only when an interval ID is due;
        // end-of-reply IDs belong to replies.
        let interval = self.station_id.config().mode == crate::config::StationIdMode::Interval;
        match self.send_with_id(clip, interval).await {
            Ok(()) => Ok(true),
            Err(AirError::NotSent(err)) => {
                ui::error!("{what} was not transmitted: {err:#}");
                Ok(false)
            }
            Err(AirError::Playback(err)) => {
                ui::error!("{what} failed during transmission: {err:#}");
                Ok(false)
            }
            Err(other) => Err(other),
        }
    }
}
