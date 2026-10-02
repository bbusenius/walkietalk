//! The Claude Code CLI with its saved account login.

use std::time::Duration;

use anyhow::bail;
use async_trait::async_trait;
use serde_json::{Value, json};

use super::cli::{self, Deadline};
use super::{Request, TextAgent, WEB_SEARCH_TURNS};
use crate::config::CliAgentConfig;
use crate::exec::{self, Job};

const ENV: &[&str] = &[
    "HOME",
    "PATH",
    "CLAUDE_CONFIG_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "DBUS_SESSION_BUS_ADDRESS",
    "LANG",
    "LC_ALL",
    "TZ",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
];
const REQUIRED_FLAGS: &[&str] = &[
    "--safe-mode",
    "--restricted",
    "--permission-prompts",
    "--no-session-persistence",
];
const ISOLATION: &[&str] = &[
    "--safe-mode",
    "--restricted",
    "--setting-sources",
    "",
    "--settings",
    r#"{"disableAllHooks":true}"#,
];

pub struct ClaudeCli {
    config: CliAgentConfig,
    timeout: Duration,
}

impl ClaudeCli {
    pub fn new(config: CliAgentConfig, timeout: Duration) -> ClaudeCli {
        ClaudeCli { config, timeout }
    }
}

fn block_allowed(block: &Value, web_search: bool) -> bool {
    match block["type"].as_str() {
        Some("text" | "thinking" | "redacted_thinking") => true,
        Some("tool_use") => web_search && block["name"] == "WebSearch",
        _ => false,
    }
}

fn parse(stdout: &[u8], session: &str, model: &str, web_search: bool) -> anyhow::Result<String> {
    let bad = |why: &str| anyhow::anyhow!("Claude Code {why}; reply discarded");
    let tools = if web_search {
        json!(["WebSearch"])
    } else {
        json!([])
    };
    let mut initialized = false;
    let mut result = None;
    for event in cli::json_lines(stdout, "Claude Code")? {
        let kind = event["type"].as_str();
        if kind == Some("rate_limit_event") && initialized {
            continue;
        }
        if result.is_some() {
            return Err(bad("returned events after its result"));
        }
        match kind {
            Some("system") if event["subtype"] == "init" && !initialized => {
                // Plugins bundled with the CLI are allowed; user-installed ones are not.
                let plugins_off = event["plugins"]
                    .as_array()
                    .is_none_or(|p| p.iter().all(|p| p["path"] == "builtin"));
                if event["session_id"] != session
                    || event["model"] != model
                    || event["tools"] != tools
                    || event["mcp_servers"] != json!([])
                    || !plugins_off
                {
                    return Err(bad("session, model, or disabled tools did not match"));
                }
                initialized = true;
            }
            Some("assistant") if initialized => {
                let message = &event["message"];
                let ok = event.get("error").is_none_or(Value::is_null)
                    && message["model"] == model
                    && message["content"]
                        .as_array()
                        .is_some_and(|c| c.iter().all(|b| block_allowed(b, web_search)));
                if !ok {
                    return Err(bad("reported an error or used unexpected tools"));
                }
            }
            Some("user") if initialized && web_search => {
                let content = event["message"]["content"].as_array();
                if content
                    .is_none_or(|c| c.is_empty() || c.iter().any(|b| b["type"] != "tool_result"))
                {
                    return Err(bad("returned an unexpected event"));
                }
            }
            Some("result") if initialized => {
                let stop_ok = matches!(
                    event["stop_reason"].as_str(),
                    None | Some("end_turn" | "stop_sequence" | "refusal")
                );
                let denials = event["permission_denials"]
                    .as_array()
                    .is_some_and(|d| !d.is_empty());
                let ok = event["subtype"] == "success"
                    && event["is_error"] == false
                    && event["session_id"] == session
                    && stop_ok
                    && !denials;
                match (ok, event["result"].as_str()) {
                    (true, Some(text)) => result = Some(text.to_string()),
                    _ => bail!(
                        "Claude Code did not complete successfully; check its login, usage limits, and model access"
                    ),
                }
            }
            _ => return Err(bad("returned an unexpected event")),
        }
    }
    result.ok_or_else(|| anyhow::anyhow!("Claude Code returned no complete answer"))
}

#[async_trait]
impl TextAgent for ClaudeCli {
    fn label(&self) -> String {
        format!(
            "claude ({}; reasoning {}; saved Claude login)",
            self.config.model, self.config.reasoning_effort
        )
    }

