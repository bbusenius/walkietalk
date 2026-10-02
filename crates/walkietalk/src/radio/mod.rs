//! Transmission: the only way audio reaches the air.
//!
//! - A live [`Radio`] needs a [`TransmitConsent`], which exists only when
//!   `--transmit` was given together with an explicitly named config.
//! - Audio is prepared (device open, samples queued) before keying.
//! - The PTT supervisor enforces the hard cap even if playback hangs.
//! - The transmitter is released on success, error, and drop.
//! - A PTT fault is reported as [`TxError::Ptt`], which callers treat as fatal.

pub mod ptt;
pub mod station_id;

use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::audio::playback::{AudioOut, DeviceOut, DryOut, Feed, Playback};
use crate::audio::{Clip, Fit};
use crate::cli::ConfigPath;
use crate::config::Config;
use crate::ui;
use ptt::{DryLine, Ptt, PttLine, PttOwner, SerialLine};

/// Proof that the user asked to transmit with an explicit config.
#[derive(Debug)]
pub struct TransmitConsent(());

impl TransmitConsent {
    /// Consent exists only for `--transmit` with a config named on the command line.
    pub fn grant(transmit: bool, config: &ConfigPath) -> anyhow::Result<Option<TransmitConsent>> {
        if !transmit {
            return Ok(None);
        }
        anyhow::ensure!(
            config.is_explicit(),
            "--transmit requires the config to be named explicitly with --config"
        );
        Ok(Some(TransmitConsent(())))
    }
}

#[derive(Debug, thiserror::Error)]
pub enum TxError {
    /// Nothing went on the air.
    #[error("transmission not started: {0:#}")]
    NotKeyed(anyhow::Error),
    /// The transmitter was keyed and has been released.
    #[error("playback failed while transmitting: {0:#}")]
    Playback(anyhow::Error),
    /// The keying hardware misbehaved; stop and check the radio.
    #[error("PTT fault: {0:#}. Turn the radio off and check the interface before restarting")]
    Ptt(anyhow::Error),
}

impl TxError {
    pub fn is_fatal(&self) -> bool {
        matches!(self, TxError::Ptt(_))
    }
}

#[derive(Debug, Clone, Copy)]
pub struct Timing {
    pub max_tx: Duration,
    pub settle: Duration,
    pub gain: f64,
}

impl Timing {
    pub fn from_config(config: &Config) -> Timing {
        Timing {
            max_tx: config.radio.max_tx(),
            settle: config.radio.settle(),
            gain: config.audio.gain,
        }
    }

    /// Audio that fits after the settle delay.
    pub fn speech_budget(&self) -> Duration {
        self.max_tx.saturating_sub(self.settle)
    }
}

/// The transmitter: PTT plus playback.
pub struct Radio {
    owner: PttOwner,
    out: Box<dyn AudioOut>,
    timing: Timing,
    live: bool,
}

impl Radio {
    /// The real serial line and audio device.
    pub fn live(config: &Config, _consent: TransmitConsent) -> anyhow::Result<Radio> {
        let line = SerialLine::open(&config.ptt.port, config.ptt.line)?;
        ui::status!(
            "PTT: {} keys with {}; the other line is held low",
            config.ptt.port.display(),
            config.ptt.line
        );
        Ok(Radio::with_parts(
            Box::new(line),
            Box::new(DeviceOut::new(&config.audio.output)),
            Timing::from_config(config),
            true,
        ))
    }

    /// Simulated keying and playback.
    pub fn dry(config: &Config) -> Radio {
        ui::status!("DRY RUN: no serial port or audio device will be opened");
        Radio::with_parts(Box::new(DryLine::default()), Box::new(DryOut), Timing::from_config(config), false)
    }

    pub fn with_parts(line: Box<dyn PttLine>, out: Box<dyn AudioOut>, timing: Timing, live: bool) -> Radio {
        Radio {
            owner: PttOwner::start(line),
            out,
            timing,
            live,
        }
    }

    pub fn is_live(&self) -> bool {
        self.live
    }

