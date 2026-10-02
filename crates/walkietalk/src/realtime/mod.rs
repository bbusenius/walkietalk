//! Grok realtime: speech-to-speech that replaces separate recognition,
//! agent, and voice backends.
//!
//! During `talk`, captured audio streams to the session while someone
//! speaks. When the utterance ends, the audio is committed and the server's
//! own transcript goes through the same shutdown, sleep, and wake gates as
//! any other backend. Rejected and control utterances are deleted from the
//! remote conversation before any reply is requested.

pub mod client;
pub mod tx;

use std::collections::VecDeque;
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{Context, bail};
use serde_json::{Value, json};

use crate::agent::instructions::{self, Limits};
use crate::audio::stream::StreamResampler;
use crate::config::{Config, RealtimeConfig};
use crate::credentials::{Credentials, Secret};
use crate::radio::{Radio, TxError};
use client::{Client, Event, Role};
use tx::{RATE, StreamTx};

/// About 100 ms of 24 kHz audio per append.
const APPEND_SAMPLES: usize = RATE as usize / 10;
/// Wait for the first response event after asking for a reply.
const RESPONSE_START: Duration = Duration::from_secs(15);
/// Wait for the input transcript after committing audio.
const TRANSCRIPT_WAIT: Duration = Duration::from_secs(5);
/// An empty final transcript can arrive just before the real one.
const EMPTY_GRACE: Duration = Duration::from_millis(400);

/// No transcript came back for the committed audio.
#[derive(Debug, thiserror::Error)]
#[error("no transcript arrived for that audio")]
pub struct NoTranscript;

/// What came back for one reply.
#[derive(Debug, Default)]
pub struct Reply {
    pub heard: String,
    pub said: String,
    pub audio: Vec<i16>,
    pub audible: bool,
    pub truncated: bool,
}

/// Settings for every realtime connection.
#[derive(Clone)]
pub struct Settings {
    pub rt: RealtimeConfig,
    pub key: Secret,
    pub web_search: bool,
    pub history_turns: usize,
    pub instructions: String,
    pub keyterms: Vec<String>,
}

impl Settings {
    pub fn from_config(config: &Config, creds: &Credentials) -> anyhow::Result<Settings> {
        let key = creds
            .token(&config.agent.realtime.key_env)
            .context("grok-realtime needs a billed xAI API key; a Grok login is never used")?;
        let limits = Limits {
            max_reply_chars: config.agent.max_reply_chars,
            spoken_seconds: config.radio.speech_budget().as_secs_f64(),
        };
        let guidance = instructions::guidance(&config.agent.instructions, limits, true);
        Ok(Settings {
            rt: config.agent.realtime.clone(),
            key,
            web_search: config.agent.web_search,
            history_turns: config.agent.history_turns,
            instructions: routing_instructions(&guidance, config),
            keyterms: crate::backends::keyterms(config),
        })
    }

    fn session(&self, instructions: Option<&str>, tools: bool, keyterms: bool) -> Value {
        let mut session = json!({
            "voice": self.rt.voice,
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": RATE}},
                "output": {"format": {"type": "audio/pcm", "rate": RATE}},
            },
            // The bridge decides when a turn ends (half duplex).
            "turn_detection": null,
        });
        if let Some(text) = instructions {
            session["instructions"] = json!(text);
        }
        if tools && self.web_search {
            session["tools"] = json!([{"type": "web_search"}]);
        }
        if keyterms && !self.keyterms.is_empty() {
            let terms: Vec<&String> = self.keyterms.iter().filter(|t| t.chars().count() <= 50).take(100).collect();
            session["audio"]["input"]["transcription"] = json!({"keyterms": terms});
        }
        session
    }

    async fn connect(&self) -> anyhow::Result<Client> {
        Client::connect(&self.rt.url, &self.rt.model, &self.key, self.rt.connect_timeout()).await
    }
}

/// The wake phrase stays in the audio, so tell the model to treat it as a
/// routing prefix rather than part of the question or its own name.
fn routing_instructions(guidance: &str, config: &Config) -> String {
    let mut names: Vec<&str> = vec![config.wake.name.as_str()];
    names.extend(config.wake.aliases.iter().map(String::as_str));
    names.dedup();
    let names = serde_json::to_string(&names).expect("names serialize");
    format!(
        "{guidance}\n\n## Radio routing\nThe radio bridge has already accepted this request. The bridge uses these wake \
         phrases as routing prefixes: {names}. They do not define your name, identity, or persona. When one occurs at the \
         start of the audio, interpret the request as if that prefix had been removed. Answer the request that follows \
         directly. Do not repeat, acknowledge, explain, or correct the wake phrase merely because it was used. Do not add \
         a greeting because a wake phrase was used. Answer follow-ups without a wake phrase normally. For questions such \
         as \"Who are you?\", use your established identity or configured persona; never infer one from the wake phrases. \
         Discuss a wake phrase only when the user explicitly asks about it."
    )
}

