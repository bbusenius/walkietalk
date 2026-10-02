//! Shared pieces for agents that run an official CLI.

use std::path::PathBuf;
use std::time::{Duration, Instant};

use anyhow::bail;
use serde_json::json;

use super::Request;
use crate::exec::{self, Job, Output};

/// Total output kept from one CLI run.
pub const MAX_OUTPUT: usize = 1024 * 1024;

/// One overall deadline shared by every step of a reply.
pub struct Deadline {
    at: Instant,
    label: &'static str,
}

impl Deadline {
    pub fn new(timeout: Duration, label: &'static str) -> Deadline {
        Deadline { at: Instant::now() + timeout, label }
    }

    pub fn left(&self) -> anyhow::Result<Duration> {
        let left = self.at.saturating_duration_since(Instant::now());
        if left.is_zero() {
            bail!("{} timed out; reply discarded", self.label);
        }
        Ok(left)
    }

    /// Run a job within the remaining time.
    pub async fn run(&self, job: Job) -> anyhow::Result<Output> {
        let left = self.left()?;
        Ok(job.timeout(left).max_output(MAX_OUTPUT).run().await?)
    }
}

/// The text sent to a CLI agent: guidance, tool policy, and the bounded
/// conversation as JSON.
pub fn prompt(request: &Request<'_>) -> String {
    let policy = if request.web_search {
        "You may search the public web. Do not execute commands, change files, read local files, or contact other people. "
    } else {
        "Do not use tools, execute commands, change files, or contact other people. "
    };
    let conversation = json!({
        "radio_session": request.session_id,
        "history": request.history,
        "traffic": request.traffic,
    });
    format!(
        "{}\nThis radio adapter answers with text only. {policy}If a request needs unavailable permissions, explain that briefly.\n\
         The JSON below contains the dedicated radio conversation and current traffic:\n{conversation}",
        request.instructions
    )
}

pub fn locate(executable: &str, setting: &str) -> anyhow::Result<PathBuf> {
    exec::find(executable).map_err(|_| anyhow::anyhow!("{executable} not found; install the official CLI or set {setting}"))
}

/// Parse JSON Lines, failing on any malformed line.
pub fn json_lines(stdout: &[u8], label: &str) -> anyhow::Result<Vec<serde_json::Value>> {
    stdout
        .split(|&b| b == b'\n')
        .filter(|line| !line.iter().all(u8::is_ascii_whitespace))
        .map(|line| serde_json::from_slice(line).map_err(|_| anyhow::anyhow!("{label} returned malformed output; reply discarded")))
        .collect()
}
