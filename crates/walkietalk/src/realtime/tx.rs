//! Transmitting speech as it streams in from the realtime voice.
//!
//! The transmitter keys only when audible audio arrives (with a short
//! lead-in), unkeys at the end of each stretch of speech (and across tool
//! calls), and never exceeds the transmit cap for the whole response. The
//! PTT supervisor enforces that cap independently.

use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::audio::clip::rms;
use crate::audio::playback::{Feed, Playback};
use crate::audio::stream::StreamResampler;
use crate::config::RADIO_RATE;
use crate::radio::{Keyed, Radio, TxError};

/// The realtime session's audio rate.
pub const RATE: u32 = 24_000;
/// Level that counts as speech (about 200 of 32768).
const KEY_LEVEL: f64 = 0.006;
/// Quiet audio kept in front of the first audible chunk.
const LEAD_IN: usize = (RATE as usize) * 150 / 1000;

struct Live {
    radio: Arc<Radio>,
    prepared: Option<(Feed, Box<dyn Playback>)>,
    keyed: Option<(Keyed, Feed, Box<dyn Playback>)>,
    key_started: Option<Instant>,
    airtime: Duration,
    samples_left: usize,
    upsample: StreamResampler,
}

pub struct StreamTx {
    live: Option<Live>,
    lead_in: Vec<i16>,
    armed: bool,
    /// Audible speech arrived (whether or not the radio was keyed).
    pub audible: bool,
    /// The transmit cap cut the reply short.
    pub truncated: bool,
    /// Everything the voice sent, at 24 kHz.
    pub audio: Vec<i16>,
}

async fn blocking<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> T {
    tokio::task::spawn_blocking(f)
        .await
        .expect("transmit worker panicked")
}

impl StreamTx {
    /// `radio` is `None` for receive-only use: audio is collected and
    /// judged audible, but nothing is keyed.
    pub fn new(radio: Option<Arc<Radio>>) -> StreamTx {
        StreamTx {
            live: radio.map(|radio| Live {
                radio,
                prepared: None,
                keyed: None,
                key_started: None,
                airtime: Duration::ZERO,
                samples_left: 0,
                upsample: StreamResampler::new(RATE, RADIO_RATE),
            }),
            lead_in: Vec::new(),
            armed: false,
            audible: false,
            truncated: false,
            audio: Vec::new(),
        }
    }

    pub fn keyed(&self) -> bool {
        self.live.as_ref().is_some_and(|l| l.keyed.is_some())
    }

    /// Time left before the cap while keyed.
    pub fn time_left(&self) -> Option<Duration> {
        let live = self.live.as_ref()?;
        let (keyed, _, _) = live.keyed.as_ref()?;
        Some(keyed.deadline().saturating_duration_since(Instant::now()))
    }

    /// Open the playback device ahead of time, with the transmitter off.
    pub async fn prepare(&mut self) -> Result<(), TxError> {
        let Some(live) = self.live.as_mut() else {
            return Ok(());
        };
        if live.prepared.is_none() && live.keyed.is_none() {
            let radio = live.radio.clone();
            let prepared = blocking(move || radio.prepare_stream())
                .await
                .map_err(TxError::NotKeyed)?;
            live.prepared = Some(prepared);
        }
        Ok(())
    }

    pub async fn audio(&mut self, pcm: Vec<i16>) -> Result<(), TxError> {
        self.audio.extend_from_slice(&pcm);
        if self.truncated {
            return Ok(());
        }
        let chunk = if self.armed {
            pcm
        } else {
            if rms(&pcm) < KEY_LEVEL {
                self.lead_in.extend_from_slice(&pcm);
                let excess = self.lead_in.len().saturating_sub(LEAD_IN);
                self.lead_in.drain(..excess);
                return Ok(());
            }
            self.armed = true;
            self.audible = true;
            let mut chunk = std::mem::take(&mut self.lead_in);
            chunk.extend_from_slice(&pcm);
            if !self.key().await? {
                return Ok(());
            }
            chunk
        };
        self.push(&chunk).await
    }

    /// Key for the airtime that remains. Returns false if none remains.
    async fn key(&mut self) -> Result<bool, TxError> {
        self.prepare().await?;
        let Some(live) = self.live.as_mut() else {
            return Ok(true);
        };
        let timing = live.radio.timing();
        let remaining = timing.max_tx.saturating_sub(live.airtime);
        if remaining <= timing.settle + crate::config::DRAIN_MARGIN {
            self.truncated = true;
            return Ok(false);
        }
        let (feed, mut playback) = live.prepared.take().expect("prepared above");
        let ptt = live.radio.ptt();
        let deadline = Instant::now() + remaining;
        let keyed = blocking(move || Keyed::new(ptt, deadline)).await?;
        live.key_started = Some(Instant::now());
        tokio::time::sleep(timing.settle).await;
        if let Err(err) = playback.start() {
            drop(keyed);
            return Err(TxError::Playback(err));
        }
        let room = remaining - timing.settle - crate::config::DRAIN_MARGIN;
        live.samples_left = (room.as_secs_f64() * RADIO_RATE as f64) as usize;
        live.keyed = Some((keyed, feed, playback));
        Ok(true)
    }

