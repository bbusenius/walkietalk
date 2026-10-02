//! `talk`: the main loop.
//!
//! Listen for an utterance, transcribe it, apply the shutdown and wake
//! gates, route the traffic, and speak replies on the air only when the
//! user consented to transmit. Capture is closed while processing and
//! transmitting. Recoverable failures are reported and listening resumes.

mod air;

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{Context, bail};
use tokio_util::sync::CancellationToken;

use crate::agent::Conversation;
use crate::audio::capture::Capture;
use crate::audio::listener::{Heard, Listener, Utterance};
use crate::config::Config;
use crate::credentials::Credentials;
use crate::gate::{Control, Decision, Destination, Gate, Shutdown};
use crate::radio::{Radio, TransmitConsent};
use crate::stt::Transcriber;
use crate::{backends, signals, ui};
use air::{Air, AirError};

pub struct Options {
    pub wav: Option<PathBuf>,
    pub consent: Option<TransmitConsent>,
    pub once: bool,
    /// Wait-for-speech limit for a single capture.
    pub timeout: Option<f64>,
}

enum Input {
    /// A WAV file, consumed by the first listen.
    Wav(Option<PathBuf>),
    Capture,
}

struct Talk {
    config: Config,
    air: Option<Air>,
    stt: Arc<dyn Transcriber>,
    conversation: Conversation,
    gate: Gate,
    shutdown: Shutdown,
    input: Input,
    once: bool,
    wait: Option<Duration>,
    stop: CancellationToken,
}

/// What to do after handling an utterance.
#[derive(PartialEq, Eq)]
enum Next {
    Listen,
    Exit,
}

pub async fn run(config: Config, creds: Credentials, options: Options) -> anyhow::Result<()> {
    let once = options.once || options.wav.is_some();
    if options.timeout.is_some() && !(options.once && options.wav.is_none()) {
        bail!("--timeout applies only to --capture --once; continuous listening has no idle limit");
    }
    let wait = match (once, options.wav.is_some()) {
        (true, false) => {
            let seconds = options.timeout.unwrap_or(60.0);
            anyhow::ensure!(seconds > 0.0 && seconds <= 600.0, "--timeout must be greater than 0 and at most 600");
            Some(Duration::from_secs_f64(seconds))
        }
        _ => None,
    };
    let transmit = options.consent.is_some();

    let stt: Arc<dyn Transcriber> = Arc::from(backends::transcriber(&config, &creds)?);
    ui::status!("Speech recognition: {}", stt.label());
    let agent = backends::text_agent(&config, &creds)?;
    ui::status!("Agent: {}", agent.label());
    let conversation = Conversation::new(agent, &config, transmit);

    // Everything that can fail is checked before the serial port opens.
    let air = match options.consent {
        Some(consent) => {
            let voice: Arc<dyn crate::tts::Voice> = Arc::from(backends::voice(&config, &creds)?);
            voice.prepare().await.context("voice is not ready; nothing was transmitted")?;
            ui::status!("Voice: {}", voice.label());
            let radio = Radio::live(&config, consent)?;
            signals::protect(radio.ptt());
            ui::status!("Transmit enabled: replies are spoken on the radio.");
            if config.radio.station_id.enabled() {
                let id = &config.radio.station_id;
                ui::status!("Station ID {} ({:?}, {:?}).", id.callsign, id.mode, id.method);
            }
            Some(Air::new(radio, Some(voice), &config))
        }
        None => {
            ui::status!("Receive only: replies are printed and the transmitter is never keyed.");
            None
        }
    };
    if options.wav.is_none() {
        crate::audio::device::find(&config.audio.input, crate::audio::device::Direction::Input)?;
    }
    ui::meter!("Preparing {}...", stt.label());
    stt.prepare().await?;
    if config.shutdown.enabled {
        ui::status!("Remote shutdown enabled: the phrase and code, together or in two transmissions.");
    }

    let mut talk = Talk {
        gate: Gate::new(&config),
        shutdown: Shutdown::new(&config),
        config,
        air,
        stt,
        conversation,
        input: match options.wav {
            Some(path) => Input::Wav(Some(path)),
            None => Input::Capture,
        },
        once,
        wait,
        stop: signals::token(),
    };
    talk.run().await
}

