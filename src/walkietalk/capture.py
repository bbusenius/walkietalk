"""Bounded capture from a pinned input device or a WAV file. Never opens PTT."""

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .audio import read_wav
from .config import Config, WalkietalkError
from .devices import audio_devices, resolve_device
from .session import uninterrupted_cleanup
from .vad import MIN_SPEECH_MS, SETTLE_FRAMES, EnergyVad, frame_samples

LOG_EVERY_FRAMES = 25


@dataclass(frozen=True)
class Utterance:
    pcm: bytes
    rate: int
    peak_rms: float
    first_speech_rms: float
    duration: float
    end_reason: str
    started_at: float | None = None


def frames_from_pcm(pcm: bytes, rate: int) -> Iterable[tuple[bytes, bool]]:
    width = frame_samples(rate) * 2
    offset = 0
    while offset < len(pcm):
        chunk = pcm[offset : offset + width]
        if len(chunk) < width:
            chunk = chunk + b"\x00" * (width - len(chunk))
        yield chunk, False
        offset += width


def collect_utterance(
    frames: Iterable[tuple[bytes, bool]],
    *,
    rate: int,
    energy_threshold: float,
    hangover_ms: int,
    max_utterance_seconds: float,
    wait_deadline: float | None,
    log: Callable[[str], None],
    on_wait: Callable[[], None] | None = None,
) -> Utterance:
    vad = EnergyVad(energy_threshold, hangover_ms, max_utterance_seconds, rate)
    warned_overflow = False
    last_state = "waiting"
    n = 0
    started_at = None
    for frame, overflowed in frames:
        if overflowed and not warned_overflow:
            log("Warning: capture overflow; some audio was lost")
            warned_overflow = True
        if not vad.speaking and wait_deadline is not None and time.monotonic() >= wait_deadline:
            break
        state, level = vad.push(frame)
        n += 1
        if state == "waiting" and on_wait is not None:
            on_wait()
        if state == "speaking" and last_state == "waiting":
            started_at = time.monotonic()
            log(f"Speech started (RMS {level:.3f})")
        elif state == "waiting" and last_state == "speaking":
            log(f"Ignored short noise (RMS {level:.3f})")
        elif n % LOG_EVERY_FRAMES == 0:
            if state == "waiting":
                log(f"RMS {level:.3f} (threshold {energy_threshold:.3f})")
            else:
                log(f"RMS {level:.3f} peak={vad.peak_rms:.3f}")
        last_state = state
        if state == "finished":
            break
    if not vad.speaking:
        wait = ""
        if wait_deadline is not None:
            wait = " within the wait limit"
        raise WalkietalkError(
            f"No speech detected{wait} (peak RMS {vad.peak_rms:.3f}, "
            f"threshold {energy_threshold:.3f}). Raise gateway volume, speak closer, "
            "or lower vad.energy_threshold."
        )
    if vad.end_reason is None:
        if vad.voiced_ms() < MIN_SPEECH_MS:
            raise WalkietalkError(
                f"No speech detected (peak RMS {vad.peak_rms:.3f}, "
                f"threshold {energy_threshold:.3f}). Raise gateway volume, speak closer, "
                "or lower vad.energy_threshold."
            )
        vad.end_reason = "silence"
    reason = "maximum utterance length" if vad.end_reason == "max" else "silence"
    log(f"Speech ended after {vad.duration():.1f}s ({reason}; peak RMS {vad.peak_rms:.3f})")
    return Utterance(
        bytes(vad.captured),
        rate,
        vad.peak_rms,
        vad.first_speech_rms,
        vad.duration(),
        vad.end_reason,
        started_at,
    )


def _log(message: str) -> None:
    print(message, flush=True)


def capture_from_wav(path: Path, config: Config, log: Callable[[str], None] = _log) -> Utterance:
    wav = read_wav(path, 60, gain=1)
    log(f"WAV: {wav.rate} Hz, mono PCM16, {wav.duration:.3f}s")
    return collect_utterance(
        frames_from_pcm(wav.frames, wav.rate),
        rate=wav.rate,
        energy_threshold=config.energy_threshold,
        hangover_ms=config.hangover_ms,
        max_utterance_seconds=config.max_utterance_seconds,
        wait_deadline=None,
        log=log,
    )


def capture_from_device(
    device_name: str,
    config: Config,
    wait_seconds: float,
    log: Callable[[str], None] = _log,
    on_wait: Callable[[], None] | None = None,
) -> Utterance:
    import sounddevice as sd

    devices = audio_devices()
    index = resolve_device(devices, device_name, "input")
    rate = int(devices[index]["default_samplerate"])
    samples = frame_samples(rate)
    try:
        sd.check_input_settings(device=index, channels=1, dtype="int16", samplerate=rate)
        stream = sd.RawInputStream(
            device=index,
            samplerate=rate,
            channels=1,
            dtype="int16",
            blocksize=samples,
        )
    except sd.PortAudioError as exc:
        raise WalkietalkError(
            f"Cannot open capture device {device_name!r} at {rate} Hz mono PCM16: {exc}. "
            "Close other recording apps and deselect the AIOC in desktop sound settings."
        ) from exc
    log(f"Listening on capture [{index}]: {device_name} at {rate} Hz")
    log(
        f"Waiting for someone to talk (give up after {wait_seconds:g}s; "
        f"threshold {config.energy_threshold:.3f})..."
    )

    def frames():
        skipped = 0
        while True:
            data, overflowed = stream.read(samples)
            if skipped < SETTLE_FRAMES:
                skipped += 1
                continue
            yield bytes(data), bool(overflowed)

    try:
        stream.start()
        return collect_utterance(
            frames(),
            rate=rate,
            energy_threshold=config.energy_threshold,
            hangover_ms=config.hangover_ms,
            max_utterance_seconds=config.max_utterance_seconds,
            wait_deadline=time.monotonic() + wait_seconds,
            log=log,
            on_wait=on_wait,
        )
    except sd.PortAudioError as exc:
        raise WalkietalkError(
            f"Capture failed: {exc}. Close other recording apps and deselect the AIOC "
            "in desktop sound settings."
        ) from exc
    finally:
        with uninterrupted_cleanup():
            stream.close()
