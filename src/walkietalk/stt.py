"""Local faster-whisper transcription. Models are downloaded on request, not at import."""

from pathlib import Path

import numpy as np

from .config import STT_MODELS, WalkietalkError

CACHE = Path.home() / ".cache" / "walkietalk" / "faster-whisper"
WHISPER_RATE = 16000


def cache_dir(name: str) -> Path:
    return CACHE / name


def model_ready(name: str) -> bool:
    return (cache_dir(name) / "model.bin").is_file()


def ensure_model(name: str, *, download: bool) -> Path:
    if name not in STT_MODELS:
        raise WalkietalkError("stt.model must be tiny or base")
    dest = cache_dir(name)
    if model_ready(name):
        return dest
    if not download:
        raise WalkietalkError(f"Speech model {name!r} is not downloaded. Run: walkietalk models")
    dest.mkdir(parents=True, exist_ok=True)
    size = "75 MB" if name == "tiny" else "145 MB"
    print(f"Downloading faster-whisper {name} ({size}) to {dest} ...", flush=True)
    try:
        from faster_whisper.utils import download_model

        download_model(name, output_dir=str(dest), local_files_only=False)
    except Exception as exc:
        raise WalkietalkError(
            f"Cannot download speech model {name!r}. Check the network and retry "
            f"`walkietalk models`: {exc}"
        ) from exc
    if not model_ready(name):
        raise WalkietalkError(f"Download finished but model.bin is missing in {dest}")
    return dest


def model_size_bytes(name: str) -> int:
    path = cache_dir(name)
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def to_whisper_audio(pcm: bytes, rate: int) -> np.ndarray:
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    if samples.size == 0 or rate == WHISPER_RATE:
        return samples
    if rate <= 0:
        raise WalkietalkError("Invalid capture sample rate")
    n = int(round(samples.size * WHISPER_RATE / rate))
    if n < 1:
        return samples[:0]
    return np.interp(
        np.linspace(0, 1, n, endpoint=False),
        np.linspace(0, 1, samples.size, endpoint=False),
        samples,
    ).astype(np.float32)


def load_model(name: str):
    path = ensure_model(name, download=False)
    try:
        from faster_whisper import WhisperModel

        return WhisperModel(str(path), device="cpu", compute_type="int8")
    except Exception as exc:
        raise WalkietalkError(f"Cannot load speech model {name!r}: {exc}") from exc


def transcribe_audio(model, pcm: bytes, rate: int) -> str:
    audio = to_whisper_audio(pcm, rate)
    if audio.size < WHISPER_RATE // 20:
        return ""
    try:
        segments, _info = model.transcribe(
            audio,
            language="en",
            vad_filter=False,
            beam_size=5,
            condition_on_previous_text=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()
    except Exception as exc:
        raise WalkietalkError(f"Transcription failed: {exc}") from exc