/// Read a response, driving the transmitter, until `response.done`.
async fn receive(client: &mut Client, tx: &mut StreamTx, idle: Duration) -> anyhow::Result<Reply> {
    let result = receive_inner(client, tx, idle).await;
    if result.is_err() {
        // Release the radio before anything else.
        tx.abort();
    }
    result
}

async fn receive_inner(client: &mut Client, tx: &mut StreamTx, idle: Duration) -> anyhow::Result<Reply> {
    tx.prepare().await?;
    let mut reply = Reply::default();
    let mut said = String::new();
    let mut first = true;
    loop {
        let mut wait = if first { RESPONSE_START } else { idle };
        if let Some(left) = tx.time_left() {
            wait = wait.min(left + Duration::from_millis(200));
        }
        let event = match client.next(wait).await {
            Ok(event) => event,
            Err(_) if tx.keyed() => {
                tx.truncated = true;
                tx.abort();
                break;
            }
            Err(err) if first => {
                return Err(err.context("the voice produced no response; the audio may not have been intelligible speech"));
            }
            Err(err) => return Err(err),
        };
        first = false;
        match event {
            Event::AudioDelta(pcm) => tx.audio(pcm).await?,
            Event::AudioDone | Event::ToolCall => tx.end_segment().await?,
            Event::OutputTranscriptDelta(text) => said.push_str(&text),
            Event::OutputTranscriptDone(text) => reply.said = text,
            Event::InputTranscript { text, .. } if !text.is_empty() => reply.heard = text,
            Event::ResponseDone { status } => {
                tx.end_segment().await?;
                if status != "completed" {
                    bail!("the voice response ended with status {status}");
                }
                break;
            }
            _ => {}
        }
        if tx.truncated {
            tx.abort();
            break;
        }
    }
    if reply.said.is_empty() {
        reply.said = said;
    }
    reply.said = reply.said.trim().to_string();
    reply.audio = std::mem::take(&mut tx.audio);
    reply.audible = tx.audible;
    reply.truncated = tx.truncated;
    Ok(reply)
}

/// Speak fixed text in the realtime voice on a fresh connection. Used for
/// confirmations and voice station IDs.
pub async fn speak(settings: &Settings, text: &str, radio: Option<Arc<Radio>>) -> anyhow::Result<Reply> {
    let mut client = settings.connect().await?;
    let result = async {
        client.configure(settings.session(None, false, false), settings.rt.connect_timeout()).await?;
        client
            .send(json!({
                "type": "conversation.item.create",
                "item": {
                    "type": "force_message",
                    "role": "assistant",
                    "interruptible": false,
                    "content": [{"type": "output_text", "text": text}],
                },
            }))
            .await?;
        let mut tx = StreamTx::new(radio);
        receive(&mut client, &mut tx, settings.rt.idle_timeout()).await
    }
    .await;
    client.close().await;
    result
}

/// One complete turn from recorded audio, on a fresh connection.
pub async fn single_turn(settings: &Settings, audio: &crate::audio::Clip, radio: Option<Arc<Radio>>) -> anyhow::Result<Reply> {
    let mut client = settings.connect().await?;
    let result = async {
        client
            .configure(settings.session(Some(&settings.instructions), true, true), settings.rt.connect_timeout())
            .await?;
        let pcm = audio.resample(RATE).into_samples();
        anyhow::ensure!(!pcm.is_empty(), "the recording is empty");
        for chunk in pcm.chunks(APPEND_SAMPLES) {
            client.append_audio(chunk).await?;
        }
        client.send(json!({"type": "input_audio_buffer.commit"})).await?;
        client
            .wait_for(settings.rt.idle_timeout(), |e| matches!(e, Event::Committed { .. }))
            .await?;
        client.send(json!({"type": "response.create"})).await?;
        let mut tx = StreamTx::new(radio);
        receive(&mut client, &mut tx, settings.rt.idle_timeout()).await
    }
    .await;
    client.close().await;
    result
}

