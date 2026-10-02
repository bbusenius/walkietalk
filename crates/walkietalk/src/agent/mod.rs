//! Text agents and the bounded radio conversation.
//!
//! A [`Conversation`] keeps the last few completed turns. Asking returns a
//! [`PendingReply`]; the turn joins the history only when the caller commits
//! it after the reply was actually delivered. A failed or unheard turn is
//! simply dropped. Backends never fall back to one another.

pub mod claude_api;
pub mod claude_cli;
mod cli;
pub mod codex;
pub mod grok_cli;
pub mod hermes;
pub mod instructions;

use std::collections::VecDeque;

use anyhow::bail;
use async_trait::async_trait;

use crate::config::Config;
use instructions::Limits;

pub const MAX_TRAFFIC_CHARS: usize = 4000;
/// Room for one web lookup plus a short answer, never a research loop.
pub const WEB_SEARCH_TURNS: u32 = 4;

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Turn {
    pub user: String,
    pub assistant: String,
}

/// Everything a backend needs for one reply.
#[derive(Debug, Clone, Copy)]
pub struct Request<'a> {
    /// Stable for one run of the program.
    pub session_id: &'a str,
    pub instructions: &'a str,
    pub history: &'a [Turn],
    pub traffic: &'a str,
    pub web_search: bool,
}

#[async_trait]
pub trait TextAgent: Send + Sync {
    fn label(&self) -> String;
    /// Return only the final answer, or a clear error. Never transmit,
    /// never substitute another backend.
    async fn reply(&self, request: Request<'_>) -> anyhow::Result<String>;
}

/// A reply that has not yet been delivered.
#[must_use = "commit the reply once it has been delivered"]
#[derive(Debug, Clone)]
pub struct PendingReply {
    pub text: String,
    traffic: String,
}

pub struct Conversation {
    agent: Box<dyn TextAgent>,
    session_id: String,
    history: VecDeque<Turn>,
    max_turns: usize,
    max_chars: usize,
    instructions: String,
    web_search: bool,
}

impl Conversation {
    /// `spoken` adds radio guidance to the default instructions.
    pub fn new(agent: Box<dyn TextAgent>, config: &Config, spoken: bool) -> Conversation {
        let limits = Limits {
            max_reply_chars: config.agent.max_reply_chars,
            spoken_seconds: config.radio.speech_budget().as_secs_f64(),
        };
        let mut text = instructions::guidance(&config.agent.instructions, limits, spoken);
        if !text.ends_with(char::is_whitespace) {
            text.push(' ');
        }
        text.push_str(if config.agent.web_search {
            "You may search the public web when the question needs current information. \
             Do not run commands, change files, read local files, or contact other people. "
        } else {
            "Do not search the web. "
        });
        text.push_str("Return only the final answer, without Markdown or tool diagnostics.");
        Conversation {
            agent,
            session_id: uuid::Uuid::new_v4().to_string(),
            history: VecDeque::new(),
            max_turns: config.agent.history_turns,
            max_chars: config.agent.max_reply_chars,
            instructions: text,
            web_search: config.agent.web_search,
        }
    }

    pub fn label(&self) -> String {
        self.agent.label()
    }

    pub fn history(&self) -> impl Iterator<Item = &Turn> {
        self.history.iter()
    }

    /// Ask the agent. Nothing is remembered until [`Conversation::commit`].
    pub async fn ask(&self, traffic: &str) -> anyhow::Result<PendingReply> {
        let traffic = traffic.trim();
        if traffic.is_empty() {
            bail!("nothing to ask");
        }
        if traffic.chars().count() > MAX_TRAFFIC_CHARS {
            bail!("the request is longer than {MAX_TRAFFIC_CHARS} characters");
        }
        let history: Vec<Turn> = self.history.iter().cloned().collect();
        let raw = self
            .agent
            .reply(Request {
                session_id: &self.session_id,
                instructions: &self.instructions,
                history: &history,
                traffic,
                web_search: self.web_search,
            })
            .await?;
        Ok(PendingReply {
            text: clean_reply(&raw, self.max_chars)?,
            traffic: traffic.to_string(),
        })
    }

