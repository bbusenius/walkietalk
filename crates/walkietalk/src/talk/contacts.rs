//! WhatsApp and Signal traffic in the talk loop: queueing incoming
//! messages, sending outgoing ones, and reading replies on the radio.

use std::time::{Duration, Instant};

use anyhow::{Context, bail};

use super::air::AirError;
use super::{Talk, log};
use crate::audio::listener::Utterance;
use crate::audio::{Clip, Fit};
use crate::config::{ListeningMode, Service};
use crate::gate::Destination;
use crate::messaging::{Content, Inbound, Progress, text, voice};
use crate::operator::lock;
use crate::ui;

/// Attempts before an unplayable message is skipped (automatic mode only).
const ATTEMPTS: u32 = 3;
/// Pause after a failed delivery.
const RETRY_AFTER: Duration = Duration::from_secs(5);
/// Listening opportunity between delivered pieces.
const BETWEEN_PIECES: Duration = Duration::from_secs(1);

impl Talk {
    /// A message arrived from a contact.
    pub(super) fn accept(&mut self, message: Inbound) {
        let Some(m) = self.messaging.as_mut() else { return };
        let label = self.config.messaging.contact(message.service).map(|c| c.label().to_string()).unwrap_or_default();
        let (voice, words) = match &message.content {
            Content::Text(t) => (false, t.clone()),
            Content::Voice(_) => (true, String::new()),
        };
        let (service, id) = (message.service, message.id.clone());
        if !m.queues.add(message) {
            return;
        }
        match &m.operator {
            Some(op) => {
                let words = if voice { String::new() } else { text::normalize(&words) };
                lock(&op.shared).review.add_incoming(service, &id, voice, &words);
            }
            None => ui::event!("{} message from {label} queued ({} waiting).", service, m.queues.len(service)),
        }
    }

    /// The contact whose next message may be delivered now, if any.
    pub(super) fn ready_service(&self) -> Option<Service> {
        let m = self.messaging.as_ref()?;
        let now = Instant::now();
        if now < m.next_at || self.shutdown.armed(now) {
            return None;
        }
        let dispatched = m.operator.as_ref().and_then(|op| lock(&op.shared).review.dispatched().map(|i| i.service));
        let service = dispatched.or(match self.gate.selected() {
            Some(Destination::Contact(service)) => Some(service),
            _ => None,
        })?;
        self.can_deliver(service).then_some(service)
    }

    /// Whether the head of `service`'s queue may be delivered now.
    fn can_deliver(&self, service: Service) -> bool {
        let Some(m) = self.messaging.as_ref() else { return false };
        let Some(head) = m.queues.head(service) else { return false };
        let Some(op) = &m.operator else {
            // Automatic mode: the selected contact can reply whenever idle.
            return self.gate.selected() == Some(Destination::Contact(service));
        };
        let board = lock(&op.shared);
        let review = &board.review;
        if !review.is_approved(service, &head.id) {
            return false;
        }
        if review.dispatched().is_some_and(|i| i.service == service && i.id == head.id) {
            return true;
        }
        let mode = self.config.messaging.contact(service).map(|c| c.listening.mode);
        self.gate.selected() == Some(Destination::Contact(service))
            && (mode == Some(ListeningMode::WakePhrase) || self.gate.follow_up_open(Instant::now()))
    }

    /// The conversation moved; an operator delivery for another contact pauses.
    pub(super) fn switched_to(&mut self, to: Destination) {
        let Some(op) = self.messaging.as_ref().and_then(|m| m.operator.as_ref()) else { return };
        let mut board = lock(&op.shared);
        let other = board.review.dispatched().is_some_and(|i| to != Destination::Contact(i.service));
        if other && board.review.cancel_dispatch() {
            ui::status!("Operator delivery paused; the rest of the message stays approved.");
            board.delivery = "Delivery paused; the rest stays approved.".into();
        }
    }

