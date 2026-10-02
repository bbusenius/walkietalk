//! Deciding what a transcript means: traffic for the agent or a contact,
//! a wake or sleep control, or noise to ignore. Remote shutdown is decided
//! separately and first. Both are pure state machines driven by explicit
//! timestamps, so they never touch audio or PTT.

use std::fmt;
use std::time::{Duration, Instant};

use crate::config::{Config, ListeningConfig, ListeningMode, Service};
use crate::phrases::{self, Phrase};

/// Who traffic goes to.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Destination {
    Agent,
    Contact(Service),
}

impl fmt::Display for Destination {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Destination::Agent => f.write_str("agent"),
            Destination::Contact(service) => write!(f, "{service}"),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Decision {
    /// Nothing was said.
    Empty,
    /// The sleep phrase: the conversation closed.
    Sleep,
    /// Not addressed and no follow-up window open.
    NeedsWake,
    /// A wake phrase with nothing after it.
    WakeOnly(Destination),
    /// Traffic to deliver. `addressed` is false for an open-window follow-up.
    Traffic {
        to: Destination,
        text: String,
        addressed: bool,
    },
}

struct Wake {
    to: Destination,
    phrase: Phrase,
}

pub struct Gate {
    wakes: Vec<Wake>,
    sleep: Vec<Phrase>,
    agent: ListeningConfig,
    contacts: Vec<(Service, ListeningConfig)>,
    selected: Option<Destination>,
    open_until: Option<Instant>,
}

impl Gate {
    pub fn new(config: &Config) -> Gate {
        let mut wakes: Vec<Wake> = std::iter::once(&config.wake.name)
            .chain(&config.wake.aliases)
            .map(|p| Wake { to: Destination::Agent, phrase: Phrase::new(p) })
            .collect();
        let mut contacts = Vec::new();
        for (service, contact) in config.messaging.enabled() {
            contacts.push((service, contact.listening.clone()));
            for p in std::iter::once(&contact.wake).chain(&contact.aliases) {
                wakes.push(Wake { to: Destination::Contact(service), phrase: Phrase::new(p) });
            }
        }
        Gate {
            wakes,
            sleep: config.sleep_phrases().into_iter().map(Phrase::new).collect(),
            agent: config.listening.clone(),
            contacts,
            selected: None,
            open_until: None,
        }
    }

    fn listening(&self, to: Destination) -> &ListeningConfig {
        match to {
            Destination::Agent => &self.agent,
            Destination::Contact(service) => self
                .contacts
                .iter()
                .find(|(s, _)| *s == service)
                .map(|(_, l)| l)
                .unwrap_or(&self.agent),
        }
    }

    /// The listening rules of the open conversation (the agent's when asleep).
    fn current(&self) -> &ListeningConfig {
        self.listening(self.selected.unwrap_or(Destination::Agent))
    }

    pub fn selected(&self) -> Option<Destination> {
        self.selected
    }

    /// Whether unaddressed traffic that began at `at` is accepted.
    pub fn follow_up_open(&self, at: Instant) -> bool {
        self.current().mode == ListeningMode::Conversation && self.open_until.is_some_and(|until| at < until)
    }

    /// Seconds left in the follow-up window.
    pub fn window_left(&self, now: Instant) -> Option<Duration> {
        self.open_until
            .filter(|_| self.current().mode == ListeningMode::Conversation)
            .map(|until| until.saturating_duration_since(now))
            .filter(|left| !left.is_zero())
    }

    /// The wake name to suggest for the open conversation.
    pub fn wake_name(&self) -> &str {
        let to = self.selected.unwrap_or(Destination::Agent);
        self.wakes
            .iter()
            .find(|w| w.to == to)
            .map_or("", |w| w.phrase.text())
    }

    pub fn decide(&mut self, transcript: &str, started_at: Instant) -> Decision {
        let text = transcript.trim();
        if phrases::normalize(text).is_empty() {
            return Decision::Empty;
        }
        let phrases: Vec<Phrase> = self.wakes.iter().map(|w| w.phrase.clone()).collect();
        let matched = phrases::longest_prefix(text, &phrases).map(|(index, words)| {
            (self.wakes[index].to, phrases::after_words(text, words))
        });
        let body = matched.map_or(text, |(_, rest)| rest);
        if self.sleep.iter().any(|s| s.matches_all(text) || s.matches_all(body)) {
            self.sleep();
            return Decision::Sleep;
        }
        match matched {
            Some((to, rest)) if phrases::normalize(rest).is_empty() => {
                self.select(to);
                Decision::WakeOnly(to)
            }
            Some((to, rest)) => {
                self.select(to);
                Decision::Traffic { to, text: rest.to_string(), addressed: true }
            }
            None if self.follow_up_open(started_at) => Decision::Traffic {
                to: self.selected.unwrap_or(Destination::Agent),
                text: text.to_string(),
                addressed: false,
            },
            None => Decision::NeedsWake,
        }
    }

