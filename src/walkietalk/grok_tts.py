"""Grok Voice TTS: saved Grok login or explicitly selected billed API, never fallback."""

import asyncio
import json
import math
import os
import struct
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from .agent import validate_reply
from .agent_process import run_cli
from .audio import Wav, read_wav
from .config import Config, WalkietalkError
from .stt import GROK_TOKEN_URL, _session_expired, load_grok_store, save_grok_store
from .tts import level_wav, radio_wav, speech_byte_budget, write_wav

GROK_TTS_URL = "https://api.x.ai/v1/tts"


def complete_wav_header(audio: bytes) -> bytes:
    """xAI streams PCM with a 0x7fffffff data length even for a finished REST response.

    Repair only that known 44-byte header; ordinary/truncated WAVs retain their
    declared lengths and must pass the normal strict WAV reader.
    """
    if (
        len(audio) >= 44
        and audio[:4] == b"RIFF"
        and audio[8:16] == b"WAVEfmt "
        and audio[36:40] == b"data"
        and struct.unpack_from("<I", audio, 4)[0] == 0x80000023
        and struct.unpack_from("<I", audio, 40)[0] == 0x7FFFFFFF
        and struct.unpack_from("<IHHIIHH", audio, 16) == (16, 1, 1, 48000, 96000, 2, 16)
    ):
        if (len(audio) - 44) % 2:
            raise WalkietalkError("Grok TTS returned truncated PCM; no transmission")
        header = bytearray(audio[:44])
        struct.pack_into("<I", header, 4, len(audio) - 8)
        struct.pack_into("<I", header, 40, len(audio) - 44)
        return bytes(header) + audio[44:]
    return audio


def valid_token(value: object) -> bool:
    return isinstance(value, str) and bool(value) and all(33 <= ord(c) <= 126 for c in value)


