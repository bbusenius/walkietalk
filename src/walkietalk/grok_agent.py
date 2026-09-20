"""Grok Build headless agent with CLI-owned login; unrelated to Voice Transcribe."""

import json
import os
import shutil
import tempfile
import time
import tomllib
from pathlib import Path
from uuid import UUID, uuid4

from .agent import SessionContext, validate_reply
from .agent_process import run_cli
from .config import Config, WalkietalkError

ENV_NAMES = (
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
)


def cli_environment() -> dict[str, str]:
    env = {name: os.environ[name] for name in ENV_NAMES if name in os.environ}
    env.update(
        GROK_DISABLE_API_KEY_AUTH="1",
        GROK_DISABLE_AUTOUPDATER="1",
        GROK_MANAGED_MCPS_ENABLED="0",
        GROK_MANAGED_MCP_GATEWAY_TOOLS_ENABLED="0",
        GROK_MEMORY="0",
        GROK_SUBAGENTS="0",
        GROK_WEB_FETCH="0",
        GROK_WRITE_FILE="0",
        GROK_LSP_TOOLS="0",
        GROK_CAMPAIGNS="0",
        GROK_TOOL_SEARCH="0",
    )
    for vendor in ("CLAUDE", "CURSOR", "CODEX"):
        for kind in ("SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS"):
            env[f"GROK_{vendor}_{kind}_ENABLED"] = "0"
    return env


def inspect_profile(output: bytes) -> None:
    """Check effective restrictions before inference, without displaying config."""
    try:
        profile = json.loads(output)
        if profile["loginPolicy"]["apiKeyAuthDisabled"] is not True:
            raise ValueError
        for key in ("hooks", "mcpServers", "plugins", "lspServers"):
            entries = profile[key]
            if not isinstance(entries, list):
                raise ValueError
            if any(entry.get("disabled") is not True for entry in entries):
                raise WalkietalkError(
                    "Grok Build radio mode requires inactive hooks, MCP servers, plugins, "
                    "and LSP servers; inspect the profile with `grok inspect`"
                )
        # API-key prohibition applies to first-party endpoints. Reject custom
        # model/provider overrides so a BYOK endpoint cannot evade that policy.
        layers = profile["configSources"]["layers"]
        if not isinstance(layers, list):
            raise ValueError
        for layer in layers:
            path = Path(layer["path"])
            if not path.is_absolute():
                raise ValueError
            with path.open("rb") as stream:
                raw = stream.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError
            config = tomllib.loads(raw.decode())
            if any(config.get(key) for key in ("model", "model_providers", "auth_provider")):
                raise WalkietalkError(
                    "Grok Build radio mode requires the standard first-party model catalog; "
                    "custom provider/model credentials are not supported"
                )
    except (ValueError, TypeError, KeyError, AttributeError, OSError, RecursionError):
        raise WalkietalkError(
            "Cannot verify Grok Build login policy and profile; check CLI version "
            "and `grok inspect`. No question was sent"
        ) from None


