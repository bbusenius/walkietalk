//! WhatsApp and Signal contacts: stored messages only, never live calls.
//!
//! Incoming messages wait in a per-service queue until that conversation
//! is open and the radio is idle. Long text is read in pieces across
//! transmissions; voice notes are played or transcribed. Outgoing traffic
//! is sent as text, or as the original recording.

pub mod signal;
pub mod text;
pub mod voice;
pub mod whatsapp;

use std::collections::{HashMap, HashSet, VecDeque};
use std::path::PathBuf;
use std::time::Duration;

use anyhow::bail;
use tokio::sync::mpsc;

use crate::audio::{Clip, Fit, TooLong};
use crate::config::{Config, Service};
use crate::tts::Voice;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Content {
    Text(String),
    Voice(PathBuf),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Inbound {
    pub service: Service,
    /// Unique within the service.
    pub id: String,
    pub content: Content,
}

impl Inbound {
    pub fn key(&self) -> (Service, String) {
        (self.service, self.id.clone())
    }

    pub fn is_voice(&self) -> bool {
        matches!(self.content, Content::Voice(_))
    }
}

/// Whether a sender or chat ID is the configured contact: an exact match,
/// or the same digits (ignoring formatting and any `@domain`).
pub fn same_contact(configured: &str, candidate: &str) -> bool {
    if configured.trim() == candidate.trim() {
        return true;
    }
    let digits = |s: &str| s.chars().filter(char::is_ascii_digit).collect::<String>();
    let configured = digits(configured);
    !configured.is_empty() && configured == digits(candidate.split('@').next().unwrap_or(""))
}

/// A messaging service that can send to its configured contact.
#[async_trait::async_trait]
pub trait Contact: Send + Sync {
    async fn send_text(&self, text: &str) -> anyhow::Result<()>;
    async fn send_voice(&self, file: &std::path::Path) -> anyhow::Result<()>;
    async fn stop(self: Box<Self>);
}

#[async_trait::async_trait]
impl Contact for whatsapp::WhatsApp {
    async fn send_text(&self, text: &str) -> anyhow::Result<()> {
        whatsapp::WhatsApp::send_text(self, text).await
    }
    async fn send_voice(&self, file: &std::path::Path) -> anyhow::Result<()> {
        whatsapp::WhatsApp::send_voice(self, file).await
    }
    async fn stop(self: Box<Self>) {
        whatsapp::WhatsApp::stop(*self).await
    }
}

#[async_trait::async_trait]
impl Contact for signal::Signal {
    async fn send_text(&self, text: &str) -> anyhow::Result<()> {
        signal::Signal::send_text(self, text).await
    }
    async fn send_voice(&self, file: &std::path::Path) -> anyhow::Result<()> {
        signal::Signal::send_voice(self, file).await
    }
    async fn stop(self: Box<Self>) {
        signal::Signal::stop(*self).await
    }
}

/// The running messaging services.
pub struct Bridge {
    contacts: HashMap<Service, Box<dyn Contact>>,
}

impl Bridge {
    /// Start the configured services; incoming messages arrive on the receiver.
    pub async fn start(config: &Config) -> anyhow::Result<(Bridge, mpsc::Receiver<Inbound>)> {
        let (tx, inbox) = mpsc::channel(256);
        let mut bridge = Bridge {
            contacts: HashMap::new(),
        };
        for (service, contact) in config.messaging.enabled() {
            let started: anyhow::Result<Box<dyn Contact>> = match service {
                Service::WhatsApp => whatsapp::WhatsApp::start(contact, tx.clone())
                    .await
                    .map(|c| Box::new(c) as _),
                Service::Signal => signal::Signal::start(contact, tx.clone())
                    .await
                    .map(|c| Box::new(c) as _),
            };
            match started {
                Ok(started) => {
                    bridge.contacts.insert(service, started);
                }
                Err(err) => {
                    bridge.stop().await;
                    return Err(err);
                }
            }
        }
        Ok((bridge, inbox))
    }

    #[cfg(test)]
    pub fn with(contacts: HashMap<Service, Box<dyn Contact>>) -> Bridge {
        Bridge { contacts }
    }

    fn contact(&self, service: Service) -> anyhow::Result<&dyn Contact> {
        match self.contacts.get(&service) {
            Some(contact) => Ok(contact.as_ref()),
            None => bail!("{service} is not configured"),
        }
    }

    pub async fn send_text(&self, service: Service, text: &str) -> anyhow::Result<()> {
        self.contact(service)?.send_text(text).await
    }

    /// Send the captured recording as a voice note. A failure never falls
    /// back to sending text.
    pub async fn send_voice(
        &self,
        service: Service,
        audio: &Clip,
        max: Duration,
    ) -> anyhow::Result<()> {
        let contact = self.contact(service)?;
        let dir = tempfile::Builder::new()
            .prefix("walkietalk-send-")
            .tempdir()?;
        let clip = audio.clone().fit(max, Fit::Crop)?;
        let file = voice::encode(&clip, dir.path()).await.map_err(|err| {
            anyhow::anyhow!("Message not sent. Could not prepare the voice message: {err}")
        })?;
        contact.send_voice(&file).await
    }

    pub async fn stop(self) {
        for (_, contact) in self.contacts {
            contact.stop().await;
        }
    }
}

/// Incoming messages per service, oldest first, each delivered once.
#[derive(Default)]
pub struct Queues {
    queues: HashMap<Service, VecDeque<Inbound>>,
    seen: HashSet<(Service, String)>,
}

impl Queues {
    /// Queue a new message. Returns false for a duplicate or empty one.
    pub fn add(&mut self, mut message: Inbound) -> bool {
        if message.id.is_empty() || !self.seen.insert(message.key()) {
            return false;
        }
        if let Content::Text(body) = &message.content {
            let clean = text::normalize(body);
            if clean.is_empty() {
                return false;
            }
            message.content = Content::Text(clean);
        }
        self.queues
            .entry(message.service)
            .or_default()
            .push_back(message);
        true
    }

    pub fn head(&self, service: Service) -> Option<&Inbound> {
        self.queues.get(&service).and_then(VecDeque::front)
    }

    pub fn get(&self, service: Service, id: &str) -> Option<&Inbound> {
        self.queues.get(&service)?.iter().find(|m| m.id == id)
    }

    pub fn len(&self, service: Service) -> usize {
        self.queues.get(&service).map_or(0, VecDeque::len)
    }

    /// Remove a message (after delivery, skipping, or denial).
    pub fn remove(&mut self, service: Service, id: &str) {
        if let Some(queue) = self.queues.get_mut(&service) {
            queue.retain(|m| m.id != id);
        }
    }
}

/// Delivery progress for one incoming message. Progress is committed only
/// after a successful transmission, so a failed piece is retried.
#[derive(Debug, Default)]
pub struct Progress {
    /// Text still to be read.
    pub remaining: Option<String>,
    /// Transcript of a voice note, kept across retries.
    pub transcript: Option<String>,
    /// Prepared radio audio for a played (not transcribed) voice note.
    pub speech: Option<Clip>,
    pub failures: u32,
    /// Learned characters per transmission for this voice.
    chunk_chars: Option<usize>,
}

/// One prepared transmission of a text message.
pub struct Piece {
    pub audio: Clip,
    /// The words this piece speaks (after the sender introduction).
    pub said: String,
    /// Text left for later transmissions.
    pub rest: String,
}

impl Progress {
    /// Synthesize the next piece: "<label> says: <text>", with "over" on
    /// the final piece. A piece whose speech would not fit is halved until
    /// it does; nothing is summarized or cut.
    pub async fn next_piece(
        &mut self,
        voice: &dyn Voice,
        label: &str,
        remaining: &str,
        budget: Duration,
        max_chars: usize,
    ) -> anyhow::Result<Piece> {
        let prefix = format!("{label} says: ");
        let room = max_chars.saturating_sub(prefix.chars().count() + ", over".len());
        anyhow::ensure!(
            room > 0,
            "the sender introduction leaves no room for the message"
        );
        // Start from a cautious speaking rate, then learn the voice's rate.
        let guess = ((budget.as_secs_f64() * 12.0) as usize)
            .saturating_sub(prefix.len() + 6)
            .max(24);
        let mut size = self.chunk_chars.unwrap_or(guess).min(room);
        loop {
            let (body, rest) = text::split(remaining, size);
            let line = if rest.is_empty() {
                format!("{prefix}{}", text::with_over(&body))
            } else {
                format!("{prefix}{body}")
            };
            match voice.synthesize(&line, Fit::Strict).await {
                Ok(audio) => {
                    let rate = line.chars().count() as f64 / audio.seconds().max(0.1);
                    let estimate = (rate * budget.as_secs_f64() * 0.9) as usize;
                    self.chunk_chars =
                        Some(estimate.saturating_sub(prefix.len() + 6).clamp(1, room));
                    let said = if rest.is_empty() {
                        text::with_over(&body)
                    } else {
                        body
                    };
                    return Ok(Piece { audio, said, rest });
                }
                Err(err) if err.downcast_ref::<TooLong>().is_some() && body.chars().count() > 1 => {
                    size = (body.chars().count() / 2).max(1);
                    self.chunk_chars = Some(size);
                }
                Err(err) => {
                    if err.to_string().contains("timed out") {
                        // A smaller piece may finish in time on the next try.
                        self.chunk_chars = Some((body.chars().count() / 2).max(1));
                    }
                    return Err(err);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use async_trait::async_trait;
    use std::sync::Mutex;

    #[test]
    fn contacts_match_by_digits() {
        assert!(same_contact(
            "+1 (555) 123-4567",
            "15551234567@s.whatsapp.net"
        ));
        assert!(same_contact("+15551234567", "+15551234567"));
        assert!(!same_contact("+15551234567", "+15551234568"));
        assert!(!same_contact("", "12345"));
    }

    #[test]
    fn queue_delivers_each_message_once_and_cleans_text() {
        let mut q = Queues::default();
        let m = |id: &str, text: &str| Inbound {
            service: Service::Signal,
            id: id.into(),
            content: Content::Text(text.into()),
        };
        assert!(q.add(m("1", "Hi\u{200B}\nthere")));
        assert!(!q.add(m("1", "again")));
        assert!(!q.add(m("2", "\u{200B} ")));
        assert_eq!(
            q.head(Service::Signal).unwrap().content,
            Content::Text("Hi there".into())
        );
        q.remove(Service::Signal, "1");
        assert_eq!(q.len(Service::Signal), 0);
    }

    /// Speaks at ten characters per second, failing anything over the budget.
    struct SlowVoice {
        calls: Mutex<Vec<String>>,
        budget: Duration,
    }

    #[async_trait]
    impl Voice for SlowVoice {
        fn label(&self) -> String {
            "slow".into()
        }
        async fn prepare(&self) -> anyhow::Result<()> {
            Ok(())
        }
        async fn synthesize(&self, text: &str, fit: Fit) -> anyhow::Result<Clip> {
            self.calls.lock().unwrap().push(text.to_string());
            let clip = Clip::silence(
                Duration::from_millis(text.chars().count() as u64 * 100),
                48_000,
            );
            Ok(clip.fit(self.budget, fit)?)
        }
    }

    #[tokio::test]
    async fn long_text_is_split_into_fitting_pieces_with_over_at_the_end() {
        let budget = Duration::from_secs(5);
        let voice = SlowVoice {
            calls: Mutex::new(vec![]),
            budget,
        };
        let mut progress = Progress::default();
        let mut remaining = "This is a long message. It has several sentences. Each one matters a lot to the reader.".to_string();
        let mut spoken = Vec::new();
        while !remaining.is_empty() {
            let piece = progress
                .next_piece(&voice, "Nana", &remaining, budget, 600)
                .await
                .unwrap();
            assert!(piece.audio.duration() <= budget);
            remaining = piece.rest;
            spoken.push(voice.calls.lock().unwrap().last().unwrap().clone());
        }
        assert!(spoken.len() > 1);
        assert!(spoken.iter().all(|s| s.starts_with("Nana says: ")));
        assert!(spoken.last().unwrap().ends_with(", over"));
        assert!(
            spoken[..spoken.len() - 1]
                .iter()
                .all(|s| !s.ends_with("over"))
        );
        let words: String = spoken
            .iter()
            .map(|s| {
                s.trim_start_matches("Nana says: ")
                    .trim_end_matches(", over")
            })
            .collect::<Vec<_>>()
            .join(" ");
        assert!(words.contains("reader"), "nothing is dropped: {words}");
    }
}