    pub fn timing(&self) -> Timing {
        self.timing
    }

    /// A handle for emergency release from signal handlers.
    pub fn ptt(&self) -> Ptt {
        self.owner.handle()
    }

    /// Transmit one clip that must fit the speech budget.
    pub fn transmit(&self, clip: &Clip) -> Result<(), TxError> {
        let clip = clip
            .to_radio()
            .fit(self.timing.speech_budget(), Fit::Strict)
            .map_err(|err| TxError::NotKeyed(err.into()))?
            .with_gain(self.timing.gain);
        let playback = self.out.prepare(&clip).map_err(TxError::NotKeyed)?;
        let ptt = self.owner.handle();
        let deadline = Instant::now() + self.timing.max_tx;
        let mut keyed = Keyed::new(&ptt, deadline)?;
        keyed.settle(self.timing.settle);
        let played = play(playback, deadline);
        let capped = keyed.release()?;
        match played {
            Ok(true) if !capped => Ok(()),
            Ok(_) => Err(TxError::Playback(anyhow::anyhow!(
                "audio did not finish within the transmit cap; the transmitter was released"
            ))),
            Err(err) => Err(TxError::Playback(err)),
        }
    }

    /// Open playback for streamed audio. Keying is up to the caller.
    pub fn prepare_stream(&self) -> anyhow::Result<(Feed, Box<dyn Playback>)> {
        self.out.prepare_stream()
    }
}

fn play(mut playback: Box<dyn Playback>, deadline: Instant) -> anyhow::Result<bool> {
    playback.start()?;
    playback.wait(deadline)
}

/// The transmitter is keyed while this exists.
pub struct Keyed<'a> {
    ptt: &'a Ptt,
    deadline: Instant,
    released: bool,
}

impl<'a> Keyed<'a> {
    pub fn new(ptt: &'a Ptt, deadline: Instant) -> Result<Keyed<'a>, TxError> {
        let mut keyed = Keyed { ptt, deadline, released: false };
        if let Err(err) = ptt.key(deadline) {
            // Make sure a partial assertion is released before reporting.
            keyed.released = true;
            let _ = ptt.release();
            return Err(TxError::Ptt(err));
        }
        Ok(keyed)
    }

    /// Wait out the key-up delay, never past the deadline.
    pub fn settle(&self, settle: Duration) {
        let left = self.deadline.saturating_duration_since(Instant::now());
        std::thread::sleep(settle.min(left));
    }

    pub fn deadline(&self) -> Instant {
        self.deadline
    }

    /// Release and report whether the cap had already released the line.
    pub fn release(mut self) -> Result<bool, TxError> {
        self.released = true;
        self.ptt.release().map_err(TxError::Ptt)
    }
}

impl Drop for Keyed<'_> {
    fn drop(&mut self) {
        if !self.released {
            if let Err(err) = self.ptt.release() {
                ui::error!("PTT release failed: {err:#}. Turn the radio off.");
            }
        }
    }
}

/// Shared ownership for use across threads and tasks.
pub type SharedRadio = Arc<Radio>;

#[cfg(test)]
pub mod tests {
    use super::ptt::fake::FakeLine;
    use super::*;
    use std::sync::Mutex;

    /// Plays instantly, hangs, or fails, as configured.
    #[derive(Clone, Default)]
    pub struct FakeOut {
        pub played: Arc<Mutex<Vec<usize>>>,
        pub hang: bool,
        pub fail_prepare: bool,
        pub fail_playback: bool,
    }

    struct FakePlayback {
        out: FakeOut,
        len: usize,
    }

    impl AudioOut for FakeOut {
        fn prepare(&self, clip: &Clip) -> anyhow::Result<Box<dyn Playback>> {
            anyhow::ensure!(!self.fail_prepare, "simulated device failure");
            Ok(Box::new(FakePlayback { out: self.clone(), len: clip.len() }))
        }

        fn prepare_stream(&self) -> anyhow::Result<(Feed, Box<dyn Playback>)> {
            anyhow::ensure!(!self.fail_prepare, "simulated device failure");
            Ok((Feed::default(), Box::new(FakePlayback { out: self.clone(), len: 0 })))
        }
    }

