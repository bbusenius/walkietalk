"""Text-only agent contract and bounded, dedicated radio conversation history."""

from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from .config import AGENT_BACKEND_ERROR, Config, WalkietalkError

STUB_REPLY = "This is a pretend answer. The radio bridge brought me your words."
MAX_TRAFFIC_CHARS = 4000


@dataclass(frozen=True)
class Turn:
    user_text: str
    reply_text: str


@dataclass(frozen=True)
class SessionContext:
    session_id: str
    instructions: str
    history: tuple[Turn, ...]


class AgentBackend(Protocol):
    """Return only final answer text; raise a safe local error on failure.

    Transport adapters must bound and cancel their work, extract only the final
    answer, and translate diagnostics into errors without exposing credentials.
    They must not perform radio capture, playback, or PTT operations.
    """

    def label(self) -> str: ...
    def reply(self, user_text: str, session_context: SessionContext) -> str: ...


class StubAgent:
    """A fixed answer, in process, without accounts, I/O, or network access."""

    def label(self) -> str:
        return "stub (offline pretend answer)"

    def reply(self, user_text: str, session_context: SessionContext) -> str:
        return STUB_REPLY


def open_agent(config: Config) -> AgentBackend:
    if config.agent_backend == "stub":
        return StubAgent()
    if config.agent_backend == "hermes":
        from .hermes import HermesAgent

        return HermesAgent(config)
    if config.agent_backend == "codex":
        from .codex import CodexAgent

        return CodexAgent(config)
    if config.agent_backend == "grok":
        from .grok_agent import GrokAgent

        return GrokAgent(config)
    if config.agent_backend == "claude":
        from .claude import ClaudeCodeAgent

        return ClaudeCodeAgent(config)
    if config.agent_backend == "claude_api":
        from .claude_api import ClaudeApiAgent

        return ClaudeApiAgent(config)
    raise WalkietalkError(AGENT_BACKEND_ERROR)


class AgentSession:
    """One backend and one bounded history per invocation; no desktop sessions.

    Wake-window expiry does not erase history. Restarting the command creates a
    fresh session, including when selecting a different backend in config.
    """

    def __init__(
        self, config: Config, backend: AgentBackend, *, spoken_seconds: float | None = None
    ) -> None:
        self.config = config
        self.backend = backend
        self.session_id = str(uuid4())
        self.history: tuple[Turn, ...] = ()
        self.spoken_seconds = spoken_seconds

    def reply(self, user_text: str) -> str:
        traffic = user_text.strip()
        if not traffic:
            raise WalkietalkError("Agent needs non-empty traffic")
        if len(traffic) > MAX_TRAFFIC_CHARS:
            raise WalkietalkError(f"Agent traffic exceeds {MAX_TRAFFIC_CHARS} characters")
        context = SessionContext(
            session_id=self.session_id,
            instructions=(
                "Answer in short, plain, spoken-style sentences suitable for a family. "
                "Return only the final answer, without Markdown or tool diagnostics. "
                f"Use at most {self.config.agent_max_reply_chars} characters."
                + (
                    f" This answer will be spoken on a radio. Aim for one short sentence, "
                    f"at most {max(1, int(self.spoken_seconds * 2))} words, to fit within "
                    f"{self.spoken_seconds:g} seconds."
                    if self.spoken_seconds is not None
                    else ""
                )
            ),
            history=self.history,
        )
        answer = self.backend.reply(traffic, context)
        answer = validate_reply(answer, self.config.agent_max_reply_chars)
        self.history = (*self.history, Turn(traffic, answer))[-self.config.agent_history_turns :]
        return answer


def validate_reply(answer: str, max_chars: int) -> str:
    """Reject invalid final text before retaining it in any conversation."""
    if not isinstance(answer, str) or not answer.strip():
        raise WalkietalkError("Agent returned no final answer text")
    answer = answer.strip()
    if len(answer) > max_chars:
        raise WalkietalkError(
            f"Agent reply exceeds agent.max_reply_chars ({max_chars}); reply discarded"
        )
    if any(not char.isprintable() and char not in "\n\r\t" for char in answer):
        raise WalkietalkError("Agent returned control characters; reply discarded")
    answer = " ".join(answer.split())
    return answer
