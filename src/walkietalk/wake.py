"""Wake-name matching and the conversation follow-up window."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .config import Config


def _wake_pattern(primary: str, aliases: Sequence[str]) -> re.Pattern[str]:
    names = [primary, *aliases]
    names = sorted({name.casefold() for name in names}, key=len, reverse=True)
    joined = "|".join(re.escape(name) for name in names)
    return re.compile(rf"^(?:{joined})(?:\s*[,.?!:;\-]+\s*|\s+|$)", re.IGNORECASE)


def strip_wake(text: str, primary: str, aliases: Sequence[str]) -> tuple[bool, str]:
    stripped = text.strip()
    if not stripped:
        return False, ""
    match = _wake_pattern(primary, aliases).match(stripped)
    if match is None:
        return False, stripped
    return True, stripped[match.end() :].strip()


@dataclass(frozen=True)
class GateDecision:
    accepted: bool
    kind: str
    traffic: str
    state: str
    message: str


class ListeningSession:
    """Decide whether a transcript is radio traffic. Never opens PTT."""

    def __init__(self, config: Config, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.clock = clock
        self.awake_until: float | None = None

    def state(self) -> str:
        if self.config.listening_mode == "conversation" and self._follow_up_open(self.clock()):
            return "awake"
        return "waiting_for_wake"

    def status_line(self) -> str:
        names = " / ".join((self.config.wake_primary, *self.config.wake_aliases))
        timeout = self.config.conversation_timeout_seconds
        if self.config.listening_mode == "conversation":
            mode = f"conversation (follow-up window {timeout:g}s after each reply)"
        else:
            mode = "wake_phrase (phrase required each time; follow-up window off)"
        if self.state() == "awake":
            remaining = max(0.0, (self.awake_until or 0) - self.clock())
            return f"Mode: {mode}. State: awake ({remaining:.0f}s left)."
        return f'Mode: {mode}. State: waiting for wake "{names}".'

    def _follow_up_open(self, at: float) -> bool:
        return self.awake_until is not None and at < self.awake_until

    def close(self) -> None:
        self.awake_until = None

    def expire_if_needed(self) -> str | None:
        if self.config.listening_mode != "conversation" or self.awake_until is None:
            return None
        if self.clock() < self.awake_until:
            return None
        self.close()
        return f'Follow-up window ended; say "{self.config.wake_primary}" first.'

    def complete_turn(self) -> None:
        if self.config.listening_mode == "conversation":
            self.awake_until = self.clock() + self.config.conversation_timeout_seconds
        else:
            self.awake_until = None

    def decide(self, transcript: str, speech_started_at: float) -> GateDecision:
        text = transcript.strip()
        state = self.state()
        if not text:
            return GateDecision(
                False, "empty", "", state, "Ignored (empty transcript). Window unchanged."
            )
        matched, traffic = strip_wake(text, self.config.wake_primary, self.config.wake_aliases)
        follow_up = self.config.listening_mode == "conversation" and self._follow_up_open(
            speech_started_at
        )
        if not matched and not follow_up:
            return GateDecision(
                False,
                "wake_required",
                text,
                "waiting_for_wake",
                f'Ignored (say "{self.config.wake_primary}" first).',
            )
        if matched and not traffic:
            if self.config.listening_mode == "conversation":
                return GateDecision(
                    False,
                    "wake_only",
                    "",
                    state,
                    "Wake heard; listening for traffic.",
                )
            return GateDecision(
                False,
                "empty",
                "",
                state,
                "Wake heard, but no traffic. Window unchanged.",
            )
        if matched:
            kind = "wake"
            note = "Accepted (wake name)."
        else:
            kind = "follow_up"
            note = "Accepted (follow-up)."
        return GateDecision(True, kind, traffic if matched else text, state, note)
