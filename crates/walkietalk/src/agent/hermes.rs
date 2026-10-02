//! A Hermes agent environment through its Runs API.

use std::time::Duration;

use anyhow::{Context, bail};
use async_trait::async_trait;
use reqwest::StatusCode;
use serde_json::{Value, json};

use super::{Request, TextAgent};
use crate::credentials::{Credentials, Secret};
use crate::{http, ui};

const MAX_RESPONSE: usize = 64 * 1024;
const POLL: Duration = Duration::from_millis(200);

pub struct Hermes {
    base: String,
    token_env: String,
    creds: Credentials,
    timeout: Duration,
    client: reqwest::Client,
}

impl Hermes {
    pub fn new(url: &str, token_env: &str, creds: Credentials, timeout: Duration) -> Hermes {
        Hermes {
            base: url.trim_end_matches('/').to_string(),
            token_env: token_env.to_string(),
            creds,
            timeout,
            client: http::client(Duration::from_secs(10)),
        }
    }

    async fn call(&self, token: &Secret, method: reqwest::Method, path: &str, body: Option<Value>) -> anyhow::Result<Value> {
        let mut request = self.client.request(method, format!("{}/{path}", self.base)).bearer_auth(token.expose());
        if let Some(body) = body {
            request = request.json(&body);
        }
        let response = request
            .send()
            .await
            .map_err(|err| anyhow::anyhow!("Hermes {}; check agent.hermes.url and the service", http::without_url(&err)))?;
        match response.status() {
            StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN => {
                bail!("Hermes refused the bearer token; check {}", self.token_env)
            }
            s if !s.is_success() => bail!("Hermes returned HTTP {}; details withheld", s.as_u16()),
            _ => http::json(response, MAX_RESPONSE, "Hermes").await,
        }
    }

    async fn run(&self, token: &Secret, request: Request<'_>, run_id: &mut Option<String>) -> anyhow::Result<String> {
        let capabilities = self.call(token, reqwest::Method::GET, "v1/capabilities", None).await?;
        let features = &capabilities["features"];
        if !["run_submission", "run_status", "run_stop"].iter().all(|f| features[f] == true) {
            bail!("this Hermes service does not support run submission, status, and stop");
        }
        let history: Vec<Value> = request
            .history
            .iter()
            .flat_map(|t| [json!({"role": "user", "content": t.user}), json!({"role": "assistant", "content": t.assistant})])
            .collect();
        let submitted = self
            .call(
                token,
                reqwest::Method::POST,
                "v1/runs",
                Some(json!({
                    "input": request.traffic,
                    "instructions": request.instructions,
                    "session_id": format!("walkietalk-{}", request.session_id),
                    "conversation_history": history,
                })),
            )
            .await?;
        let id = submitted["run_id"]
            .as_str()
            .filter(|id| id.starts_with("run_") && id[4..].chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-'))
            .context("Hermes returned no valid run identifier")?
            .to_string();
        *run_id = Some(id.clone());
        loop {
            let status = self.call(token, reqwest::Method::GET, &format!("v1/runs/{id}"), None).await?;
            if status["run_id"] != id.as_str() {
                bail!("Hermes returned a different run");
            }
            match status["status"].as_str() {
                Some("completed") => {
                    *run_id = None;
                    return status["output"]
                        .as_str()
                        .filter(|o| !o.trim().is_empty())
                        .map(String::from)
                        .context("Hermes finished without an answer");
                }
                Some(state @ ("failed" | "cancelled" | "interrupted")) => {
                    *run_id = None;
                    bail!("Hermes run {state}; check the Hermes service and its provider login")
                }
                Some("waiting_for_approval") => bail!("Hermes asked for a permission; none is granted from the radio"),
                Some("queued" | "running" | "stopping") => tokio::time::sleep(POLL).await,
                _ => bail!("Hermes returned an unknown run status"),
            }
        }
    }
}

#[async_trait]
impl TextAgent for Hermes {
    fn label(&self) -> String {
        "hermes (agent environment)".into()
    }

    async fn reply(&self, request: Request<'_>) -> anyhow::Result<String> {
        let token = self.creds.token(&self.token_env)?;
        let mut run_id = None;
        let result = http::within(self.timeout, "Hermes", self.run(&token, request, &mut run_id)).await;
        if let Some(id) = run_id.filter(|_| result.is_err()) {
            // Ask the service to stop work we no longer want.
            let path = format!("v1/runs/{id}/stop");
            let stop = self.call(&token, reqwest::Method::POST, &path, Some(json!({})));
            if tokio::time::timeout(Duration::from_secs(2), stop).await.map_or(true, |r| r.is_err()) {
                ui::warning!("Hermes did not confirm stopping the abandoned run.");
            }
        }
        result
    }
}