    /// Cancel a pending operator delivery. Returns whether one was pending.
    pub(super) fn cancel_dispatch(&mut self) -> bool {
        self.messaging
            .as_ref()
            .and_then(|m| m.operator.as_ref())
            .is_some_and(|op| lock(&op.shared).review.cancel_dispatch())
    }

    /// A contact's wake phrase alone: read what is waiting, or say so.
    pub(super) async fn contact_wake(&mut self, service: Service) -> anyhow::Result<()> {
        let contact = self.config.messaging.contact(service).context("contact not configured")?.clone();
        ui::status!("Wake heard for {}.", contact.label());
        let deliverable = self.messaging.as_ref().is_some_and(|m| {
            m.queues.head(service).is_some_and(|head| {
                m.operator.as_ref().is_none_or(|op| lock(&op.shared).review.is_approved(service, &head.id))
            })
        });
        self.gate.complete_turn(Instant::now());
        if deliverable {
            self.deliver(service).await?;
        } else {
            if !contact.empty_queue_reply.is_empty() {
                ui::status!("Nothing waiting from {}.", contact.label());
            }
            self.say(&contact.empty_queue_reply, "Empty-queue reply").await?;
            self.gate.complete_turn(Instant::now());
        }
        Ok(())
    }

    /// Traffic for a contact: send it (or hold it for review).
    pub(super) async fn contact_traffic(&mut self, service: Service, traffic: &str, whole: &str, utterance: &Utterance) -> anyhow::Result<()> {
        let contact = self.config.messaging.contact(service).context("contact not configured")?.clone();
        ui::accepted!("For {} on {service}: {traffic}", contact.label());
        self.gate.close();
        let as_voice = contact.send_as_voice;
        let Some(m) = self.messaging.as_mut() else { bail!("messaging is not running") };
        if let Some(op) = &m.operator {
            // The recording includes the wake phrase, so its transcript does too.
            let words = text::normalize(if as_voice { whole } else { traffic });
            let audio = as_voice.then(|| utterance.audio.clone());
            lock(&op.shared).review.add_outgoing(service, &words, audio);
            ui::status!("Outgoing message held for operator approval.");
            self.gate.complete_turn(Instant::now());
            return Ok(());
        }
        let limit = Duration::from_secs_f64(self.config.vad.max_utterance_seconds + 1.0);
        let sent = if as_voice {
            m.bridge.send_voice(service, &utterance.audio, limit).await
        } else {
            m.bridge.send_text(service, traffic).await
        };
        if let Err(err) = sent {
            let notice = err.to_string();
            ui::error!("{notice}");
            self.say(&notice, "Send-failure notice").await?;
            if self.once {
                return Err(err);
            }
            return Ok(());
        }
        ui::status!("Sent to {}.", contact.label());
        if !self.deliver(service).await? {
            self.gate.complete_turn(Instant::now());
        }
        Ok(())
    }

    /// Transcribe a voice note for reading or review.
    async fn transcribe_note(&mut self, path: &std::path::Path) -> anyhow::Result<String> {
        let notes = match self.messaging.as_ref().and_then(|m| m.notes.clone()) {
            Some(stt) => stt,
            None => {
                let stt: std::sync::Arc<dyn crate::stt::Transcriber> =
                    std::sync::Arc::from(crate::backends::transcriber(&self.config, &self.creds)?);
                ui::status!("Preparing voice transcription: {}...", stt.label());
                stt.prepare().await?;
                if let Some(m) = self.messaging.as_mut() {
                    m.notes = Some(stt.clone());
                }
                stt
            }
        };
        let audio = voice::decode(path, 16_000, voice::MAX_TRANSCRIBE, Fit::Strict).await?;
        let words = text::normalize(&notes.transcribe(&audio).await?);
        anyhow::ensure!(!words.is_empty(), "the voice message transcription returned no words");
        Ok(words)
    }

