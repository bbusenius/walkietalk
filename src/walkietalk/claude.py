"""Official Claude Code print mode, CLI-owned login, and bounded radio history."""

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from .agent import WEB_SEARCH_TURNS, SessionContext, validate_reply
from .agent_process import run_cli
from .config import Config, WalkietalkError

# Preserve the official CLI's login location, never inject provider keys, OAuth
# tokens, alternative gateways, desktop-agent variables, or tool configuration.
CLI_ENV = {
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
}
REQUIRED_FLAGS = ("--safe-mode", "--restricted", "--permission-prompts", "--no-session-persistence")


def cli_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key in CLI_ENV}


def _claude_block_allowed(block: object, *, web_search: bool) -> bool:
    if not isinstance(block, dict):
        return False
    kind = block.get("type")
    if kind in ("text", "thinking", "redacted_thinking"):
        return True
    return web_search and kind == "tool_use" and block.get("name") == "WebSearch"


def parse_result(output: bytes, session_id: str, model: str, *, web_search: bool = False) -> str:
    initialized = False
    final = None
    try:
        events = [json.loads(line) for line in output.splitlines() if line.strip()]
    except (ValueError, UnicodeError, RecursionError):
        raise WalkietalkError("Claude Code returned malformed JSON; reply discarded") from None
    for event in events:
        if isinstance(event, dict) and event.get("type") == "rate_limit_event" and initialized:
            # Official stream metadata, never answer text (including after a result).
            continue
        if not isinstance(event, dict) or final is not None:
            raise WalkietalkError("Claude Code returned an invalid event sequence; reply discarded")
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            if (
                initialized
                or event.get("session_id") != session_id
                or event.get("model") != model
                or event.get("tools") != (["WebSearch"] if web_search else [])
                or event.get("mcp_servers") != []
                or event.get("plugins", []) != []
            ):
                raise WalkietalkError(
                    "Claude Code session, model, or disabled tools did not match; reply discarded"
                )
            initialized = True
        elif kind == "assistant":
            message = event.get("message")
            if not initialized or not isinstance(message, dict):
                raise WalkietalkError("Claude Code returned invalid assistant data; discarded")
            content = message.get("content")
            if (
                event.get("error")
                or message.get("model") != model
                or not isinstance(content, list)
                or any(not _claude_block_allowed(block, web_search=web_search) for block in content)
            ):
                raise WalkietalkError("Claude Code reported an error or unexpected tools/model")
        elif kind == "user" and web_search:
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if (
                not isinstance(content, list)
                or not content
                or any(
                    not isinstance(block, dict) or block.get("type") != "tool_result"
                    for block in content
                )
            ):
                raise WalkietalkError("Claude Code returned an unexpected event; reply discarded")
        elif kind == "result":
            if (
                not initialized
                or event.get("subtype") != "success"
                or event.get("is_error") is not False
                or event.get("session_id") != session_id
                or event.get("permission_denials")
                or event.get("stop_reason") not in (None, "end_turn", "stop_sequence", "refusal")
                or not isinstance(event.get("result"), str)
            ):
                raise WalkietalkError(
                    "Claude Code did not complete successfully; check CLI login, usage limits, "
                    "and model access. Diagnostics withheld"
                )
            final = event["result"]
        else:
            # Hook, tool, permission, and unknown events never enter the reply path.
            raise WalkietalkError("Claude Code returned an unexpected event; reply discarded")
    if final is None:
        raise WalkietalkError("Claude Code returned no complete final answer")
    return final


class ClaudeCodeAgent:
    def __init__(self, config: Config) -> None:
        self.config = config

    def label(self) -> str:
        return (
            f"claude ({self.config.claude_model}; CLI saved login; "
            f"reasoning {self.config.claude_reasoning_effort})"
        )

    def reply(self, user_text: str, session_context: SessionContext) -> str:
        deadline = time.monotonic() + self.config.agent_timeout_seconds
        env = cli_environment()
        executable = shutil.which(self.config.claude_executable, path=env.get("PATH", ""))
        if executable is None:
            raise WalkietalkError(
                "Claude Code CLI not found; install the official CLI and check "
                "agent.claude_executable"
            )
        with tempfile.TemporaryDirectory(prefix="walkietalk-claude-") as directory:

            def run(arguments, prompt=b""):
                return run_cli(
                    [executable, *arguments],
                    prompt=prompt,
                    cwd=directory,
                    env=env,
                    deadline=deadline,
                    name="Claude Code",
                    executable_setting="agent.claude_executable",
                )

            code, help_text, _ = run(["--help"])
            if code or any(flag.encode() not in help_text for flag in REQUIRED_FLAGS):
                raise WalkietalkError(
                    "Claude Code CLI lacks required isolation options; update the official CLI "
                    "(verified with 2.1.277). No answer requested"
                )
            isolation = [
                "--safe-mode",
                "--restricted",
                "--setting-sources",
                "",
                "--settings",
                '{"disableAllHooks":true}',
            ]
            code, status, _ = run([*isolation, "auth", "status", "--json"])
            try:
                auth = json.loads(status)
            except (ValueError, UnicodeError, RecursionError):
                auth = None
            if (
                code
                or not isinstance(auth, dict)
                or auth.get("loggedIn") is not True
                or auth.get("authMethod") != "claude.ai"
                or auth.get("apiProvider") != "firstParty"
            ):
                raise WalkietalkError(
                    "Claude Code requires its own saved Claude account login; run `claude auth "
                    "login` yourself. For billed API-key access select claude_api explicitly. "
                    "No fallback was attempted"
                )
            system_path = Path(directory) / "system.txt"
            system_path.write_text(session_context.instructions)
            arguments = [
                *isolation,
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--tools",
                "WebSearch" if self.config.agent_web_search else "",
                "--disallowedTools",
                "mcp__*",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--disable-slash-commands",
                "--no-chrome",
                "--permission-mode",
                "dontAsk",
                "--permission-prompts",
                "none",
                "--no-session-persistence",
                "--session-id",
                session_context.session_id,
                "--system-prompt-file",
                str(system_path),
                "--max-turns",
                str(WEB_SEARCH_TURNS) if self.config.agent_web_search else "1",
                "--model",
                self.config.claude_model,
            ]
            if self.config.claude_reasoning_effort != "default":
                arguments.extend(["--effort", self.config.claude_reasoning_effort])
            # Replay only bridge-owned completed turns; never resume a desktop session.
            prompt = json.dumps(
                {
                    "radio_session": session_context.session_id,
                    "history": [
                        {"user": turn.user_text, "assistant": turn.reply_text}
                        for turn in session_context.history
                    ],
                    "traffic": user_text,
                }
            ).encode()
            code, output, _ = run(arguments, prompt)
            if code:
                raise WalkietalkError(
                    "Claude Code failed; check CLI login, usage limits, model access, and "
                    "installed CLI options. Diagnostics withheld; no fallback attempted"
                )
            answer = parse_result(
                output,
                session_context.session_id,
                self.config.claude_model,
                web_search=self.config.agent_web_search,
            )
            return validate_reply(answer, self.config.agent_max_reply_chars)
