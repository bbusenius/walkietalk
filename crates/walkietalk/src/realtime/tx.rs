//! Transmitting speech as it streams in from the realtime voice.
//!
//! The transmitter keys only when audible audio arrives (with a short
//! lead-in) and unkeys at the end of each stretch of speech (and across tool
//! calls). A reply longer than one transmission continues in further
//! transmissions, each ending at a pause in the speech; between them the
//! bridge listens for another station breaking in. The PTT supervisor
//! enforces the transmit cap on every transmission independently.

use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::audio::clip::rms;
use crate::audio::playback::{Feed, Playback};
use crate::audio::stream::StreamResampler;
use crate::config::{DRAIN_MARGIN, RADIO_RATE};
use crate::radio::{BreakIn, Keyed, Radio, TxError};

/// The realtime session's audio rate.
pub const RATE: u32 = 24_000;
/// Level that counts as speech (about 200 of 32768).
const KEY_LEVEL: f64 = 0.006;
/// Quiet audio kept in front of the first audible chunk.
const LEAD_IN: usize = (RATE as usize) * 150 / 1000;
/// Pauses are found in steps of 10 ms.
const STEP: usize = RATE as usize / 100;
/// Quiet this long counts as a pause between words (80 ms).
const PAUSE_STEPS: usize = 8;
/// Speech this long without a pause is sent without waiting for one.
const LONG_RUN: usize = RATE as usize * 3 / 2;

/// How a reply ended.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum Ending {
    #[default]
    Complete,
    /// Another station started talking between two transmissions.
    BrokenIn,
    /// The reply used every transmission it may.
    Limit,
}

struct Live {
    radio: Arc<Radio>,
    prepared: Option<(Feed, Box<dyn Playback>)>,
    keyed: Option<(Keyed, Feed, Box<dyn Playback>)>,
    /// Audio (at `RATE`) that fits in this transmission, and how much is in it.
    room: usize,
    filled: usize,
    /// Radio-rate samples that still fit; a guard against resampler rounding.
    samples_left: usize,
    upsample: StreamResampler,
    transmissions: usize,
    /// The last transmission ended because it was full.
    continuing: bool,
}

pub struct StreamTx<'a> {
    live: Option<Live>,
    max_transmissions: usize,
    break_in: Option<&'a mut dyn BreakIn>,
    /// Audio before this index was transmitted or skipped as silence.
    sent: usize,
    armed: bool,
    /// Audible speech arrived (whether or not the radio was keyed).
    pub audible: bool,
    /// Audio was cut off: the cap released the transmitter or playback stalled.
    pub truncated: bool,
    pub ending: Ending,
    /// Everything the voice sent, at 24 kHz.
    pub audio: Vec<i16>,
}

async fn blocking<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> T {
    tokio::task::spawn_blocking(f)
        .await
        .expect("transmit worker panicked")
}

impl StreamTx<'static> {
    /// One transmission at most. `radio` is `None` for receive-only use:
    /// audio is collected and judged audible, but nothing is keyed.
    pub fn new(radio: Option<Arc<Radio>>) -> StreamTx<'static> {
        StreamTx::reply(radio, 1, None)
    }
}

