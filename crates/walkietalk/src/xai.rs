//! Shared plumbing for xAI speech endpoints: which credential to use and
//! how to retry once after a rejected saved login.

use crate::credentials::{Credentials, Secret};
use crate::grok_login::GrokLogin;

pub const API_BASE: &str = "https://api.x.ai";

/// How a request to xAI is authorized. The two never substitute for each other.
#[derive(Debug, Clone)]
pub enum Auth {
    /// The Grok CLI's saved account login.
    Login(GrokLogin),
    /// A billed API key held in the named variable.
    Key { env: String, creds: Credentials },
}

impl Auth {
    pub fn describe(&self) -> String {
        match self {
            Auth::Login(_) => "saved Grok login".into(),
            Auth::Key { env, .. } => format!("{env}, billed API"),
        }
    }

    /// Check credentials exist, without network access.
    pub fn check(&self) -> anyhow::Result<()> {
        match self {
            Auth::Login(login) => login.check(),
            Auth::Key { env, creds } => creds.token(env).map(|_| ()),
        }
    }

    pub async fn token(&self, client: &reqwest::Client) -> anyhow::Result<Secret> {
        match self {
            Auth::Login(login) => login.token(client).await,
            Auth::Key { env, creds } => creds.token(env),
        }
    }

    /// A fresh token after HTTP 401, for a saved login only.
    pub async fn retry_token(&self, client: &reqwest::Client) -> Option<anyhow::Result<Secret>> {
        match self {
            Auth::Login(login) => Some(login.refresh(client).await),
            Auth::Key { .. } => None,
        }
    }

    /// The fix to suggest when access is refused.
    pub fn remedy(&self) -> &'static str {
        match self {
            Auth::Login(_) => "run `grok login`; API keys are never used for this backend",
            Auth::Key { .. } => "check the billed API key; a Grok login is never used for this backend",
        }
    }
}
