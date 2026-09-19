"""Bounded PCM WAV input and a supervised playback worker."""

import array
import os
import selectors
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from .config import WalkietalkError


@dataclass(frozen=True)
class Wav:
    frames: bytes
    rate: int
    duration: float


def read_wav(path: Path, maximum: float, gain: float = 1) -> Wav:
    try:
        with wave.open(str(path), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getcomptype() != "NONE":
                raise WalkietalkError("Use an uncompressed mono, 16-bit PCM WAV file")
            rate, count = wav.getframerate(), wav.getnframes()
            if rate not in {8000, 11025, 12000, 16000, 22050, 24000, 32000, 48000}:
                raise WalkietalkError("Unsupported WAV rate; use 48000 Hz for the AIOC")
            duration = count / rate
            if count == 0 or duration > maximum:
                raise WalkietalkError(
                    f"WAV must be nonempty and no longer than {maximum:g} seconds"
                )
            frames = wav.readframes(count)
            if len(frames) != count * 2:
                raise WalkietalkError("Truncated WAV data; refusing transmission")
    except (OSError, EOFError, wave.Error) as exc:
        raise WalkietalkError(f"Cannot read WAV {path}: {exc}") from exc
    samples = array.array("h", frames)
    if sys.byteorder != "little":
        samples.byteswap()
    samples = array.array("h", (round(sample * gain) for sample in samples))
    if sys.byteorder != "little":
        samples.byteswap()
    return Wav(samples.tobytes(), rate, duration)


class Playback:
    """Prepare with PTT off, then play with a parent-enforced deadline."""

    def __init__(self, path: Path, device_name: str, gain: float, maximum: float):
        self.command = [
            sys.executable,
            "-m",
            "walkietalk.worker",
            str(path),
            device_name,
            str(gain),
            str(maximum),
        ]
        self.process = None
        self.errors = None

    def diagnostic(self) -> str:
        if self.errors is None:
            return ""
        self.errors.seek(0, os.SEEK_END)
        self.errors.seek(max(0, self.errors.tell() - 2048))
        return self.errors.read().decode(errors="replace").strip()

    def prepare(self, timeout: float = 10) -> None:
        # Native audio diagnostics must not block the child or corrupt its protocol.
        self.errors = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.errors,
            start_new_session=True,
            bufsize=0,
        )
        deadline = time.monotonic() + timeout
        response = b""
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b"\n" not in response:
                if not selector.select(timeout=max(0, deadline - time.monotonic())):
                    raise WalkietalkError("Audio preparation timed out; PTT was not asserted")
                chunk = os.read(self.process.stdout.fileno(), 256)
                if not chunk or len(response) + len(chunk) > 256:
                    break
                response += chunk
        if response != b"READY\n":
            raise WalkietalkError(
                f"Audio preparation failed; PTT was not asserted: {self.diagnostic()}"
            )

    def play(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WalkietalkError("Transmit time limit reached")
        try:
            self.process.stdin.write(b"GO\n")
            self.process.stdin.flush()
            result = self.process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise WalkietalkError("Transmit time limit reached; playback stopped") from exc
        except (BrokenPipeError, OSError) as exc:
            raise WalkietalkError(f"Audio worker failed: {exc}") from exc
        if result:
            raise WalkietalkError(
                f"Audio playback failed (worker exit {result}): {self.diagnostic()}"
            )

    def close(self) -> None:
        if self.process is None:
            if self.errors is not None:
                self.errors.close()
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()
        self.errors.close()
