//! The Grok Build CLI with its saved login.

use std::collections::HashMap;
use std::path::Path;
use std::time::Duration;

use anyhow::bail;
use async_trait::async_trait;
use serde_json::Value;

use super::cli::{self, Deadline};
use super::{Request, TextAgent, WEB_SEARCH_TURNS};
use crate::config::CliAgentConfig;
use crate::exec::{self, Job};
use crate::grok_login::GrokLogin;

const ENV: &[&str] = &[
    "HOME",
    "PATH",
    "GROK_HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
];

pub struct GrokCli {
    config: CliAgentConfig,
    timeout: Duration,
}

impl GrokCli {
    pub fn new(config: CliAgentConfig, timeout: Duration) -> GrokCli {
        GrokCli { config, timeout }
    }
}

/// Turn off every optional integration so only the model (and web search,
/// when allowed) is available, and never fall back to an API key.
fn environment(grok_home: &Path, home: &Path) -> HashMap<String, String> {
    let mut env = exec::inherit(ENV);
    env.insert("HOME".into(), home.display().to_string());
    env.insert("GROK_HOME".into(), grok_home.display().to_string());
    for (key, value) in [
        ("GROK_DISABLE_API_KEY_AUTH", "1"),
        ("GROK_DISABLE_AUTOUPDATER", "1"),
        ("GROK_MANAGED_MCPS_ENABLED", "0"),
        ("GROK_MANAGED_MCP_GATEWAY_TOOLS_ENABLED", "0"),
        ("GROK_MEMORY", "0"),
        ("GROK_SUBAGENTS", "0"),
        ("GROK_WEB_FETCH", "0"),
        ("GROK_WRITE_FILE", "0"),
        ("GROK_LSP_TOOLS", "0"),
        ("GROK_CAMPAIGNS", "0"),
        ("GROK_TOOL_SEARCH", "0"),
    ] {
        env.insert(key.into(), value.into());
    }
    for vendor in ["CLAUDE", "CURSOR", "CODEX"] {
        for kind in ["SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS"] {
            env.insert(format!("GROK_{vendor}_{kind}_ENABLED"), "0".into());
        }
    }
    env
}

/// The effective profile must have API keys disabled, no active extensions,
/// and no custom model providers.
fn check_profile(output: &[u8]) -> anyhow::Result<()> {
    let unverifiable = || {
        anyhow::anyhow!(
            "cannot verify the Grok profile; check the CLI version and `grok inspect`. No question was sent"
        )
    };
    let profile: Value = serde_json::from_slice(output).map_err(|_| unverifiable())?;
    if profile["loginPolicy"]["apiKeyAuthDisabled"] != true {
        return Err(unverifiable());
    }
    for key in ["hooks", "mcpServers", "plugins", "lspServers"] {
        let entries = profile[key].as_array().ok_or_else(unverifiable)?;
        if entries.iter().any(|e| e["disabled"] != true) {
            bail!(
                "radio mode needs Grok hooks, MCP servers, plugins, and LSP servers inactive; see `grok inspect`"
            );
        }
    }
    for layer in profile["configSources"]["layers"]
        .as_array()
        .ok_or_else(unverifiable)?
    {
        let path = layer["path"]
            .as_str()
            .map(Path::new)
            .filter(|p| p.is_absolute())
            .ok_or_else(unverifiable)?;
        let Ok(meta) = std::fs::metadata(path) else {
            continue;
        };
        if meta.len() > 1024 * 1024 {
            return Err(unverifiable());
        }
        let text = std::fs::read_to_string(path).map_err(|_| unverifiable())?;
        let table: toml::Table = text.parse().map_err(|_| unverifiable())?;
        let custom = ["model", "model_providers", "auth_provider"]
            .iter()
            .any(|k| {
                table.get(*k).is_some_and(|v| {
                    v.as_str() != Some("") && v.as_table().is_none_or(|t| !t.is_empty())
                })
            });
        if custom {
            bail!(
                "radio mode needs the standard Grok model catalog; custom models or providers are not supported"
            );
        }
    }
    Ok(())
}

