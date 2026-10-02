//! xAI speech-to-text, with a saved Grok login or a billed API key.

use std::time::Duration;

use anyhow::bail;
use async_trait::async_trait;
use reqwest::StatusCode;
use reqwest::multipart::{Form, Part};

use super::Transcriber;
use crate::audio::Clip;
use crate::credentials::Secret;
use crate::http;
use crate::xai::Auth;

const MODEL: &str = "grok-voice-transcribe-2.0";
const MAX_KEYTERMS: usize = 100;
const MAX_KEYTERM_CHARS: usize = 50;

pub struct GrokStt {
    auth: Auth,
    base: String,
    timeout: Duration,
    max_response: usize,
    keyterms: Vec<String>,
    client: reqwest::Client,
}

impl GrokStt {
    pub fn new(auth: Auth, timeout: Duration, max_response: usize, keyterms: Vec<String>) -> GrokStt {
        GrokStt::with_base(auth, crate::xai::API_BASE.into(), timeout, max_response, keyterms)
    }

    pub fn with_base(auth: Auth, base: String, timeout: Duration, max_response: usize, keyterms: Vec<String>) -> GrokStt {
        let mut seen = std::collections::HashSet::new();
        let keyterms = keyterms
            .into_iter()
            .map(|t| t.split_whitespace().collect::<Vec<_>>().join(" "))
            .filter(|t| !t.is_empty() && t.chars().count() <= MAX_KEYTERM_CHARS && seen.insert(t.to_lowercase()))
            .take(MAX_KEYTERMS)
            .collect();
        GrokStt {
            auth,
            base,
            timeout,
            max_response,
            keyterms,
            client: http::client(Duration::from_secs(10)),
        }
    }

    async fn post(&self, wav: &[u8], token: &Secret) -> anyhow::Result<(StatusCode, Vec<u8>)> {
        let mut form = Form::new()
            .text("model", MODEL)
            .text("language", "en")
            .text("format", "true");
        for term in &self.keyterms {
            form = form.text("keyterm", term.clone());
        }
        let file = Part::bytes(wav.to_vec()).file_name("utterance.wav").mime_str("audio/wav")?;
        form = form.part("file", file);
        let response = self
            .client
            .post(format!("{}/v1/stt", self.base))
            .bearer_auth(token.expose())
            .multipart(form)
            .send()
            .await
            .map_err(|err| anyhow::anyhow!("Grok speech-to-text {}", http::without_url(&err)))?;
        let status = response.status();
        let body = http::body(response, self.max_response, "Grok speech-to-text").await?;
        Ok((status, body))
    }

    async fn run(&self, audio: &Clip) -> anyhow::Result<String> {
        let wav = audio.to_wav_bytes();
        let token = self.auth.token(&self.client).await?;
        let (mut status, mut body) = self.post(&wav, &token).await?;
        if status == StatusCode::UNAUTHORIZED {
            if let Some(token) = self.auth.retry_token(&self.client).await {
                (status, body) = self.post(&wav, &token?).await?;
            }
        }
        match status {
            s if s.is_success() => {}
            s @ (StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN) => {
                bail!("Grok speech-to-text refused access (HTTP {}); {}", s.as_u16(), self.auth.remedy())
            }
            s => bail!("Grok speech-to-text failed (HTTP {}); details withheld", s.as_u16()),
        }
        let payload: serde_json::Value =
            serde_json::from_slice(&body).map_err(|_| anyhow::anyhow!("Grok speech-to-text returned malformed JSON"))?;
        match payload.get("text").and_then(|t| t.as_str()) {
            Some(text) => Ok(text.trim().to_string()),
            None => bail!("Grok speech-to-text returned no transcript"),
        }
    }
}

#[async_trait]
impl Transcriber for GrokStt {
    fn label(&self) -> String {
        format!("grok ({MODEL}; {})", self.auth.describe())
    }

    async fn prepare(&self) -> anyhow::Result<()> {
        self.auth.check()
    }

    async fn transcribe(&self, audio: &Clip) -> anyhow::Result<String> {
        http::within(self.timeout, "Grok speech-to-text", self.run(audio)).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::credentials::Credentials;
    use axum::extract::Multipart;
    use axum::http::HeaderMap;
    use axum::{Json, Router, routing::post};
    use std::sync::{Arc, Mutex};

    fn env_none(_: &str) -> Option<String> {
        None
    }

    fn key_auth() -> Auth {
        Auth::Key {
            env: "XAI_API_KEY".into(),
            creds: Credentials::for_tests(&[("XAI_API_KEY", "test-key")], env_none),
        }
    }

    async fn serve(app: Router) -> String {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        format!("http://{addr}")
    }

    fn clip() -> Clip {
        Clip::silence(Duration::from_millis(500), 16_000)
    }

    #[tokio::test]
    async fn sends_audio_keyterms_and_bearer_key() {
        let seen = Arc::new(Mutex::new(Vec::<String>::new()));
        let record = seen.clone();
        let app = Router::new().route(
            "/v1/stt",
            post(move |headers: HeaderMap, mut form: Multipart| {
                let record = record.clone();
                async move {
                    record.lock().unwrap().push(headers["authorization"].to_str().unwrap().to_string());
                    while let Some(field) = form.next_field().await.unwrap() {
                        let name = field.name().unwrap().to_string();
                        let data = field.bytes().await.unwrap();
                        if name == "keyterm" {
                            record.lock().unwrap().push(String::from_utf8(data.to_vec()).unwrap());
                        }
                    }
                    Json(serde_json::json!({"text": " Charlotte, hello "}))
                }
            }),
        );
        let base = serve(app).await;
        let stt = GrokStt::with_base(key_auth(), base, Duration::from_secs(5), 1 << 20, vec!["Charlotte".into(), "charlotte".into(), "go to  sleep".into()]);
        assert_eq!(stt.transcribe(&clip()).await.unwrap(), "Charlotte, hello");
        let seen = seen.lock().unwrap();
        assert_eq!(seen[0], "Bearer test-key");
        assert_eq!(&seen[1..], ["Charlotte", "go to sleep"]);
    }

    #[tokio::test]
    async fn oversized_response_is_discarded() {
        let app = Router::new().route("/v1/stt", post(|| async { "x".repeat(10_000) }));
        let base = serve(app).await;
        let stt = GrokStt::with_base(key_auth(), base, Duration::from_secs(5), 1000, vec![]);
        let err = stt.transcribe(&clip()).await.unwrap_err().to_string();
        assert!(err.contains("larger"), "{err}");
    }

    #[tokio::test]
    async fn slow_server_hits_the_deadline() {
        let app = Router::new().route(
            "/v1/stt",
            post(|| async {
                tokio::time::sleep(Duration::from_secs(5)).await;
                "{}"
            }),
        );
        let base = serve(app).await;
        let stt = GrokStt::with_base(key_auth(), base, Duration::from_millis(300), 1000, vec![]);
        let err = stt.transcribe(&clip()).await.unwrap_err().to_string();
        assert!(err.contains("timed out"), "{err}");
    }

    #[tokio::test]
    async fn rejected_key_is_not_retried_and_hides_details() {
        let app = Router::new().route("/v1/stt", post(|| async { (axum::http::StatusCode::UNAUTHORIZED, "secret diagnostics") }));
        let base = serve(app).await;
        let stt = GrokStt::with_base(key_auth(), base, Duration::from_secs(5), 1000, vec![]);
        let err = stt.transcribe(&clip()).await.unwrap_err().to_string();
        assert!(err.contains("401") && !err.contains("secret diagnostics"), "{err}");
    }
}