    /// Remember a delivered reply, dropping the oldest turn beyond the limit.
    pub fn commit(&mut self, reply: PendingReply) {
        self.history.push_back(Turn {
            user: reply.traffic,
            assistant: reply.text,
        });
        while self.history.len() > self.max_turns {
            self.history.pop_front();
        }
    }
}

/// Validate final text: non-empty, within the limit, no control characters,
/// and on one line.
pub fn clean_reply(raw: &str, max_chars: usize) -> anyhow::Result<String> {
    let text = raw.trim();
    if text.is_empty() {
        bail!("the agent returned no answer");
    }
    if text.chars().any(|c| c.is_control() && !matches!(c, '\n' | '\r' | '\t')) {
        bail!("the agent returned control characters; reply discarded");
    }
    let text = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if text.chars().count() > max_chars {
        bail!("the reply is longer than agent.max_reply_chars ({max_chars}); discarded");
    }
    Ok(text)
}

/// The offline stand-in: a fixed answer, no network or accounts.
pub struct Stub;

pub const STUB_REPLY: &str = "This is a pretend answer. The radio bridge brought me your words.";

#[async_trait]
impl TextAgent for Stub {
    fn label(&self) -> String {
        "stub (offline pretend answer)".into()
    }

    async fn reply(&self, _request: Request<'_>) -> anyhow::Result<String> {
        Ok(STUB_REPLY.into())
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    /// Answers from a script and records what it was asked.
    #[derive(Clone, Default)]
    pub struct Scripted {
        pub answers: Arc<Mutex<VecDeque<anyhow::Result<String>>>>,
        pub seen: Arc<Mutex<Vec<(Vec<Turn>, String)>>>,
    }

    impl Scripted {
        pub fn new(answers: Vec<anyhow::Result<String>>) -> Scripted {
            Scripted {
                answers: Arc::new(Mutex::new(answers.into())),
                seen: Arc::default(),
            }
        }
    }

    #[async_trait]
    impl TextAgent for Scripted {
        fn label(&self) -> String {
            "scripted".into()
        }

        async fn reply(&self, request: Request<'_>) -> anyhow::Result<String> {
            self.seen
                .lock()
                .unwrap()
                .push((request.history.to_vec(), request.traffic.to_string()));
            self.answers.lock().unwrap().pop_front().unwrap_or_else(|| Ok("ok".into()))
        }
    }

    fn config(turns: usize) -> Config {
        let text = format!(
            "{}\n[agent]\nhistory_turns = {turns}\nmax_reply_chars = 20\n",
            crate::config::tests_support::MINIMAL
        );
        Config::parse(&text, "/".into()).unwrap()
    }

    #[tokio::test]
    async fn history_is_bounded_and_oldest_dropped() {
        let agent = Scripted::default();
        let mut convo = Conversation::new(Box::new(agent.clone()), &config(2), false);
        for q in ["one", "two", "three"] {
            let reply = convo.ask(q).await.unwrap();
            convo.commit(reply);
        }
        let users: Vec<_> = convo.history().map(|t| t.user.as_str()).collect();
        assert_eq!(users, ["two", "three"]);
    }

    #[tokio::test]
    async fn uncommitted_or_failed_turns_are_not_remembered() {
        let agent = Scripted::new(vec![Ok("first".into()), Err(anyhow::anyhow!("boom")), Ok("unheard".into()), Ok("x".into())]);
        let mut convo = Conversation::new(Box::new(agent.clone()), &config(8), false);
        let first = convo.ask("a").await.unwrap();
        convo.commit(first);
        assert!(convo.ask("b").await.is_err());
        let _unheard = convo.ask("c").await.unwrap();
        let _ = convo.ask("d").await.unwrap();
        let seen = agent.seen.lock().unwrap();
        assert_eq!(seen[3].0.len(), 1, "only the committed turn is sent as history");
        assert_eq!(seen[3].0[0].assistant, "first");
    }

    #[tokio::test]
    async fn oversized_or_empty_replies_are_rejected() {
        let agent = Scripted::new(vec![Ok("x".repeat(21)), Ok("   ".into()), Ok("bad\u{7}".into())]);
        let convo = Conversation::new(Box::new(agent), &config(8), false);
        for _ in 0..3 {
            assert!(convo.ask("q").await.is_err());
        }
    }

    #[test]
    fn replies_are_flattened_to_one_line() {
        assert_eq!(clean_reply(" a\n\tb  c ", 100).unwrap(), "a b c");
    }

    #[tokio::test]
    async fn traffic_limits_apply() {
        let convo = Conversation::new(Box::new(Stub), &config(8), false);
        assert!(convo.ask("   ").await.is_err());
        assert!(convo.ask(&"x".repeat(MAX_TRAFFIC_CHARS + 1)).await.is_err());
    }
}
