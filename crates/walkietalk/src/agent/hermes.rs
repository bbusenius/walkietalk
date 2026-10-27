//! A Hermes agent environment through its Runs API.

use std::time::Duration;

use anyhow::{Context, bail};
use async_trait::async_trait;
use reqwest::StatusCode;
use serde_json::{Value, json};

use super::{Request, TextAgent};
use crate::config::{HermesAgentConfig, ReasoningEffort};
use crate::credentials::{Credentials, Secret};
use crate::{http, ui};

const MAX_RESPONSE: usize = 64 * 1024;
const POLL: Duration = Duration::from_millis(200);

pub struct Hermes {
    base: String,
    token_env: String,
    model: String,
    effort: ReasoningEffort,
    creds: Credentials,
    timeout: Duration,
    client: reqwest::Client,
}

impl Hermes {
    pub fn new(config: &HermesAgentConfig, creds: Credentials, timeout: Duration) -> Hermes {
        Hermes {
            base: config.url.trim_end_matches('/').to_string(),
            token_env: config.token_env.clone(),
            model: config.model.clone(),
            effort: config.reasoning_effort,
            creds,
            timeout,
            client: http::client(Duration::from_secs(10)),
        }
    }

    async fn call(
        &self,
        token: &Secret,
        method: reqwest::Method,
        path: &str,
        body: Option<Value>,
    ) -> anyhow::Result<Value> {
        let mut request = self
            .client
            .request(method, format!("{}/{path}", self.base))
            .bearer_auth(token.expose());
        if let Some(body) = body {
            request = request.json(&body);
        }
        let response = request.send().await.map_err(|err| {
            anyhow::anyhow!(
                "Hermes {}; check agent.hermes.url and the service",
                http::without_url(&err)
            )
        })?;
        match response.status() {
            StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN => {
                bail!("Hermes refused the bearer token; check {}", self.token_env)
            }
            s if !s.is_success() => bail!("Hermes returned HTTP {}; details withheld", s.as_u16()),
            _ => http::json(response, MAX_RESPONSE, "Hermes").await,
        }
    }

    async fn run(
        &self,
        token: &Secret,
        request: Request<'_>,
        run_id: &mut Option<String>,
    ) -> anyhow::Result<String> {
        let capabilities = self
            .call(token, reqwest::Method::GET, "v1/capabilities", None)
            .await?;
        let features = &capabilities["features"];
        if !["run_submission", "run_status", "run_stop"]
            .iter()
            .all(|f| features[f] == true)
        {
            bail!("this Hermes service does not support run submission, status, and stop");
        }
        let history: Vec<Value> = request
            .history
            .iter()
            .flat_map(|t| {
                [
                    json!({"role": "user", "content": t.user}),
                    json!({"role": "assistant", "content": t.assistant}),
                ]
            })
            .collect();
        let mut body = json!({
            "input": request.traffic,
            "instructions": request.instructions,
            "session_id": format!("walkietalk-{}", request.session_id),
            "conversation_history": history,
        });
        if !self.model.is_empty() {
            body["model"] = json!(self.model);
        }
        match self.effort {
            ReasoningEffort::Default => {}
            ReasoningEffort::None => {
                body["model_options"] = json!({"reasoning": {"enabled": false}});
            }
            effort => body["model_options"] = json!({"reasoning": {"effort": effort.as_str()}}),
        }
        let submitted = self
            .call(token, reqwest::Method::POST, "v1/runs", Some(body))
            .await?;
        let id = submitted["run_id"]
            .as_str()
            .filter(|id| {
                id.starts_with("run_")
                    && id[4..]
                        .chars()
                        .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
            })
            .context("Hermes returned no valid run identifier")?
            .to_string();
        *run_id = Some(id.clone());
        loop {
            let status = self
                .call(token, reqwest::Method::GET, &format!("v1/runs/{id}"), None)
                .await?;
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
                Some("waiting_for_approval") => {
                    bail!("Hermes asked for a permission; none is granted from the radio")
                }
                Some("queued" | "running" | "stopping") => tokio::time::sleep(POLL).await,
                _ => bail!("Hermes returned an unknown run status"),
            }
        }
    }
}

