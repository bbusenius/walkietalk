//! `talk`: the main loop.
//!
//! Listen for an utterance, transcribe it, apply the shutdown and wake
//! gates, route the traffic, and speak replies on the air only when the
//! user consented to transmit. Capture is closed while processing and
//! transmitting. Recoverable failures are reported and listening resumes.

mod agent;
mod air;
mod contacts;
mod operator;
#[cfg(test)]
mod tests;

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{Context, bail};
use tokio_util::sync::CancellationToken;

use crate::agent::Conversation;
use crate::audio::capture::Capture;
use crate::audio::listener::{Heard, Listener, Utterance};
use crate::config::{Config, Service};
use crate::credentials::Credentials;
use crate::gate::{Control, Decision, Destination, Gate, Shutdown};
use crate::messaging::{Bridge, Progress, Queues};
use crate::operator::Command;
use crate::radio::{Radio, TransmitConsent};
use crate::stt::Transcriber;
use crate::{backends, realtime, signals, ui};
use air::Air;

pub struct Options {
    pub wav: Option<PathBuf>,
    pub consent: Option<TransmitConsent>,
    pub once: bool,
    /// Wait-for-speech limit for a single capture.
    pub timeout: Option<f64>,
    /// The config file, which names the operator control socket.
    pub config_path: PathBuf,
    pub panel: bool,
}

enum Input {
    /// A WAV file, consumed by the first listen.
    Wav(Option<PathBuf>),
    Capture,
    /// Frames supplied by a test; the loop ends when the sender is dropped.
    #[cfg(test)]
    Frames(Option<tokio::sync::mpsc::Receiver<Vec<i16>>>),
}

/// Where captured frames come from.
enum Source {
    Device(Capture),
    #[cfg(test)]
    Frames(tokio::sync::mpsc::Receiver<Vec<i16>>),
}

impl Source {
    fn rate(&self) -> u32 {
        match self {
            Source::Device(capture) => capture.rate(),
            #[cfg(test)]
            Source::Frames(_) => 16_000,
        }
    }

    /// The next frame, or `None` at the end of test input.
    async fn next_frame(&mut self) -> anyhow::Result<Option<Vec<i16>>> {
        match self {
            Source::Device(capture) => capture.next_frame().await.map(Some),
            #[cfg(test)]
            Source::Frames(rx) => Ok(rx.recv().await),
        }
    }

    fn take_overflow(&self) -> bool {
        match self {
            Source::Device(capture) => capture.take_overflow(),
            #[cfg(test)]
            Source::Frames(_) => false,
        }
    }
}

/// What turns speech into replies.
enum Brain {
    /// Separate recognition, text agent, and voice.
    Text {
        stt: Arc<dyn Transcriber>,
        conversation: Conversation,
    },
    /// One speech-to-speech session.
    Realtime(Box<realtime::Session>),
}

/// WhatsApp and Signal state.
struct Messaging {
    bridge: Bridge,
    inbox: tokio::sync::mpsc::Receiver<crate::messaging::Inbound>,
    queues: Queues,
    progress: HashMap<(Service, String), Progress>,
    /// No message is delivered before this moment.
    next_at: Instant,
    /// Recognizer for incoming voice notes.
    notes: Option<Arc<dyn Transcriber>>,
    operator: Option<operator::Operator>,
}

struct Talk {
    config: Config,
    creds: Credentials,
    air: Option<Air>,
    brain: Brain,
    gate: Gate,
    shutdown: Shutdown,
    messaging: Option<Messaging>,
    input: Input,
    once: bool,
    wait: Option<Duration>,
    stop: CancellationToken,
}

/// What interrupted listening.
enum Event {
    Heard(Utterance),
    /// A queued message can be delivered now.
    Deliver(Service),
    Operator(Command),
}

/// What to do after handling an event.
#[derive(PartialEq, Eq)]
enum Next {
    Listen,
    Exit,
}