    /// Words of an incoming voice note, transcribing once and caching.
    pub(super) async fn note_transcript(&mut self, message: &Inbound) -> anyhow::Result<String> {
        let key = message.key();
        if let Some(words) = self.messaging.as_ref().and_then(|m| m.progress.get(&key)).and_then(|p| p.transcript.clone()) {
            return Ok(words);
        }
        let Content::Voice(path) = &message.content else { bail!("not a voice message") };
        let words = self.transcribe_note(path).await?;
        if let Some(m) = self.messaging.as_mut() {
            m.progress.entry(key).or_default().transcript = Some(words.clone());
        }
        Ok(words)
    }

    /// Deliver the next piece of `service`'s first message. Returns whether
    /// something was delivered.
    pub(super) async fn deliver(&mut self, service: Service) -> anyhow::Result<bool> {
        if !self.can_deliver(service) {
            return Ok(false);
        }
        let Some(m) = self.messaging.as_ref() else { return Ok(false) };
        let Some(message) = m.queues.head(service).cloned() else { return Ok(false) };
        let contact = self.config.messaging.contact(service).context("contact not configured")?.clone();
        let label = contact.label().to_string();
        let operator = m.operator.as_ref().map(|op| op.shared.clone());
        let (requested, edited) = match &operator {
            Some(shared) => {
                let board = lock(shared);
                let item = board.review.incoming(service, &message.id);
                (
                    board.review.dispatched().is_some_and(|i| i.service == service && i.id == message.id),
                    item.and_then(|i| i.edited.clone()),
                )
            }
            None => (false, None),
        };
        let as_text = !message.is_voice() || contact.transcribe_voice || edited.is_some();
        if let Some(shared) = &operator {
            lock(shared).delivery = if self.air.is_some() { "Preparing the message for the radio...".into() } else { "Showing the message (receive only)...".into() };
        }

        let prepared = self.prepare_delivery(&message, &label, as_text, edited).await;
        let (audio, rest) = match prepared {
            Ok(ready) => ready,
            Err(err) => {
                ui::error!("Message preparation failed: {err:#}");
                self.delivery_failed(&message, operator.as_ref());
                return Ok(false);
            }
        };
        // Preparation can outlast a receive window or a sleep command.
        if operator.is_some() && !self.can_deliver(service) {
            if let Some(shared) = &operator {
                lock(shared).delivery = "Delivery paused; waiting for an eligible receive window.".into();
            }
            return Ok(false);
        }
        let mut id_failure = None;
        if let (Some(air), Some(audio)) = (self.air.as_mut(), audio) {
            match air.send(audio).await {
                Ok(()) => {}
                Err(AirError::Fatal { message: why, reply_sent: true }) => id_failure = Some(why),
                Err(AirError::Fatal { message: why, .. }) => bail!(why),
                Err(AirError::NotSent(err)) => {
                    ui::error!("Message not transmitted: {err:#}");
                    ui::warning!("Nothing was transmitted; the message is kept.");
                    self.delivery_failed(&message, operator.as_ref());
                    return Ok(false);
                }
                Err(AirError::Playback(err)) if operator.is_some() => {
                    ui::error!("Message transmission failed: {err:#}");
                    ui::warning!("The message is kept for review; part of it may have been heard.");
                    air.mute().await;
                    self.delivery_failed(&message, operator.as_ref());
                    return Ok(false);
                }
                Err(AirError::Playback(err)) => return Err(err.context("playback failed while transmitting")),
            }
        }
        // Commit progress only after the piece went out (or was shown).
        let m = self.messaging.as_mut().expect("messaging checked above");
        let key = message.key();
        let finished = rest.is_empty();
        if finished {
            m.queues.remove(service, &message.id);
            m.progress.remove(&key);
            if let Some(shared) = &operator {
                let mut board = lock(shared);
                board.review.remove_incoming(service, &message.id);
                board.delivery = match (self.air.is_some(), requested) {
                    (true, true) => "Message transmitted; conversation unchanged.".into(),
                    (true, false) => "Message transmitted.".into(),
                    (false, _) => "Message shown (receive only).".into(),
                };
            }
            if requested {
                ui::status!("Operator delivery complete; conversation unchanged.");
            }
        } else {
            let progress = m.progress.entry(key).or_default();
            progress.remaining = Some(rest);
            progress.failures = 0;
            if let Some(shared) = &operator {
                lock(shared).delivery = "Part transmitted; the rest follows.".into();
            }
        }
        m.next_at = Instant::now() + BETWEEN_PIECES;
        if let Some(why) = id_failure {
            bail!(why);
        }
        if !requested {
            self.gate.complete_turn(Instant::now());
        }
        Ok(true)
    }

