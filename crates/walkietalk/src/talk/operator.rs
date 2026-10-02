//! Operator mode in the talk loop: running review commands between radio
//! activity, and announcing what waits for review.

use std::time::Instant;

use anyhow::bail;
use tokio::sync::mpsc;

use super::{Talk, log};
use crate::config::{Config, ListeningMode};
use crate::gate::Destination;
use crate::operator::review::{Direction, Review};
use crate::operator::server::Server;
use crate::operator::{Action, Board, Command, Shared, lock};
use crate::ui;

pub(super) struct Operator {
    pub shared: Shared,
    pub commands: mpsc::Receiver<Command>,
    server: Server,
    panel: Option<crate::panel::Panel>,
    announced: Option<(Option<u64>, u64)>,
}

impl Operator {
    pub fn start(
        config: &Config,
        reserved: (std::path::PathBuf, std::fs::File),
        panel: bool,
    ) -> anyhow::Result<Operator> {
        let shared: Shared = std::sync::Arc::new(std::sync::Mutex::new(Board {
            review: Review::new(config),
            conversation: String::new(),
            delivery: String::new(),
        }));
        let (tx, commands) = mpsc::channel(16);
        let server = Server::start(reserved, shared.clone(), tx.clone())?;
        ui::status!(
            "Operator mode: every message waits for review. Use `walkietalk operator` from another terminal."
        );
        let panel = if panel {
            Some(crate::panel::Panel::start(shared.clone(), tx)?)
        } else {
            None
        };
        Ok(Operator {
            shared,
            commands,
            server,
            panel,
            announced: None,
        })
    }

    pub fn close(self) {
        if let Some(panel) = self.panel {
            panel.close();
        }
        drop(self.server);
    }
}

impl Talk {
    fn operator(&self) -> Option<&Operator> {
        self.messaging.as_ref().and_then(|m| m.operator.as_ref())
    }

    /// Keep the shared status current, and announce a new review head.
    pub(super) fn refresh_operator(&mut self) -> anyhow::Result<()> {
        let status = self.conversation_status();
        let Some(op) = self.messaging.as_mut().and_then(|m| m.operator.as_mut()) else {
            return Ok(());
        };
        if op.server.failed() {
            bail!("the operator controls stopped unexpectedly; stopping talk");
        }
        if let Some(panel) = &op.panel {
            panel.check()?;
        }
        let mut board = lock(&op.shared);
        board.conversation = status;
        let head = board.review.head().map(|i| i.number);
        let shown = (head, board.review.revision(false));
        if op.announced == Some(shown) {
            return Ok(());
        }
        let was_empty = op.announced.is_none_or(|(h, _)| h.is_none());
        op.announced = Some(shown);
        match board.review.head() {
            None if !was_empty => ui::status!("Operator review queue empty."),
            None => {}
            Some(item) => {
                let view = board.review.view(item);
                let direction = if item.direction == Direction::Incoming {
                    "incoming"
                } else {
                    "outgoing"
                };
                let kind = if item.voice { "voice" } else { "text" };
                ui::status!(
                    "Operator review: {direction} {}, {}, {kind}.",
                    item.service,
                    view.alias
                );
                match (&view.original, view.readable) {
                    (Some(original), _) => {
                        ui::reply!("Edited: {}", view.content);
                        ui::status!("Original: {original}");
                    }
                    (None, true) => ui::reply!(
                        "{}{}",
                        if item.voice { "Voice transcript: " } else { "" },
                        view.content
                    ),
                    (None, false) => {
                        ui::status!("Voice message: read its transcript before approving.")
                    }
                }
            }
        }
        Ok(())
    }

    /// A short description of the open conversation for status displays.
    fn conversation_status(&self) -> String {
        let now = Instant::now();
        let Some(to) = self.gate.selected() else {
            return "asleep; waiting for wake".into();
        };
        let (name, mode, contact) = match to {
            Destination::Agent => (
                self.config.wake.name.clone(),
                self.config.listening.mode,
                false,
            ),
            Destination::Contact(service) => {
                let contact = self.config.messaging.contact(service);
                (
                    format!("{} ({service})", contact.map(|c| c.label()).unwrap_or("")),
                    contact.map_or(ListeningMode::Conversation, |c| c.listening.mode),
                    true,
                )
            }
        };
        match (mode, self.gate.window_left(now)) {
            (ListeningMode::WakePhrase, _) => {
                format!(
                    "{name}; wake required to send{}",
                    if contact { "; receives when idle" } else { "" }
                )
            }
            (ListeningMode::Conversation, Some(left)) => {
                format!("{name}; follow-up open ({:.0}s left)", left.as_secs_f64())
            }
            (ListeningMode::Conversation, None) => format!("{name}; waiting for wake"),
        }
    }

    pub(super) fn pending_command(&mut self) -> Option<Command> {
        self.messaging
            .as_mut()?
            .operator
            .as_mut()?
            .commands
            .try_recv()
            .ok()
    }

    pub(super) async fn operator_command(&mut self, command: Command) -> anyhow::Result<()> {
        let result = self.run_command(&command).await;
        command.finish(matches!(result, Ok(true)));
        self.refresh_operator()?;
        result.map(|_| ())
    }

