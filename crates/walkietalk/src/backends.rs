//! Choosing backends from the config.

use anyhow::bail;

use crate::agent::{self, TextAgent};
use crate::config::{AgentBackend, Config, SttBackend, TtsBackend};
use crate::credentials::Credentials;
use crate::grok_login::GrokLogin;
use crate::xai::Auth;
use crate::stt::{self, Transcriber};
use crate::tts::{self, Shaping, Voice};

pub fn text_agent(config: &Config, creds: &Credentials) -> anyhow::Result<Box<dyn TextAgent>> {
    let a = &config.agent;
    Ok(match a.backend {
        AgentBackend::Stub => Box::new(agent::Stub),
        AgentBackend::Hermes => Box::new(agent::hermes::Hermes::new(&a.hermes.url, &a.hermes.token_env, creds.clone(), a.timeout())),
        AgentBackend::Codex => Box::new(agent::codex::Codex::new(a.codex.clone(), a.timeout())),
        AgentBackend::Grok => Box::new(agent::grok_cli::GrokCli::new(a.grok.clone(), a.timeout())),
        AgentBackend::Claude => Box::new(agent::claude_cli::ClaudeCli::new(a.claude.clone(), a.timeout())),
        AgentBackend::ClaudeApi => Box::new(agent::claude_api::ClaudeApi::new(
            &a.claude_api.key_env,
            creds.clone(),
            &a.claude_api.model,
            a.claude_api.reasoning_effort,
            a.max_reply_chars,
            a.timeout(),
        )),
        AgentBackend::GrokRealtime => bail!(
            "agent.backend grok-realtime is a speech-to-speech session, not a text agent; use voice-agent-check or talk"
        ),
    })
}

pub fn transcriber(config: &Config, creds: &Credentials) -> anyhow::Result<Box<dyn Transcriber>> {
    let remote = |auth| {
        Box::new(stt::grok::GrokStt::new(auth, config.stt.timeout(), config.stt.max_response_bytes, keyterms(config)))
    };
    Ok(match config.stt.backend {
        SttBackend::Whisper => Box::new(stt::whisper::Whisper::new(config.stt.model, config.stt.timeout())),
        SttBackend::Grok => remote(Auth::Login(GrokLogin::locate())),
        SttBackend::GrokApi => remote(Auth::Key { env: config.stt.api_key_env.clone(), creds: creds.clone() }),
    })
}

/// Names the recognizer should expect: wake names and sleep phrases.
/// The shutdown code is deliberately left out.
pub fn keyterms(config: &Config) -> Vec<String> {
    let mut terms: Vec<String> = std::iter::once(&config.wake.name).chain(&config.wake.aliases).cloned().collect();
    for (_, contact) in config.messaging.enabled() {
        terms.extend(std::iter::once(&contact.wake).chain(&contact.aliases).cloned());
    }
    terms.extend(config.sleep_phrases().into_iter().map(String::from));
    terms
}

pub fn voice(config: &Config, creds: &Credentials) -> anyhow::Result<Box<dyn Voice>> {
    let shaping = Shaping::from_config(config);
    let grok = |auth| Box::new(tts::grok::GrokTts::new(auth, config.tts.grok.clone(), shaping));
    Ok(match config.tts.backend {
        TtsBackend::Piper => Box::new(tts::piper::Piper::new(&config.tts.piper.executable, config.piper_model(), shaping)),
        TtsBackend::Grok => grok(Auth::Login(GrokLogin::locate())),
        TtsBackend::GrokApi => grok(Auth::Key { env: config.tts.grok.key_env.clone(), creds: creds.clone() }),
        TtsBackend::Hermes => Box::new(tts::hermes::HermesTts::new(
            &config.tts.hermes.url,
            &config.tts.hermes.token_env,
            creds.clone(),
            shaping,
        )),
    })
}