#[async_trait]
impl TextAgent for Hermes {
    fn label(&self) -> String {
        let model = if self.model.is_empty() {
            "profile model"
        } else {
            &self.model
        };
        let effort = match self.effort {
            ReasoningEffort::Default => "profile reasoning".to_string(),
            effort => format!("{effort} reasoning"),
        };
        format!("hermes (agent environment; {model}, {effort})")
    }

    async fn reply(&self, request: Request<'_>) -> anyhow::Result<String> {
        let token = self.creds.token(&self.token_env)?;
        let mut run_id = None;
        let result = http::within(
            self.timeout,
            "Hermes",
            self.run(&token, request, &mut run_id),
        )
        .await;
        if let Some(id) = run_id.filter(|_| result.is_err()) {
            // Ask the service to stop work we no longer want.
            let path = format!("v1/runs/{id}/stop");
            let stop = self.call(&token, reqwest::Method::POST, &path, Some(json!({})));
            if tokio::time::timeout(Duration::from_secs(2), stop)
                .await
                .map_or(true, |r| r.is_err())
            {
                ui::warning!("Hermes did not confirm stopping the abandoned run.");
            }
        }
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::routing::{get, post};
    use axum::{Json, Router};
    use std::sync::{Arc, Mutex};

    /// A Runs API that completes every run at once, recording submissions.
    async fn serve() -> (String, Arc<Mutex<Vec<Value>>>) {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let record = seen.clone();
        let app = Router::new()
            .route(
                "/v1/capabilities",
                get(|| async {
                    Json(json!({"features": {"run_submission": true, "run_status": true, "run_stop": true}}))
                }),
            )
            .route(
                "/v1/runs",
                post(move |Json(body): Json<Value>| {
                    let record = record.clone();
                    async move {
                        record.lock().unwrap().push(body);
                        Json(json!({"run_id": "run_1"}))
                    }
                }),
            )
            .route(
                "/v1/runs/run_1",
                get(|| async {
                    Json(json!({"run_id": "run_1", "status": "completed", "output": "Copy."}))
                }),
            );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        (format!("http://{addr}"), seen)
    }

    async fn submit(model: &str, effort: ReasoningEffort) -> Value {
        let (url, seen) = serve().await;
        let config = HermesAgentConfig {
            url,
            model: model.into(),
            reasoning_effort: effort,
            ..Default::default()
        };
        let creds = Credentials::for_tests(&[("WALKIETALK_HERMES_TOKEN", "t")], |_| None);
        let hermes = Hermes::new(&config, creds, Duration::from_secs(5));
        let request = Request {
            session_id: "s",
            instructions: "Be brief.",
            history: &[],
            traffic: "Radio check.",
            web_search: false,
        };
        assert_eq!(hermes.reply(request).await.unwrap(), "Copy.");
        seen.lock().unwrap().pop().unwrap()
    }

    #[tokio::test]
    async fn defaults_leave_the_profile_model_and_reasoning() {
        let body = submit("", ReasoningEffort::Default).await;
        assert!(body.get("model").is_none() && body.get("model_options").is_none());
    }

    #[tokio::test]
    async fn configured_model_and_reasoning_are_requested_per_run() {
        let body = submit("grok-4.5", ReasoningEffort::Low).await;
        assert_eq!(body["model"], "grok-4.5");
        assert_eq!(
            body["model_options"],
            json!({"reasoning": {"effort": "low"}})
        );
    }

    #[tokio::test]
    async fn no_reasoning_disables_it() {
        let body = submit("", ReasoningEffort::None).await;
        assert_eq!(
            body["model_options"],
            json!({"reasoning": {"enabled": false}})
        );
    }
}