class GrokTts:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.api = config.tts_backend == "grok_api"

    def label(self) -> str:
        auth = (
            f"{self.config.grok_tts_api_key_env}; billed API"
            if self.api
            else "SuperGrok saved login"
        )
        return (
            f"{self.config.tts_backend} ({self.config.grok_tts_voice}; "
            f"speed={self.config.grok_tts_speed:g}; {auth})"
        )

    def _credentials(self) -> tuple[str, dict | None, dict | None]:
        if self.api:
            token = os.environ.get(self.config.grok_tts_api_key_env)
            if not valid_token(token):
                raise WalkietalkError(
                    f"tts.backend grok_api requires {self.config.grok_tts_api_key_env} "
                    "for billed xAI API access; no subscription-login fallback"
                )
            return token, None, None
        try:
            store, _, session = load_grok_store()
        except WalkietalkError:
            raise WalkietalkError(
                "Grok TTS needs a valid saved SuperGrok login. Run: grok login. No API-key fallback"
            ) from None
        token = session.get("key")
        if not valid_token(token):
            raise WalkietalkError("Grok TTS login is invalid. Run: grok login")
        return token, store, session

    def prepare(self) -> None:
        # Read credentials only. All network work, including refresh, shares the synth deadline.
        self._credentials()

    async def _refresh(self, client, store: dict, session: dict) -> str:
        refresh, client_id = session.get("refresh_token"), session.get("oidc_client_id")
        if not valid_token(refresh) or not valid_token(client_id):
            raise WalkietalkError("Grok TTS login expired. Run: grok login; no API-key fallback")
        async with client.stream(
            "POST",
            GROK_TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id},
        ) as response:
            if response.status_code != 200:
                raise WalkietalkError("Grok TTS login refresh failed. Run: grok login")
            body = await self._body(response, 65536)
        try:
            payload = json.loads(body)
            token = payload.get("access_token")
            replacement = payload.get("refresh_token", refresh)
            expires = payload.get("expires_in", 3600)
            if (
                not valid_token(token)
                or not valid_token(replacement)
                or isinstance(expires, bool)
                or not isinstance(expires, (int, float))
                or not math.isfinite(expires)
                or not 0 < expires <= 365 * 86400
            ):
                raise ValueError
        except (ValueError, AttributeError, UnicodeError, RecursionError, OverflowError):
            raise WalkietalkError(
                "Grok TTS login refresh returned invalid data; run grok login"
            ) from None
        session.update(
            key=token,
            refresh_token=replacement,
            expires_at=(datetime.now(UTC) + timedelta(seconds=expires)).isoformat(),
        )
        save_grok_store(store)
        return token

    @staticmethod
    async def _body(response, maximum: int) -> bytes:
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=65536):
            if len(body) + len(chunk) > maximum:
                raise WalkietalkError("Grok TTS response exceeded the transport limit; discarded")
            body.extend(chunk)
        return bytes(body)

    async def _fetch(self, text: str) -> bytes:
        try:
            async with asyncio.timeout(self.config.tts_timeout_seconds):
                token, store, session = self._credentials()
                async with httpx.AsyncClient(
                    trust_env=False, follow_redirects=False, timeout=None
                ) as client:
                    refreshed = False
                    if session is not None and _session_expired(session):
                        token = await self._refresh(client, store, session)
                        refreshed = True
                    for _ in range(2):
                        async with client.stream(
                            "POST",
                            GROK_TTS_URL,
                            headers={"Authorization": f"Bearer {token}"},
                            json={
                                "text": text,
                                "voice_id": self.config.grok_tts_voice,
                                "language": self.config.grok_tts_language,
                                "speed": self.config.grok_tts_speed,
                                "text_normalization": False,
                                "output_format": {"codec": "wav", "sample_rate": 48000},
                            },
                        ) as response:
                            status = response.status_code
                            retry_login = status == 401 and session is not None and not refreshed
                            if status == 200:
                                kind = (
                                    response.headers.get("content-type", "").split(";")[0].lower()
                                )
                                if kind not in {
                                    "audio/wav",
                                    "audio/x-wav",
                                    "audio/wave",
                                    "application/octet-stream",
                                }:
                                    raise WalkietalkError(
                                        "Grok TTS returned no WAV audio; discarded"
                                    )
                                return await self._body(
                                    response, speech_byte_budget(self.config.max_tx_seconds) * 2
                                )
                            if not retry_login:
                                if status in {401, 403}:
                                    auth = (
                                        "billed API key/access"
                                        if self.api
                                        else "SuperGrok login/access (grok login)"
                                    )
                                    raise WalkietalkError(
                                        f"Grok TTS rejected {auth} (HTTP {status}); no fallback"
                                    )
                                raise WalkietalkError(
                                    f"Grok TTS HTTP {status}; check voice, language, speed, "
                                    "and service status. "
                                    "Server diagnostics withheld"
                                )
                        token = await self._refresh(client, store, session)
                        refreshed = True
        except (TimeoutError, httpx.TimeoutException):
            raise WalkietalkError("Grok TTS timed out; audio discarded; no transmission") from None
        except httpx.HTTPError:
            raise WalkietalkError("Grok TTS connection failed; no transmission") from None
        raise WalkietalkError("Grok TTS returned no audio; no transmission")

    def _synthesize_direct(self, text: str) -> Wav:
        deadline = time.monotonic() + self.config.tts_timeout_seconds
        text = validate_reply(text, self.config.agent_max_reply_chars)
        audio = asyncio.run(self._fetch(text))
        with tempfile.TemporaryDirectory(prefix="walkietalk-grok-voice-") as directory:
            path = Path(directory) / "voice.wav"
            path.write_bytes(complete_wav_header(audio))
            speech = level_wav(
                radio_wav(
                    read_wav(path, self.config.max_tx_seconds * 2),
                    self.config.max_tx_seconds - self.config.settle_seconds,
                    truncate=True,
                ),
                self.config.tts_normalize,
            )
        if time.monotonic() >= deadline:
            raise WalkietalkError("Grok TTS timed out; audio discarded; no transmission")
        return speech

    def synthesize(self, text: str) -> Wav:
        # The parent deadline also bounds DNS/library shutdown that may block
        # cancellation of an async HTTP request. This worker cannot access PTT.
        deadline = time.monotonic() + self.config.tts_timeout_seconds
        text = validate_reply(text, self.config.agent_max_reply_chars)
        self.prepare()
        fields = (
            "tts_backend",
            "tts_timeout_seconds",
            "grok_tts_voice",
            "grok_tts_language",
            "grok_tts_speed",
            "grok_tts_api_key_env",
            "tts_normalize",
            "agent_max_reply_chars",
            "max_tx_seconds",
            "settle_seconds",
        )
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {"HOME", "PATH", "LANG", "LC_ALL", "GROK_HOME"}
        }
        if self.api:
            env[self.config.grok_tts_api_key_env] = os.environ[self.config.grok_tts_api_key_env]
        with tempfile.TemporaryDirectory(prefix="walkietalk-grok-tts-worker-") as directory:
            path = Path(directory) / "speech.wav"
            code, stdout, _ = run_cli(
                [sys.executable, "-m", "walkietalk.grok_tts", str(path)],
                prompt=json.dumps(
                    {
                        "text": text,
                        "config": {field: getattr(self.config, field) for field in fields},
                    }
                ).encode(),
                cwd=directory,
                env=env,
                deadline=deadline,
                final_path=path,
                max_final_bytes=speech_byte_budget(self.config.max_tx_seconds) * 2,
                name="Grok TTS",
            )
            if code:
                try:
                    error = json.loads(stdout)["error"]
                    if not isinstance(error, str) or len(error) > 2000 or not error.isprintable():
                        raise ValueError
                except (ValueError, KeyError, TypeError):
                    error = "Grok TTS worker failed; diagnostics withheld; no transmission"
                raise WalkietalkError(error)
            if (
                not path.is_file()
                or path.stat().st_size > speech_byte_budget(self.config.max_tx_seconds) * 2
            ):
                raise WalkietalkError("Grok TTS returned no bounded WAV; no transmission")
            speech = level_wav(
                radio_wav(
                    read_wav(path, self.config.max_tx_seconds * 2),
                    self.config.max_tx_seconds - self.config.settle_seconds,
                    truncate=True,
                ),
                self.config.tts_normalize,
            )
            if time.monotonic() >= deadline:
                raise WalkietalkError("Grok TTS timed out; audio discarded; no transmission")
            return speech


def main() -> int:
    """Private synthesis worker; credentials are never passed in command arguments."""
    try:
        request = json.loads(sys.stdin.buffer.read(65536))
        voice = GrokTts(Config(**request["config"]))
        write_wav(Path(sys.argv[1]), voice._synthesize_direct(request["text"]))
        return 0
    except (WalkietalkError, OSError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
