//! `devices`, `ptt`, and `play`.

use std::path::Path;
use std::time::{Duration, Instant};

use anyhow::Context;

use crate::audio::Clip;
use crate::audio::device;
use crate::config::Config;
use crate::radio::{Keyed, Radio, TransmitConsent};
use crate::{signals, ui};

pub fn devices(all: bool) -> anyhow::Result<()> {
    let listings = device::list()?;
    let shown: Vec<_> = listings.iter().filter(|d| all || d.recommended()).collect();
    if shown.is_empty() {
        ui::status!("No sound cards found by name. Connect the interface, or use --all to see every ALSA device.");
    }
    for d in &shown {
        let dirs = match (d.input, d.output) {
            (true, true) => "capture+playback",
            (true, false) => "capture",
            (false, true) => "playback",
            (false, false) => "-",
        };
        ui::status!("{:<44} {:<17} {}", d.name, dirs, d.description.lines().next().unwrap_or(""));
    }
    let by_id = Path::new("/dev/serial/by-id");
    let mut ports: Vec<_> = std::fs::read_dir(by_id)
        .map(|entries| entries.flatten().map(|e| e.path()).collect())
        .unwrap_or_default();
    ports.sort();
    if ports.is_empty() {
        ui::status!("No serial ports under /dev/serial/by-id; connect the interface.");
    }
    for port in ports {
        ui::status!("serial: {}", port.display());
    }
    Ok(())
}

/// Open the transmitter: real hardware with consent, otherwise simulated.
pub fn radio(config: &Config, consent: Option<TransmitConsent>) -> anyhow::Result<Radio> {
    let radio = match consent {
        Some(consent) => Radio::live(config, consent)?,
        None => Radio::dry(config),
    };
    signals::protect(radio.ptt());
    Ok(radio)
}

pub fn ptt(config: &Config, consent: Option<TransmitConsent>, seconds: f64) -> anyhow::Result<()> {
    let max = config.radio.max_tx_seconds;
    anyhow::ensure!(
        seconds.is_finite() && seconds > 0.0 && seconds <= max,
        "--seconds must be greater than 0 and at most radio.max_tx_seconds ({max})"
    );
    let radio = radio(config, consent)?;
    let ptt = radio.ptt();
    let deadline = Instant::now() + radio.timing().max_tx;
    let keyed = Keyed::new(&ptt, deadline)?;
    let until = Instant::now() + Duration::from_secs_f64(seconds);
    while Instant::now() < until && !signals::stop_requested() {
        std::thread::sleep(Duration::from_millis(20).min(until.saturating_duration_since(Instant::now())));
    }
    keyed.release()?;
    ui::status!("Finished; transmitter released.");
    Ok(())
}

pub fn play(config: &Config, consent: Option<TransmitConsent>, wav: &Path) -> anyhow::Result<()> {
    let budget = config.radio.speech_budget();
    let clip = Clip::read_wav(wav, budget).with_context(|| {
        format!("the WAV must fit in {:.1}s (max_tx_seconds minus settle_seconds)", budget.as_secs_f64())
    })?;
    ui::status!("WAV: {} Hz mono, {:.2}s, gain {}", clip.rate(), clip.seconds(), config.audio.gain);
    let radio = radio(config, consent)?;
    radio.transmit(&clip)?;
    ui::status!("Finished; transmitter released.");
    Ok(())
}