    fn select(&mut self, to: Destination) {
        if self.selected != Some(to) {
            self.open_until = None;
        }
        self.selected = Some(to);
    }

    /// A turn finished: (re)open the follow-up window in conversation mode.
    pub fn complete_turn(&mut self, now: Instant) {
        let listening = self.current();
        self.open_until = (listening.mode == ListeningMode::Conversation).then(|| now + listening.follow_up());
    }

    /// Close the follow-up window, keeping the selected conversation.
    pub fn close(&mut self) {
        self.open_until = None;
    }

    /// Close the conversation entirely; a wake phrase is needed again.
    pub fn sleep(&mut self) {
        self.open_until = None;
        self.selected = None;
    }

    /// Close an expired window. Returns true when it just expired.
    pub fn expire(&mut self, now: Instant) -> bool {
        match self.open_until {
            Some(until) if now >= until => {
                self.open_until = None;
                true
            }
            _ => false,
        }
    }

    pub fn status(&self, now: Instant) -> String {
        let listening = self.current();
        let to = match self.selected {
            None | Some(Destination::Agent) => String::new(),
            Some(other) => format!(" with {other}"),
        };
        match (listening.mode, self.window_left(now)) {
            (ListeningMode::Conversation, Some(left)) => {
                format!("Listening: follow-up open{to} ({:.0}s left).", left.as_secs_f64())
            }
            (ListeningMode::Conversation, None) => format!("Listening: say \"{}\" to start.", self.wake_name()),
            (ListeningMode::WakePhrase, _) => format!("Listening: start each request with \"{}\".", self.wake_name()),
        }
    }
}

/// What the remote shutdown check decided.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Control {
    /// Not a shutdown control; continue with the normal gate.
    None,
    /// The phrase alone: waiting for the code.
    Armed,
    /// Phrase and code: stop the program.
    Confirmed,
    /// A control utterance that did nothing. It must not reach the agent.
    Rejected(&'static str),
}

pub struct Shutdown {
    enabled: bool,
    phrases: Vec<String>,
    codes: Vec<String>,
    wakes: Vec<Phrase>,
    window: Duration,
    armed_until: Option<Instant>,
}

impl Shutdown {
    pub fn new(config: &Config) -> Shutdown {
        let s = &config.shutdown;
        let normalized = |first: &String, rest: &[String]| {
            std::iter::once(first)
                .chain(rest)
                .map(|p| phrases::normalize(p))
                .filter(|p| !p.is_empty())
                .collect::<Vec<_>>()
        };
        let mut wakes: Vec<Phrase> = std::iter::once(&config.wake.name)
            .chain(&config.wake.aliases)
            .map(|p| Phrase::new(p))
            .collect();
        for (_, contact) in config.messaging.enabled() {
            wakes.extend(std::iter::once(&contact.wake).chain(&contact.aliases).map(|p| Phrase::new(p)));
        }
        Shutdown {
            enabled: s.enabled,
            phrases: normalized(&s.phrase, &s.phrase_aliases),
            codes: normalized(&s.code, &s.code_aliases),
            wakes,
            window: Duration::from_secs_f64(s.confirm_window_seconds),
            armed_until: None,
        }
    }

    pub fn armed(&self, now: Instant) -> bool {
        self.armed_until.is_some_and(|until| now < until)
    }

    pub fn cancel(&mut self) {
        self.armed_until = None;
    }

    /// Disarm after the window. Returns true when it just expired.
    pub fn expire(&mut self, now: Instant) -> bool {
        match self.armed_until {
            Some(until) if now >= until => {
                self.armed_until = None;
                true
            }
            _ => false,
        }
    }