/// The warm connection used by `talk`.
pub struct Session {
    settings: Settings,
    client: Option<Client>,
    upload: StreamResampler,
    pending: Vec<i16>,
    appended: usize,
    committed: Option<String>,
    /// Item IDs of each completed turn, oldest first.
    turns: VecDeque<Vec<String>>,
}

impl Session {
    pub fn new(settings: Settings) -> Session {
        Session {
            settings,
            client: None,
            upload: StreamResampler::new(48_000, RATE),
            pending: Vec::new(),
            appended: 0,
            committed: None,
            turns: VecDeque::new(),
        }
    }

    pub fn label(&self) -> String {
        format!("grok-realtime ({}, voice {}; {}, billed API)", self.settings.rt.model, self.settings.rt.voice, self.settings.rt.key_env)
    }

    pub fn settings(&self) -> &Settings {
        &self.settings
    }

    pub fn connected(&self) -> bool {
        self.client.as_ref().is_some_and(Client::alive)
    }

    /// Connect and configure, if not already connected.
    pub async fn connect(&mut self) -> anyhow::Result<()> {
        if self.connected() {
            return Ok(());
        }
        self.reset().await;
        let mut client = self.settings.connect().await?;
        let session = self.settings.session(Some(&self.settings.instructions), true, true);
        client.configure(session, self.settings.rt.connect_timeout()).await?;
        self.client = Some(client);
        Ok(())
    }

    /// Drop the connection and the remote conversation with it.
    pub async fn reset(&mut self) {
        if let Some(client) = self.client.take() {
            client.close().await;
        }
        self.pending.clear();
        self.appended = 0;
        self.committed = None;
        self.turns.clear();
    }

    fn client(&mut self) -> anyhow::Result<&mut Client> {
        self.client.as_mut().context("the realtime session is not connected")
    }

    /// Start a new utterance captured at `rate`.
    pub fn begin(&mut self, rate: u32) {
        self.upload = StreamResampler::new(rate, RATE);
        self.pending.clear();
        self.appended = 0;
        self.committed = None;
    }

    /// Stream captured audio to the server.
    pub async fn append(&mut self, samples: &[i16]) -> anyhow::Result<()> {
        let converted = self.upload.push(samples);
        self.pending.extend(converted);
        while self.pending.len() >= APPEND_SAMPLES {
            let chunk: Vec<i16> = self.pending.drain(..APPEND_SAMPLES).collect();
            self.client()?.append_audio(&chunk).await?;
            self.appended += chunk.len();
        }
        Ok(())
    }

    async fn flush(&mut self) -> anyhow::Result<()> {
        let mut rest = std::mem::take(&mut self.pending);
        rest.extend(self.upload.flush());
        if !rest.is_empty() {
            self.client()?.append_audio(&rest).await?;
            self.appended += rest.len();
        }
        Ok(())
    }

    /// Throw away uncommitted or committed audio for a rejected utterance.
    pub async fn discard(&mut self) -> anyhow::Result<()> {
        self.pending.clear();
        let timeout = self.settings.rt.idle_timeout();
        if let Some(item) = self.committed.take() {
            self.client()?.delete(&item, timeout).await?;
        } else if self.appended > 0 {
            self.client()?.send(json!({"type": "input_audio_buffer.clear"})).await?;
        }
        self.appended = 0;
        Ok(())
    }