class GrokAgent:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._thread_id: str | None = None
        self._radio_id: str | None = None
        self._turns = 0

    def label(self) -> str:
        effort = self.config.grok_reasoning_effort
        return (
            f"grok ({self.config.grok_model}; reasoning={effort}; "
            "Grok Build saved login; text only)"
        )

    def reply(self, user_text: str, session_context: SessionContext) -> str:
        try:
            return self._reply(user_text, session_context)
        except BaseException:
            self._thread_id = None
            self._turns = 0
            raise

    def _reply(self, user_text: str, context: SessionContext) -> str:
        deadline = time.monotonic() + self.config.agent_timeout_seconds
        executable = shutil.which(self.config.grok_executable)
        if executable is None:
            raise WalkietalkError(
                "Grok Build CLI not found; install it and check agent.grok_executable"
            )
        env = cli_environment()
        home = Path(env.get("GROK_HOME", str(Path.home() / ".grok"))).expanduser()
        home = home.resolve()
        if not (home / "auth.json").is_file():
            raise WalkietalkError(
                "Grok Build saved login is missing; run `grok login` as this user. "
                "API-key billing was not used"
            )
        if context.session_id != self._radio_id:
            self._thread_id = None
            self._turns = 0
            self._radio_id = context.session_id
        resume = self._thread_id is not None and self._turns < self.config.agent_history_turns
        thread_id = self._thread_id if resume else str(uuid4())
        with tempfile.TemporaryDirectory(prefix="walkietalk-grok-") as directory:
            # This CLI version's headless MCP discovery does not consistently
            # honor compatibility flags. Give it an empty HOME while retaining
            # the real GROK_HOME for CLI-owned login, refresh, and sessions.
            isolated_home = Path(directory) / "home"
            isolated_home.mkdir(mode=0o700)
            env["HOME"] = str(isolated_home)
            env["GROK_HOME"] = str(home)
            common = dict(
                cwd=directory,
                env=env,
                deadline=deadline,
                name="Grok Build",
                executable_setting="agent.grok_executable",
            )
            code, out, _err = run_cli([executable, "inspect", "--json"], prompt=b"", **common)
            if code:
                raise WalkietalkError("Grok Build profile check failed; diagnostics withheld")
            inspect_profile(out)
            command = [
                executable,
                "--prompt-file",
                "/dev/stdin",
                "--verbatim",
                "--output-format",
                "streaming-messages-json",
                "--permission-mode",
                "dontAsk",
                "--sandbox",
                "read-only",
                "--tools",
                "read_file",
                "--disallowed-tools",
                "read_file",
                "--deny",
                "*",
                "--no-subagents",
                "--disable-web-search",
                "--no-plan",
                "--max-turns",
                "1",
                "--model",
                self.config.grok_model,
                "--resume" if resume else "--session-id",
                thread_id,
            ]
            if self.config.grok_reasoning_effort != "default":
                command.extend(["--reasoning-effort", self.config.grok_reasoning_effort])
            history = (
                []
                if resume
                else [
                    {"user": turn.user_text, "assistant": turn.reply_text}
                    for turn in context.history
                ]
            )
            prompt = (
                context.instructions + "\nThis radio adapter answers with text only. "
                "Do not use tools, execute commands, change files, or contact other people. "
                "If the request needs unavailable permissions, explain that briefly.\n"
                + json.dumps(
                    {"radio_session": context.session_id, "history": history, "traffic": user_text},
                    ensure_ascii=False,
                )
            ).encode()
            code, out, _err = run_cli(command, prompt=prompt, **common)
            if code:
                raise WalkietalkError(
                    "Grok Build failed; check `grok login`, account access, and CLI version. "
                    "No API-key fallback; diagnostics withheld"
                )
            answer = parse_completion(out, thread_id)
            answer = validate_reply(answer, self.config.agent_max_reply_chars)
            self._thread_id = thread_id
            self._turns = self._turns + 1 if resume else len(context.history) + 1
            return answer


def parse_completion(output: bytes, expected_id: str) -> str:
    """Only the successful terminal result is an answer, never reasoning or logs."""
    initialized = False
    result = None
    try:
        for line in output.splitlines():
            event = json.loads(line)
            if not isinstance(event, dict) or result is not None:
                raise ValueError
            if event.get("session_id") != expected_id:
                raise ValueError
            UUID(event["session_id"])
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                if initialized or event.get("apiKeySource") != "oauth":
                    raise ValueError
                if event.get("permissionMode") != "dontAsk":
                    raise ValueError
                if any(
                    server.get("status") != "disabled" for server in event.get("mcp_servers", [])
                ):
                    raise ValueError
                initialized = True
            elif not initialized:
                raise ValueError
            elif kind == "assistant":
                for block in event["message"]["content"]:
                    if block.get("type") not in ("text", "thinking", "redacted_thinking"):
                        raise ValueError
            elif kind == "result":
                if (
                    event.get("subtype") != "success"
                    or event.get("is_error") is not False
                    or event.get("stop_reason") != "end_turn"
                    or event.get("errors")
                ):
                    raise ValueError
                result = event["result"]
                if not isinstance(result, str) or not result.strip():
                    raise ValueError
            else:
                raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise WalkietalkError(
            "Grok Build returned an incomplete, failed, or unexpected result; reply discarded"
        ) from None
    if result is None:
        raise WalkietalkError("Grok Build did not complete a reply; output discarded")
    return result