impl Talk {
    async fn run(&mut self) -> anyhow::Result<()> {
        loop {
            ui::status!("{}", self.gate.status(Instant::now()));
            let Some(utterance) = self.next_utterance().await? else {
                return Ok(());
            };
            if self.handle(utterance).await? == Next::Exit || self.once {
                return Ok(());
            }
        }
    }

    /// The next utterance, or `None` when there is nothing more to hear.
    async fn next_utterance(&mut self) -> anyhow::Result<Option<Utterance>> {
        match &mut self.input {
            Input::Wav(path) => match path.take() {
                Some(path) => crate::commands::speech::utterance_from_wav(&self.config, &path).map(Some),
                None => Ok(None),
            },
            Input::Capture => self.capture().await,
        }
    }

    async fn capture(&mut self) -> anyhow::Result<Option<Utterance>> {
        let mut capture = Capture::open(&self.config.audio.input)?;
        let mut listener = Listener::new(&self.config.vad, capture.rate(), true);
        let deadline = self.wait.map(|w| tokio::time::Instant::now() + w);
        let mut tick = tokio::time::interval(Duration::from_secs(1));
        loop {
            tokio::select! {
                frame = capture.next_frame() => {
                    let frame = frame.context("capture failed; check the audio interface")?;
                    if capture.take_overflow() {
                        ui::warning!("Capture overflow; some audio was lost.");
                    }
                    if let Heard::Finished(utterance) = listener.push(&frame) {
                        return Ok(Some(utterance));
                    }
                }
                _ = self.stop.cancelled() => return Ok(None),
                _ = tick.tick(), if !listener.speaking() => self.housekeeping(),
                _ = sleep_until(deadline), if !listener.speaking() => bail!(
                    "no speech within the wait limit (peak RMS {:.3}, threshold {:.3})",
                    listener.peak(),
                    self.config.vad.threshold
                ),
            }
        }
    }

    /// Timers that run while waiting for speech.
    fn housekeeping(&mut self) {
        let now = Instant::now();
        if self.shutdown.expire(now) {
            ui::warning!("Shutdown confirmation window expired; shutdown cancelled.");
        }
        if self.gate.expire(now) {
            ui::warning!("Follow-up window ended.");
            ui::status!("{}", self.gate.status(now));
        }
    }

    async fn handle(&mut self, utterance: Utterance) -> anyhow::Result<Next> {
        ui::meter!("Transcribing...");
        let text = match self.stt.transcribe(&utterance.audio).await {
            Ok(text) => text,
            Err(err) if self.once => return Err(err.context("transcription failed")),
            Err(err) => {
                ui::error!("Transcription failed: {err:#}");
                self.gate.close();
                self.shutdown.cancel();
                ui::status!("Still listening; say the wake phrase and try again.");
                return Ok(Next::Listen);
            }
        };
        let now = Instant::now();
        match self.shutdown.decide(&text, utterance.started_at, now) {
            Control::None => {}
            control => return self.shutdown_control(control).await,
        }
        if text.trim().is_empty() {
            ui::ignored!("Ignored: no words recognized.");
            return Ok(Next::Listen);
        }
        ui::transcript!("Transcript: {text}");
        match self.gate.decide(&text, utterance.started_at) {
            Decision::Empty => ui::ignored!("Ignored: no words recognized."),
            Decision::NeedsWake => ui::ignored!("Ignored: say \"{}\" first.", self.gate.wake_name()),
            Decision::Sleep => {
                self.shutdown.cancel();
                ui::status!("Sleep heard; say \"{}\" to start again.", self.gate.wake_name());
                let confirmation = self.config.sleep_confirmation().to_string();
                self.say(&confirmation, "Sleep confirmation").await?;
            }
            Decision::WakeOnly(Destination::Agent) => {
                ui::status!("Wake heard; listening for your request.");
                let confirmation = self.config.wake.confirmation.clone();
                self.say(&confirmation, "Wake confirmation").await?;
                self.gate.complete_turn(Instant::now());
            }
            Decision::Traffic { to: Destination::Agent, text, addressed } => {
                ui::accepted!("Accepted ({}): {text}", if addressed { "wake name" } else { "follow-up" });
                self.answer(&text).await?;
            }
            Decision::WakeOnly(Destination::Contact(service)) | Decision::Traffic { to: Destination::Contact(service), .. } => {
                ui::warning!("Messaging with {service} is not available in this build.");
            }
        }
        Ok(Next::Listen)
    }

