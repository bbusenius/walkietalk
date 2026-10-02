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
use crate::realtime;
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

/// What turns speech into replies.
enum Brain {
    /// Separate recognition, text agent, and voice.
    Text { stt: Arc<dyn Transcriber>, conversation: Conversation },
    /// One speech-to-speech session.
    Realtime(Box<realtime::Session>),
}

struct Talk {
    config: Config,
    air: Option<Air>,
    brain: Brain,
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

    let realtime = config.agent.backend.is_realtime();
    let brain = if realtime {
        let session = realtime::Session::new(realtime::Settings::from_config(&config, &creds)?);
        ui::status!("Agent: {} (its own transcripts drive the wake gate)", session.label());
        Brain::Realtime(Box::new(session))
    } else {
        let stt: Arc<dyn Transcriber> = Arc::from(backends::transcriber(&config, &creds)?);
        ui::status!("Speech recognition: {}", stt.label());
        let agent = backends::text_agent(&config, &creds)?;
        ui::status!("Agent: {}", agent.label());
        Brain::Text { stt, conversation: Conversation::new(agent, &config, transmit) }
    };

    // Everything that can fail is checked before the serial port opens.
    let air = match options.consent {
        Some(consent) => {
            // Realtime speaks with its own voice; the configured voice is
            // needed only for messaging.
            let voice: Option<Arc<dyn crate::tts::Voice>> = if !realtime || config.messaging_enabled() {
                let voice: Arc<dyn crate::tts::Voice> = Arc::from(backends::voice(&config, &creds)?);
                voice.prepare().await.context("voice is not ready; nothing was transmitted")?;
                ui::status!("Voice: {}", voice.label());
                Some(voice)
            } else {
                None
            };
            let radio = Radio::live(&config, consent)?;
            signals::protect(radio.ptt());
            ui::status!("Transmit enabled: replies are spoken on the radio.");
            if config.radio.station_id.enabled() {
                let id = &config.radio.station_id;
                ui::status!("Station ID {} ({:?}, {:?}).", id.callsign, id.mode, id.method);
            }
            Some(Air::new(radio, voice, &config))
        }
        None => {
            ui::status!("Receive only: replies are printed and the transmitter is never keyed.");
            None
        }
    };
    if options.wav.is_none() {
        crate::audio::device::find(&config.audio.input, crate::audio::device::Direction::Input)?;
    }
    if let Brain::Text { stt, .. } = &brain {
        ui::meter!("Preparing {}...", stt.label());
        stt.prepare().await?;
    }
    if config.shutdown.enabled {
        ui::status!("Remote shutdown enabled: the phrase and code, together or in two transmissions.");
    }

    let mut talk = Talk {
        gate: Gate::new(&config),
        shutdown: Shutdown::new(&config),
        config,
        air,
        brain,
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
        let result = self.run_loop().await;
        if let Brain::Realtime(session) = &mut self.brain {
            session.reset().await;
        }
        result
    }

    async fn run_loop(&mut self) -> anyhow::Result<()> {
        loop {
            if self.stop.is_cancelled() {
                return Ok(());
            }
            if !self.ensure_realtime().await? {
                continue;
            }
            ui::status!("{}", self.gate.status(Instant::now()));
            let utterance = match self.next_utterance().await {
                Ok(Some(utterance)) => utterance,
                Ok(None) => return Ok(()),
                Err(err) if err.downcast_ref::<Retry>().is_some() => continue,
                Err(err) => return Err(err),
            };
            if self.handle(utterance).await? == Next::Exit || self.once {
                return Ok(());
            }
        }
    }

    /// Connect the realtime session before listening, retrying each second.
    /// Returns false when the caller should loop again.
    async fn ensure_realtime(&mut self) -> anyhow::Result<bool> {
        let Brain::Realtime(session) = &mut self.brain else { return Ok(true) };
        if session.connected() {
            return Ok(true);
        }
        ui::meter!("Connecting the voice session...");
        match session.connect().await {
            Ok(()) => Ok(true),
            Err(err) if realtime::is_auth_error(&err) || self.once => Err(err),
            Err(err) => {
                ui::error!("Voice connection failed: {err:#}");
                ui::status!("Retrying in 1 s.");
                self.gate.close();
                self.shutdown.cancel();
                tokio::select! {
                    _ = tokio::time::sleep(Duration::from_secs(1)) => {}
                    _ = self.stop.cancelled() => {}
                }
                Ok(false)
            }
        }
    }

    /// The next utterance, or `None` when there is nothing more to hear.
    async fn next_utterance(&mut self) -> anyhow::Result<Option<Utterance>> {
        let utterance = match &mut self.input {
            Input::Wav(path) => match path.take() {
                Some(path) => Some(crate::commands::speech::utterance_from_wav(&self.config, &path)?),
                None => None,
            },
            Input::Capture => self.capture().await?,
        };
        if let (Some(u), Brain::Realtime(session), Input::Wav(_)) = (&utterance, &mut self.brain, &self.input) {
            // A file arrives all at once; stream it like live audio.
            session.begin(u.audio.rate());
            session.append(u.audio.samples()).await?;
        }
        Ok(utterance)
    }