fn block_allowed(block: &Value, web_search: bool) -> bool {
    match block["type"].as_str() {
        Some("text" | "thinking" | "redacted_thinking") => true,
        Some("web_search_tool_result") => web_search,
        Some("server_tool_use" | "tool_use") => web_search && block["name"] == "web_search",
        _ => false,
    }
}

/// Only the successful final result is an answer.
fn parse(stdout: &[u8], session: &str, web_search: bool) -> anyhow::Result<String> {
    let bad = || {
        anyhow::anyhow!(
            "Grok returned an incomplete, failed, or unexpected result; reply discarded"
        )
    };
    let mut initialized = false;
    let mut result = None;
    for event in cli::json_lines(stdout, "Grok")? {
        if result.is_some() || event["session_id"] != session {
            return Err(bad());
        }
        match (event["type"].as_str(), initialized) {
            (Some("system"), false) if event["subtype"] == "init" => {
                let mcp_off = event["mcp_servers"]
                    .as_array()
                    .is_none_or(|s| s.iter().all(|m| m["status"] == "disabled"));
                if event["apiKeySource"] != "oauth"
                    || event["permissionMode"] != "dontAsk"
                    || !mcp_off
                {
                    return Err(bad());
                }
                initialized = true;
            }
            (Some("assistant"), true) => {
                let content = event["message"]["content"].as_array().ok_or_else(bad)?;
                if !content.iter().all(|b| block_allowed(b, web_search)) {
                    return Err(bad());
                }
            }
            (Some("user"), true) if web_search => {
                let content = event["message"]["content"].as_array().ok_or_else(bad)?;
                if content.is_empty() || content.iter().any(|b| b["type"] != "tool_result") {
                    return Err(bad());
                }
            }
            (Some("result"), true) => {
                let ok = event["subtype"] == "success"
                    && event["is_error"] == false
                    && event["stop_reason"] == "end_turn"
                    && event["errors"].as_array().is_none_or(|e| e.is_empty());
                let text = event["result"].as_str().filter(|t| !t.trim().is_empty());
                match (ok, text) {
                    (true, Some(text)) => result = Some(text.to_string()),
                    _ => return Err(bad()),
                }
            }
            _ => return Err(bad()),
        }
    }
    result.ok_or_else(|| anyhow::anyhow!("Grok did not complete a reply; output discarded"))
}

#[async_trait]
impl TextAgent for GrokCli {
    fn label(&self) -> String {
        format!(
            "grok ({}; reasoning {}; saved Grok login)",
            self.config.model, self.config.reasoning_effort
        )
    }

    async fn reply(&self, request: Request<'_>) -> anyhow::Result<String> {
        let deadline = Deadline::new(self.timeout, "Grok");
        let program = cli::locate(&self.config.executable, "agent.grok.executable")?;
        GrokLogin::locate().check()?;
        let grok_home = std::env::var_os("GROK_HOME")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|| crate::paths::home().join(".grok"));
        let dir = tempfile::Builder::new()
            .prefix("walkietalk-grok-")
            .tempdir()?;
        // An empty HOME keeps desktop integrations out; GROK_HOME keeps the login.
        let home = dir.path().join("home");
        std::fs::create_dir(&home)?;
        let env = environment(&grok_home, &home);
        let base = || {
            Job::new(program.clone(), "Grok")
                .env(env.clone())
                .cwd(dir.path())
        };

        let inspect = deadline.run(base().args(["inspect", "--json"])).await?;
        if !inspect.success() {
            bail!("the Grok profile check failed; details withheld");
        }
        check_profile(&inspect.stdout)?;

