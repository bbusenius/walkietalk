"""Speech from a Hermes environment; its provider settings and keys stay there."""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import httpx

from .agent import validate_reply
from .agent_process import run_cli
from .audio import Wav, read_wav
from .config import Config, WalkietalkError
from .tts import level_wav, radio_wav, speech_byte_budget, write_wav


class HermesTts:
    def __init__(self, config: Config) -> None:
        self.config = config

    def label(self) -> str:
        return "hermes (environment-configured speech provider and voice)"

    def _token(self) -> str:
        token = os.environ.get(self.config.hermes_tts_token_env, "")
        if not token or any(not 33 <= ord(c) <= 126 for c in token):
            raise WalkietalkError(
                f"Hermes TTS requires {self.config.hermes_tts_token_env}, the speech service's "
                "bearer token; provider credentials stay in Hermes; no fallback"
            )
        return token

    def prepare(self) -> None:
        self._token()

    async def _fetch(self, text: str) -> bytes:
        try:
            async with asyncio.timeout(self.config.tts_timeout_seconds):
                async with httpx.AsyncClient(
                    trust_env=False, follow_redirects=False, timeout=None
                ) as client:
                    async with client.stream(
                        "POST",
                        self.config.hermes_tts_url.rstrip("/") + "/v1/audio/speech",
                        headers={"Authorization": f"Bearer {self._token()}"},
                        json={
                            "text": text,
                            "timeout_seconds": self.config.tts_timeout_seconds,
                            "max_audio_seconds": self.config.max_tx_seconds
                            - self.config.settle_seconds,
                        },
                    ) as response:
                        status = response.status_code
                        if status in {401, 403}:
                            raise WalkietalkError(
                                f"Hermes TTS rejected the service token (HTTP {status}); "
                                "no fallback"
                            )
                        if status == 504:
                            raise WalkietalkError("Hermes TTS timed out; no transmission")
                        if status != 200:
                            raise WalkietalkError(
                                f"Hermes TTS HTTP {status}; check the speech service and its "
                                "configured provider. Diagnostics withheld; no fallback"
                            )
                        if response.headers.get("content-type", "").split(";")[0] != "audio/wav":
                            raise WalkietalkError("Hermes TTS returned no WAV; no transmission")
                        data = bytearray()
                        limit = speech_byte_budget(self.config.max_tx_seconds)
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            if len(data) + len(chunk) > limit:
                                raise WalkietalkError("Hermes TTS audio exceeded transport limit")
                            data.extend(chunk)
                        return bytes(data)
        except (TimeoutError, httpx.TimeoutException):
            raise WalkietalkError("Hermes TTS timed out; no transmission") from None
        except httpx.HTTPError:
            raise WalkietalkError(
                "Hermes TTS connection failed; check tts.hermes_url and start the speech service"
            ) from None

    def _read(self, path: Path) -> Wav:
        return level_wav(
            radio_wav(
                read_wav(path, self.config.max_tx_seconds),
                self.config.max_tx_seconds - self.config.settle_seconds,
                truncate=True,
            ),
            self.config.tts_normalize,
        )

    def _synthesize_direct(self, text: str) -> Wav:
        text = validate_reply(text, self.config.agent_max_reply_chars)
        with tempfile.TemporaryDirectory(prefix="walkietalk-hermes-audio-") as directory:
            path = Path(directory) / "speech.wav"
            path.write_bytes(asyncio.run(self._fetch(text)))
            return self._read(path)

    def synthesize(self, text: str) -> Wav:
        # Parent deadline also bounds DNS and HTTP-library shutdown. No worker owns PTT.
        deadline = time.monotonic() + self.config.tts_timeout_seconds
        text = validate_reply(text, self.config.agent_max_reply_chars)
        token = self._token()
        fields = (
            "hermes_tts_url",
            "hermes_tts_token_env",
            "tts_timeout_seconds",
            "tts_normalize",
            "agent_max_reply_chars",
            "max_tx_seconds",
            "settle_seconds",
        )
        env = {k: v for k, v in os.environ.items() if k in {"HOME", "PATH", "LANG", "LC_ALL"}}
        env[self.config.hermes_tts_token_env] = token
        with tempfile.TemporaryDirectory(prefix="walkietalk-hermes-tts-") as directory:
            path = Path(directory) / "speech.wav"
            code, stdout, _ = run_cli(
                [sys.executable, "-m", "walkietalk.hermes_tts", str(path)],
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
                max_final_bytes=speech_byte_budget(self.config.max_tx_seconds),
                name="Hermes TTS",
            )
            if code:
                try:
                    error = json.loads(stdout)["error"]
                    if not isinstance(error, str) or not error.isprintable() or len(error) > 2000:
                        raise ValueError
                except (ValueError, KeyError, TypeError):
                    error = "Hermes TTS worker failed; diagnostics withheld; no transmission"
                raise WalkietalkError(error)
            if not path.is_file() or path.stat().st_size > speech_byte_budget(
                self.config.max_tx_seconds
            ):
                raise WalkietalkError("Hermes TTS returned no bounded WAV; no transmission")
            speech = self._read(path)
            if time.monotonic() >= deadline:
                raise WalkietalkError("Hermes TTS timed out; no transmission")
            return speech


def main() -> int:
    try:
        request = json.loads(sys.stdin.buffer.read(65536))
        voice = HermesTts(Config(**request["config"]))
        write_wav(Path(sys.argv[1]), voice._synthesize_direct(request["text"]))
        return 0
    except (WalkietalkError, OSError):
        # Only our own sanitized errors can leave the worker.
        exc = sys.exception()
        message = str(exc) if isinstance(exc, WalkietalkError) else "Hermes TTS audio file failed"
        print(json.dumps({"error": message}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