    async fn run_command(&mut self, command: &Command) -> anyhow::Result<bool> {
        let say = |kind: &str, message: &str| log(Some(command), kind, message);
        if command.cancelled() {
            say("warn", "That command was withdrawn before it started.");
            return Ok(false);
        }
        if command.action == Action::Sleep {
            self.enter_sleep("Sleep requested by the operator", Some(command))
                .await?;
            return Ok(true);
        }
        let Some(shared) = self.operator().map(|op| op.shared.clone()) else {
            return Ok(false);
        };
        let item = lock(&shared)
            .review
            .current(command.approved_view, command.revision)
            .cloned();
        let Some(item) = item else {
            say("warn", "The displayed item changed; review it again.");
            return Ok(false);
        };
        let number = item.number;
        let key = (item.service, item.id.clone());
        match command.action {
            Action::Status | Action::Sleep => Ok(true),
            Action::Deny => {
                if let Some(m) = self.messaging.as_mut()
                    && item.direction == Direction::Incoming
                {
                    m.queues.remove(item.service, &item.id);
                    m.progress.remove(&key);
                }
                lock(&shared).review.remove(number);
                say("status", "Message denied.");
                Ok(true)
            }
            Action::Edit => {
                let mut board = lock(&shared);
                if let Some(block) = board.review.edit_block(&item) {
                    drop(board);
                    say("warn", &block);
                    return Ok(false);
                }
                let words = command.text.clone().unwrap_or_default();
                let Some(before) = board.review.edit(number, &words) else {
                    drop(board);
                    say("status", "Edit cancelled; the message is unchanged.");
                    return Ok(true);
                };
                let after = board
                    .review
                    .get(number)
                    .map(|i| i.content().to_string())
                    .unwrap_or_default();
                let alias = board.review.view(&item).alias;
                drop(board);
                if item.direction == Direction::Incoming {
                    // Delivery restarts from the edited words.
                    if let Some(m) = self.messaging.as_mut() {
                        m.progress.remove(&key);
                    }
                }
                let direction = if item.direction == Direction::Incoming {
                    "incoming"
                } else {
                    "outgoing"
                };
                say(
                    "status",
                    &format!(
                        "Operator edited item {number} ({direction} {}, {alias}).",
                        item.service
                    ),
                );
                say("status", &format!("Before: {before}"));
                say("reply", &format!("After: {after}"));
                Ok(true)
            }
            Action::Read => {
                say(
                    "status",
                    "Preparing the preview; the transmitter stays off.",
                );
                let words = if !item.voice || item.readable() {
                    item.content().to_string()
                } else {
                    let message = self
                        .messaging
                        .as_ref()
                        .and_then(|m| m.queues.get(item.service, &item.id).cloned());
                    let Some(message) = message else {
                        say("error", "That voice message is no longer available.");
                        return Ok(false);
                    };
                    say("status", "Transcribing the voice message for review...");
                    match self.note_transcript(&message).await {
                        Ok(words) => {
                            lock(&shared).review.set_transcript(number, &words);
                            words
                        }
                        Err(err) => {
                            say("error", &format!("Read failed: {err:#}"));
                            return Ok(false);
                        }
                    }
                };
                say(
                    "reply",
                    &format!(
                        "{}{words}",
                        if item.voice { "Voice transcript: " } else { "" }
                    ),
                );
                Ok(true)
            }
            Action::Approve => {
                if !lock(&shared).review.is_waiting(number) {
                    say(
                        "warn",
                        "This message is already approved; use transmit to release it.",
                    );
                    return Ok(false);
                }
                if !item.readable() {
                    say("warn", "Read the voice message before approving it.");
                    return Ok(false);
                }
                if item.direction == Direction::Incoming {
                    lock(&shared).review.approve(number);
                    say("status", "Message approved; waiting for radio delivery.");
                    return Ok(true);
                }
                say("status", "Sending the approved message...");
                let limit =
                    std::time::Duration::from_secs_f64(self.config.vad.max_utterance_seconds + 1.0);
                let Some(m) = self.messaging.as_ref() else {
                    return Ok(false);
                };
                let sent = match &item.audio {
                    Some(audio) => m.bridge.send_voice(item.service, audio, limit).await,
                    None => m.bridge.send_text(item.service, item.content()).await,
                };
                match sent {
                    Ok(()) => {
                        lock(&shared).review.approve(number);
                        say("status", "Approved message sent.");
                        Ok(true)
                    }
                    Err(err) => {
                        say("error", &format!("Send failed: {err:#}"));
                        say(
                            "warn",
                            "Message kept. Delivery may be uncertain after a failed send; review it before approving again.",
                        );
                        lock(&shared).review.hold(number);
                        Ok(false)
                    }
                }
            }
            Action::Transmit => {
                if item.direction == Direction::Outgoing {
                    say(
                        "warn",
                        "Transmit applies to incoming messages; use approve to send outgoing ones.",
                    );
                    return Ok(false);
                }
                if self.shutdown.armed(Instant::now()) {
                    say(
                        "warn",
                        "Transmission is held while shutdown waits for its code.",
                    );
                    return Ok(false);
                }
                if lock(&shared).review.dispatched().is_some() {
                    say(
                        "warn",
                        "An operator delivery is already pending; wait for it to finish.",
                    );
                    return Ok(false);
                }
                let first = self
                    .messaging
                    .as_ref()
                    .and_then(|m| m.queues.head(item.service))
                    .is_some_and(|h| h.id == item.id);
                if !first {
                    say(
                        "warn",
                        "An earlier message from this contact is waiting; review it first or use --approved.",
                    );
                    return Ok(false);
                }
                if !item.readable() {
                    say("warn", "Read the voice message before transmitting it.");
                    return Ok(false);
                }
                let mut board = lock(&shared);
                if board.review.is_waiting(number) {
                    board.review.approve(number);
                }
                board.review.dispatch(number);
                drop(board);
                let how = if self.air.is_some() {
                    "transmission"
                } else {
                    "display (receive only)"
                };
                say(
                    "status",
                    &format!("One message scheduled for operator {how}; conversation unchanged."),
                );
                Ok(true)
            }
        }
    }
}