    pub fn decide(&mut self, transcript: &str, started_at: Instant, now: Instant) -> Control {
        if !self.enabled {
            return Control::None;
        }
        // The whole utterance, and the utterance after any wake phrase.
        let mut candidates = vec![phrases::normalize(transcript)];
        for wake in &self.wakes {
            if let Some((_, words)) = phrases::longest_prefix(transcript, std::slice::from_ref(wake)) {
                candidates.push(phrases::normalize(phrases::after_words(transcript, words)));
            }
        }
        let is = |list: &[String]| candidates.iter().any(|c| list.contains(c));
        let together = self
            .phrases
            .iter()
            .flat_map(|p| self.codes.iter().map(move |c| format!("{p} {c}")))
            .any(|both| candidates.contains(&both));
        if together {
            self.armed_until = None;
            return Control::Confirmed;
        }
        if is(&self.phrases) {
            self.armed_until = Some(now + self.window);
            return Control::Armed;
        }
        if let Some(until) = self.armed_until.take() {
            return if is(&self.codes) && started_at < until {
                Control::Confirmed
            } else {
                Control::Rejected("shutdown cancelled: the code was wrong or late")
            };
        }
        if is(&self.codes) {
            return Control::Rejected("shutdown code ignored: shutdown is not armed");
        }
        let starts_control = candidates.iter().any(|c| {
            self.phrases.iter().chain(&self.codes).any(|p| c.starts_with(&format!("{p} ")))
        });
        if starts_control {
            return Control::Rejected("shutdown control not recognized; say the phrase alone or the phrase then the code");
        }
        Control::None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::tests_support::MINIMAL;

    fn config(extra: &str) -> Config {
        Config::parse(&format!("{MINIMAL}\n{extra}"), "/".into()).unwrap()
    }

    const SLEEP: &str = "[sleep]\nphrase = \"go to sleep\"\naliases = [\"stop listening\"]\n";
    const CONTACTS: &str = "[messaging.whatsapp]\nwake = \"code\"\nto = \"+15550001\"\n[messaging.signal]\nwake = \"code one\"\nto = \"+15550002\"\nlistening = { mode = \"wake-phrase\", follow_up_seconds = 60 }\n";
    const SHUTDOWN: &str = "[shutdown]\nenabled = true\nphrase = \"bird\"\nphrase_aliases = [\"picard epsilon\"]\ncode = \"seven\"\ncode_aliases = [\"7\"]\nconfirm_window_seconds = 30\narmed_reply = \"armed\"\nconfirmed_reply = \"goodbye\"\n";

    fn secs(t0: Instant, s: u64) -> Instant {
        t0 + Duration::from_secs(s)
    }

    #[test]
    fn traffic_needs_the_wake_name() {
        let mut gate = Gate::new(&config(""));
        let t0 = Instant::now();
        assert_eq!(gate.decide("what time is it", t0), Decision::NeedsWake);
        assert_eq!(
            gate.decide("Charlotte, what time is it?", t0),
            Decision::Traffic { to: Destination::Agent, text: "what time is it?".into(), addressed: true }
        );
        assert_eq!(gate.decide("  ", t0), Decision::Empty);
    }

    #[test]
    fn follow_up_window_opens_after_a_turn_and_expires() {
        let mut gate = Gate::new(&config(""));
        let t0 = Instant::now();
        let _ = gate.decide("charlotte hi", t0);
        gate.complete_turn(t0);
        assert!(matches!(gate.decide("and then?", secs(t0, 29)), Decision::Traffic { addressed: false, .. }));
        assert_eq!(gate.decide("and then?", secs(t0, 31)), Decision::NeedsWake);
        assert!(gate.expire(secs(t0, 31)));
    }

    #[test]
    fn eligibility_uses_when_speech_started() {
        let mut gate = Gate::new(&config(""));
        let t0 = Instant::now();
        let _ = gate.decide("charlotte hi", t0);
        gate.complete_turn(t0);
        // Started inside the window, even if transcribed after it closed.
        assert!(matches!(gate.decide("one more", secs(t0, 29)), Decision::Traffic { .. }));
    }

    #[test]
    fn wake_phrase_mode_has_no_follow_ups() {
        let mut gate = Gate::new(&config("[listening]\nmode = \"wake-phrase\"\n"));
        let t0 = Instant::now();
        let _ = gate.decide("charlotte hi", t0);
        gate.complete_turn(t0);
        assert_eq!(gate.decide("and then?", secs(t0, 1)), Decision::NeedsWake);
    }

    #[test]
    fn wake_alone_selects_without_traffic() {
        let mut gate = Gate::new(&config(""));
        let t0 = Instant::now();
        assert_eq!(gate.decide("Charlot!", t0), Decision::WakeOnly(Destination::Agent));
        gate.complete_turn(t0);
        assert!(gate.follow_up_open(secs(t0, 1)));
    }

    #[test]
    fn sleep_closes_and_never_becomes_traffic() {
        let mut gate = Gate::new(&config(SLEEP));
        let t0 = Instant::now();
        let _ = gate.decide("charlotte hi", t0);
        gate.complete_turn(t0);
        assert_eq!(gate.decide("Charlotte, go to sleep.", secs(t0, 1)), Decision::Sleep);
        assert_eq!(gate.selected(), None);
        assert_eq!(gate.decide("follow up", secs(t0, 2)), Decision::NeedsWake);
        assert_eq!(gate.decide("stop listening", secs(t0, 3)), Decision::Sleep);
        // A mention inside a longer request is ordinary traffic.
        assert!(matches!(gate.decide("charlotte, when do kids go to sleep", secs(t0, 4)), Decision::Traffic { .. }));
    }

    #[test]
    fn longest_wake_wins_and_switches_conversation() {
        let mut gate = Gate::new(&config(CONTACTS));
        let t0 = Instant::now();
        assert_eq!(
            gate.decide("code one hello", t0),
            Decision::Traffic { to: Destination::Contact(Service::Signal), text: "hello".into(), addressed: true }
        );
        assert_eq!(
            gate.decide("code two", t0),
            Decision::Traffic { to: Destination::Contact(Service::WhatsApp), text: "two".into(), addressed: true }
        );
        gate.complete_turn(t0);
        assert!(matches!(
            gate.decide("unaddressed", secs(t0, 5)),
            Decision::Traffic { to: Destination::Contact(Service::WhatsApp), addressed: false, .. }
        ));
        assert!(matches!(gate.decide("charlotte hi", secs(t0, 6)), Decision::Traffic { to: Destination::Agent, .. }));
    }

    #[test]
    fn each_contact_uses_its_own_listening_mode() {
        let mut gate = Gate::new(&config(CONTACTS));
        let t0 = Instant::now();
        let _ = gate.decide("code one hello", t0);
        gate.complete_turn(t0);
        assert_eq!(gate.decide("unaddressed", secs(t0, 1)), Decision::NeedsWake);
    }

    #[test]
    fn shutdown_phrase_then_code_within_window() {
        let mut s = Shutdown::new(&config(SHUTDOWN));
        let t0 = Instant::now();
        assert_eq!(s.decide("Bird.", t0, t0), Control::Armed);
        assert_eq!(s.decide("seven", secs(t0, 10), secs(t0, 11)), Control::Confirmed);
    }

    #[test]
    fn shutdown_together_with_optional_wake() {
        let mut s = Shutdown::new(&config(SHUTDOWN));
        let t0 = Instant::now();
        assert_eq!(s.decide("Charlotte, Picard epsilon 7!", t0, t0), Control::Confirmed);
    }

    #[test]
    fn shutdown_code_alone_wrong_or_late_does_nothing() {
        let mut s = Shutdown::new(&config(SHUTDOWN));
        let t0 = Instant::now();
        assert!(matches!(s.decide("seven", t0, t0), Control::Rejected(_)));
        assert_eq!(s.decide("bird", t0, t0), Control::Armed);
        assert!(matches!(s.decide("eight", secs(t0, 1), secs(t0, 1)), Control::Rejected(_)));
        assert!(!s.armed(secs(t0, 1)), "a wrong answer cancels arming");
        assert_eq!(s.decide("bird", t0, t0), Control::Armed);
        assert!(matches!(s.decide("seven", secs(t0, 31), secs(t0, 31)), Control::Rejected(_)));
    }

    #[test]
    fn near_miss_controls_never_reach_the_agent() {
        let mut s = Shutdown::new(&config(SHUTDOWN));
        let t0 = Instant::now();
        assert!(matches!(s.decide("bird seven eight", t0, t0), Control::Rejected(_)));
        assert_eq!(s.decide("tell me about birds", t0, t0), Control::None);
    }

    #[test]
    fn disabled_shutdown_ignores_everything() {
        let mut s = Shutdown::new(&config(""));
        assert_eq!(s.decide("bird seven", Instant::now(), Instant::now()), Control::None);
    }
}
