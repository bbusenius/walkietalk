//! Deciding what a transcript means: traffic for the agent or a contact,
//! a wake or sleep control, or noise to ignore. Remote shutdown is decided
//! separately and first. Both are pure state machines driven by explicit
//! timestamps, so they never touch audio or PTT.

use std::fmt;
use std::time::{Duration, Instant};

use crate::config::{Config, ListeningConfig, ListeningMode, Service};
use crate::phrases::{self, Phrase};
use crate::sarneg::{self, Key};

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

/// How speech is addressed: by wake phrases, or in SARNEG mode by codes.
/// Each list is in configuration order, the agent first.
enum Wakes {
    Phrases(Vec<(Destination, Phrase)>),
    Codes(Key, Vec<(Destination, String)>),
}

impl Wakes {
    fn new(config: &Config) -> Wakes {
        let contacts = || config.messaging.enabled();
        if let Some(key) = config.sarneg_key() {
            let codes = std::iter::once((Destination::Agent, config.wake.sarneg_code.clone()))
                .chain(contacts().map(|(service, contact)| {
                    (Destination::Contact(service), contact.sarneg_code.clone())
                }))
                .collect();
            return Wakes::Codes(key, codes);
        }
        let phrases = |to, first: &String, rest: &[String]| {
            std::iter::once(first)
                .chain(rest)
                .map(move |p| (to, Phrase::new(p)))
                .collect::<Vec<_>>()
        };
        let mut wakes = phrases(
            Destination::Agent,
            &config.wake.wake_phrase,
            &config.wake.aliases,
        );
        for (service, contact) in contacts() {
            wakes.extend(phrases(
                Destination::Contact(service),
                &contact.wake_phrase,
                &contact.aliases,
            ));
        }
        Wakes::Phrases(wakes)
    }

    /// The longest wake that starts `text`, and the text after it.
    fn find<'a>(&self, text: &'a str) -> Option<(Destination, &'a str)> {
        let (to, words) = match self {
            Wakes::Phrases(wakes) => {
                let phrases: Vec<Phrase> = wakes.iter().map(|(_, p)| p.clone()).collect();
                let (index, words) = phrases::longest_prefix(text, &phrases)?;
                (wakes[index].0, words)
            }
            Wakes::Codes(key, codes) => {
                let numbers: Vec<String> = codes.iter().map(|(_, n)| n.clone()).collect();
                let (index, words) = sarneg::longest_prefix(key, text, &numbers)?;
                (codes[index].0, words)
            }
        };
        Some((to, phrases::after_words(text, words)))
    }

    /// What to say to address `to`, for status lines.
    fn hint(&self, to: Destination) -> String {
        match self {
            Wakes::Phrases(wakes) => wakes
                .iter()
                .find(|(t, _)| *t == to)
                .map_or_else(String::new, |(_, p)| format!("\"{}\"", p.text())),
            Wakes::Codes(..) => format!("the {to} SARNEG code"),
        }
    }
}

pub struct Gate {
    wakes: Wakes,
    sleep: Codes,
    agent: ListeningConfig,
    contacts: Vec<(Service, ListeningConfig)>,
    selected: Option<Destination>,
    open_until: Option<Instant>,
}

