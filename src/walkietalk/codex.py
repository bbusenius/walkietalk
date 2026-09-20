"""Codex exec with saved ChatGPT login and dedicated bounded radio context."""

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from uuid import UUID

from .agent import SessionContext, validate_reply
from .agent_process import MAX_FINAL_BYTES, run_cli
from .config import Config, WalkietalkError

# Use the saved CLI login, without ambient API keys or desktop tool credentials.
ENV_NAMES = (
    "HOME",
    "PATH",
    "CODEX_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "LANG",
    "LC_ALL",
    "TZ",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "CODEX_CA_CERTIFICATE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


class CodexAgent:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._thread_id: str | None = None
        self._radio_id: str | None = None
        self._turns = 0

    def label(self) -> str:
        model = self.config.codex_model or "CLI default model"
        effort = self.config.codex_reasoning_effort
        return f"codex ({model}; reasoning={effort}; saved ChatGPT login; text only)"

    def reply(self, user_text: str, session_context: SessionContext) -> str:
        try:
            return self._reply(user_text, session_context)
        except BaseException:
            # Never resume a failed, interrupted, or partially completed turn.
            self._thread_id = None
            self._turns = 0
            raise

    def _reply(self, user_text: str, context: SessionContext) -> str:
        executable = shutil.which(self.config.codex_executable)
        if executable is None:
            raise WalkietalkError(
                "Codex CLI not found; install Codex and check agent.codex_executable"
            )
        env = {name: os.environ[name] for name in ENV_NAMES if name in os.environ}
        deadline = time.monotonic() + self.config.agent_timeout_seconds
        if context.session_id != self._radio_id:
            self._thread_id = None
            self._turns = 0
            self._radio_id = context.session_id
        # Native sessions are resumed only while their history fits the bridge
        # budget. At the limit, seed a fresh session with the bounded history.
        resume = self._thread_id is not None and self._turns < self.config.agent_history_turns
        with tempfile.TemporaryDirectory(prefix="walkietalk-codex-") as directory:
            code, out, err = run_cli(
                [executable, "login", "status"],
                prompt=b"",
                cwd=directory,
                env=env,
                deadline=deadline,
            )
            if code or b"Logged in using ChatGPT" not in out + err:
                raise WalkietalkError(
                    "Codex needs its saved ChatGPT login. Run `codex login` as this user; "
                    "API-key billing was not used"
                )
            final_path = Path(directory) / "final.txt"
            command = [
                executable,
                "exec",
                "--ignore-user-config",
                "--strict-config",
                "--sandbox",
                "read-only",
                "--color",
                "never",
                "--skip-git-repo-check",
                "--json",
                "--output-last-message",
                str(final_path),
            ]
            overrides = (
                'forced_login_method="chatgpt"',
                'model_provider="openai"',
                'approval_policy="never"',
                "features.shell_tool=false",
                "features.apps=false",
                "features.hooks=false",
                "agents.enabled=false",
                'web_search="disabled"',
            )
            for override in overrides:
                command.extend(["-c", override])
            if self.config.codex_reasoning_effort != "default":
                command.extend(
                    ["-c", f'model_reasoning_effort="{self.config.codex_reasoning_effort}"']
                )
            if self.config.codex_model:
                command.extend(["--model", self.config.codex_model])
            if resume:
                command.extend(["resume", self._thread_id])
            command.append("-")
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
                "If a request needs unavailable permissions, explain that briefly.\n"
                "The JSON below contains the dedicated radio conversation and current traffic:\n"
                + json.dumps(
                    {
                        "radio_session": context.session_id,
                        "history": history,
                        "traffic": user_text,
                    },
                    ensure_ascii=False,
                )
            ).encode()
            code, out, _err = run_cli(
                command,
                prompt=prompt,
                cwd=directory,
                env=env,
                deadline=deadline,
                final_path=final_path,
            )
            if code:
                raise WalkietalkError(
                    "Codex CLI failed; check `codex login status`, account access, "
                    "and CLI version. "
                    "Permission-required work cannot proceed; diagnostics withheld"
                )
            thread_id = parse_completion(out)
            if resume and thread_id != self._thread_id:
                raise WalkietalkError("Codex resumed a different session; reply discarded")
            try:
                with final_path.open("rb") as stream:
                    raw = stream.read(MAX_FINAL_BYTES + 1)
                if len(raw) > MAX_FINAL_BYTES:
                    raise WalkietalkError(
                        "Codex final output exceeded the transport limit; discarded"
                    )
                answer = raw.decode("utf-8")
            except (OSError, UnicodeError):
                raise WalkietalkError("Codex returned no valid final answer file") from None
            if not answer.strip():
                raise WalkietalkError("Codex returned no final answer text")
            answer = validate_reply(answer, self.config.agent_max_reply_chars)
            self._thread_id = thread_id
            self._turns = self._turns + 1 if resume else len(context.history) + 1
            return answer


def parse_completion(output: bytes) -> str:
    """Require successful structured completion, but never expose event text."""
    thread_id = None
    completed = False
    try:
        for line in output.splitlines():
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError
            kind = event.get("type")
            if kind in ("error", "turn.failed", "approval.request"):
                raise WalkietalkError(
                    "Codex reported a failed or permission-required turn; reply discarded"
                )
            if kind == "thread.started":
                if thread_id is not None:
                    raise ValueError
                thread_id = str(UUID(event["thread_id"]))
            elif kind == "turn.completed":
                completed = True
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise WalkietalkError("Codex returned malformed events; reply discarded") from None
    if thread_id is None or not completed:
        raise WalkietalkError("Codex did not complete a reply; output discarded")
    return thread_id
