//! The Codex CLI with its saved ChatGPT login.

use std::time::Duration;

use anyhow::bail;
use async_trait::async_trait;

use super::cli::{self, Deadline};
use super::{Request, TextAgent};
use crate::config::CliAgentConfig;
use crate::exec::{self, Job};

const ENV: &[&str] = &[
    "HOME", "PATH", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "CODEX_CA_CERTIFICATE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
];
const MAX_ANSWER: u64 = 64 * 1024;

pub struct Codex {
    config: CliAgentConfig,
    timeout: Duration,
}

impl Codex {
    pub fn new(config: CliAgentConfig, timeout: Duration) -> Codex {
        Codex { config, timeout }
    }
}

#[async_trait]
impl TextAgent for Codex {
    fn label(&self) -> String {
        let model = if self.config.model.is_empty() { "CLI default model" } else { &self.config.model };
        format!("codex ({model}; reasoning {}; saved ChatGPT login)", self.config.reasoning_effort)
    }

    async fn reply(&self, request: Request<'_>) -> anyhow::Result<String> {
        let deadline = Deadline::new(self.timeout, "Codex");
        let program = cli::locate(&self.config.executable, "agent.codex.executable")?;
        let dir = tempfile::Builder::new().prefix("walkietalk-codex-").tempdir()?;
        let base = || Job::new(program.clone(), "Codex").env(exec::inherit(ENV)).cwd(dir.path());

        let status = deadline.run(base().args(["login", "status"])).await?;
        let text = [status.stdout.as_slice(), status.stderr.as_slice()].concat();
        if !status.success() || !String::from_utf8_lossy(&text).contains("Logged in using ChatGPT") {
            bail!("Codex needs its saved ChatGPT login; run `codex login` as this user. API keys are not used");
        }

        let answer_path = dir.path().join("answer.txt");
        let mut job = base()
            .args(["exec", "--ignore-user-config", "--strict-config", "--sandbox", "read-only", "--color", "never"])
            .args(["--skip-git-repo-check", "--json", "--output-last-message"])
            .arg(answer_path.as_os_str());
        let search = if request.web_search { "web_search=\"live\"" } else { "web_search=\"disabled\"" };
        for setting in [
            "forced_login_method=\"chatgpt\"",
            "model_provider=\"openai\"",
            "approval_policy=\"never\"",
            "features.shell_tool=false",
            "features.apps=false",
            "features.hooks=false",
            "agents.enabled=false",
            search,
        ] {
            job = job.args(["-c", setting]);
        }
        if let Some(effort) = self.config.reasoning_effort.explicit() {
            job = job.args(["-c".to_string(), format!("model_reasoning_effort=\"{effort}\"")]);
        }
        if !self.config.model.is_empty() {
            job = job.args(["--model", &self.config.model]);
        }
        let output = deadline.run(job.arg("-").stdin(cli::prompt(&request).into_bytes())).await?;
        if !output.success() {
            bail!("Codex failed; check `codex login status`, account access, and the CLI version. Details withheld");
        }
        check_events(&output.stdout)?;
        let size = std::fs::metadata(&answer_path).map(|m| m.len()).unwrap_or(0);
        if size == 0 || size > MAX_ANSWER {
            bail!("Codex returned no usable final answer");
        }
        let answer = std::fs::read_to_string(&answer_path).map_err(|_| anyhow::anyhow!("Codex answer was not UTF-8"))?;
        Ok(answer)
    }
}

/// Require a started thread and a completed turn; refuse failures and
/// permission requests. Event text is never shown.
fn check_events(stdout: &[u8]) -> anyhow::Result<()> {
    let mut started = false;
    let mut completed = false;
    for event in cli::json_lines(stdout, "Codex")? {
        match event["type"].as_str() {
            Some("error" | "turn.failed" | "approval.request") => {
                bail!("Codex reported a failed or permission-requiring turn; reply discarded")
            }
            Some("thread.started") => started = true,
            Some("turn.completed") => completed = true,
            _ => {}
        }
    }
    if !(started && completed) {
        bail!("Codex did not complete a reply; output discarded");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agent::Turn;
    use crate::config::ReasoningEffort;
    use crate::exec::tests::script;

    fn request<'a>(history: &'a [Turn]) -> Request<'a> {
        Request { session_id: "s1", instructions: "Be brief.", history, traffic: "hello", web_search: false }
    }

    fn fake_codex(dir: &std::path::Path, login: &str, answer: &str) -> std::path::PathBuf {
        // Writes the answer to the --output-last-message path and records the prompt.
        let body = format!(
            r#"if [ "$1" = login ]; then echo "{login}"; exit 0; fi
out=""; prev=""
for a in "$@"; do [ "$prev" = "--output-last-message" ] && out="$a"; prev="$a"; done
cat > "{dir}/prompt.txt"
printf '%s' "{answer}" > "$out"
echo '{{"type":"thread.started","thread_id":"00000000-0000-0000-0000-000000000001"}}'
echo '{{"type":"turn.completed"}}'"#,
            dir = dir.display()
        );
        script(dir, "codex", &body)
    }

    fn agent(path: &std::path::Path) -> Codex {
        Codex::new(
            CliAgentConfig { executable: path.display().to_string(), model: String::new(), reasoning_effort: ReasoningEffort::Low },
            Duration::from_secs(10),
        )
    }

    #[tokio::test]
    async fn returns_the_final_answer_and_sends_history() {
        let dir = tempfile::tempdir().unwrap();
        let codex = fake_codex(dir.path(), "Logged in using ChatGPT", "Hi there.");
        let history = [Turn { user: "q1".into(), assistant: "a1".into() }];
        assert_eq!(agent(&codex).reply(request(&history)).await.unwrap(), "Hi there.");
        let prompt = std::fs::read_to_string(dir.path().join("prompt.txt")).unwrap();
        assert!(prompt.contains("\"traffic\":\"hello\"") && prompt.contains("\"assistant\":\"a1\""), "{prompt}");
    }

    #[tokio::test]
    async fn api_key_login_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        let codex = fake_codex(dir.path(), "Logged in using an API key", "Hi.");
        let err = agent(&codex).reply(request(&[])).await.unwrap_err().to_string();
        assert!(err.contains("codex login"), "{err}");
    }

    #[test]
    fn failed_turns_are_rejected() {
        assert!(check_events(b"{\"type\":\"thread.started\"}\n{\"type\":\"turn.failed\"}\n").is_err());
        assert!(check_events(b"{\"type\":\"thread.started\"}\n").is_err());
        assert!(check_events(b"not json\n").is_err());
    }
}
