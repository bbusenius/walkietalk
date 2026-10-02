//! xAI text-to-speech, with a saved Grok login or a billed API key.

use std::time::Duration;

use anyhow::bail;
use async_trait::async_trait;
use reqwest::StatusCode;

use super::{Shaping, Voice, check_text};
use crate::audio::{Clip, Fit};
use crate::config::GrokVoiceConfig;
use crate::credentials::Secret;
use crate::http;
use crate::xai::Auth;

pub struct GrokTts {
    auth: Auth,
    base: String,
    voice: GrokVoiceConfig,
    shaping: Shaping,
    client: reqwest::Client,
}

impl GrokTts {
    pub fn new(auth: Auth, voice: GrokVoiceConfig, shaping: Shaping) -> GrokTts {
        GrokTts::with_base(auth, crate::xai::API_BASE.into(), voice, shaping)
    }

    pub fn with_base(
        auth: Auth,
        base: String,
        voice: GrokVoiceConfig,
        shaping: Shaping,
    ) -> GrokTts {
        GrokTts {
            auth,
            base,
            voice,
            shaping,
            client: http::client(Duration::from_secs(10)),
        }
    }

    fn max_bytes(&self) -> usize {
        (self.shaping.decode_limit().as_secs_f64() * 48_000.0 * 2.0) as usize + 64 * 1024
    }

    async fn post(
        &self,
        text: &str,
        token: &Secret,
    ) -> anyhow::Result<(StatusCode, Option<String>, Vec<u8>)> {
        let response = self
            .client
            .post(format!("{}/v1/tts", self.base))
            .bearer_auth(token.expose())
            .json(&serde_json::json!({
                "text": text,
                "voice_id": self.voice.voice,
                "language": self.voice.language,
                "speed": self.voice.speed,
                "text_normalization": false,
                "output_format": {"codec": "wav", "sample_rate": 48000},
            }))
            .send()
            .await
            .map_err(|err| anyhow::anyhow!("Grok voice {}", http::without_url(&err)))?;
        let status = response.status();
        let kind = response
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .map(|v| {
                v.split(';')
                    .next()
                    .unwrap_or("")
                    .trim()
                    .to_ascii_lowercase()
            });
        let body = http::body(response, self.max_bytes(), "Grok voice").await?;
        Ok((status, kind, body))
    }

    async fn run(&self, text: &str, fit: Fit) -> anyhow::Result<Clip> {
        let token = self.auth.token(&self.client).await?;
        let (mut status, mut kind, mut body) = self.post(text, &token).await?;
        if status == StatusCode::UNAUTHORIZED
            && let Some(token) = self.auth.retry_token(&self.client).await
        {
            (status, kind, body) = self.post(text, &token?).await?;
        }
        match status {
            s if s.is_success() => {}
            s @ (StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN) => {
                bail!(
                    "Grok voice refused access (HTTP {}); {}",
                    s.as_u16(),
                    self.auth.remedy()
                )
            }
            s => bail!(
                "Grok voice failed (HTTP {}); check tts.grok voice, language, and speed. Details withheld",
                s.as_u16()
            ),
        }
        let audio_kind = matches!(
            kind.as_deref(),
            Some("audio/wav" | "audio/x-wav" | "audio/wave" | "application/octet-stream")
        );
        if !audio_kind {
            bail!("Grok voice returned something other than WAV audio");
        }
        let clip = Clip::from_wav_bytes(&body, self.shaping.decode_limit())?;
        self.shaping.finish(clip, fit)
    }
}

#[async_trait]
impl Voice for GrokTts {
    fn label(&self) -> String {
        format!(
            "grok ({}, speed {}; {})",
            self.voice.voice,
            self.voice.speed,
            self.auth.describe()
        )
    }

    async fn prepare(&self) -> anyhow::Result<()> {
        self.auth.check()
    }

    async fn synthesize(&self, text: &str, fit: Fit) -> anyhow::Result<Clip> {
        let text = check_text(text)?;
        http::within(self.shaping.timeout, "Grok voice", self.run(&text, fit)).await
    }
}
