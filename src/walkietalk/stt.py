"""Speech-to-text backends. Capture and wake matching stay outside this module."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
import wave
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np

from .config import STT_BACKENDS, STT_MODELS, Config, WalkietalkError

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


def pcm_wav_bytes(pcm: bytes, rate: int) -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


class SttBackend(Protocol):
    def label(self) -> str: ...
    def prepare(self) -> None: ...
    def transcribe(self, pcm: bytes, rate: int) -> str: ...


class FasterWhisperStt:
    def __init__(self, model_name: str, timeout: float) -> None:
        self.model_name = model_name
        self.timeout = timeout
        self._model = None
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()

    def label(self) -> str:
        return f"faster-whisper ({self.model_name})"

    def prepare(self) -> None:
        self._model = load_model(self.model_name)

    def transcribe(self, pcm: bytes, rate: int) -> str:
        # Keep results local to this call so a timed-out result or error is discarded.
        result: list[str] = []
        error: list[BaseException] = []

        def worker() -> None:
            try:
                result.append(transcribe_audio(self._model, pcm, rate))
            except BaseException as exc:
                error.append(exc)

        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                raise WalkietalkError(
                    "faster-whisper is still processing the previous audio; this utterance "
                    "was skipped. Wait for it to finish, or restart walkietalk if it remains stuck."
                )
            if self._model is None:
                self.prepare()
            thread = threading.Thread(target=worker, daemon=True)
            self._worker = thread
            thread.start()
        thread.join(self.timeout)
        if thread.is_alive():
            raise WalkietalkError(f"faster-whisper timed out after {self.timeout:g}s")
        if error:
            raise error[0]
        return result[0]


GROK_STT_URL = "https://api.x.ai/v1/stt"
GROK_STT_MODEL = "grok-voice-transcribe-2.0"
GROK_TOKEN_URL = "https://auth.x.ai/oauth2/token"


def grok_auth_path() -> Path:
    return Path(os.environ.get("GROK_HOME", Path.home() / ".grok")) / "auth.json"


def load_grok_store() -> tuple[dict, str, dict]:
    path = grok_auth_path()
    if not path.is_file():
        raise WalkietalkError(
            "No SuperGrok login found. Run: grok login. "
            "Walkietalk will not use XAI_API_KEY unless stt.backend is grok_api."
        )
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise WalkietalkError(f"Cannot read SuperGrok login at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise WalkietalkError("SuperGrok login file is invalid. Run: grok login")
    for account, session in data.items():
        if isinstance(session, dict) and session.get("key"):
            return data, account, session
    raise WalkietalkError("SuperGrok login has no access token. Run: grok login")


def save_grok_store(data: dict) -> None:
    path = grok_auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)
    path.chmod(0o600)


def _session_expired(session: dict, skew_seconds: int = 60) -> bool:
    raw = session.get("expires_at")
    if not isinstance(raw, str) or not raw:
        return False
    try:
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = datetime.now(UTC)
    return now.timestamp() >= expires.timestamp() - skew_seconds


def refresh_grok_access_token(session: dict) -> str:
    refresh = session.get("refresh_token")
    client_id = session.get("oidc_client_id")
    if not isinstance(refresh, str) or not isinstance(client_id, str):
        raise WalkietalkError("SuperGrok login expired. Run: grok login")
    body = urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client_id,
        }
    ).encode()
    request = Request(
        GROK_TOKEN_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode())
    except (HTTPError, URLError, json.JSONDecodeError) as exc:
        raise WalkietalkError("SuperGrok login expired. Run: grok login") from exc
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise WalkietalkError("SuperGrok login expired. Run: grok login")
    session["key"] = token
    if isinstance(payload.get("refresh_token"), str):
        session["refresh_token"] = payload["refresh_token"]
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        session["expires_at"] = (datetime.now(UTC) + timedelta(seconds=int(expires_in))).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    return token


def post_grok_stt(wav: bytes, token: str, timeout: float, keyterms: tuple[str, ...] = ()) -> str:
    boundary = "----walkietalk" + uuid.uuid4().hex
    parts: list[bytes] = [
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="model"\r\n\r\n'
            f"{GROK_STT_MODEL}\r\n"
        ).encode(),
        (f'--{boundary}\r\nContent-Disposition: form-data; name="language"\r\n\r\nen\r\n').encode(),
        (f'--{boundary}\r\nContent-Disposition: form-data; name="format"\r\n\r\ntrue\r\n').encode(),
    ]
    for term in keyterms[:100]:
        if not term or len(term) > 50:
            continue
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="keyterm"\r\n\r\n{term}\r\n'
            ).encode()
        )
    parts.extend(
        [
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="file"; filename="utterance.wav"\r\n'
                "Content-Type: audio/wav\r\n\r\n"
            ).encode(),
            wav,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    request = Request(
        GROK_STT_URL,
        data=b"".join(parts),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200]
        raise WalkietalkError(f"Grok speech-to-text failed ({exc.code}): {detail}") from exc
    except URLError as exc:
        raise WalkietalkError(
            f"Grok speech-to-text is unreachable. faster-whisper was not used: {exc.reason}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise WalkietalkError("Grok speech-to-text returned invalid JSON") from exc
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        raise WalkietalkError("Grok speech-to-text returned no transcript text")
    return text.strip()


class GrokAccountStt:
    def __init__(self, timeout: float, keyterms: tuple[str, ...] = ()) -> None:
        self.timeout = timeout
        self.keyterms = keyterms
        self._store: dict | None = None
        self._session: dict | None = None

    def label(self) -> str:
        return f"grok ({GROK_STT_MODEL}; SuperGrok Plus login)"

    def _load(self) -> dict:
        # TTS or the official CLI may have refreshed this shared saved login.
        # Reload before each use so we never reuse a rotated refresh token.
        self._store, _account, self._session = load_grok_store()
        return self._session

    def _token(self) -> str:
        session = self._load()
        token = session.get("key")
        if not isinstance(token, str) or not token:
            raise WalkietalkError("SuperGrok login has no access token. Run: grok login")
        if _session_expired(session):
            return self._refresh()
        return token

    def _refresh(self) -> str:
        session = self._load()
        token = refresh_grok_access_token(session)
        if self._store is not None:
            save_grok_store(self._store)
        return token

    def _remaining(self, deadline: float) -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise WalkietalkError(f"Grok speech-to-text timed out after {self.timeout:g}s")
        return left

    def prepare(self) -> None:
        self._token()

    def transcribe(self, pcm: bytes, rate: int) -> str:
        wav = pcm_wav_bytes(pcm, rate)
        deadline = time.monotonic() + self.timeout
        try:
            return post_grok_stt(wav, self._token(), self._remaining(deadline), self.keyterms)
        except WalkietalkError as exc:
            if "(401)" not in str(exc):
                raise
            token = self._refresh()
            return post_grok_stt(wav, token, self._remaining(deadline), self.keyterms)


class GrokApiStt:
    def __init__(
        self, timeout: float, token_env: str = "XAI_API_KEY", keyterms: tuple[str, ...] = ()
    ) -> None:
        self.timeout = timeout
        self.token_env = token_env
        self.keyterms = keyterms

    def label(self) -> str:
        return f"grok_api ({GROK_STT_MODEL}; {self.token_env})"

    def prepare(self) -> None:
        if not os.environ.get(self.token_env):
            raise WalkietalkError(
                f"stt.backend grok_api requires {self.token_env} in the environment. "
                "This uses billed Speech-to-Text API credits, not SuperGrok Plus. "
                "Walkietalk will not fall back to faster-whisper or SuperGrok login."
            )

    def transcribe(self, pcm: bytes, rate: int) -> str:
        self.prepare()
        token = os.environ[self.token_env]
        wav = pcm_wav_bytes(pcm, rate)
        return post_grok_stt(wav, token, self.timeout, self.keyterms)


def open_stt(config: Config) -> SttBackend:
    keyterms = (config.wake_primary, *config.wake_aliases)
    if config.stt_backend == "faster-whisper":
        return FasterWhisperStt(config.stt_model, config.stt_timeout_seconds)
    if config.stt_backend == "grok":
        return GrokAccountStt(config.stt_timeout_seconds, keyterms)
    if config.stt_backend == "grok_api":
        return GrokApiStt(config.stt_timeout_seconds, keyterms=keyterms)
    raise WalkietalkError(f"stt.backend must be one of {', '.join(STT_BACKENDS)}")