impl<'a> StreamTx<'a> {
    /// Up to `max_transmissions`, pausing at `break_in` between them.
    pub fn reply(
        radio: Option<Arc<Radio>>,
        max_transmissions: usize,
        break_in: Option<&'a mut dyn BreakIn>,
    ) -> StreamTx<'a> {
        StreamTx {
            live: radio.map(|radio| Live {
                radio,
                prepared: None,
                keyed: None,
                room: 0,
                filled: 0,
                samples_left: 0,
                upsample: StreamResampler::new(RATE, RADIO_RATE),
                transmissions: 0,
                continuing: false,
            }),
            max_transmissions: max_transmissions.max(1),
            break_in,
            sent: 0,
            armed: false,
            audible: false,
            truncated: false,
            ending: Ending::Complete,
            audio: Vec::new(),
        }
    }

    pub fn keyed(&self) -> bool {
        self.live.as_ref().is_some_and(|l| l.keyed.is_some())
    }

    /// The reply stopped before its end (break-in or the limit).
    pub fn stopped(&self) -> bool {
        self.ending != Ending::Complete
    }

    /// Audio before this index of [`StreamTx::audio`] went on the air or
    /// was skipped as silence.
    pub fn aired(&self) -> usize {
        self.sent
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
        if self.stopped() || self.truncated {
            return Ok(());
        }
        if !self.armed {
            let Some(loud) = first_loud(&self.audio[self.sent..]) else {
                self.sent = self.sent.max(self.audio.len().saturating_sub(LEAD_IN));
                return Ok(());
            };
            self.sent = self.sent.max((self.sent + loud).saturating_sub(LEAD_IN));
            self.armed = true;
            self.audible = true;
        }
        self.pump(false).await
    }

    /// A stretch of speech ended: send what is left and release.
    pub async fn end_segment(&mut self) -> Result<(), TxError> {
        if self.armed {
            self.pump(true).await?;
        }
        self.armed = false;
        self.release(false).await
    }

    /// The voice went quiet while keyed: finish this transmission. Speech
    /// that arrives later continues in the next one.
    pub async fn pause(&mut self) -> Result<(), TxError> {
        self.release(true).await
    }

    /// Send what can go now. Near the end of a transmission, audio waits for
    /// a pause so the transmission ends between words.
    async fn pump(&mut self, last: bool) -> Result<(), TxError> {
        if self.live.is_none() {
            self.sent = self.audio.len();
            return Ok(());
        }
        while !self.stopped() && !self.truncated {
            let pending = self.audio.len() - self.sent;
            if pending == 0 {
                break;
            }
            if !self.keyed() && !self.key().await? {
                break;
            }
            let room = self.room();
            let fits = pending <= room;
            let upto = if fits && last {
                pending
            } else {
                match last_pause(&self.audio[self.sent..self.sent + pending.min(room)]) {
                    0 if !fits => room,
                    0 if pending >= LONG_RUN => pending,
                    at => at,
                }
            };
            if upto > 0 {
                self.push(upto).await?;
            }
            if fits {
                break;
            }
            self.release(true).await?;
        }
        Ok(())
    }

    /// Audio that still fits in this transmission: what is left of its
    /// budget, and what can play before the cap after the audio already
    /// queued. Audio that arrives slowly leaves gaps, so the clock can run
    /// out before the budget does.
    fn room(&self) -> usize {
        let Some(live) = self.live.as_ref() else {
            return 0;
        };
        let Some((keyed, feed, _)) = live.keyed.as_ref() else {
            return 0;
        };
        let left = keyed
            .deadline()
            .saturating_duration_since(Instant::now())
            .saturating_sub(DRAIN_MARGIN);
        let by_clock = (left.as_secs_f64() * RATE as f64) as usize;
        let queued = feed.queued() * RATE as usize / RADIO_RATE as usize;
        (live.room - live.filled).min(by_clock.saturating_sub(queued))
    }

    /// Key for a new transmission. Returns false when the reply must stop.
    async fn key(&mut self) -> Result<bool, TxError> {
        let live = self.live.as_mut().expect("live");
        if live.transmissions >= self.max_transmissions {
            self.ending = Ending::Limit;
            return Ok(false);
        }
        if std::mem::take(&mut live.continuing)
            && let Some(break_in) = self.break_in.as_mut()
            && break_in.listen().await
        {
            self.ending = Ending::BrokenIn;
            return Ok(false);
        }
        self.prepare().await?;
        let live = self.live.as_mut().expect("live");
        let timing = live.radio.timing();
        let (feed, mut playback) = live.prepared.take().expect("prepared above");
        let ptt = live.radio.ptt();
        let deadline = Instant::now() + timing.max_tx;
        let keyed = blocking(move || Keyed::new(ptt, deadline)).await?;
        tokio::time::sleep(timing.settle).await;
        if let Err(err) = playback.start() {
            drop(keyed);
            return Err(TxError::Playback(err));
        }
        let room = timing.speech_budget();
        live.room = (room.as_secs_f64() * RATE as f64) as usize;
        live.filled = 0;
        live.samples_left = (room.as_secs_f64() * RADIO_RATE as f64) as usize;
        live.transmissions += 1;
        live.keyed = Some((keyed, feed, playback));
        Ok(true)
    }

    /// Queue the next `count` samples for the transmitter.
    async fn push(&mut self, count: usize) -> Result<(), TxError> {
        let chunk = &self.audio[self.sent..self.sent + count];
        self.sent += count;
        let Some(live) = self.live.as_mut() else {
            return Ok(());
        };
        live.filled += count;
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
        Ok(())
    }

    /// Drain the audio and release. `continuing` marks a full transmission
    /// whose reply goes on in the next one.
    async fn release(&mut self, continuing: bool) -> Result<(), TxError> {
        let Some(live) = self.live.as_mut() else {
            return Ok(());
        };
        let Some((keyed, feed, mut playback)) = live.keyed.take() else {
            return Ok(());
        };
        live.continuing = continuing;
        let tail = live.upsample.flush();
        let take = tail.len().min(live.samples_left);
        feed.push(&tail[..take]);
        feed.finish();
        let deadline = keyed.deadline();
        let (played, keyed) = blocking(move || (playback.wait(deadline), keyed)).await;
        let capped = blocking(move || keyed.release()).await?;
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

impl Drop for StreamTx<'_> {
    fn drop(&mut self) {
        self.abort();
    }
}

