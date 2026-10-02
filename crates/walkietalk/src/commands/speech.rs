//! `models` and `listen`.

use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::bail;

use crate::audio::Clip;
use crate::audio::capture::Capture;
use crate::audio::listener::{Heard, Listener, Utterance};
use crate::config::{Config, SttBackend};
use crate::credentials::Credentials;
use crate::stt::{self, Transcriber};
use crate::{signals, ui};

pub async fn models(config: &Config) -> anyhow::Result<()> {
    if config.stt.backend != SttBackend::Whisper {
        ui::status!("stt.backend is {}; no local model is needed, but downloading anyway.", config.stt.backend);
    }
    stt::models::download(config.stt.model).await?;
    Ok(())
}

/// Read one utterance from a WAV file.
pub fn utterance_from_wav(config: &Config, wav: &Path) -> anyhow::Result<Utterance> {
    let clip = Clip::read_wav(wav, Duration::from_secs(120))?;
    ui::status!("WAV: {} Hz mono, {:.2}s", clip.rate(), clip.seconds());
    match Listener::first_utterance(&config.vad, &clip) {
        Some(utterance) => Ok(utterance),
        None => bail!(
            "no speech found in the WAV at vad.threshold {}; lower the threshold or use a louder recording",
            config.vad.threshold
        ),
    }
}

/// Wait up to `wait` for someone to talk, then record one utterance.
pub async fn utterance_from_device(config: &Config, wait: Option<Duration>) -> anyhow::Result<Utterance> {
    let mut capture = Capture::open(&config.audio.input)?;
    ui::meter!("Listening on {} at {} Hz", config.audio.input, capture.rate());
    let mut listener = Listener::new(&config.vad, capture.rate(), true);
    let deadline = wait.map(|w| tokio::time::Instant::now() + w);
    let stop = signals::token();
    loop {
        let frame = tokio::select! {
            frame = capture.next_frame() => frame?,
            _ = stop.cancelled() => bail!("stopped"),
            _ = sleep_until(deadline), if !listener.speaking() => bail!(
                "no speech within {:.0}s (peak RMS {:.3}, threshold {:.3}); check the receive volume or lower vad.threshold",
                wait.unwrap_or_default().as_secs_f64(), listener.peak(), config.vad.threshold
            ),
        };
        if capture.take_overflow() {
            ui::warning!("Capture overflow; some audio was lost.");
        }
        if let Heard::Finished(utterance) = listener.push(&frame) {
            return Ok(utterance);
        }
    }
}

async fn sleep_until(deadline: Option<tokio::time::Instant>) {
    match deadline {
        Some(d) => tokio::time::sleep_until(d).await,
        None => std::future::pending().await,
    }
}

pub async fn listen(config: &Config, creds: &Credentials, wav: Option<PathBuf>, timeout: f64) -> anyhow::Result<()> {
    let stt = open_stt(config, creds)?;
    ui::status!("Speech recognition: {}", stt.label());
    stt.prepare().await?;
    let utterance = match wav {
        Some(path) => utterance_from_wav(config, &path)?,
        None => {
            anyhow::ensure!(timeout > 0.0 && timeout <= 600.0, "--timeout must be greater than 0 and at most 600");
            utterance_from_device(config, Some(Duration::from_secs_f64(timeout))).await?
        }
    };
    ui::meter!("Transcribing...");
    let text = stt.transcribe(&utterance.audio).await?;
    if text.trim().is_empty() {
        ui::ignored!("Transcript: (no words)");
    } else {
        ui::transcript!("Transcript: {text}");
    }
    ui::status!("Receive only; nothing was transmitted.");
    Ok(())
}

pub fn open_stt(config: &Config, _creds: &Credentials) -> anyhow::Result<Box<dyn Transcriber>> {
    match config.stt.backend {
        SttBackend::Whisper => Ok(Box::new(stt::whisper::Whisper::new(config.stt.model, config.stt.timeout()))),
        other => bail!("stt.backend {other} is not available yet"),
    }
}
