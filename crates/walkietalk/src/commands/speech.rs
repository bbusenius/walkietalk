//! `models` and `listen`.

use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::bail;

use crate::agent::Conversation;
use crate::audio::capture::Capture;
use crate::audio::listener::{Heard, Listener, Utterance};
use crate::audio::{Clip, Fit};
use crate::config::{Config, SttBackend};
use crate::credentials::Credentials;
use crate::stt;
use crate::{backends, signals, ui};

pub async fn models(config: &Config) -> anyhow::Result<()> {
    if config.stt.backend != SttBackend::Whisper {
        ui::status!(
            "stt.backend is {}; no local model is needed, but downloading anyway.",
            config.stt.backend
        );
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
pub async fn utterance_from_device(
    config: &Config,
    wait: Option<Duration>,
) -> anyhow::Result<Utterance> {
    let mut capture = Capture::open(&config.audio.input).await?;
    ui::meter!(
        "Listening on {} at {} Hz",
        config.audio.input,
        capture.rate()
    );
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

pub async fn listen(
    config: &Config,
    creds: &Credentials,
    wav: Option<PathBuf>,
    timeout: f64,
) -> anyhow::Result<()> {
    let stt = backends::transcriber(config, creds)?;
    ui::status!("Speech recognition: {}", stt.label());
    stt.prepare().await?;
    let utterance = match wav {
        Some(path) => utterance_from_wav(config, &path)?,
        None => {
            anyhow::ensure!(
                timeout > 0.0 && timeout <= 600.0,
                "--timeout must be greater than 0 and at most 600"
            );
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

/// Ask the text agent one question.
pub async fn agent_check(config: &Config, creds: &Credentials, text: &str) -> anyhow::Result<()> {
    let agent = backends::text_agent(config, creds)?;
    ui::status!("Agent: {} (no audio or PTT)", agent.label());
    let conversation = Conversation::new(agent, config, false);
    let reply = conversation.ask(text).await?;
    ui::reply!("Reply: {}", reply.text);
    Ok(())
}

/// Synthesize speech into a new WAV.
pub async fn tts_check(
    config: &Config,
    creds: &Credentials,
    text: &str,
    output: &Path,
) -> anyhow::Result<()> {
    anyhow::ensure!(
        !output.exists(),
        "{} already exists; choose a new --output",
        output.display()
    );
    let voice = backends::voice(config, creds)?;
    ui::status!("Voice: {}", voice.label());
    voice.prepare().await?;
    let clip = voice.synthesize(text, Fit::Crop).await?;
    clip.write_new_wav(output)?;
    ui::status!(
        "Wrote {}: {} Hz mono, {:.2}s (at most {:.1}s fits one transmission). No hardware opened.",
        output.display(),
        clip.rate(),
        clip.seconds(),
        config.radio.speech_budget().as_secs_f64()
    );
    Ok(())
}

/// One realtime speech-to-speech turn, saved to a new WAV.
pub async fn voice_agent_check(
    config: &Config,
    creds: &Credentials,
    wav: Option<PathBuf>,
    output: &Path,
    supervised: bool,
    consent: Option<crate::radio::TransmitConsent>,
) -> anyhow::Result<()> {
    anyhow::ensure!(
        config.agent.backend.is_realtime(),
        "voice-agent-check needs agent.backend = \"grok-realtime\""
    );
    anyhow::ensure!(
        consent.is_none() || supervised,
        "--transmit requires --supervised"
    );
    anyhow::ensure!(
        !output.exists(),
        "{} already exists; choose a new --output",
        output.display()
    );
    let settings = crate::realtime::Settings::from_config(config, creds)?;
    let utterance = match wav {
        Some(path) => utterance_from_wav(config, &path)?,
        None => utterance_from_device(config, Some(Duration::from_secs(60))).await?,
    };
    let live = consent.is_some();
    let radio = if supervised {
        Some(std::sync::Arc::new(crate::commands::hardware::radio(
            config, consent,
        )?))
    } else {
        None
    };
    ui::status!(
        "Sending {:.1}s of audio to the voice agent (billed API)...",
        utterance.audio.seconds()
    );
    let reply = crate::realtime::single_turn(&settings, &utterance.audio, radio.clone()).await?;
    if let Some(radio) = radio.filter(|_| live && reply.audible) {
        station_id_after(config, &settings, radio).await?;
    }
    if !reply.heard.is_empty() {
        ui::transcript!("Heard: {}", reply.heard);
    }
    ui::reply!(
        "Reply: {}",
        if reply.said.is_empty() {
            "(no transcript)"
        } else {
            &reply.said
        }
    );
    if reply.audio.is_empty() {
        bail!("the voice agent returned no audio");
    }
    let clip = Clip::new(reply.audio, crate::realtime::tx::RATE);
    clip.write_new_wav(output)?;
    ui::status!("Wrote {} ({:.2}s).", output.display(), clip.seconds());
    if reply.truncated {
        ui::warning!("The transmit cap cut the reply short.");
    }
    if !supervised {
        ui::status!("No playback or PTT.");
    }
    Ok(())
}

/// Identify after model speech went on the air, in its own transmission.
async fn station_id_after(
    config: &Config,
    settings: &crate::realtime::Settings,
    radio: std::sync::Arc<crate::radio::Radio>,
) -> anyhow::Result<()> {
    let id = &config.radio.station_id;
    if !id.enabled() {
        return Ok(());
    }
    ui::status!("Station ID follows in its own transmission.");
    tokio::time::sleep(crate::radio::station_id::GAP).await;
    match id.method {
        crate::config::StationIdMethod::Morse => {
            let clip = crate::audio::morse::morse(&id.callsign)?;
            tokio::task::spawn_blocking(move || radio.transmit(&clip)).await??;
        }
        crate::config::StationIdMethod::Voice => {
            let reply = crate::realtime::speak(settings, &id.callsign, Some(radio)).await?;
            anyhow::ensure!(
                reply.audible && !reply.truncated,
                "the station ID was not transmitted in full"
            );
        }
    }
    Ok(())
}
