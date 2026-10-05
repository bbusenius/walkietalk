//! The operator review queue.
//!
//! Every incoming and outgoing message waits here, oldest first, until the
//! operator approves or denies it. Approved incoming messages stay in their
//! contact's delivery queue. Every action is bound to a revision of what
//! the operator saw, so a command aimed at old content never applies to new
//! content.

use std::collections::BTreeMap;

use serde::Serialize;

use crate::audio::Clip;
use crate::config::{Config, Service};
use crate::messaging::text::normalize;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Direction {
    Incoming,
    Outgoing,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum State {
    Waiting,
    Approved,
}

#[derive(Debug, Clone)]
pub struct Item {
    pub number: u64,
    pub service: Service,
    pub direction: Direction,
    /// The incoming message ID (empty for outgoing).
    pub id: String,
    pub voice: bool,
    /// Incoming text, or the outgoing traffic.
    pub text: String,
    /// The outgoing recording, when sent as voice.
    pub audio: Option<Clip>,
    /// Voice transcript, once read.
    pub transcript: Option<String>,
    pub edited: Option<String>,
    state: State,
}

impl Item {
    /// Words before any edit. Voice uses its transcript.
    pub fn original(&self) -> &str {
        if self.voice {
            self.transcript.as_deref().unwrap_or("")
        } else {
            &self.text
        }
    }

    /// The words to deliver.
    pub fn content(&self) -> &str {
        self.edited.as_deref().unwrap_or_else(|| self.original())
    }

    /// Approval needs readable words.
    pub fn readable(&self) -> bool {
        !self.voice
            || self
                .transcript
                .as_deref()
                .is_some_and(|t| !t.trim().is_empty())
    }
}

/// How an item looks to the operator.
#[derive(Debug, Clone, Serialize, serde::Deserialize, PartialEq)]
pub struct ItemView {
    pub number: u64,
    pub direction: Direction,
    pub service: Service,
    pub alias: String,
    pub voice: bool,
    pub content: String,
    pub readable: bool,
    pub original: Option<String>,
    pub edit_block: Option<String>,
}

#[derive(Debug, Clone, Default, Serialize, serde::Deserialize, PartialEq)]
pub struct Snapshot {
    pub waiting: usize,
    pub approved: usize,
    pub conversation: String,
    pub item: Option<ItemView>,
    pub revision: u64,
    pub approved_item: Option<ItemView>,
    pub approved_revision: u64,
    pub delivering: Option<ItemView>,
}

pub const APPROVED_EDIT_BLOCK: &str = "Approved messages can't be edited; deny to drop it.";

pub struct Review {
    items: BTreeMap<u64, Item>,
    next: u64,
    aliases: BTreeMap<Service, String>,
    transcribe_voice: BTreeMap<Service, bool>,
    /// A single delivery the operator released with `transmit`.
    dispatch: Option<u64>,
    revision: u64,
    approved_revision: u64,
    shown: Fingerprint,
    shown_approved: Fingerprint,
}

#[derive(Debug, Clone, PartialEq, Default)]
struct Fingerprint(Option<(u64, Option<String>, Option<String>, bool)>);

impl Review {
    pub fn new(config: &Config) -> Review {
        let mut review = Review {
            items: BTreeMap::new(),
            next: 1,
            aliases: config
                .messaging
                .enabled()
                .map(|(s, c)| (s, c.label().to_string()))
                .collect(),
            transcribe_voice: config
                .messaging
                .enabled()
                .map(|(s, c)| (s, c.transcribe_voice))
                .collect(),
            dispatch: None,
            revision: 1,
            approved_revision: 1,
            shown: Fingerprint::default(),
            shown_approved: Fingerprint::default(),
        };
        review.refresh();
        review
    }

    fn fingerprint(item: Option<&Item>, dispatched: bool) -> Fingerprint {
        Fingerprint(item.map(|i| (i.number, i.transcript.clone(), i.edited.clone(), dispatched)))
    }

    /// Bump a view's revision when what it shows changed.
    fn refresh(&mut self) {
        let head = Self::fingerprint(self.head(), false);
        if head != self.shown {
            self.revision += 1;
            self.shown = head;
        }
        let approved = self.approved_head();
        let dispatched = approved.is_some_and(|i| Some(i.number) == self.dispatch);
        let fp = Self::fingerprint(approved, dispatched);
        if fp != self.shown_approved {
            self.approved_revision += 1;
            self.shown_approved = fp;
        }
    }

    pub fn add_incoming(&mut self, service: Service, id: &str, voice: bool, text: &str) -> u64 {
        self.insert(Item {
            number: 0,
            service,
            direction: Direction::Incoming,
            id: id.to_string(),
            voice,
            text: text.to_string(),
            audio: None,
            transcript: None,
            edited: None,
            state: State::Waiting,
        })
    }

    /// Outgoing traffic. A recording carries its capture transcript, so it
    /// is readable at once.
    pub fn add_outgoing(&mut self, service: Service, text: &str, audio: Option<Clip>) -> u64 {
        let voice = audio.is_some();
        self.insert(Item {
            number: 0,
            service,
            direction: Direction::Outgoing,
            id: String::new(),
            voice,
            text: text.to_string(),
            audio,
            transcript: voice.then(|| text.to_string()),
            edited: None,
            state: State::Waiting,
        })
    }

    fn insert(&mut self, mut item: Item) -> u64 {
        let number = self.next;
        self.next += 1;
        item.number = number;
        self.items.insert(number, item);
        self.refresh();
        number
    }

    /// The oldest item waiting for review.
    pub fn head(&self) -> Option<&Item> {
        self.items.values().find(|i| i.state == State::Waiting)
    }

    /// The oldest approved incoming item that is first in its contact's
    /// queue. An earlier item still under review blocks its contact.
    pub fn approved_head(&self) -> Option<&Item> {
        let mut firsts: BTreeMap<Service, &Item> = BTreeMap::new();
        for item in self
            .items
            .values()
            .filter(|i| i.direction == Direction::Incoming)
        {
            firsts.entry(item.service).or_insert(item);
        }
        firsts
            .into_values()
            .filter(|i| i.state == State::Approved)
            .min_by_key(|i| i.number)
    }

    pub fn get(&self, number: u64) -> Option<&Item> {
        self.items.get(&number)
    }

    pub fn incoming(&self, service: Service, id: &str) -> Option<&Item> {
        self.items
            .values()
            .find(|i| i.direction == Direction::Incoming && i.service == service && i.id == id)
    }

    pub fn is_approved(&self, service: Service, id: &str) -> bool {
        self.incoming(service, id)
            .is_some_and(|i| i.state == State::Approved)
    }

    pub fn is_waiting(&self, number: u64) -> bool {
        self.items
            .get(&number)
            .is_some_and(|i| i.state == State::Waiting)
    }

    pub fn revision(&self, approved_view: bool) -> u64 {
        if approved_view {
            self.approved_revision
        } else {
            self.revision
        }
    }

    /// The item a command for `revision` of a view refers to, if unchanged.
    pub fn current(&self, approved_view: bool, revision: u64) -> Option<&Item> {
        if revision != self.revision(approved_view) {
            return None;
        }
        if approved_view {
            self.approved_head()
        } else {
            self.head()
        }
    }

    /// Why the review head cannot be edited, if it can't.
    pub fn edit_block(&self, item: &Item) -> Option<String> {
        if item.state != State::Waiting {
            return Some(APPROVED_EDIT_BLOCK.into());
        }
        if item.voice {
            if item.direction == Direction::Outgoing {
                return Some("Outgoing voice sends the recording; it can't be edited.".into());
            }
            if !self
                .transcribe_voice
                .get(&item.service)
                .copied()
                .unwrap_or(false)
            {
                return Some("This voice message plays as audio; it can't be edited.".into());
            }
            if !item.readable() {
                return Some("Read the voice transcript before editing.".into());
            }
        }
        None
    }

    pub fn set_transcript(&mut self, number: u64, transcript: &str) {
        if let Some(item) = self.items.get_mut(&number) {
            item.transcript = Some(transcript.to_string());
        }
        self.refresh();
    }

    /// Replace the words. Returns the previous words, or `None` when the
    /// change is empty or the same words (not an edit).
    pub fn edit(&mut self, number: u64, text: &str) -> Option<String> {
        let item = self.items.get_mut(&number)?;
        let updated = normalize(text);
        if updated.is_empty() || updated == normalize(item.content()) {
            return None;
        }
        let previous = item.content().to_string();
        item.edited = (updated != item.original()).then_some(updated);
        self.refresh();
        Some(previous)
    }

    pub fn approve(&mut self, number: u64) {
        if let Some(item) = self.items.get_mut(&number) {
            if item.direction == Direction::Outgoing {
                self.items.remove(&number);
            } else {
                item.state = State::Approved;
            }
        }
        self.refresh();
    }

    /// Remove an item (denied, delivered, or sent).
    pub fn remove(&mut self, number: u64) {
        self.items.remove(&number);
        if self.dispatch == Some(number) {
            self.dispatch = None;
        }
        self.refresh();
    }

    pub fn remove_incoming(&mut self, service: Service, id: &str) {
        if let Some(number) = self.incoming(service, id).map(|i| i.number) {
            self.remove(number);
        }
    }

    /// A failed delivery or send needs a fresh decision, even for the same item.
    pub fn hold(&mut self, number: u64) {
        if let Some(item) = self.items.get_mut(&number) {
            item.state = State::Waiting;
        }
        if self.dispatch == Some(number) {
            self.dispatch = None;
        }
        // Force a new revision so earlier commands cannot retry it.
        self.revision += 1;
        self.refresh();
    }

    pub fn dispatch(&mut self, number: u64) {
        self.dispatch = Some(number);
        self.refresh();
    }

    pub fn dispatched(&self) -> Option<&Item> {
        self.dispatch.and_then(|n| self.items.get(&n))
    }

    /// Cancel a pending operator delivery. Returns whether one was pending.
    pub fn cancel_dispatch(&mut self) -> bool {
        let had = self.dispatch.take().is_some();
        self.refresh();
        had
    }

    pub fn view(&self, item: &Item) -> ItemView {
        ItemView {
            number: item.number,
            direction: item.direction,
            service: item.service,
            alias: self.aliases.get(&item.service).cloned().unwrap_or_default(),
            voice: item.voice,
            content: item.content().to_string(),
            readable: item.readable(),
            original: item.edited.as_ref().map(|_| item.original().to_string()),
            edit_block: self.edit_block(item),
        }
    }

    pub fn snapshot(&self, conversation: String) -> Snapshot {
        Snapshot {
            waiting: self
                .items
                .values()
                .filter(|i| i.state == State::Waiting)
                .count(),
            approved: self
                .items
                .values()
                .filter(|i| i.state == State::Approved)
                .count(),
            conversation,
            item: self.head().map(|i| self.view(i)),
            revision: self.revision,
            approved_item: self.approved_head().map(|i| self.view(i)),
            approved_revision: self.approved_revision,
            delivering: self.dispatched().map(|i| self.view(i)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::tests_support::MINIMAL;

    fn review() -> Review {
        let text = format!(
            "{MINIMAL}\n[messaging]\noperator_mode = true\n[messaging.whatsapp]\nwake_phrase = \"nana\"\nto = \"+1555\"\ntranscribe_voice = true\n[messaging.signal]\nwake_phrase = \"grandma\"\nto = \"+1666\"\n"
        );
        Review::new(&Config::parse(&text, "/".into()).unwrap())
    }

    #[test]
    fn items_are_reviewed_oldest_first_across_services() {
        let mut r = review();
        let a = r.add_incoming(Service::Signal, "s1", false, "first");
        let _b = r.add_outgoing(Service::WhatsApp, "second", None);
        assert_eq!(r.head().unwrap().number, a);
        r.approve(a);
        assert_eq!(r.head().unwrap().text, "second");
        assert_eq!(r.approved_head().unwrap().number, a);
    }

    #[test]
    fn earlier_waiting_message_blocks_later_approvals_for_that_contact() {
        let mut r = review();
        let first = r.add_incoming(Service::Signal, "1", false, "a");
        let second = r.add_incoming(Service::Signal, "2", false, "b");
        let other = r.add_incoming(Service::WhatsApp, "3", false, "c");
        r.approve(second);
        r.approve(other);
        assert_eq!(
            r.approved_head().unwrap().number,
            other,
            "Signal is blocked by its first message"
        );
        r.approve(first);
        assert_eq!(r.approved_head().unwrap().number, first);
    }

    #[test]
    fn commands_bound_to_old_content_do_not_apply() {
        let mut r = review();
        let n = r.add_incoming(Service::WhatsApp, "v", true, "");
        let seen = r.revision(false);
        assert!(r.current(false, seen).is_some());
        r.set_transcript(n, "hello there");
        assert!(
            r.current(false, seen).is_none(),
            "a transcript changes what was shown"
        );
        let seen = r.revision(false);
        r.edit(n, "hello friend").unwrap();
        assert!(
            r.current(false, seen).is_none(),
            "an edit changes what was shown"
        );
    }

    #[test]
    fn edits_normalize_and_restoring_clears_the_mark() {
        let mut r = review();
        let n = r.add_incoming(Service::Signal, "t", false, "see you at noon");
        assert!(
            r.edit(n, "  see you at\nnoon ").is_none(),
            "same words is not an edit"
        );
        assert!(r.edit(n, "   ").is_none(), "empty is not an edit");
        assert_eq!(
            r.edit(n, "see you at one").as_deref(),
            Some("see you at noon")
        );
        assert_eq!(r.get(n).unwrap().content(), "see you at one");
        r.edit(n, "see you at noon").unwrap();
        assert!(r.get(n).unwrap().edited.is_none());
    }

    #[test]
    fn voice_needs_a_transcript_and_some_voice_is_not_editable() {
        let mut r = review();
        let played = r.add_incoming(Service::Signal, "v1", true, "");
        assert!(!r.get(played).unwrap().readable());
        r.set_transcript(played, "hi");
        assert!(
            r.edit_block(r.get(played).unwrap())
                .unwrap()
                .contains("plays as audio")
        );
        let out = r.add_outgoing(
            Service::WhatsApp,
            "nana hello",
            Some(Clip::silence(std::time::Duration::from_millis(10), 8000)),
        );
        assert!(r.get(out).unwrap().readable());
        assert!(
            r.edit_block(r.get(out).unwrap())
                .unwrap()
                .contains("recording")
        );
    }

    #[test]
    fn failed_delivery_returns_to_review_with_a_new_revision() {
        let mut r = review();
        let n = r.add_incoming(Service::Signal, "1", false, "a");
        r.approve(n);
        r.dispatch(n);
        let seen = r.revision(false);
        r.hold(n);
        assert!(r.dispatched().is_none());
        assert_eq!(r.head().unwrap().number, n);
        assert_ne!(r.revision(false), seen);
    }

    #[test]
    fn approving_outgoing_removes_it() {
        let mut r = review();
        let n = r.add_outgoing(Service::Signal, "hi", None);
        r.approve(n);
        assert!(r.get(n).is_none());
        assert_eq!(r.snapshot(String::new()).waiting, 0);
    }
}