    async fn shutdown_control(&mut self, control: Control) -> anyhow::Result<Next> {
        self.gate.close();
        match control {
            Control::Confirmed => {
                ui::status!("Shutdown confirmed; stopping walkietalk.");
                let reply = self.config.shutdown.confirmed_reply.clone();
                self.say(&reply, "Shutdown confirmation").await?;
                Ok(Next::Exit)
            }
            Control::Armed => {
                ui::status!(
                    "Shutdown armed; send the code within {:.0}s.",
                    self.config.shutdown.confirm_window_seconds
                );
                let reply = self.config.shutdown.armed_reply.clone();
                self.say(&reply, "Shutdown phrase confirmation").await?;
                Ok(Next::Listen)
            }
            Control::Rejected(why) => {
                ui::status!("{why}");
                Ok(Next::Listen)
            }
            Control::None => Ok(Next::Listen),
        }
    }

    /// Ask the agent and deliver its reply.
    async fn answer(&mut self, text: &str) -> anyhow::Result<()> {
        // The window was judged when speech began; it stays closed until
        // this turn completes, so a failure never reopens it.
        self.gate.close();
        ui::meter!("Asking the agent...");
        let reply = match self.conversation.ask(text).await {
            Ok(reply) => reply,
            Err(err) if self.once => return Err(err.context("agent failed")),
            Err(err) => {
                ui::error!("Agent failed: {err:#}");
                ui::status!("Still listening; say the wake phrase and try again.");
                return Ok(());
            }
        };
        let Some(air) = self.air.as_mut() else {
            ui::reply!("Reply: {}", reply.text);
            self.conversation.commit(reply);
            self.gate.complete_turn(Instant::now());
            return Ok(());
        };
        ui::meter!("Synthesizing the reply; transmitter off...");
        let clip = match air.synthesize(&reply.text).await {
            Ok(clip) => clip,
            Err(err) if self.once => return Err(err.context("speech failed")),
            Err(err) => {
                ui::error!("Speech failed: {err:#}");
                ui::status!("The reply was not spoken and is not kept as context.");
                return Ok(());
            }
        };
        ui::reply!("Reply: {}", reply.text);
        match air.send(clip).await {
            Ok(()) => {
                self.conversation.commit(reply);
                self.gate.complete_turn(Instant::now());
                Ok(())
            }
            Err(AirError::NotSent(err)) if !self.once => {
                ui::error!("The reply was not transmitted: {err:#}");
                Ok(())
            }
            Err(AirError::NotSent(err)) => Err(err.context("the reply was not transmitted")),
            Err(AirError::Fatal { message, reply_sent }) => {
                if reply_sent {
                    self.conversation.commit(reply);
                }
                bail!(message)
            }
        }
    }

    /// Speak a fixed phrase when transmitting; receive-only stays silent.
    async fn say(&mut self, text: &str, what: &str) -> anyhow::Result<bool> {
        if text.is_empty() {
            return Ok(false);
        }
        let Some(air) = self.air.as_mut() else {
            ui::meter!("{what} not spoken (receive only).");
            return Ok(false);
        };
        match air.say(text, what).await {
            Ok(spoken) => Ok(spoken),
            Err(err) => Err(anyhow::anyhow!("{err}")),
        }
    }
}

async fn sleep_until(deadline: Option<tokio::time::Instant>) {
    match deadline {
        Some(d) => tokio::time::sleep_until(d).await,
        None => std::future::pending().await,
    }
}