    /// Commit the utterance and return the server's transcript of it.
    pub async fn transcript(&mut self) -> anyhow::Result<String> {
        self.flush().await?;
        anyhow::ensure!(self.appended > 0, "no audio was streamed for this utterance");
        let wait = TRANSCRIPT_WAIT.min(self.settings.rt.idle_timeout());
        let client = self.client.as_mut().context("the realtime session is not connected")?;
        client.send(json!({"type": "input_audio_buffer.commit"})).await?;
        let deadline = Instant::now() + wait;
        let mut committed: Option<String> = None;
        let mut partial: std::collections::HashMap<String, String> = Default::default();
        let mut empty_at: Option<Instant> = None;
        let outcome = loop {
            let now = Instant::now();
            let mut left = deadline.saturating_duration_since(now);
            if let Some(at) = empty_at {
                left = left.min(EMPTY_GRACE.saturating_sub(now - at));
            }
            if left.is_zero() {
                break None;
            }
            let event = match client.next(left).await {
                Ok(event) => event,
                Err(_) if committed.is_some() => break None,
                Err(err) => return Err(err),
            };
            match event {
                Event::Committed { item_id } => committed = Some(item_id),
                Event::InputTranscriptFailed => bail!("the realtime transcription failed"),
                Event::InputTranscript { item_id, text, done } => {
                    if !text.is_empty() {
                        partial.insert(item_id.clone(), text.clone());
                    }
                    if done && committed.as_deref() == Some(item_id.as_str()) {
                        if !text.is_empty() {
                            break Some(text);
                        }
                        if let Some(text) = partial.get(&item_id) {
                            break Some(text.clone());
                        }
                        empty_at = Some(Instant::now());
                    }
                }
                _ => {}
            }
        };
        self.committed = committed.clone();
        match (outcome, committed) {
            (Some(text), _) => Ok(text),
            (None, Some(item)) => match partial.remove(&item) {
                Some(text) => Ok(text),
                None if empty_at.is_some() => Ok(String::new()),
                None => Err(NoTranscript.into()),
            },
            (None, None) => Err(NoTranscript.into()),
        }
    }

    /// Ask for a reply to the committed audio and transmit it as it streams.
    /// An interrupted or silent reply resets the conversation so the next
    /// turn starts fresh.
    pub async fn respond(&mut self, radio: Option<Arc<Radio>>) -> anyhow::Result<Reply> {
        anyhow::ensure!(self.committed.is_some(), "no committed audio to answer");
        let idle = self.settings.rt.idle_timeout();
        let result = async {
            let client = self.client()?;
            client.send(json!({"type": "response.create"})).await?;
            let mut tx = StreamTx::new(radio);
            receive(client, &mut tx, idle).await
        }
        .await;
        self.committed = None;
        self.appended = 0;
        match &result {
            Ok(reply) if reply.audible && !reply.truncated => self.keep_turn().await,
            _ => self.reset().await,
        }
        result
    }

    /// Remember this turn's items and delete the oldest beyond the limit.
    async fn keep_turn(&mut self) {
        let Some(client) = self.client.as_mut() else { return };
        let kept: std::collections::HashSet<&String> = self.turns.iter().flatten().collect();
        let current: Vec<String> = client.order.iter().filter(|id| !kept.contains(id)).cloned().collect();
        let roles: Vec<Role> = current.iter().filter_map(|id| client.items.get(id).copied()).collect();
        if !(roles.contains(&Role::User) && roles.contains(&Role::Assistant)) {
            // Without both halves the turn cannot be removed cleanly later.
            self.reset().await;
            return;
        }
        self.turns.push_back(current);
        let timeout = self.settings.rt.connect_timeout();
        while self.turns.len() > self.settings.history_turns {
            let oldest = self.turns.pop_front().expect("non-empty");
            for item in oldest {
                let Some(client) = self.client.as_mut() else { return };
                if client.delete(&item, timeout).await.is_err() {
                    // The reply already aired; start fresh rather than keep unbounded history.
                    self.reset().await;
                    return;
                }
            }
        }
    }
}

/// Whether an error means the transmitter hardware failed.
pub fn is_ptt_fault(err: &anyhow::Error) -> bool {
    err.downcast_ref::<TxError>().is_some_and(TxError::is_fatal)
}

