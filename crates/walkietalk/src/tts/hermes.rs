//! Speech from a Hermes installation, through the walkietalk speech
//! companion. Hermes keeps its own provider, voice, and credentials.

use std::time::Duration;

use anyhow::bail;
use async_trait::async_trait;
use reqwest::StatusCode;

use super::{Shaping, Voice, check_text};
use crate::audio::{Clip, Fit, TooLong};
use crate::credentials::Credentials;
use crate::http;

pub struct HermesTts {
    url: String,
    token_env: String,
    creds: Credentials,
    shaping: Shaping,
    client: reqwest::Client,
}

impl HermesTts {
    pub fn new(url: &str, token_env: &str, creds: Credentials, shaping: Shaping) -> HermesTts {
        HermesTts {
            url: url.trim_end_matches('/').to_string(),
            token_env: token_env.to_string(),
            creds,
            shaping,
            client: http::client(Duration::from_secs(10)),
        }
    }

    async fn run(&self, text: &str, fit: Fit) -> anyhow::Result<Clip> {
        let token = self.creds.token(&self.token_env)?;
        let response = self
            .client
            .post(format!("{}/v1/speech", self.url))
            .bearer_auth(token.expose())
            .json(&serde_json::json!({
                "text": text,
                "max_seconds": self.shaping.budget.as_secs_f64(),
                "crop": fit == Fit::Crop,
            }))
            .send()
            .await
            .map_err(|err| anyhow::anyhow!("Hermes speech {}; check tts.hermes.url and that the companion is running", http::without_url(&err)))?;
        match response.status() {
            StatusCode::OK => {}
            s @ (StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN) => {
                bail!("Hermes speech refused the service token (HTTP {}); check {}", s.as_u16(), self.token_env)
            }
            StatusCode::PAYLOAD_TOO_LARGE => bail!(TooLong {
                actual: self.shaping.budget + Duration::from_millis(1),
                limit: self.shaping.budget,
            }),
            StatusCode::SERVICE_UNAVAILABLE => bail!("Hermes speech is busy with another request"),
            StatusCode::GATEWAY_TIMEOUT => bail!("Hermes speech timed out"),
            s => bail!("Hermes speech failed (HTTP {}); check the companion and its Hermes provider", s.as_u16()),
        }
        let max = (self.shaping.decode_limit().as_secs_f64() * 48_000.0 * 2.0) as usize + 64 * 1024;
        let body = http::body(response, max, "Hermes speech").await?;
        let clip = Clip::from_wav_bytes(&body, self.shaping.decode_limit())?;
        self.shaping.finish(clip, fit)
    }
}

#[async_trait]
impl Voice for HermesTts {
    fn label(&self) -> String {
        "hermes (voice configured in Hermes)".into()
    }

    async fn prepare(&self) -> anyhow::Result<()> {
        self.creds.token(&self.token_env).map(|_| ())
    }

    async fn synthesize(&self, text: &str, fit: Fit) -> anyhow::Result<Clip> {
        let text = check_text(text)?;
        http::within(self.shaping.timeout, "Hermes speech", self.run(&text, fit)).await
    }
}
