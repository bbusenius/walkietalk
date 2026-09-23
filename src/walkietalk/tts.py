"""Voice plug: text to bounded PCM, without radio or audio-device access."""

import os
import shutil
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Protocol

import numpy as np

from .agent import validate_reply
from .agent_process import run_cli
from .audio import Wav, read_wav
from .config import Config, WalkietalkError

RADIO_RATE = 48000


def speech_byte_budget(max_seconds: float, rate: int = RADIO_RATE) -> int:
    """PCM bytes for the configured TX window, plus WAV header slack."""
    return int(max(max_seconds, 0) * rate * 2) + 4096


MAX_TTS_BYTES = speech_byte_budget(10)


class TtsBackend(Protocol):
    def label(self) -> str: ...
    def prepare(self) -> None: ...
    def synthesize(self, text: str, *, truncate: bool = True) -> Wav: ...


def _crop(wav: Wav, maximum: float) -> Wav:
    count = min(len(wav.frames) // 2, max(0, int(maximum * wav.rate)))
    if count < 1:
        raise WalkietalkError("Spoken reply exceeds the transmit limit; no transmission")
    frames = wav.frames[: count * 2]
    return Wav(frames, wav.rate, count / wav.rate)


def radio_wav(wav: Wav, maximum: float, *, truncate: bool = False) -> Wav:
    """Validate actual PCM duration and convert speech to the AIOC's 48 kHz."""
    if (
        not isinstance(wav, Wav)
        or not isinstance(wav.frames, bytes)
        or type(wav.rate) is not int
        or wav.rate not in {8000, 11025, 12000, 16000, 22050, 24000, 32000, 48000}
        or not wav.frames
        or len(wav.frames) % 2
    ):
        raise WalkietalkError("Voice returned invalid PCM audio; no transmission")
    duration = len(wav.frames) / (2 * wav.rate)
    if duration > maximum:
        if not truncate:
            raise WalkietalkError(
                f"Spoken reply is {duration:.2f}s; maximum is {maximum:g}s after PTT settle. "
                "Ask for a shorter answer; no transmission"
            )
        wav = _crop(wav, maximum)
        duration = wav.duration
    if wav.rate == RADIO_RATE:
        return Wav(wav.frames, RADIO_RATE, duration)
    samples = np.frombuffer(wav.frames, dtype="<i2")
    count = max(1, round(len(samples) * RADIO_RATE / wav.rate))
    # All accepted source rates are <= 48 kHz: only speech upsampling is needed.
    values = np.interp(np.arange(count) * wav.rate / RADIO_RATE, np.arange(len(samples)), samples)
    frames = np.rint(values).clip(-32768, 32767).astype("<i2").tobytes()
    cropped = Wav(frames, RADIO_RATE, count / RADIO_RATE)
    if cropped.duration > maximum:
        if not truncate:
            raise WalkietalkError("Spoken reply exceeds the transmit limit; no transmission")
        return _crop(cropped, maximum)
    return cropped


def level_wav(wav: Wav, normalize: str) -> Wav:
    """Optionally scale synthesized PCM. off leaves the engine's level unchanged."""
    if normalize == "off":
        return wav
    samples = np.frombuffer(wav.frames, dtype="<i2").astype(np.int32)
    peak = int(np.max(np.abs(samples))) if samples.size else 0
    if peak == 0 or peak == 32767:
        return wav
    frames = np.rint(samples * (32767 / peak)).clip(-32768, 32767).astype("<i2").tobytes()
    return Wav(frames, wav.rate, wav.duration)


def write_wav(path: Path, wav: Wav) -> None:
    """Create a new WAV without overwriting an existing recording."""
    with path.open("xb") as output:
        with wave.open(output, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(wav.rate)
            writer.writeframes(wav.frames)


class PiperTts:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.executable: str | None = None
        self.model = Path(config.piper_model).expanduser().resolve()

    def label(self) -> str:
        return f"piper ({self.model.name}; local voice)"

    def prepare(self) -> None:
        search_path = os.pathsep.join(
            (str(Path(sys.executable).parent), os.environ.get("PATH", ""))
        )
        self.executable = shutil.which(self.config.piper_executable, path=search_path)
        if not self.executable:
            raise WalkietalkError(
                "Piper CLI not found; install walkietalk's piper extra or set tts.piper_executable"
            )
        for path in (self.model, Path(str(self.model) + ".json")):
            if not path.is_file():
                raise WalkietalkError(
                    f"Piper voice file missing: {path}. Download the voice explicitly; "
                    "no automatic download or fallback"
                )

    def synthesize(self, text: str, *, truncate: bool = True) -> Wav:
        deadline = time.monotonic() + self.config.tts_timeout_seconds
        text = validate_reply(text, self.config.agent_max_reply_chars)
        self.prepare()
        with tempfile.TemporaryDirectory(prefix="walkietalk-piper-") as directory:
            path = Path(directory) / "voice.wav"
            code, _, _ = run_cli(
                [self.executable, "--model", str(self.model), "--output-file", str(path)],
                prompt=(text + "\n").encode(),
                cwd=directory,
                env={
                    key: value
                    for key, value in os.environ.items()
                    if key in {"HOME", "PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH"}
                },
                deadline=deadline,
                final_path=path,
                max_final_bytes=speech_byte_budget(self.config.max_tx_seconds) * 2,
                name="Piper",
                executable_setting="tts.piper_executable",
            )
            if code:
                raise WalkietalkError(
                    "Piper synthesis failed; diagnostics withheld; no transmission"
                )
            if (
                not path.is_file()
                or path.stat().st_size > speech_byte_budget(self.config.max_tx_seconds) * 2
            ):
                raise WalkietalkError("Piper returned no bounded WAV file; no transmission")
            wav = read_wav(path, self.config.max_tx_seconds * 2)
            wav = radio_wav(
                wav, self.config.max_tx_seconds - self.config.settle_seconds, truncate=truncate
            )
            wav = level_wav(wav, self.config.tts_normalize)
            if time.monotonic() >= deadline:
                raise WalkietalkError("Piper timed out; audio discarded; no transmission")
            return wav


def open_tts(config: Config) -> TtsBackend:
    if config.tts_backend == "piper":
        return PiperTts(config)
    if config.tts_backend in {"grok", "grok_api"}:
        from .grok_tts import GrokTts

        return GrokTts(config)
    if config.tts_backend == "hermes":
        from .hermes_tts import HermesTts

        return HermesTts(config)
    raise WalkietalkError("tts.backend must be piper, grok, grok_api, or hermes; no fallback")