/// Whether an error means the API key was refused.
pub fn is_auth_error(err: &anyhow::Error) -> bool {
    err.chain().any(|e| e.downcast_ref::<client::AuthError>().is_some())
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use crate::radio::ptt::fake::FakeLine;
    use crate::radio::tests::{FakeOut, radio};
    use base64::Engine;
    use futures_util::{SinkExt, StreamExt};
    use std::sync::Mutex;
    use tokio_tungstenite::tungstenite::Message;

    /// A scripted stand-in for the xAI realtime server.
    pub async fn fake_server(transcripts: Vec<&'static str>) -> (String, Arc<Mutex<Vec<String>>>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("ws://{}/v1/realtime", listener.local_addr().unwrap());
        let log = Arc::new(Mutex::new(Vec::new()));
        let record = log.clone();
        let transcripts = Arc::new(Mutex::new(VecDeque::from(transcripts)));
        tokio::spawn(async move {
            let mut n = 0;
            while let Ok((stream, _)) = listener.accept().await {
                let (record, transcripts) = (record.clone(), transcripts.clone());
                n += 100;
                let base = n;
                tokio::spawn(async move {
                    let mut ws = tokio_tungstenite::accept_async(stream).await.unwrap();
                    let mut turn = base;
                    let tone: Vec<u8> = (0..4800).flat_map(|i: i32| (if i % 2 == 0 { 8000i16 } else { -8000 }).to_le_bytes()).collect();
                    let delta = base64::engine::general_purpose::STANDARD.encode(tone);
                    while let Some(Ok(Message::Text(text))) = ws.next().await {
                        let msg: Value = serde_json::from_str(&text).unwrap();
                        let kind = msg["type"].as_str().unwrap().to_string();
                        if kind != "input_audio_buffer.append" {
                            record.lock().unwrap().push(kind.clone());
                        }
                        let mut out = vec![];
                        match kind.as_str() {
                            "session.update" => out.push(json!({"type": "session.updated"})),
                            "input_audio_buffer.commit" => {
                                turn += 1;
                                let id = format!("u{turn}");
                                let words = transcripts.lock().unwrap().pop_front().unwrap_or("");
                                out.push(json!({"type": "input_audio_buffer.committed", "item_id": id}));
                                out.push(json!({"type": "conversation.item.input_audio_transcription.completed", "item_id": id, "transcript": words}));
                            }
                            "conversation.item.delete" => out.push(json!({"type": "conversation.item.deleted", "item_id": msg["item_id"]})),
                            "response.create" | "conversation.item.create" => {
                                out.push(json!({"type": "response.output_item.added", "item": {"id": format!("a{turn}"), "role": "assistant"}}));
                                out.push(json!({"type": "response.output_audio.delta", "delta": delta}));
                                out.push(json!({"type": "response.output_audio.done"}));
                                out.push(json!({"type": "response.output_audio_transcript.done", "transcript": "Reply."}));
                                out.push(json!({"type": "response.done", "response": {"status": "completed"}}));
                            }
                            _ => {}
                        }
                        for event in out {
                            ws.send(Message::Text(event.to_string().into())).await.unwrap();
                        }
                    }
                });
            }
        });
        (url, log)
    }

    pub fn settings(url: String, history_turns: usize) -> Settings {
        Settings {
            rt: RealtimeConfig { url, ..Default::default() },
            key: Secret::new("test-key"),
            web_search: false,
            history_turns,
            instructions: "Be brief.".into(),
            keyterms: vec!["charlotte".into()],
        }
    }

    fn speech() -> Vec<i16> {
        (0..9600).map(|i| if i % 2 == 0 { 3000 } else { -3000 }).collect()
    }

    #[tokio::test]
    async fn rejected_utterances_are_deleted_before_any_reply() {
        let (url, log) = fake_server(vec!["what time is it"]).await;
        let mut session = Session::new(settings(url, 4));
        session.connect().await.unwrap();
        session.begin(48_000);
        session.append(&speech()).await.unwrap();
        assert_eq!(session.transcript().await.unwrap(), "what time is it");
        session.discard().await.unwrap();
        let log = log.lock().unwrap();
        assert!(log.contains(&"conversation.item.delete".to_string()));
        assert!(!log.contains(&"response.create".to_string()));
    }

    #[tokio::test]
    async fn accepted_turn_keys_the_radio_and_old_turns_are_pruned() {
        let (url, log) = fake_server(vec!["charlotte one", "charlotte two"]).await;
        let (line, out) = (FakeLine::default(), FakeOut::default());
        let radio = Arc::new(radio(&line, &out, 5000));
        let mut session = Session::new(settings(url, 1));
        session.connect().await.unwrap();
        for _ in 0..2 {
            session.begin(48_000);
            session.append(&speech()).await.unwrap();
            session.transcript().await.unwrap();
            let reply = session.respond(Some(radio.clone())).await.unwrap();
            assert!(reply.audible && !reply.truncated);
            assert_eq!(reply.said, "Reply.");
        }
        assert!(line.changes().contains(&true));
        assert!(!line.keyed());
        let deletes = log.lock().unwrap().iter().filter(|k| *k == "conversation.item.delete").count();
        assert_eq!(deletes, 2, "the first turn's user and assistant items are removed");
    }

    #[tokio::test]
    async fn fixed_text_is_spoken_on_a_fresh_connection() {
        let (url, log) = fake_server(vec![]).await;
        let reply = speak(&settings(url, 4), "Standing by.", None).await.unwrap();
        assert!(reply.audible);
        assert!(log.lock().unwrap().contains(&"conversation.item.create".to_string()));
    }
}