    async fn capture(&mut self) -> anyhow::Result<Option<Utterance>> {
        let mut capture = Capture::open(&self.config.audio.input)?;
        let mut listener = Listener::new(&self.config.vad, capture.rate(), true);
        if let Brain::Realtime(session) = &mut self.brain {
            session.begin(capture.rate());
        }
        let deadline = self.wait.map(|w| tokio::time::Instant::now() + w);
        let mut tick = tokio::time::interval(Duration::from_secs(1));
        loop {
            tokio::select! {
                frame = capture.next_frame() => {
                    let frame = frame.context("capture failed; check the audio interface")?;
                    if capture.take_overflow() {
                        ui::warning!("Capture overflow; some audio was lost.");
                    }
                    let heard = listener.push(&frame);
                    if let Err(err) = self.stream(&heard).await {
                        if self.once {
                            return Err(err);
                        }
                        ui::error!("Voice session lost while listening: {err:#}");
                        ui::status!("Reconnecting; say the wake phrase again.");
                        self.gate.close();
                        self.shutdown.cancel();
                        if let Brain::Realtime(session) = &mut self.brain {
                            session.reset().await;
                        }
                        return self.retry_capture();
                    }
                    if let Heard::Finished(utterance) = heard {
                        return Ok(Some(utterance));
                    }
                }
                _ = self.stop.cancelled() => return Ok(None),
                _ = tick.tick(), if !listener.speaking() => {
                    self.housekeeping();
                    if matches!(&self.brain, Brain::Realtime(s) if !s.connected()) {
                        // Reconnect before someone starts talking into a dead session.
                        return self.retry_capture();
                    }
                }
                _ = sleep_until(deadline), if !listener.speaking() => bail!(
                    "no speech within the wait limit (peak RMS {:.3}, threshold {:.3})",
                    listener.peak(),
                    self.config.vad.threshold
                ),
            }
        }
    }

    /// Leave capture so the loop reconnects and listens again.
    fn retry_capture(&mut self) -> anyhow::Result<Option<Utterance>> {
        Err(Retry.into())
    }