    async fn push(&mut self, chunk: &[i16]) -> Result<(), TxError> {
        let Some(live) = self.live.as_mut() else {
            return Ok(());
        };
        let Some((_, feed, _)) = live.keyed.as_ref() else {
            return Ok(());
        };
        let gain = live.radio.timing().gain;
        let up: Vec<i16> = live
            .upsample
            .push(chunk)
            .into_iter()
            .map(|s| {
                (s as f64 * gain)
                    .round()
                    .clamp(i16::MIN as f64, i16::MAX as f64) as i16
            })
            .collect();
        let take = up.len().min(live.samples_left);
        feed.push(&up[..take]);
        live.samples_left -= take;
        if take < up.len() {
            self.truncated = true;
            self.end_segment().await?;
        }
        Ok(())
    }

    /// A stretch of speech ended: drain the audio and release.
    pub async fn end_segment(&mut self) -> Result<(), TxError> {
        self.armed = false;
        self.lead_in.clear();
        let Some(live) = self.live.as_mut() else {
            return Ok(());
        };
        let Some((keyed, feed, mut playback)) = live.keyed.take() else {
            return Ok(());
        };
        feed.push(&live.upsample.flush());
        feed.finish();
        let deadline = keyed.deadline();
        let (played, keyed) = blocking(move || (playback.wait(deadline), keyed)).await;
        let capped = blocking(move || keyed.release()).await?;
        if let Some(started) = live.key_started.take() {
            live.airtime += started.elapsed();
        }
        live.upsample = StreamResampler::new(RATE, RADIO_RATE);
        if capped || matches!(played, Ok(false)) {
            self.truncated = true;
        }
        played.map(|_| ()).map_err(TxError::Playback)
    }

    /// Release immediately (errors and cancellation).
    pub fn abort(&mut self) {
        if let Some(live) = self.live.as_mut() {
            live.keyed = None;
            live.prepared = None;
        }
    }
}

impl Drop for StreamTx {
    fn drop(&mut self) {
        self.abort();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::radio::ptt::fake::FakeLine;
    use crate::radio::tests::{FakeOut, radio};

    fn tone(ms: usize, level: i16) -> Vec<i16> {
        (0..RATE as usize * ms / 1000)
            .map(|i| if i % 2 == 0 { level } else { -level })
            .collect()
    }

    #[tokio::test]
    async fn keys_only_when_audible_audio_arrives() {
        let (line, out) = (FakeLine::default(), FakeOut::default());
        let mut tx = StreamTx::new(Some(Arc::new(radio(&line, &out, 2000))));
        tx.audio(tone(100, 0)).await.unwrap();
        tx.audio(tone(100, 10)).await.unwrap();
        assert!(
            line.changes().is_empty(),
            "silence and near-silence never key"
        );
        tx.audio(tone(100, 5000)).await.unwrap();
        assert!(line.keyed());
        tx.end_segment().await.unwrap();
        assert!(!line.keyed());
        assert!(tx.audible && !tx.truncated);
    }

    #[tokio::test]
    async fn total_airtime_is_capped_across_segments() {
        let (line, out) = (FakeLine::default(), FakeOut::default());
        let mut tx = StreamTx::new(Some(Arc::new(radio(&line, &out, 1500))));
        tx.audio(tone(100, 5000)).await.unwrap();
        assert!(line.keyed());
        tokio::time::sleep(Duration::from_millis(700)).await;
        tx.end_segment().await.unwrap();
        assert!(!tx.truncated);
        // About 0.8 s remain; a second stretch keys again but is cut short.
        tx.audio(tone(2000, 5000)).await.unwrap();
        assert!(tx.truncated);
        assert!(!line.keyed());
        assert_eq!(line.changes().iter().filter(|k| **k).count(), 2);
    }

    #[tokio::test]
    async fn receive_only_never_keys_but_detects_speech() {
        let mut tx = StreamTx::new(None);
        tx.audio(tone(100, 5000)).await.unwrap();
        tx.end_segment().await.unwrap();
        assert!(tx.audible);
        assert_eq!(tx.audio.len(), RATE as usize / 10);
    }

    #[tokio::test]
    async fn abort_releases() {
        let (line, out) = (FakeLine::default(), FakeOut::default());
        let mut tx = StreamTx::new(Some(Arc::new(radio(&line, &out, 2000))));
        tx.audio(tone(100, 5000)).await.unwrap();
        tx.abort();
        assert!(!line.keyed());
    }
}
