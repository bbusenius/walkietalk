"""Wake/sleep phrase matching and the conversation follow-up window."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .config import MESSAGING_SERVICES, Config


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
    destination: str = ""


def configured_wakes(config: Config) -> list[tuple[str, str, tuple[str, ...]]]:
    """Agent wake first, then each enabled messaging wake: destination, primary, aliases."""
    wakes = [("agent", config.wake_primary, config.wake_aliases)]
    for destination in MESSAGING_SERVICES:
        mode = getattr(config, destination)
        if mode.enabled():
            wakes.append((destination, mode.wake, mode.aliases))
    return wakes


def transcription_keyterms(config: Config) -> tuple[str, ...]:
    terms = [
        term for _, primary, aliases in configured_wakes(config) for term in (primary, *aliases)
    ]
    if config.sleep_primary:
        terms.extend((config.sleep_primary, *config.sleep_aliases))
    return tuple(dict.fromkeys(terms))


def match_wake(text: str, config: Config) -> tuple[str, str]:
    """Longest configured wake prefix wins. Returns destination and the traffic after it."""
    stripped = text.strip()
    best: tuple[int, str, str] | None = None
    for destination, primary, aliases in configured_wakes(config):
        matched, traffic = strip_wake(stripped, primary, aliases)
        if not matched:
            continue
        consumed = len(stripped) - len(traffic)
        if best is None or consumed > best[0]:
            best = (consumed, destination, traffic)
    if best is None:
        return "", stripped
    return best[1], best[2]


class ListeningSession:
    """Decide whether a transcript is radio traffic. Never opens PTT."""

    def __init__(self, config: Config, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.clock = clock
        self.awake_until: float | None = None
        self.destination = ""

    @property
    def listening_mode(self) -> str:
        if self.destination in MESSAGING_SERVICES:
            return getattr(self.config, self.destination).listening_mode
        return self.config.listening_mode

    @property
    def conversation_timeout_seconds(self) -> float:
        if self.destination in MESSAGING_SERVICES:
            return getattr(self.config, self.destination).conversation_timeout_seconds
        return self.config.conversation_timeout_seconds

    def state(self) -> str:
        if self.listening_mode == "conversation" and self._follow_up_open(self.clock()):
            return "awake"
        return "waiting_for_wake"

    @property
    def wake_names(self) -> tuple[str, ...]:
        if self.destination in MESSAGING_SERVICES:
            mode = getattr(self.config, self.destination)
            return (mode.wake, *mode.aliases)
        return (self.config.wake_primary, *self.config.wake_aliases)

    def status_line(self) -> str:
        names = " / ".join(self.wake_names)
        timeout = self.conversation_timeout_seconds
        if self.listening_mode == "conversation":
            mode = f"conversation (follow-up window {timeout:g}s after each reply)"
        else:
            mode = "wake_phrase (phrase required each time; follow-up window off)"
        if self.state() == "awake":
            remaining = max(0.0, (self.awake_until or 0) - self.clock())
            party = f", {self.destination}" if self.destination not in ("", "agent") else ""
            return f"Mode: {mode}. State: awake ({remaining:.0f}s left{party})."
        return f'Mode: {mode}. State: waiting for wake "{names}".'

    def _follow_up_open(self, at: float) -> bool:
        return self.awake_until is not None and at < self.awake_until

    def follow_up_open_at(self, at: float) -> bool:
        """True when conversation follow-up accepts traffic that started at ``at``."""
        return self.listening_mode == "conversation" and self._follow_up_open(at)

    def close(self) -> None:
        self.awake_until = None

    def expire_if_needed(self) -> str | None:
        if self.listening_mode != "conversation" or self.awake_until is None:
            return None
        if self.clock() < self.awake_until:
            return None
        self.close()
        return f'Follow-up window ended; say "{self.wake_names[0]}" first.'

    def complete_turn(self) -> None:
        if self.listening_mode == "conversation":
            self.awake_until = self.clock() + self.conversation_timeout_seconds
        else:
            self.awake_until = None

    def decide(self, transcript: str, speech_started_at: float) -> GateDecision:
        text = transcript.strip()
        state = self.state()
        if not text:
            return GateDecision(
                False, "empty", "", state, "Ignored (empty transcript). Window unchanged."
            )
        destination, traffic = match_wake(text, self.config)
        matched = bool(destination)
        if self.config.sleep_primary:
            from .shutdown import normalize_command

            sleeps = {
                normalize_command(phrase)
                for phrase in (self.config.sleep_primary, *self.config.sleep_aliases)
            }
            if sleeps & {normalize_command(text), normalize_command(traffic)}:
                wake = self.wake_names[0]
                self.close()
                self.destination = ""
                return GateDecision(
                    False,
                    "sleep",
                    "",
                    "waiting_for_wake",
                    f'Sleep heard; say "{wake}" when you need me.',
                )
        follow_up = self.listening_mode == "conversation" and self._follow_up_open(
            speech_started_at
        )
        if not matched and not follow_up:
            return GateDecision(
                False,
                "wake_required",
                text,
                "waiting_for_wake",
                f'Ignored (say "{self.wake_names[0]}" first).',
            )
        if not matched:
            destination = self.destination or "agent"
        if matched and not traffic:
            self.destination = destination
            if self.listening_mode == "conversation":
                note = "Wake heard; listening for traffic."
            else:
                note = "Wake heard, but no traffic. Window unchanged."
            return GateDecision(False, "wake_only", "", state, note, destination)
        self.destination = destination
        if matched:
            kind = "wake"
            note = "Accepted (wake name)."
            body = traffic
        else:
            kind = "follow_up"
            note = "Accepted (follow-up)."
            body = text
        return GateDecision(True, kind, body, state, note, destination)