    /// Prepare the next transmission: audio (when transmitting) and the
    /// text that will remain afterwards.
    async fn prepare_delivery(&mut self, message: &Inbound, label: &str, as_text: bool, edited: Option<String>) -> anyhow::Result<(Option<Clip>, String)> {
        let key = message.key();
        if !as_text {
            ui::reply!("Reply: {label}: voice message");
            let Some(air) = self.air.as_ref() else { return Ok((None, String::new())) };
            let cached = self.messaging.as_ref().and_then(|m| m.progress.get(&key)).and_then(|p| p.speech.clone());
            if let Some(speech) = cached {
                return Ok((Some(speech), String::new()));
            }
            let Content::Voice(path) = &message.content else { bail!("not a voice message") };
            let voice = air.voice().context("no voice is configured for the sender introduction")?;
            let mut speech = voice.synthesize(&format!("{label} says:"), Fit::Strict).await?;
            let room = air.budget().saturating_sub(speech.duration());
            anyhow::ensure!(!room.is_zero(), "the sender introduction fills the whole transmission");
            let note = voice::decode(path, crate::config::RADIO_RATE, room, Fit::Crop).await?;
            if note.duration() >= room {
                ui::warning!("Voice note cut to fit the transmit limit.");
            }
            speech.append(&note);
            if let Some(m) = self.messaging.as_mut() {
                m.progress.entry(key).or_default().speech = Some(speech.clone());
            }
            return Ok((Some(speech), String::new()));
        }
        let start = match edited {
            Some(words) => words,
            None => match &message.content {
                Content::Text(words) => words.clone(),
                Content::Voice(_) => self.note_transcript(message).await?,
            },
        };
        let remaining = self
            .messaging
            .as_ref()
            .and_then(|m| m.progress.get(&key))
            .and_then(|p| p.remaining.clone())
            .unwrap_or(start);
        let Some(air) = self.air.as_ref() else {
            ui::reply!("Reply: {label} says: {}", text::with_over(&remaining));
            return Ok((None, String::new()));
        };
        let voice = air.voice().context("no voice is configured for messages")?.clone();
        let budget = air.budget();
        let max_chars = self.config.agent.max_reply_chars;
        let m = self.messaging.as_mut().expect("messaging is running");
        let progress: &mut Progress = m.progress.entry(key).or_default();
        let piece = progress.next_piece(voice.as_ref(), label, &remaining, budget, max_chars).await?;
        ui::reply!("Reply: {label} says: {}", piece.said);
        Ok((Some(piece.audio), piece.rest))
    }

    fn delivery_failed(&mut self, message: &Inbound, operator: Option<&crate::operator::Shared>) {
        let Some(m) = self.messaging.as_mut() else { return };
        m.next_at = Instant::now() + RETRY_AFTER;
        if let Some(shared) = operator {
            let mut board = lock(shared);
            if let Some(number) = board.review.incoming(message.service, &message.id).map(|i| i.number) {
                board.review.hold(number);
            }
            board.delivery = "Delivery failed; the message is back in review. See the radio log.".into();
            log(None, "warn", "Message kept for a new operator decision.");
            return;
        }
        let progress = m.progress.entry(message.key()).or_default();
        progress.failures += 1;
        if progress.failures >= ATTEMPTS {
            ui::error!("Skipping an unplayable message after {ATTEMPTS} attempts.");
            m.queues.remove(message.service, &message.id);
            m.progress.remove(&message.key());
        }
    }
}
