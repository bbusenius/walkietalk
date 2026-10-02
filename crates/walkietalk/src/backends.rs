//! Choosing backends from the config.

use anyhow::bail;

use crate::agent::{self, TextAgent};
use crate::config::{AgentBackend, Config, SttBackend, TtsBackend};
use crate::credentials::Credentials;
use crate::stt::{self, Transcriber};
use crate::tts::{self, Shaping, Voice};

pub fn text_agent(config: &Config, _creds: &Credentials) -> anyhow::Result<Box<dyn TextAgent>> {
    Ok(match config.agent.backend {
        AgentBackend::Stub => Box::new(agent::Stub),
        AgentBackend::GrokRealtime => bail!(
            "agent.backend grok-realtime is a speech-to-speech session, not a text agent; use voice-agent-check or talk"
        ),
        other => bail!("agent.backend {other} is not available yet"),
    })
}

pub fn transcriber(config: &Config, _creds: &Credentials) -> anyhow::Result<Box<dyn Transcriber>> {
    Ok(match config.stt.backend {
        SttBackend::Whisper => Box::new(stt::whisper::Whisper::new(config.stt.model, config.stt.timeout())),
        other => bail!("stt.backend {other} is not available yet"),
    })
}

pub fn voice(config: &Config, _creds: &Credentials) -> anyhow::Result<Box<dyn Voice>> {
    let shaping = Shaping::from_config(config);
    Ok(match config.tts.backend {
        TtsBackend::Piper => Box::new(tts::piper::Piper::new(&config.tts.piper.executable, config.piper_model(), shaping)),
        other => bail!("tts.backend {other} is not available yet"),
    })
}