        let session = uuid::Uuid::new_v4().to_string();
        let mut job = base().args([
            "--prompt-file",
            "/dev/stdin",
            "--verbatim",
            "--output-format",
            "streaming-messages-json",
            "--permission-mode",
            "dontAsk",
        ]);
        job = if request.web_search {
            // The workspace sandbox allows the lookup's network; write tools stay denied.
            job.args(["--sandbox", "workspace", "--tools", "web_search"])
                .args([
                    "--deny", "Bash", "--deny", "Read", "--deny", "Edit", "--deny", "Grep",
                    "--deny", "MCPTool", "--deny", "WebFetch",
                ])
                .args(["--max-turns".to_string(), WEB_SEARCH_TURNS.to_string()])
        } else {
            job.args([
                "--sandbox",
                "read-only",
                "--tools",
                "read_file",
                "--disallowed-tools",
                "read_file",
            ])
            .args(["--deny", "*", "--disable-web-search", "--max-turns", "1"])
        };
        job = job.args([
            "--no-subagents",
            "--no-plan",
            "--model",
            &self.config.model,
            "--session-id",
            &session,
        ]);
        if let Some(effort) = self.config.reasoning_effort.explicit() {
            job = job.args(["--reasoning-effort", effort]);
        }
        let output = deadline
            .run(job.stdin(cli::prompt(&request).into_bytes()))
            .await?;
        if !output.success() {
            bail!(
                "Grok failed; check `grok login`, account access, and the CLI version. No API-key fallback; details withheld"
            );
        }
        parse(&output.stdout, &session, request.web_search)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(v: Value) -> String {
        format!("{v}\n")
    }

    fn events(session: &str, extra: &[Value]) -> Vec<u8> {
        let mut out = line(
            serde_json::json!({"type":"system","subtype":"init","session_id":session,"apiKeySource":"oauth","permissionMode":"dontAsk","mcp_servers":[]}),
        );
        for e in extra {
            out.push_str(&line(e.clone()));
        }
        out.into_bytes()
    }

    #[test]
    fn successful_result_is_the_answer() {
        let out = events(
            "s",
            &[
                serde_json::json!({"type":"assistant","session_id":"s","message":{"content":[{"type":"thinking"},{"type":"text","text":"x"}]}}),
                serde_json::json!({"type":"result","session_id":"s","subtype":"success","is_error":false,"stop_reason":"end_turn","result":"Ice floats."}),
            ],
        );
        assert_eq!(parse(&out, "s", false).unwrap(), "Ice floats.");
    }

    #[test]
    fn tools_without_permission_or_api_keys_are_rejected() {
        let tool = events(
            "s",
            &[
                serde_json::json!({"type":"assistant","session_id":"s","message":{"content":[{"type":"tool_use","name":"Bash"}]}}),
            ],
        );
        assert!(parse(&tool, "s", true).is_err());
        let search = events(
            "s",
            &[
                serde_json::json!({"type":"assistant","session_id":"s","message":{"content":[{"type":"tool_use","name":"web_search"}]}}),
            ],
        );
        assert!(parse(&search, "s", false).is_err());
        let key = line(
            serde_json::json!({"type":"system","subtype":"init","session_id":"s","apiKeySource":"env","permissionMode":"dontAsk"}),
        );
        assert!(parse(key.as_bytes(), "s", false).is_err());
    }

    #[test]
    fn profile_must_disable_api_keys_and_extensions() {
        let ok = serde_json::json!({"loginPolicy":{"apiKeyAuthDisabled":true},"hooks":[],"mcpServers":[{"disabled":true}],"plugins":[],"lspServers":[],"configSources":{"layers":[]}});
        assert!(check_profile(ok.to_string().as_bytes()).is_ok());
        let mut hook = ok.clone();
        hook["hooks"] = serde_json::json!([{"disabled": false}]);
        assert!(check_profile(hook.to_string().as_bytes()).is_err());
        let mut key = ok;
        key["loginPolicy"]["apiKeyAuthDisabled"] = Value::Bool(false);
        assert!(check_profile(key.to_string().as_bytes()).is_err());
    }
}
