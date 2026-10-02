//! Anthropic's Messages API with an explicitly billed key.

use std::time::Duration;

use anyhow::{Context, bail};
use async_trait::async_trait;
use reqwest::StatusCode;
use serde_json::{Value, json};

use super::{Request, TextAgent};
use crate::config::ReasoningEffort;
use crate::credentials::Credentials;
use crate::http;

const URL: &str = "https://api.anthropic.com/v1/messages";
const MAX_RESPONSE: usize = 256 * 1024;

pub struct ClaudeApi {
    url: String,
    key_env: String,
    creds: Credentials,
    model: String,
    effort: ReasoningEffort,
    max_reply_chars: usize,
    timeout: Duration,
    client: reqwest::Client,
}

impl ClaudeApi {
    pub fn new(
        key_env: &str,
        creds: Credentials,
        model: &str,
        effort: ReasoningEffort,
        max_reply_chars: usize,
        timeout: Duration,
    ) -> ClaudeApi {
        ClaudeApi {
            url: URL.into(),
            key_env: key_env.into(),
            creds,
            model: model.into(),
            effort,
            max_reply_chars,
            timeout,
            client: http::client(Duration::from_secs(10)),
        }
    }

    #[cfg(test)]
    pub fn at(mut self, url: String) -> ClaudeApi {
        self.url = url;
        self
    }

    fn body(&self, request: &Request<'_>) -> Value {
        let mut messages: Vec<Value> = request
            .history
            .iter()
            .flat_map(|t| {
                [
                    json!({"role": "user", "content": t.user}),
                    json!({"role": "assistant", "content": t.assistant}),
                ]
            })
            .collect();
        messages.push(json!({"role": "user", "content": request.traffic}));
        let mut body = json!({
            "model": self.model,
            "system": request.instructions,
            "messages": messages,
            // Room for reasoning; the character limit still applies to the answer.
            "max_tokens": (self.max_reply_chars * 2).max(2048),
        });
        if request.web_search {
            body["tools"] =
                json!([{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]);
        }
        if let Some(effort) = self.effort.explicit() {
            body["output_config"] = json!({"effort": effort});
        }
        body
    }

    async fn run(&self, request: Request<'_>) -> anyhow::Result<String> {
        let key = self.creds.token(&self.key_env)?;
        let response = self
            .client
            .post(&self.url)
            .header("x-api-key", key.expose())
            .header("anthropic-version", "2023-06-01")
            .json(&self.body(&request))
            .send()
            .await
            .map_err(|err| anyhow::anyhow!("Claude API {}", http::without_url(&err)))?;
        match response.status() {
            StatusCode::OK => {}
            s @ (StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN) => {
                bail!(
                    "Claude API refused the key (HTTP {}); check {} and billed API access",
                    s.as_u16(),
                    self.key_env
                )
            }
            s => bail!(
                "Claude API returned HTTP {}; check the model and effort. Details withheld",
                s.as_u16()
            ),
        }
        let message = http::json(response, MAX_RESPONSE, "Claude API").await?;
        parse(&message, request.web_search)
    }
}

/// Only completed final text counts; thinking and search blocks are skipped.
fn parse(message: &Value, web_search: bool) -> anyhow::Result<String> {
    if message["type"] != "message" || message["role"] != "assistant" {
        bail!("Claude API returned an unexpected response");
    }
    if !matches!(
        message["stop_reason"].as_str(),
        Some("end_turn" | "refusal")
    ) {
        bail!("Claude API did not finish its answer; reply discarded");
    }
    let content = message["content"]
        .as_array()
        .context("Claude API returned no content")?;
    let mut parts = Vec::new();
    for block in content {
        match block["type"].as_str() {
            Some("text") => parts.push(block["text"].as_str().context("invalid text block")?),
            Some("thinking" | "redacted_thinking") => {}
            Some("server_tool_use") if web_search && block["name"] == "web_search" => {}
            Some("web_search_tool_result") if web_search => {}
            _ => bail!("Claude API returned unexpected content; reply discarded"),
        }
    }
    Ok(parts.join("\n"))
}

#[async_trait]
impl TextAgent for ClaudeApi {
    fn label(&self) -> String {
        format!(
            "claude-api ({}; reasoning {}; {}, billed API)",
            self.model, self.effort, self.key_env
        )
    }

    async fn reply(&self, request: Request<'_>) -> anyhow::Result<String> {
        http::within(self.timeout, "Claude API", self.run(request)).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::HeaderMap;
    use axum::{Json, Router, routing::post};
    use std::sync::{Arc, Mutex};

    fn no_env(_: &str) -> Option<String> {
        None
    }

    async fn serve(reply: Value, seen: Arc<Mutex<Vec<Value>>>) -> String {
        let app = Router::new().route(
            "/v1/messages",
            post(move |headers: HeaderMap, Json(body): Json<Value>| {
                let seen = seen.clone();
                let reply = reply.clone();
                async move {
                    assert_eq!(headers["x-api-key"], "sk-test");
                    seen.lock().unwrap().push(body);
                    Json(reply)
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        format!("http://{addr}/v1/messages")
    }

    fn agent(url: String) -> ClaudeApi {
        let creds = Credentials::for_tests(&[("ANTHROPIC_API_KEY", "sk-test")], no_env);
        ClaudeApi::new(
            "ANTHROPIC_API_KEY",
            creds,
            "claude-test",
            ReasoningEffort::Low,
            600,
            Duration::from_secs(5),
        )
        .at(url)
    }

    fn request<'a>(history: &'a [super::super::Turn]) -> Request<'a> {
        Request {
            session_id: "s",
            instructions: "be brief",
            history,
            traffic: "why is ice slippery?",
            web_search: true,
        }
    }

    #[tokio::test]
    async fn sends_history_and_returns_only_final_text() {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let reply = json!({
            "type": "message", "role": "assistant", "stop_reason": "end_turn",
            "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "server_tool_use", "name": "web_search"}, {"type": "text", "text": "A thin water layer."}]
        });
        let url = serve(reply, seen.clone()).await;
        let history = [super::super::Turn {
            user: "hi".into(),
            assistant: "hello".into(),
        }];
        let answer = agent(url).reply(request(&history)).await.unwrap();
        assert_eq!(answer, "A thin water layer.");
        let body = &seen.lock().unwrap()[0];
        assert_eq!(body["messages"].as_array().unwrap().len(), 3);
        assert_eq!(body["output_config"]["effort"], "low");
        assert_eq!(body["tools"][0]["name"], "web_search");
    }

    #[tokio::test]
    async fn truncated_answers_are_discarded() {
        let reply = json!({"type": "message", "role": "assistant", "stop_reason": "max_tokens", "content": [{"type": "text", "text": "A thin"}]});
        let url = serve(reply, Arc::default()).await;
        assert!(agent(url).reply(request(&[])).await.is_err());
    }
}