/// Capture ended early so the loop can reconnect.
#[derive(Debug, thiserror::Error)]
#[error("listening restarted")]
struct Retry;

pub async fn run(config: Config, creds: Credentials, options: Options) -> anyhow::Result<()> {
    let once = options.once || options.wav.is_some();
    if options.timeout.is_some() && !(options.once && options.wav.is_none()) {
        bail!("--timeout applies only to --capture --once; continuous listening has no idle limit");
    }
    if config.messaging.operator_mode && once {
        bail!("operator mode needs continuous `talk --capture` (no WAV input or --once)");
    }
    if options.panel && !config.messaging.operator_mode {
        bail!("--panel needs messaging.operator_mode = true");
    }
    if options.panel {
        crate::panel::check_terminal()?;
    }
    let wait = match (once, options.wav.is_some()) {
        (true, false) => {
            let seconds = options.timeout.unwrap_or(60.0);
            anyhow::ensure!(
                seconds > 0.0 && seconds <= 600.0,
                "--timeout must be greater than 0 and at most 600"
            );
            Some(Duration::from_secs_f64(seconds))
        }
        _ => None,
    };
    let transmit = options.consent.is_some();

    let realtime = config.agent.backend.is_realtime();
    let brain = if realtime {
        let session = realtime::Session::new(realtime::Settings::from_config(&config, &creds)?);
        ui::status!(
            "Agent: {} (its own transcripts drive the wake gate)",
            session.label()
        );
        Brain::Realtime(Box::new(session))
    } else {
        let stt: Arc<dyn Transcriber> = Arc::from(backends::transcriber(&config, &creds)?);
        ui::status!("Speech recognition: {}", stt.label());
        let agent = backends::text_agent(&config, &creds)?;
        ui::status!("Agent: {}", agent.label());
        Brain::Text {
            stt,
            conversation: Conversation::new(agent, &config, transmit),
        }
    };

    // Reserve the config before starting anything else in operator mode.
    let reserved = if config.messaging.operator_mode {
        Some(crate::operator::server::reserve(&options.config_path)?)
    } else {
        None
    };

    // Everything that can fail is checked before the serial port opens.
    let air = match options.consent {
        Some(consent) => {
            // Realtime speaks with its own voice; the configured voice is
            // needed only for messaging.
            let voice: Option<Arc<dyn crate::tts::Voice>> =
                if !realtime || config.messaging_enabled() {
                    let voice: Arc<dyn crate::tts::Voice> =
                        Arc::from(backends::voice(&config, &creds)?);
                    voice
                        .prepare()
                        .await
                        .context("the voice is not ready; nothing was transmitted")?;
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
                ui::status!(
                    "Station ID {} ({:?}, {:?}).",
                    id.callsign,
                    id.mode,
                    id.method
                );
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
        ui::status!(
            "Remote shutdown enabled: the phrase and code, together or in two transmissions."
        );
    }

    let messaging = if config.messaging_enabled() {
        let notes: Option<Arc<dyn Transcriber>> = match &brain {
            Brain::Text { stt, .. } => Some(stt.clone()),
            Brain::Realtime(_) if config.messaging.enabled().any(|(_, c)| c.transcribe_voice) => {
                let stt: Arc<dyn Transcriber> = Arc::from(backends::transcriber(&config, &creds)?);
                stt.prepare().await?;
                ui::status!("Voice-note transcription: {}", stt.label());
                Some(stt)
            }
            Brain::Realtime(_) => None,
        };
        let (bridge, inbox) = Bridge::start(&config).await?;
        for (service, contact) in config.messaging.enabled() {
            ui::status!(
                "Messaging: {service} with \"{}\" (wake \"{}\").",
                contact.label(),
                contact.wake
            );
        }
        let operator = match reserved {
            Some(reserved) => match operator::Operator::start(&config, reserved, options.panel) {
                Ok(operator) => Some(operator),
                Err(err) => {
                    bridge.stop().await;
                    return Err(err);
                }
            },
            None => None,
        };
        Some(Messaging {
            bridge,
            inbox,
            queues: Queues::default(),
            progress: HashMap::new(),
            next_at: Instant::now(),
            notes,
            operator,
        })
    } else {
        None
    };

    let mut talk = Talk {
        gate: Gate::new(&config),
        shutdown: Shutdown::new(&config),
        config,
        creds,
        air,
        brain,
        messaging,
        input: match options.wav {
            Some(path) => Input::Wav(Some(path)),
            None => Input::Capture,
        },
        once,
        wait,
        stop: signals::token(),
    };
    let result = talk.run().await;
    talk.close().await;
    result
}

impl Talk {
    async fn close(&mut self) {
        if let Brain::Realtime(session) = &mut self.brain {
            session.reset().await;
        }
        if let Some(messaging) = self.messaging.take() {
            if let Some(op) = messaging.operator {
                op.close();
            }
            messaging.bridge.stop().await;
        }
    }

    async fn run(&mut self) -> anyhow::Result<()> {
        loop {
            if self.stop.is_cancelled() {
                return Ok(());
            }
            self.refresh_operator()?;
            if let Some(command) = self.pending_command() {
                self.operator_command(command).await?;
                continue;
            }
            if !self.ensure_realtime().await? {
                continue;
            }
            ui::status!("{}", self.gate.status(Instant::now()));
            let event = match self.next_event().await {
                Ok(Some(event)) => event,
                Ok(None) => return Ok(()),
                Err(err) if err.downcast_ref::<Retry>().is_some() => continue,
                Err(err) => return Err(err),
            };
            // A stop request abandons slow work (an agent request, say);
            // the transmitter was already released by the signal handler.
            let stop = self.stop.clone();
            let work = async {
                match event {
                    Event::Heard(utterance) => self.handle(utterance).await,
                    Event::Deliver(service) => self.deliver(service).await.map(|_| Next::Listen),
                    Event::Operator(command) => {
                        self.operator_command(command).await.map(|_| Next::Listen)
                    }
                }
            };
            let next = tokio::select! {
                next = work => next?,
                _ = stop.cancelled() => return Ok(()),
            };
            if next == Next::Exit || self.once {
                return Ok(());
            }
        }
    }

    /// Connect the realtime session before listening, retrying each second.
    /// Returns false when the caller should loop again.
    async fn ensure_realtime(&mut self) -> anyhow::Result<bool> {
        if !matches!(&self.brain, Brain::Realtime(s) if !s.connected()) {
            return Ok(true);
        }
        // Contact messages do not depend on the voice service.
        if let Some(service) = self.ready_service() {
            self.deliver(service).await?;
            return Ok(false);
        }
        ui::meter!("Connecting the voice session...");
        let Brain::Realtime(session) = &mut self.brain else {
            return Ok(true);
        };
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

    async fn next_event(&mut self) -> anyhow::Result<Option<Event>> {
        match &mut self.input {
            Input::Wav(path) => {
                let Some(path) = path.take() else {
                    return Ok(None);
                };
                let utterance = crate::commands::speech::utterance_from_wav(&self.config, &path)?;
                if let Brain::Realtime(session) = &mut self.brain {
                    // A file arrives all at once; stream it like live audio.
                    session.begin(utterance.audio.rate());
                    session.append(utterance.audio.samples()).await?;
                }
                Ok(Some(Event::Heard(utterance)))
            }
            Input::Capture => {
                let source = Source::Device(Capture::open(&self.config.audio.input)?);
                self.capture(source).await.1
            }
            #[cfg(test)]
            Input::Frames(rx) => {
                let Some(rx) = rx.take() else { return Ok(None) };
                let (source, result) = self.capture(Source::Frames(rx)).await;
                if let (Source::Frames(rx), Input::Frames(slot)) = (source, &mut self.input) {
                    *slot = Some(rx);
                }
                result
            }
        }
    }

    /// Listen until something needs attention; gives the source back.
    async fn capture(&mut self, mut capture: Source) -> (Source, anyhow::Result<Option<Event>>) {
        let result = self.listen(&mut capture).await;
        (capture, result)
    }

    async fn listen(&mut self, capture: &mut Source) -> anyhow::Result<Option<Event>> {
        let mut listener = Listener::new(&self.config.vad, capture.rate(), true);
        if let Brain::Realtime(session) = &mut self.brain {
            session.begin(capture.rate());
        }
        let deadline = self.wait.map(|w| tokio::time::Instant::now() + w);
        let mut tick = tokio::time::interval(Duration::from_millis(250));
        loop {
            let idle = !listener.speaking();
            let (inbox, commands) = match self.messaging.as_mut() {
                Some(m) => (
                    Some(&mut m.inbox),
                    m.operator.as_mut().map(|o| &mut o.commands),
                ),
                None => (None, None),
            };
            let result = tokio::select! {
                frame = capture.next_frame() => Step::Frame(frame),
                _ = self.stop.cancelled() => Step::Stop,
                message = recv(inbox) => Step::Message(message),
                command = recv(commands), if idle => Step::Command(command),
                _ = tick.tick(), if idle => Step::Tick,
                _ = sleep_until(deadline), if idle => Step::Timeout,
            };
            match result {
                Step::Frame(frame) => {
                    let frame = match frame {
                        Ok(Some(frame)) => frame,
                        Ok(None) => return Ok(None),
                        Err(err) if self.once => {
                            return Err(err.context("capture failed; check the audio interface"));
                        }
                        Err(err) => {
                            // Reopen the device; a missing device fails on reopen.
                            ui::error!("Capture stopped ({err:#}); reopening the device.");
                            self.gate.close();
                            tokio::time::sleep(Duration::from_secs(1)).await;
                            return Err(Retry.into());
                        }
                    };
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
                        return Err(Retry.into());
                    }
                    if let Heard::Finished(utterance) = heard {
                        return Ok(Some(Event::Heard(utterance)));
                    }
                }
                Step::Stop => return Ok(None),
                Step::Message(Some(message)) => self.accept(message),
                Step::Message(None) => bail!("the messaging services stopped"),
                Step::Command(Some(command)) => {
                    self.discard_realtime(false).await;
                    return Ok(Some(Event::Operator(command)));
                }
                Step::Command(None) => bail!("the operator controls stopped unexpectedly"),
                Step::Tick => {
                    self.housekeeping()?;
                    if let Some(service) = self.ready_service() {
                        self.discard_realtime(false).await;
                        return Ok(Some(Event::Deliver(service)));
                    }
                    if matches!(&self.brain, Brain::Realtime(s) if !s.connected()) {
                        // Reconnect before someone talks into a dead session.
                        return Err(Retry.into());
                    }
                }
                Step::Timeout => bail!(
                    "no speech within the wait limit (peak RMS {:.3}, threshold {:.3})",
                    listener.peak(),
                    self.config.vad.threshold
                ),
            }
        }
    }

    /// Forward captured audio to the realtime session as it arrives.
    async fn stream(&mut self, heard: &Heard) -> anyhow::Result<()> {
        let Brain::Realtime(session) = &mut self.brain else {
            return Ok(());
        };
        match heard {
            Heard::Speech { new } => session.append(new).await,
            Heard::Discarded => session.discard().await,
            Heard::Waiting | Heard::Finished(_) => Ok(()),
        }
    }

    /// Timers that run while waiting for speech.
    fn housekeeping(&mut self) -> anyhow::Result<()> {
        let now = Instant::now();
        if self.shutdown.expire(now) {
            ui::warning!("Shutdown confirmation window expired; shutdown cancelled.");
        }
        if self.gate.expire(now) {
            ui::warning!("Follow-up window ended.");
            ui::status!("{}", self.gate.status(now));
        }
        self.refresh_operator()
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
            Err(err) if realtime::is_auth_error(&err) || self.once => {
                Err(err.context("transcription failed"))
            }
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
        let Brain::Realtime(session) = &mut self.brain else {
            return;
        };
        if !session.connected() {
            return;
        }
        if let Err(err) = session.discard().await {
            ui::error!(
                "Could not remove that audio from the voice session ({err:#}); starting a fresh session."
            );
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
                self.cancel_dispatch();
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
        if !matches!(
            decision,
            Decision::Traffic {
                to: Destination::Agent,
                ..
            }
        ) {
            self.discard_realtime(false).await;
        }
        if let Decision::WakeOnly(to) | Decision::Traffic { to, .. } = &decision {
            self.switched_to(*to);
        }
        match decision {
            Decision::Empty => ui::ignored!("Ignored: no words recognized."),
            Decision::NeedsWake => {
                ui::ignored!("Ignored: say \"{}\" first.", self.gate.wake_name())
            }
            Decision::Sleep => self.enter_sleep("Sleep heard", None).await?,
            Decision::WakeOnly(Destination::Agent) => {
                ui::status!("Wake heard; listening for your request.");
                let confirmation = self.config.wake.confirmation.clone();
                self.say(&confirmation, "Wake confirmation").await?;
                self.gate.complete_turn(Instant::now());
            }
            Decision::Traffic {
                to: Destination::Agent,
                text,
                addressed,
            } => {
                ui::accepted!(
                    "Accepted ({}): {text}",
                    if addressed { "wake name" } else { "follow-up" }
                );
                self.gate.close();
                match self.brain {
                    Brain::Text { .. } => self.answer(&text).await?,
                    Brain::Realtime(_) => self.answer_realtime().await?,
                }
            }
            Decision::WakeOnly(Destination::Contact(service)) => self.contact_wake(service).await?,
            Decision::Traffic {
                to: Destination::Contact(service),
                text: traffic,
                ..
            } => {
                self.contact_traffic(service, &traffic, &text, &utterance)
                    .await?;
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

    /// Close the conversation. `local` carries an operator command to answer.
    async fn enter_sleep(&mut self, why: &str, local: Option<&Command>) -> anyhow::Result<()> {
        self.gate.sleep();
        if self.shutdown.armed(Instant::now()) {
            self.shutdown.cancel();
            log(local, "status", "Pending shutdown cancelled.");
        }
        if self.cancel_dispatch() {
            log(
                local,
                "status",
                "Operator delivery paused; the rest of the message stays approved.",
            );
        }
        self.discard_realtime(false).await;
        log(
            local,
            "status",
            &format!("{why}; say \"{}\" to start again.", self.gate.wake_name()),
        );
        let confirmation = self.config.sleep_confirmation().to_string();
        self.say(&confirmation, "Sleep confirmation").await?;
        self.refresh_operator()?;
        Ok(())
    }
}

enum Step {
    Frame(anyhow::Result<Option<Vec<i16>>>),
    Stop,
    Message(Option<crate::messaging::Inbound>),
    Command(Option<Command>),
    Tick,
    Timeout,
}

/// Log a line, also sending it to an operator client when one asked.
fn log(command: Option<&Command>, kind: &str, message: &str) {
    if let Some(command) = command {
        command.say(kind, message);
    }
    let kind = match kind {
        "reply" => ui::Kind::Reply,
        "warn" => ui::Kind::Warn,
        "error" => ui::Kind::Error,
        _ => ui::Kind::Status,
    };
    ui::emit(kind, message);
}

async fn sleep_until(deadline: Option<tokio::time::Instant>) {
    match deadline {
        Some(d) => tokio::time::sleep_until(d).await,
        None => std::future::pending().await,
    }
}

/// The next item on an optional channel; never resolves without one.
async fn recv<T>(channel: Option<&mut tokio::sync::mpsc::Receiver<T>>) -> Option<T> {
    match channel {
        Some(rx) => rx.recv().await,
        None => std::future::pending().await,
    }
}