impl Gate {
    pub fn new(config: &Config) -> Gate {
        let contacts = config
            .messaging
            .enabled()
            .map(|(service, contact)| (service, contact.listening.clone()))
            .collect();
        Gate {
            wakes: Wakes::new(config),
            sleep: match (config.sarneg_key(), &config.sleep) {
                (Some(key), Some(sleep)) => Codes::Number(key, sleep.sarneg_code.clone()),
                _ => Codes::Phrases(
                    config
                        .sleep_phrases()
                        .into_iter()
                        .map(phrases::normalize)
                        .collect(),
                ),
            },
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
        self.current().mode == ListeningMode::Conversation
            && self.open_until.is_some_and(|until| at < until)
    }

    /// Seconds left in the follow-up window.
    pub fn window_left(&self, now: Instant) -> Option<Duration> {
        self.open_until
            .filter(|_| self.current().mode == ListeningMode::Conversation)
            .map(|until| until.saturating_duration_since(now))
            .filter(|left| !left.is_zero())
    }

    /// What to say to wake the open conversation: a quoted phrase, or a
    /// description of the SARNEG code (never the code itself).
    pub fn wake_hint(&self) -> String {
        self.wakes.hint(self.selected.unwrap_or(Destination::Agent))
    }

    pub fn decide(&mut self, transcript: &str, started_at: Instant) -> Decision {
        let text = transcript.trim();
        if phrases::normalize(text).is_empty() {
            return Decision::Empty;
        }
        let matched = self.wakes.find(text);
        let body = matched.map_or(text, |(_, rest)| rest);
        if [text, body]
            .iter()
            .any(|t| self.sleep.matches(&phrases::normalize(t)))
        {
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
                Decision::Traffic {
                    to,
                    text: rest.to_string(),
                    addressed: true,
                }
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
        self.open_until =
            (listening.mode == ListeningMode::Conversation).then(|| now + listening.follow_up());
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
                format!(
                    "Listening: follow-up open{to} ({:.0}s left).",
                    left.as_secs_f64()
                )
            }
            (ListeningMode::Conversation, None) => {
                format!("Listening: say {} to start.", self.wake_hint())
            }
            (ListeningMode::WakePhrase, _) => {
                format!("Listening: start each request with {}.", self.wake_hint())
            }
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

/// A control said as a whole utterance (sleep, or the shutdown code):
/// phrases, or in SARNEG mode a number.
enum Codes {
    Phrases(Vec<String>),
    Number(Key, String),
}

impl Codes {
    /// Whether a normalized utterance is exactly the code.
    fn matches(&self, utterance: &str) -> bool {
        match self {
            Codes::Phrases(list) => list.iter().any(|c| c == utterance),
            Codes::Number(key, number) => sarneg::is_code(key, utterance, number),
        }
    }
}

pub struct Shutdown {
    enabled: bool,
    phrases: Vec<String>,
    codes: Codes,
    wakes: Wakes,
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
        let codes = match config.sarneg_key() {
            Some(key) => Codes::Number(key, s.sarneg_code.clone()),
            None => Codes::Phrases(normalized(&s.code, &s.code_aliases)),
        };
        Shutdown {
            enabled: s.enabled,
            phrases: normalized(&s.phrase, &s.phrase_aliases),
            codes,
            wakes: Wakes::new(config),
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
        // The whole utterance, and the utterance after any wake.
        let mut candidates = vec![phrases::normalize(transcript)];
        if let Some((_, rest)) = self.wakes.find(transcript) {
            candidates.push(phrases::normalize(rest));
        }
        // The text after a leading shutdown phrase.
        let after_phrase = |c: &'_ str| -> Vec<String> {
            self.phrases
                .iter()
                .filter_map(|p| c.strip_prefix(p.as_str())?.strip_prefix(' '))
                .map(str::to_string)
                .collect()
        };
        let together = candidates
            .iter()
            .flat_map(|c| after_phrase(c))
            .any(|rest| self.codes.matches(&rest));
        if together {
            self.armed_until = None;
            return Control::Confirmed;
        }
        if candidates.iter().any(|c| self.phrases.contains(c)) {
            self.armed_until = Some(now + self.window);
            return Control::Armed;
        }
        let code = candidates.iter().any(|c| self.codes.matches(c));
        if let Some(until) = self.armed_until.take() {
            return if code && started_at < until {
                Control::Confirmed
            } else {
                Control::Rejected("shutdown cancelled: the code was wrong or late")
            };
        }
        if code {
            return Control::Rejected("shutdown code ignored: shutdown is not armed");
        }
        let code_words: &[String] = match &self.codes {
            Codes::Phrases(list) => list,
            Codes::Number(..) => &[],
        };
        let starts_control = candidates.iter().any(|c| {
            self.phrases
                .iter()
                .chain(code_words)
                .any(|p| c.starts_with(&format!("{p} ")))
        });
        if starts_control {
            return Control::Rejected(
                "shutdown control not recognized; say the phrase alone or the phrase then the code",
            );
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
    const CONTACTS: &str = "[messaging.whatsapp]\nwake_phrase = \"code\"\nto = \"+15550001\"\n[messaging.signal]\nwake_phrase = \"code one\"\nto = \"+15550002\"\nlistening = { mode = \"wake-phrase\", follow_up_seconds = 60 }\n";
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
            Decision::Traffic {
                to: Destination::Agent,
                text: "what time is it?".into(),
                addressed: true
            }
        );
        assert_eq!(gate.decide("  ", t0), Decision::Empty);
    }

    #[test]
    fn follow_up_window_opens_after_a_turn_and_expires() {
        let mut gate = Gate::new(&config(""));
        let t0 = Instant::now();
        let _ = gate.decide("charlotte hi", t0);
        gate.complete_turn(t0);
        assert!(matches!(
            gate.decide("and then?", secs(t0, 29)),
            Decision::Traffic {
                addressed: false,
                ..
            }
        ));
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
        assert!(matches!(
            gate.decide("one more", secs(t0, 29)),
            Decision::Traffic { .. }
        ));
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
        assert_eq!(
            gate.decide("Charlot!", t0),
            Decision::WakeOnly(Destination::Agent)
        );
        gate.complete_turn(t0);
        assert!(gate.follow_up_open(secs(t0, 1)));
    }

    #[test]
    fn sleep_closes_and_never_becomes_traffic() {
        let mut gate = Gate::new(&config(SLEEP));
        let t0 = Instant::now();
        let _ = gate.decide("charlotte hi", t0);
        gate.complete_turn(t0);
        assert_eq!(
            gate.decide("Charlotte, go to sleep.", secs(t0, 1)),
            Decision::Sleep
        );
        assert_eq!(gate.selected(), None);
        assert_eq!(gate.decide("follow up", secs(t0, 2)), Decision::NeedsWake);
        assert_eq!(gate.decide("stop listening", secs(t0, 3)), Decision::Sleep);
        // A mention inside a longer request is ordinary traffic.
        assert!(matches!(
            gate.decide("charlotte, when do kids go to sleep", secs(t0, 4)),
            Decision::Traffic { .. }
        ));
    }

    #[test]
    fn longest_wake_wins_and_switches_conversation() {
        let mut gate = Gate::new(&config(CONTACTS));
        let t0 = Instant::now();
        assert_eq!(
            gate.decide("code one hello", t0),
            Decision::Traffic {
                to: Destination::Contact(Service::Signal),
                text: "hello".into(),
                addressed: true
            }
        );
        assert_eq!(
            gate.decide("code two", t0),
            Decision::Traffic {
                to: Destination::Contact(Service::WhatsApp),
                text: "two".into(),
                addressed: true
            }
        );
        gate.complete_turn(t0);
        assert!(matches!(
            gate.decide("unaddressed", secs(t0, 5)),
            Decision::Traffic {
                to: Destination::Contact(Service::WhatsApp),
                addressed: false,
                ..
            }
        ));
        assert!(matches!(
            gate.decide("charlotte hi", secs(t0, 6)),
            Decision::Traffic {
                to: Destination::Agent,
                ..
            }
        ));
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
        assert_eq!(
            s.decide("seven", secs(t0, 10), secs(t0, 11)),
            Control::Confirmed
        );
    }

    #[test]
    fn shutdown_together_with_optional_wake() {
        let mut s = Shutdown::new(&config(SHUTDOWN));
        let t0 = Instant::now();
        assert_eq!(
            s.decide("Charlotte, Picard epsilon 7!", t0, t0),
            Control::Confirmed
        );
    }

    #[test]
    fn shutdown_code_alone_wrong_or_late_does_nothing() {
        let mut s = Shutdown::new(&config(SHUTDOWN));
        let t0 = Instant::now();
        assert!(matches!(s.decide("seven", t0, t0), Control::Rejected(_)));
        assert_eq!(s.decide("bird", t0, t0), Control::Armed);
        assert!(matches!(
            s.decide("eight", secs(t0, 1), secs(t0, 1)),
            Control::Rejected(_)
        ));
        assert!(!s.armed(secs(t0, 1)), "a wrong answer cancels arming");
        assert_eq!(s.decide("bird", t0, t0), Control::Armed);
        assert!(matches!(
            s.decide("seven", secs(t0, 31), secs(t0, 31)),
            Control::Rejected(_)
        ));
    }

    #[test]
    fn near_miss_controls_never_reach_the_agent() {
        let mut s = Shutdown::new(&config(SHUTDOWN));
        let t0 = Instant::now();
        assert!(matches!(
            s.decide("bird seven eight", t0, t0),
            Control::Rejected(_)
        ));
        assert_eq!(s.decide("tell me about birds", t0, t0), Control::None);
    }

    /// Key AFTERSHOCK: agent 762 is O H T, WhatsApp 4518 is R S F C, Signal
    /// 905 is K A S, and the shutdown code 6338 is H E E C.
    const SARNEG: &str = "sarneg_code = \"762\"\n[sarneg]\nenabled = true\nkey = \"AFTERSHOCK\"\n[messaging.whatsapp]\nwake_phrase = \"nana\"\nto = \"+15550001\"\nsarneg_code = \"4518\"\n[messaging.signal]\nwake_phrase = \"grandma\"\nto = \"+15550002\"\nsarneg_code = \"905\"\n";

    #[test]
    fn sarneg_codes_route_and_plain_wakes_do_not() {
        let mut gate = Gate::new(&config(SARNEG));
        let t0 = Instant::now();
        assert_eq!(
            gate.decide("Oscar hotel tango, what time is it?", t0),
            Decision::Traffic {
                to: Destination::Agent,
                text: "what time is it?".into(),
                addressed: true
            }
        );
        assert_eq!(
            gate.decide("R S F C I need a hand", t0),
            Decision::Traffic {
                to: Destination::Contact(Service::WhatsApp),
                text: "I need a hand".into(),
                addressed: true
            }
        );
        assert_eq!(
            gate.decide("kilo alfa sierra", t0),
            Decision::WakeOnly(Destination::Contact(Service::Signal))
        );
        gate.sleep();
        for plain in ["charlotte, what time is it?", "nana hello", "grandma"] {
            assert_eq!(gate.decide(plain, t0), Decision::NeedsWake, "{plain}");
        }
        assert_eq!(
            gate.decide("oscar hotel echo hello", t0),
            Decision::NeedsWake,
            "a wrong letter"
        );
        assert_eq!(
            gate.status(t0),
            "Listening: say the agent SARNEG code to start."
        );
    }

    #[test]
    fn sarneg_follow_ups_need_no_code_and_sleep_has_its_own() {
        // The sleep code 338 is E E C.
        let extra = format!("{SARNEG}{SLEEP}sarneg_code = \"338\"\n");
        let mut gate = Gate::new(&config(&extra));
        let t0 = Instant::now();
        let _ = gate.decide("O H T hi", t0);
        gate.complete_turn(t0);
        assert!(matches!(
            gate.decide("and then?", secs(t0, 1)),
            Decision::Traffic {
                addressed: false,
                ..
            }
        ));
        // The plain phrase is ordinary follow-up traffic in SARNEG mode.
        assert!(matches!(
            gate.decide("go to sleep", secs(t0, 2)),
            Decision::Traffic {
                addressed: false,
                ..
            }
        ));
        assert_eq!(
            gate.decide("Echo, echo, charlie.", secs(t0, 3)),
            Decision::Sleep
        );
        let _ = gate.decide("O H T hi", secs(t0, 4));
        assert_eq!(
            gate.decide("oscar hotel tango, E E C", secs(t0, 5)),
            Decision::Sleep,
            "after a wake code"
        );
    }

    #[test]
    fn sarneg_shutdown_keeps_the_phrase_and_codes_the_number() {
        let extra = format!("{SARNEG}{SHUTDOWN}sarneg_code = \"6338\"\n");
        let mut s = Shutdown::new(&config(&extra));
        let t0 = Instant::now();
        assert_eq!(s.decide("Bird.", t0, t0), Control::Armed);
        assert_eq!(
            s.decide("hotel echo echo charlie", secs(t0, 5), secs(t0, 6)),
            Control::Confirmed
        );
        assert_eq!(
            s.decide("Oscar hotel tango, picard epsilon, H E E C", t0, t0),
            Control::Confirmed,
            "together, after the agent's code"
        );
        // The phrase code no longer works, and never reaches the agent.
        assert_eq!(s.decide("bird", t0, t0), Control::Armed);
        assert!(matches!(
            s.decide("seven", secs(t0, 1), secs(t0, 1)),
            Control::Rejected(_)
        ));
        assert!(matches!(s.decide("bird 7", t0, t0), Control::Rejected(_)));
        assert!(
            matches!(s.decide("H E E C", t0, t0), Control::Rejected(_)),
            "the code alone is not armed"
        );
        assert_eq!(
            s.decide("O H T, tell me about birds", t0, t0),
            Control::None
        );
    }

    #[test]
    fn a_wake_code_spelled_with_as_in_is_a_wake_alone() {
        let mut gate = Gate::new(&config(SARNEG));
        assert_eq!(
            gate.decide("K as in King, A as in Andy, S as in Sam.", Instant::now()),
            Decision::WakeOnly(Destination::Contact(Service::Signal))
        );
    }

    #[test]
    fn switching_sarneg_off_restores_the_phrases() {
        let off = SARNEG.replace("enabled = true", "enabled = false");
        let mut gate = Gate::new(&config(&off));
        let t0 = Instant::now();
        assert!(matches!(
            gate.decide("charlotte hi", t0),
            Decision::Traffic {
                to: Destination::Agent,
                ..
            }
        ));
        assert_eq!(gate.decide("O H T hi", secs(t0, 60)), Decision::NeedsWake);
    }

    #[test]
    fn disabled_shutdown_ignores_everything() {
        let mut s = Shutdown::new(&config(""));
        assert_eq!(
            s.decide("bird seven", Instant::now(), Instant::now()),
            Control::None
        );
    }
}
