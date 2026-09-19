"""Energy-based speech detection with silence hangover and a maximum length."""

import array
import math
import sys
from collections import deque
from dataclasses import dataclass, field

FRAME_MS = 20
MIN_SPEECH_MS = 250
SETTLE_FRAMES = 10


def frame_samples(rate: int) -> int:
    samples = rate * FRAME_MS // 1000
    if samples < 1:
        raise ValueError("Sample rate is too low for 20 ms frames")
    return samples


def rms(frame: bytes) -> float:
    if len(frame) < 2:
        return 0.0
    samples = array.array("h", frame[: len(frame) // 2 * 2])
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768.0


@dataclass
class EnergyVad:
    energy_threshold: float
    hangover_ms: int
    max_utterance_seconds: float
    rate: int
    speaking: bool = False
    hangover: int = 0
    speech_frames: int = 0
    voiced_frames: int = 0
    peak_rms: float = 0.0
    first_speech_rms: float = 0.0
    end_reason: str | None = None
    captured: bytearray = field(default_factory=bytearray)
    preroll: deque[bytes] = field(init=False)

    def __post_init__(self) -> None:
        self.frame_bytes = frame_samples(self.rate) * 2
        self.hangover_frames = max(1, round(self.hangover_ms / FRAME_MS))
        self.max_frames = max(1, round(self.max_utterance_seconds * 1000 / FRAME_MS))
        self.preroll = deque(maxlen=self.hangover_frames)

    def duration(self) -> float:
        return len(self.captured) / (2 * self.rate) if self.rate else 0.0

    def voiced_ms(self) -> int:
        return self.voiced_frames * FRAME_MS

    def _reset_utterance(self) -> None:
        self.speaking = False
        self.hangover = 0
        self.speech_frames = 0
        self.voiced_frames = 0
        self.first_speech_rms = 0.0
        self.end_reason = None
        self.captured = bytearray()
        self.preroll.clear()

    def push(self, frame: bytes) -> tuple[str, float]:
        if len(frame) < self.frame_bytes:
            frame = frame + b"\x00" * (self.frame_bytes - len(frame))
        elif len(frame) > self.frame_bytes:
            frame = frame[: self.frame_bytes]
        level = rms(frame)
        self.peak_rms = max(self.peak_rms, level)
        if not self.speaking:
            if level < self.energy_threshold:
                self.preroll.append(frame)
                return "waiting", level
            self.speaking = True
            self.first_speech_rms = level
            for prior in self.preroll:
                self.captured.extend(prior)
            self.preroll.clear()
            self.captured.extend(frame)
            self.speech_frames = 1
            self.voiced_frames = 1
            self.hangover = self.hangover_frames
            return "speaking", level
        self.captured.extend(frame)
        self.speech_frames += 1
        if self.speech_frames >= self.max_frames:
            self.end_reason = "max"
            return "finished", level
        if level >= self.energy_threshold:
            self.hangover = self.hangover_frames
            self.voiced_frames += 1
        else:
            self.hangover -= 1
            if self.hangover <= 0:
                if self.voiced_ms() < MIN_SPEECH_MS:
                    self._reset_utterance()
                    return "waiting", level
                self.end_reason = "silence"
                return "finished", level
        return "speaking", level