    async fn reply(&self, request: Request<'_>) -> anyhow::Result<String> {
        let deadline = Deadline::new(self.timeout, "Claude Code");
        let program = cli::locate(&self.config.executable, "agent.claude.executable")?;
        let dir = tempfile::Builder::new()
            .prefix("walkietalk-claude-")
            .tempdir()?;
        let base = || {
            Job::new(program.clone(), "Claude Code")
                .env(exec::inherit(ENV))
                .cwd(dir.path())
        };

        let help = deadline.run(base().arg("--help")).await?;
        let help_text = String::from_utf8_lossy(&help.stdout);
        if !help.success() || REQUIRED_FLAGS.iter().any(|f| !help_text.contains(f)) {
            bail!(
                "the Claude Code CLI lacks required isolation options; update it. No question was sent"
            );
        }
        let status = deadline
            .run(base().args(ISOLATION).args(["auth", "status", "--json"]))
            .await?;
        let auth: Value = serde_json::from_slice(&status.stdout).unwrap_or(Value::Null);
        if !status.success()
            || auth["loggedIn"] != true
            || auth["authMethod"] != "claude.ai"
            || auth["apiProvider"] != "firstParty"
        {
            bail!(
                "Claude Code needs its own saved Claude account login; run `claude auth login`. \
                 For billed API access select claude-api instead. No fallback was attempted"
            );
        }
        let system = dir.path().join("system.txt");
        std::fs::write(&system, request.instructions)?;
        let session = uuid::Uuid::new_v4().to_string();
        let turns = if request.web_search {
            WEB_SEARCH_TURNS.to_string()
        } else {
            "1".into()
        };
        let mut job = base()
            .args(ISOLATION)
            .args([
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--tools",
                if request.web_search { "WebSearch" } else { "" },
            ])
            .args([
                "--disallowedTools",
                "mcp__*",
                "--strict-mcp-config",
                "--mcp-config",
                r#"{"mcpServers":{}}"#,
            ])
            .args([
                "--disable-slash-commands",
                "--no-chrome",
                "--permission-mode",
                "dontAsk",
                "--permission-prompts",
                "none",
            ])
            .args([
                "--no-session-persistence",
                "--session-id",
                &session,
                "--system-prompt-file",
            ])
            .arg(system.as_os_str())
            .args(["--max-turns", &turns, "--model", &self.config.model]);
        if let Some(effort) = self.config.reasoning_effort.explicit() {
            job = job.args(["--effort", effort]);
        }
        let conversation = json!({"radio_session": request.session_id, "history": request.history, "traffic": request.traffic});
        let output = deadline
            .run(job.stdin(conversation.to_string().into_bytes()))
            .await?;
        if !output.success() {
            bail!(
                "Claude Code failed; check its login, usage limits, model access, and CLI version. Details withheld"
            );
        }
        parse(
            &output.stdout,
            &session,
            &self.config.model,
            request.web_search,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn init(session: &str) -> String {
        format!(
            "{}\n",
            json!({"type":"system","subtype":"init","session_id":session,"model":"m","tools":[],"mcp_servers":[]})
        )
    }

    #[test]
    fn final_result_is_returned() {
        let out = init("s")
            + &format!(
                "{}\n",
                json!({"type":"assistant","message":{"model":"m","content":[{"type":"text","text":"hi"}]}})
            )
            + &format!(
                "{}\n",
                json!({"type":"result","subtype":"success","is_error":false,"session_id":"s","result":"Hello."})
            )
            + &format!("{}\n", json!({"type":"rate_limit_event"}));
        assert_eq!(parse(out.as_bytes(), "s", "m", false).unwrap(), "Hello.");
    }

    #[test]
    fn only_builtin_plugins_are_accepted() {
        let builtin = init("s").replace(
            "\"mcp_servers\":[]",
            "\"mcp_servers\":[],\"plugins\":[{\"name\":\"x\",\"path\":\"builtin\"}]",
        );
        let done = format!(
            "{}\n",
            json!({"type":"result","subtype":"success","is_error":false,"session_id":"s","result":"ok"})
        );
        assert!(parse((builtin.clone() + &done).as_bytes(), "s", "m", false).is_ok());
        let user = builtin.replace("\"path\":\"builtin\"", "\"path\":\"/home/u/plugin\"");
        assert!(parse((user + &done).as_bytes(), "s", "m", false).is_err());
    }

    #[test]
    fn wrong_model_or_tools_are_rejected() {
        let other_model = init("s").replace("\"model\":\"m\"", "\"model\":\"x\"");
        assert!(parse(other_model.as_bytes(), "s", "m", false).is_err());
        let tool = init("s")
            + &format!(
                "{}\n",
                json!({"type":"assistant","message":{"model":"m","content":[{"type":"tool_use","name":"Bash"}]}})
            );
        assert!(parse(tool.as_bytes(), "s", "m", false).is_err());
        let denied = init("s")
            + &format!(
                "{}\n",
                json!({"type":"result","subtype":"success","is_error":false,"session_id":"s","result":"x","permission_denials":[1]})
            );
        assert!(parse(denied.as_bytes(), "s", "m", false).is_err());
    }
}