/// Where the first audible step of `audio` starts.
fn first_loud(audio: &[i16]) -> Option<usize> {
    audio
        .chunks(STEP)
        .position(|step| rms(step) >= KEY_LEVEL)
        .map(|i| i * STEP)
}

/// The middle of the last pause in `audio`, or 0 when there is none.
fn last_pause(audio: &[i16]) -> usize {
    let quiet: Vec<bool> = audio
        .as_chunks::<STEP>()
        .0
        .iter()
        .map(|step| rms(step) < KEY_LEVEL)
        .collect();
    let mut end = quiet.len();
    while end > 0 {
        if !quiet[end - 1] {
            end -= 1;
            continue;
        }
        let mut start = end;
        while start > 0 && quiet[start - 1] {
            start -= 1;
        }
        if end - start >= PAUSE_STEPS {
            return (start + end) / 2 * STEP;
        }
        end = start;
    }
    0
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

    /// Words of `word_ms` separated by 200 ms pauses, `total_ms` long.
    fn speech(total_ms: usize, word_ms: usize) -> Vec<i16> {
        let mut out = Vec::new();
        while out.len() < RATE as usize * total_ms / 1000 {
            out.extend(tone(word_ms, 5000));
            out.extend(tone(200, 0));
        }
        out
    }

    /// Records each pause and answers from a script.
    struct Listening {
        answers: Vec<bool>,
        asked: usize,
    }

    #[async_trait::async_trait]
    impl BreakIn for Listening {
        async fn listen(&mut self) -> bool {
            self.asked += 1;
            self.answers.get(self.asked - 1).copied().unwrap_or(false)
        }
    }

    fn keyings(line: &FakeLine) -> usize {
        line.changes().iter().filter(|k| **k).count()
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
        assert_eq!(tx.ending, Ending::Complete);
    }

    #[tokio::test]
    async fn a_long_reply_continues_in_further_transmissions() {
        let (line, out) = (FakeLine::default(), FakeOut::default());
        let mut listening = Listening {
            answers: vec![],
            asked: 0,
        };
        let mut tx = StreamTx::reply(
            Some(Arc::new(radio(&line, &out, 1500))),
            5,
            Some(&mut listening),
        );
        let audio = speech(3000, 300);
        for chunk in audio.chunks(2400) {
            tx.audio(chunk.to_vec()).await.unwrap();
        }
        tx.end_segment().await.unwrap();
        assert!(!line.keyed());
        assert!(!tx.truncated && tx.ending == Ending::Complete);
        assert_eq!(tx.aired(), audio.len(), "the whole reply aired");
        let keyed = keyings(&line);
        assert!(keyed >= 3, "about a second per transmission: {keyed}");
        drop(tx);
        assert_eq!(listening.asked, keyed - 1, "a pause between each");
    }

    #[tokio::test]
    async fn transmissions_end_in_pauses() {
        // Words of 270 ms: a full transmission would end inside one.
        let audio = speech(3000, 270);
        let (line, out) = (FakeLine::default(), FakeOut::default());
        let mut tx = StreamTx::reply(Some(Arc::new(radio(&line, &out, 1500))), 1, None);
        tx.audio(audio.clone()).await.unwrap();
        assert_eq!(tx.ending, Ending::Limit);
        let end = tx.aired();
        let room = (radio(&line, &out, 1500)
            .timing()
            .speech_budget()
            .as_secs_f64()
            * RATE as f64) as usize;
        assert!(end < room, "ended before the room ran out");
        assert!(
            rms(&audio[end - STEP..end + STEP]) < KEY_LEVEL,
            "the cut is inside a pause"
        );
    }

    #[tokio::test]
    async fn the_limit_stops_the_reply() {
        let (line, out) = (FakeLine::default(), FakeOut::default());
        let mut listening = Listening {
            answers: vec![],
            asked: 0,
        };
        let mut tx = StreamTx::reply(
            Some(Arc::new(radio(&line, &out, 1500))),
            2,
            Some(&mut listening),
        );
        let audio = speech(5000, 300);
        tx.audio(audio.clone()).await.unwrap();
        tx.end_segment().await.unwrap();
        assert_eq!(tx.ending, Ending::Limit);
        assert!(!tx.truncated);
        assert!(tx.aired() < audio.len());
        assert_eq!(keyings(&line), 2);
        assert!(!line.keyed());
    }

    #[tokio::test]
    async fn a_break_in_stops_the_reply() {
        let (line, out) = (FakeLine::default(), FakeOut::default());
        let mut listening = Listening {
            answers: vec![true],
            asked: 0,
        };
        let mut tx = StreamTx::reply(
            Some(Arc::new(radio(&line, &out, 1500))),
            5,
            Some(&mut listening),
        );
        let audio = speech(5000, 300);
        tx.audio(audio.clone()).await.unwrap();
        tx.end_segment().await.unwrap();
        assert_eq!(tx.ending, Ending::BrokenIn);
        assert_eq!(keyings(&line), 1, "nothing more after the break-in");
        assert!(!line.keyed());
        assert!(tx.aired() < audio.len());
        // Later audio is collected but never sent.
        tx.audio(tone(500, 5000)).await.unwrap();
        assert_eq!(keyings(&line), 1);
    }

    #[tokio::test]
    async fn a_quiet_voice_ends_the_transmission_and_speech_continues_after_a_pause() {
        let (line, out) = (FakeLine::default(), FakeOut::default());
        let mut listening = Listening {
            answers: vec![],
            asked: 0,
        };
        let mut tx = StreamTx::reply(
            Some(Arc::new(radio(&line, &out, 2000))),
            3,
            Some(&mut listening),
        );
        tx.audio(speech(600, 300)).await.unwrap();
        tx.pause().await.unwrap();
        assert!(!line.keyed());
        tx.audio(speech(600, 300)).await.unwrap();
        tx.end_segment().await.unwrap();
        assert_eq!(keyings(&line), 2);
        assert!(!tx.truncated);
        drop(tx);
        assert_eq!(listening.asked, 1);
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

    #[test]
    fn pauses_are_found_between_words() {
        let mut audio = tone(300, 5000);
        audio.extend(tone(50, 0));
        audio.extend(tone(300, 5000));
        assert_eq!(last_pause(&audio), 0, "50 ms is not a pause");
        let before = audio.len();
        audio.extend(tone(200, 0));
        audio.extend(tone(300, 5000));
        let at = last_pause(&audio);
        assert!(at > before && at < before + RATE as usize / 5);
    }
}
