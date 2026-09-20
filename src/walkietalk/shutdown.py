"""Local phrase-and-code shutdown in one or two utterances. Never opens PTT."""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from .config import Config
from .wake import strip_wake


def normalize_command(text: str) -> str:
    """Match words, ignoring case, whitespace, and STT punctuation; never fuzzy."""
    return " ".join(re.findall(r"\w+", text.casefold()))


@dataclass(frozen=True)
class ShutdownDecision:
    kind: str
    message: str = ""


class ShutdownSession:
    def __init__(self, config: Config, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.clock = clock
        self.armed_until: float | None = None
        self.phrases = {
            normalize_command(text)
            for text in (config.shutdown_phrase, *config.shutdown_phrase_aliases)
        } - {""}
        self.codes = {
            normalize_command(text)
            for text in (config.shutdown_code, *config.shutdown_code_aliases)
        } - {""}

    def close(self) -> None:
        self.armed_until = None

    def expire_if_needed(self) -> str | None:
        if self.armed_until is None or self.clock() < self.armed_until:
            return None
        self.close()
        return "Shutdown confirmation window expired; shutdown cancelled. Still listening."

    def decide(self, transcript: str, speech_started_at: float) -> ShutdownDecision:
        if not self.config.shutdown_enabled:
            return ShutdownDecision("none")
        # The command works independently of the wake gate, with or without
        # the configured wake name. Only complete command utterances match.
        _, traffic = strip_wake(transcript, self.config.wake_primary, self.config.wake_aliases)
        candidates = {normalize_command(transcript), normalize_command(traffic)}
        if any(f"{phrase} {code}" in candidates for phrase in self.phrases for code in self.codes):
            self.close()
            return ShutdownDecision("confirmed", "Shutdown confirmed; stopping walkietalk.")
        if candidates & self.phrases:
            self.armed_until = self.clock() + self.config.shutdown_confirmation_seconds
            return ShutdownDecision(
                "armed",
                "Shutdown armed. Send the confirmation code in a separate transmission "
                f"within {self.config.shutdown_confirmation_seconds:g}s.",
            )
        if self.armed_until is not None:
            valid = bool(candidates & self.codes) and speech_started_at < self.armed_until
            self.close()
            if valid:
                return ShutdownDecision("confirmed", "Shutdown confirmed; stopping walkietalk.")
            return ShutdownDecision(
                "rejected",
                "Shutdown cancelled: confirmation was incorrect, empty, or late. "
                "Still listening; say the wake phrase for ordinary traffic.",
            )
        if candidates & self.codes:
            return ShutdownDecision("rejected", "Shutdown code ignored: shutdown is not armed.")
        if any(
            candidate.startswith(value + " ")
            for candidate in candidates
            for value in self.phrases | self.codes
        ):
            return ShutdownDecision(
                "rejected",
                "Shutdown command not recognized: use the phrase alone or the phrase "
                "followed by the exact confirmation code. "
                "Shutdown is not armed.",
            )
        return ShutdownDecision("none")
