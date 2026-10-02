//! The Grok CLI's saved login, shared with its speech endpoints.
//!
//! The CLI owns `auth.json` under `$GROK_HOME` (default `~/.grok`). We read
//! it before every use (the CLI or another adapter may have rotated the
//! token), refresh an expired grant, and write it back atomically,
//! preserving every field we do not understand.

use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::PathBuf;
use std::time::Duration;

use anyhow::{Context, bail};
use serde_json::Value;

use crate::credentials::Secret;
use crate::http;

const TOKEN_URL: &str = "https://auth.x.ai/oauth2/token";
const MAX_AUTH_FILE: u64 = 1024 * 1024;
/// Refresh a little before the stated expiry.
const SKEW: Duration = Duration::from_secs(60);

#[derive(Debug, Clone)]
pub struct GrokLogin {
    path: PathBuf,
    token_url: String,
}

/// Serializes refreshes within this process.
static REFRESH: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

impl GrokLogin {
    pub fn locate() -> GrokLogin {
        let home = std::env::var_os("GROK_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| crate::paths::home().join(".grok"));
        GrokLogin {
            path: crate::paths::expand_home(&home).join("auth.json"),
            token_url: TOKEN_URL.into(),
        }
    }

    #[cfg(test)]
    pub fn at(path: PathBuf, token_url: String) -> GrokLogin {
        GrokLogin { path, token_url }
    }

    fn read(&self) -> anyhow::Result<(Value, String)> {
        let meta = std::fs::metadata(&self.path)
            .map_err(|_| anyhow::anyhow!("no saved Grok login; run `grok login` as this user"))?;
        anyhow::ensure!(meta.len() <= MAX_AUTH_FILE, "the Grok login file is unexpectedly large");
        let text = std::fs::read_to_string(&self.path).context("cannot read the Grok login")?;
        let store: Value = serde_json::from_str(&text).map_err(|_| anyhow::anyhow!("the Grok login file is invalid; run `grok login`"))?;
        let account = store
            .as_object()
            .and_then(|map| {
                map.iter()
                    .find(|(_, session)| session.get("key").and_then(Value::as_str).is_some_and(|k| !k.is_empty()))
                    .map(|(name, _)| name.clone())
            })
            .context("the Grok login has no access token; run `grok login`")?;
        Ok((store, account))
    }

    /// Confirm a login exists, without network access.
    pub fn check(&self) -> anyhow::Result<()> {
        self.read().map(|_| ())
    }

    /// A current access token, refreshing it first if it has expired.
    pub async fn token(&self, client: &reqwest::Client) -> anyhow::Result<Secret> {
        let (store, account) = self.read()?;
        let session = &store[&account];
        if expired(session) {
            return self.refresh(client).await;
        }
        token_of(session)
    }

    /// Exchange the refresh token for a new access token and save it.
    pub async fn refresh(&self, client: &reqwest::Client) -> anyhow::Result<Secret> {
        let _guard = REFRESH.lock().await;
        let (mut store, account) = self.read()?;
        let session = &store[&account];
        let refresh = session.get("refresh_token").and_then(Value::as_str).filter(|s| !s.is_empty());
        let client_id = session.get("oidc_client_id").and_then(Value::as_str).filter(|s| !s.is_empty());
        let (Some(refresh), Some(client_id)) = (refresh, client_id) else {
            bail!("the Grok login expired; run `grok login`");
        };
        let response = client
            .post(&self.token_url)
            .form(&[("grant_type", "refresh_token"), ("refresh_token", refresh), ("client_id", client_id)])
            .send()
            .await
            .map_err(|err| anyhow::anyhow!("Grok login refresh {}", http::without_url(&err)))?;
        if !response.status().is_success() {
            bail!("the Grok login could not be refreshed (HTTP {}); run `grok login`", response.status().as_u16());
        }
        let payload = http::json(response, 64 * 1024, "Grok login refresh").await?;
        let token = payload
            .get("access_token")
            .and_then(Value::as_str)
            .filter(|t| valid_token(t))
            .context("the Grok login refresh returned no token; run `grok login`")?
            .to_string();
        let session = store[&account].as_object_mut().context("invalid Grok login entry")?;
        session.insert("key".into(), Value::String(token.clone()));
        if let Some(new_refresh) = payload.get("refresh_token").and_then(Value::as_str).filter(|t| valid_token(t)) {
            session.insert("refresh_token".into(), Value::String(new_refresh.into()));
        }
        if let Some(seconds) = payload.get("expires_in").and_then(Value::as_f64).filter(|s| *s > 0.0 && *s < 1e8) {
            let expires = jiff::Timestamp::now() + jiff::SignedDuration::from_secs(seconds as i64);
            session.insert("expires_at".into(), Value::String(expires.to_string()));
        }
        self.write(&store)?;
        Ok(Secret::new(token))
    }

    fn write(&self, store: &Value) -> anyhow::Result<()> {
        let dir = self.path.parent().context("Grok login path has no directory")?;
        let tmp = dir.join(format!(".auth.json.{}.tmp", std::process::id()));
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(&tmp)
            .context("cannot save the refreshed Grok login")?;
        file.write_all(serde_json::to_string_pretty(store)?.as_bytes())?;
        file.write_all(b"\n")?;
        file.sync_all()?;
        std::fs::rename(&tmp, &self.path).context("cannot save the refreshed Grok login")?;
        Ok(())
    }
}

fn token_of(session: &Value) -> anyhow::Result<Secret> {
    session
        .get("key")
        .and_then(Value::as_str)
        .filter(|t| valid_token(t))
        .map(Secret::new)
        .context("the Grok login token is invalid; run `grok login`")
}

fn expired(session: &Value) -> bool {
    let Some(raw) = session.get("expires_at").and_then(Value::as_str) else {
        return false;
    };
    let Ok(expires) = raw.parse::<jiff::Timestamp>() else {
        return false;
    };
    jiff::Timestamp::now() + jiff::SignedDuration::try_from(SKEW).expect("small duration") >= expires
}

fn valid_token(token: &str) -> bool {
    !token.is_empty() && token.bytes().all(|b| (33..=126).contains(&b))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{Router, routing::post};

    async fn token_server(body: &'static str) -> String {
        let app = Router::new().route("/token", post(move || async move { body }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        format!("http://{addr}/token")
    }

    fn write_store(dir: &std::path::Path, expires_at: &str) -> PathBuf {
        let path = dir.join("auth.json");
        let store = serde_json::json!({
            "user@example": {
                "key": "old-token",
                "refresh_token": "refresh-1",
                "oidc_client_id": "client",
                "expires_at": expires_at,
                "extra": {"keep": true}
            }
        });
        std::fs::write(&path, store.to_string()).unwrap();
        path
    }

    #[tokio::test]
    async fn current_token_is_used_without_network() {
        let dir = tempfile::tempdir().unwrap();
        let path = write_store(dir.path(), "2999-01-01T00:00:00Z");
        let login = GrokLogin::at(path, "http://127.0.0.1:9/unused".into());
        let token = login.token(&http::client(Duration::from_secs(1))).await.unwrap();
        assert_eq!(token.expose(), "old-token");
    }

    #[tokio::test]
    async fn expired_token_is_refreshed_and_saved_preserving_fields() {
        let dir = tempfile::tempdir().unwrap();
        let path = write_store(dir.path(), "2000-01-01T00:00:00Z");
        let url = token_server(r#"{"access_token":"new-token","refresh_token":"refresh-2","expires_in":3600}"#).await;
        let login = GrokLogin::at(path.clone(), url);
        let token = login.token(&http::client(Duration::from_secs(1))).await.unwrap();
        assert_eq!(token.expose(), "new-token");
        let saved: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        let session = &saved["user@example"];
        assert_eq!(session["key"], "new-token");
        assert_eq!(session["refresh_token"], "refresh-2");
        assert_eq!(session["extra"]["keep"], true);
        assert!(!expired(session));
    }

    #[tokio::test]
    async fn missing_login_points_to_grok_login() {
        let dir = tempfile::tempdir().unwrap();
        let login = GrokLogin::at(dir.path().join("auth.json"), String::new());
        let err = login.check().unwrap_err().to_string();
        assert!(err.contains("grok login"), "{err}");
    }
}
