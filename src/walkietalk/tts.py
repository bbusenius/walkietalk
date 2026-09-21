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

MAX_TTS_BYTES = 3_000_000
RADIO_RATE = 48000


class TtsBackend(Protocol):
    def label(self) -> str: ...
    def prepare(self) -> None: ...
    def synthesize(self, text: str) -> Wav: ...


def radio_wav(wav: Wav, maximum: float) -> Wav:
    """Validate actual PCM duration and convert speech to the AIOC's 48 kHz."""
    if (
        not isinstance(wav, Wav)
        or not isinstance(wav.frames, bytes)
        or type(wav.rate) is not int
        or wav.rate not in {8000, 11025, 12000, 16000, 22050, 24000, 32000, 48000}
        or not wav.frames
        or len(wav.frames) % 2
        or len(wav.frames) > MAX_TTS_BYTES
    ):
        raise WalkietalkError("Voice returned invalid PCM audio; no transmission")
    duration = len(wav.frames) / (2 * wav.rate)
    if duration > maximum:
        raise WalkietalkError(
            f"Spoken reply is {duration:.2f}s; maximum is {maximum:g}s after PTT settle. "
            "Ask for a shorter answer; no transmission"
        )
    if wav.rate == RADIO_RATE:
        return Wav(wav.frames, RADIO_RATE, duration)
    samples = np.frombuffer(wav.frames, dtype="<i2")
    count = max(1, round(len(samples) * RADIO_RATE / wav.rate))
    # All accepted source rates are <= 48 kHz: only speech upsampling is needed.
    values = np.interp(np.arange(count) * wav.rate / RADIO_RATE, np.arange(len(samples)), samples)
    frames = np.rint(values).clip(-32768, 32767).astype("<i2").tobytes()
    if count / RADIO_RATE > maximum:
        raise WalkietalkError("Spoken reply exceeds the transmit limit; no transmission")
    return Wav(frames, RADIO_RATE, count / RADIO_RATE)


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

    def synthesize(self, text: str) -> Wav:
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
                max_final_bytes=MAX_TTS_BYTES,
                name="Piper",
                executable_setting="tts.piper_executable",
            )
            if code:
                raise WalkietalkError(
                    "Piper synthesis failed; diagnostics withheld; no transmission"
                )
            if not path.is_file() or path.stat().st_size > MAX_TTS_BYTES:
                raise WalkietalkError("Piper returned no bounded WAV file; no transmission")
            # Parse at the absolute radio ceiling, then apply this config's shorter cap.
            wav = read_wav(path, 30)
            wav = radio_wav(wav, self.config.max_tx_seconds - self.config.settle_seconds)
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
    raise WalkietalkError("tts.backend must be piper, grok, or grok_api; no fallback")