    impl Playback for FakePlayback {
        fn start(&mut self) -> anyhow::Result<()> {
            Ok(())
        }

        fn wait(&mut self, deadline: Instant) -> anyhow::Result<bool> {
            anyhow::ensure!(!self.out.fail_playback, "simulated underrun");
            if self.out.hang {
                std::thread::sleep(deadline.saturating_duration_since(Instant::now()) + Duration::from_millis(50));
                return Ok(false);
            }
            self.out.played.lock().unwrap().push(self.len);
            Ok(true)
        }
    }

    pub fn timing(max_ms: u64) -> Timing {
        Timing {
            max_tx: Duration::from_millis(max_ms),
            settle: Duration::from_millis(10),
            gain: 1.0,
        }
    }

    pub fn radio(line: &FakeLine, out: &FakeOut, max_ms: u64) -> Radio {
        Radio::with_parts(Box::new(line.clone()), Box::new(out.clone()), timing(max_ms), true)
    }

    fn clip(ms: u64) -> Clip {
        Clip::silence(Duration::from_millis(ms), 48_000)
    }

    #[test]
    fn successful_transmission_keys_then_releases() {
        let (line, out) = (FakeLine::default(), FakeOut::default());
        radio(&line, &out, 1000).transmit(&clip(100)).unwrap();
        assert_eq!(line.changes().last(), Some(&false));
        assert!(line.changes().contains(&true));
        assert_eq!(out.played.lock().unwrap().len(), 1);
    }

    #[test]
    fn preparation_failure_never_keys() {
        let line = FakeLine::default();
        let out = FakeOut { fail_prepare: true, ..Default::default() };
        let err = radio(&line, &out, 1000).transmit(&clip(100)).unwrap_err();
        assert!(matches!(err, TxError::NotKeyed(_)));
        assert!(!line.changes().contains(&true));
    }

    #[test]
    fn audio_longer_than_the_budget_is_refused_before_keying() {
        let (line, out) = (FakeLine::default(), FakeOut::default());
        let err = radio(&line, &out, 500).transmit(&clip(600)).unwrap_err();
        assert!(matches!(err, TxError::NotKeyed(_)));
        assert!(!line.changes().contains(&true));
    }

    #[test]
    fn hung_playback_is_cut_off_at_the_cap() {
        let line = FakeLine::default();
        let out = FakeOut { hang: true, ..Default::default() };
        let started = Instant::now();
        let err = radio(&line, &out, 200).transmit(&clip(100)).unwrap_err();
        assert!(matches!(err, TxError::Playback(_)));
        assert!(!line.keyed());
        let (_, released_at) = *line.events.lock().unwrap().iter().find(|(k, _)| !*k).unwrap();
        assert!(released_at.duration_since(started) < Duration::from_millis(400));
    }

    #[test]
    fn playback_error_still_releases() {
        let line = FakeLine::default();
        let out = FakeOut { fail_playback: true, ..Default::default() };
        let err = radio(&line, &out, 1000).transmit(&clip(100)).unwrap_err();
        assert!(matches!(err, TxError::Playback(_)));
        assert!(!line.keyed());
    }

    #[test]
    fn ptt_fault_is_fatal() {
        let line = FakeLine::default();
        *line.fail_key.lock().unwrap() = true;
        let err = radio(&line, &FakeOut::default(), 1000).transmit(&clip(100)).unwrap_err();
        assert!(err.is_fatal());
        assert!(!line.keyed());
    }

    #[test]
    fn consent_requires_transmit_and_an_explicit_config() {
        let explicit = ConfigPath::Explicit("/x/config.toml".into());
        let default = ConfigPath::Default("/x/config.toml".into());
        assert!(TransmitConsent::grant(false, &explicit).unwrap().is_none());
        assert!(TransmitConsent::grant(true, &default).is_err());
        assert!(TransmitConsent::grant(true, &explicit).unwrap().is_some());
    }
}