    /// Forward captured audio to the realtime session as it arrives.
    async fn stream(&mut self, heard: &Heard) -> anyhow::Result<()> {
        let Brain::Realtime(session) = &mut self.brain else { return Ok(()) };
        match heard {
            Heard::Speech { new } => session.append(new).await,
            Heard::Discarded => session.discard().await,
            Heard::Waiting | Heard::Finished(_) => Ok(()),
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

    /// Text for an utterance, or `None` to skip it.
    async fn transcribe(&mut self, utterance: &Utterance) -> anyhow::Result<Option<String>> {
        ui::meter!("Transcribing...");
        let result = match &mut self.brain {
            Brain::Text { stt, .. } => stt.transcribe(&utterance.audio).await,
            Brain::Realtime(session) => session.transcript().await,
        };
        match result {
            Ok(text) => Ok(Some(text)),
            Err(err) if realtime::is_auth_error(&err) || self.once => Err(err.context("transcription failed")),
            Err(err) if err.downcast_ref::<realtime::NoTranscript>().is_some() => {
                ui::warning!("No transcript arrived for that audio; window unchanged.");
                self.shutdown.cancel();
                self.discard_realtime(true).await;
                Ok(None)
            }
            Err(err) => {
                ui::error!("Transcription failed: {err:#}");
                self.gate.close();
                self.shutdown.cancel();
                self.discard_realtime(true).await;
                ui::status!("Still listening; say the wake phrase and try again.");
                Ok(None)
            }
        }
    }

    /// Remove a rejected or control utterance from the realtime conversation.
    async fn discard_realtime(&mut self, drop_connection: bool) {
        let Brain::Realtime(session) = &mut self.brain else { return };
        if let Err(err) = session.discard().await {
            ui::error!("Could not remove that audio from the voice session ({err:#}); starting a fresh session.");
            session.reset().await;
        } else if drop_connection {
            session.reset().await;
        }
    }

    async fn handle(&mut self, utterance: Utterance) -> anyhow::Result<Next> {
        let Some(text) = self.transcribe(&utterance).await? else {
            return Ok(Next::Listen);
        };
        let now = Instant::now();
        match self.shutdown.decide(&text, utterance.started_at, now) {
            Control::None => {}
            control => {
                self.discard_realtime(false).await;
                return self.shutdown_control(control).await;
            }
        }
        if text.trim().is_empty() {
            ui::ignored!("Ignored: no words recognized.");
            // An empty result can leave the realtime connection unusable.
            self.discard_realtime(true).await;
            return Ok(Next::Listen);
        }
        ui::transcript!("Transcript: {text}");
        let decision = self.gate.decide(&text, utterance.started_at);
        if !matches!(decision, Decision::Traffic { to: Destination::Agent, .. }) {
            self.discard_realtime(false).await;
        }
        match decision {
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
                self.gate.close();
                match self.brain {
                    Brain::Text { .. } => self.answer(&text).await?,
                    Brain::Realtime(_) => self.answer_realtime().await?,
                }
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

    /// Ask the text agent and deliver its reply.
    async fn answer(&mut self, text: &str) -> anyhow::Result<()> {
        let Brain::Text { conversation, .. } = &mut self.brain else { unreachable!("text agent") };
        ui::meter!("Asking the agent...");
        let reply = match conversation.ask(text).await {
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
            conversation.commit(reply);
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
                conversation.commit(reply);
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
                    conversation.commit(reply);
                }
                bail!(message)
            }
        }
    }

    /// Ask the realtime voice to answer the committed audio.
    async fn answer_realtime(&mut self) -> anyhow::Result<()> {
        let Brain::Realtime(session) = &mut self.brain else { unreachable!("realtime") };
        let radio = self.air.as_ref().map(|air| air.radio().clone());
        ui::meter!("Voice turn; the transmitter keys only when speech arrives...");
        let reply = match session.respond(radio).await {
            Ok(reply) => reply,
            Err(err) if realtime::is_ptt_fault(&err) || realtime::is_auth_error(&err) || self.once => return Err(err),
            Err(err) => {
                ui::error!("Voice agent failed: {err:#}");
                ui::status!("Still listening; say the wake phrase and try again.");
                return Ok(());
            }
        };
        if !reply.heard.is_empty() {
            ui::transcript!("Heard: {}", reply.heard);
        }
        if !reply.audible {
            if self.once {
                bail!("the voice agent returned no audible reply");
            }
            ui::warning!("The voice agent returned no audible reply; the follow-up window stays closed.");
            return Ok(());
        }
        ui::reply!("Reply: {}", if reply.said.is_empty() { "(audio only)" } else { &reply.said });
        if reply.truncated {
            ui::warning!("The transmit cap cut the reply short; the next turn starts a fresh conversation.");
        }
        if self.air.is_some() {
            self.realtime_station_id().await?;
            if let Some(air) = &self.air {
                air.mute().await;
            }
        }
        if !reply.truncated {
            self.gate.complete_turn(Instant::now());
        }
        Ok(())
    }

    /// A due station ID after a realtime reply, in its own transmission.
    async fn realtime_station_id(&mut self) -> anyhow::Result<()> {
        let (Some(air), Brain::Realtime(session)) = (self.air.as_mut(), &self.brain) else { return Ok(()) };
        if !air.station_id().due(Instant::now()) {
            return Ok(());
        }
        ui::status!("Station ID follows in its own transmission.");
        tokio::time::sleep(crate::radio::station_id::GAP).await;
        let sent = match air.station_id().config().method {
            crate::config::StationIdMethod::Morse => {
                let clip = crate::audio::morse::morse(air.station_id().callsign())?;
                air.transmit(clip).await.map_err(anyhow::Error::from)
            }
            crate::config::StationIdMethod::Voice => {
                let callsign = air.station_id().callsign().to_string();
                match realtime::speak(session.settings(), &callsign, Some(air.radio().clone())).await {
                    Ok(reply) if reply.audible && !reply.truncated => Ok(()),
                    Ok(_) => Err(anyhow::anyhow!("the station ID was not transmitted in full")),
                    Err(err) => Err(err),
                }
            }
        };
        match sent {
            Ok(()) => {
                air.station_id_mut().mark_sent(Instant::now());
                Ok(())
            }
            Err(err) => bail!("station ID failed: {err:#}; stopping"),
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
        if let Brain::Realtime(session) = &self.brain {
            let result = realtime::speak(session.settings(), text, Some(air.radio().clone())).await;
            return match result {
                Ok(reply) if reply.audible && !reply.truncated => {
                    air.mute().await;
                    Ok(true)
                }
                Ok(_) => {
                    ui::error!("{what} was not transmitted in full.");
                    Ok(false)
                }
                Err(err) if realtime::is_ptt_fault(&err) => Err(err),
                Err(err) => {
                    ui::error!("{what} failed: {err:#}");
                    Ok(false)
                }
            };
        }
        air.say(text, what).await.map_err(|err| anyhow::anyhow!("{err}"))
    }
}

/// Capture ended early so the loop can reconnect.
#[derive(Debug, thiserror::Error)]
#[error("listening restarted")]
struct Retry;

async fn sleep_until(deadline: Option<tokio::time::Instant>) {
    match deadline {
        Some(d) => tokio::time::sleep_until(d).await,
        None => std::future::pending().await,
    }
}
