"""Standalone companion for Hermes's Python environment, not an upstream API.

Copy this file to the Hermes host; run with that environment's Python and ffmpeg.
Only the worker imports Hermes. Walkietalk itself never imports provider packages.
"""

import argparse
import hmac
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_REQUEST_BYTES = 16384
MAX_SOURCE_BYTES = 64 * 1024 * 1024


class SpeechFailure(Exception):
    pass


def validate_request(value, max_seconds=120):
    required = {
        "text",
        "timeout_seconds",
        "max_audio_seconds",
    }
    if not isinstance(value, dict) or not required <= set(value) <= required | {"truncate"}:
        raise ValueError("Invalid speech request")
    if not isinstance(value.get("truncate", True), bool):
        raise ValueError("Invalid truncate setting")
    text = value["text"]
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        raise ValueError("Text must contain 1–2000 characters")
    if any(not c.isprintable() for c in text):
        raise ValueError("Text contains control characters")
    for key, maximum in (("timeout_seconds", 120), ("max_audio_seconds", max_seconds)):
        number = value[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise ValueError("Invalid speech limit")
        if not math.isfinite(number) or not 0 < number <= maximum:
            raise ValueError("Invalid speech limit")
    return value


def synthesize_in_environment(request, directory: Path):
    """Use Hermes's public speech tool, with its profile and no agent turn."""
    from hermes_cli.config import load_config
    from tools.tts_tool import BUILTIN_TTS_PROVIDERS, text_to_speech_tool

    config = load_config().get("tts", {})
    provider = config.get("provider", "").strip().lower()
    custom = config.get("providers", {}).get(provider, {})
    if not provider or (provider not in BUILTIN_TTS_PROVIDERS and custom.get("type") != "command"):
        raise SpeechFailure("Select an explicit built-in or command TTS provider in Hermes")
    result = json.loads(text_to_speech_tool(request["text"], str(directory / "source.mp3")))
    if result.get("success") is not True or result.get("provider") != provider:
        # Includes Hermes's Edge -> NeuTTS fallback; never return substituted speech.
        raise SpeechFailure("Configured Hermes speech provider failed; no fallback")
    source = Path(result.get("file_path", "")).resolve()
    if not source.is_relative_to(directory.resolve()) or not source.is_file():
        raise SpeechFailure("Hermes returned an invalid speech file")
    if not 0 < source.stat().st_size <= MAX_SOURCE_BYTES:
        raise SpeechFailure("Hermes speech file exceeded the limit")
    # Retain one extra sample in strict mode so generate() can detect and reject
    # overlong audio instead of accepting an ID shortened by ffmpeg.
    maximum = request["max_audio_seconds"]
    if not request.get("truncate", True):
        maximum = (math.ceil(maximum * 48000) + 1) / 48000
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-t",
            str(maximum),
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            str(directory / "speech.wav"),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=request["timeout_seconds"],
    )


def generate(request, hermes_root: str, stop_event=None) -> bytes:
    """A separate process group bounds provider calls, conversion, and descendants."""
    deadline = time.monotonic() + request["timeout_seconds"]
    with tempfile.TemporaryDirectory(prefix="walkietalk-hermes-service-") as directory:
        root = Path(directory)
        request_path = root / "request.json"
        request_path.write_text(json.dumps(request))
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--hermes-root",
                hermes_root,
                "--worker",
                str(request_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            while process.poll() is None:
                if stop_event is not None and stop_event.is_set():
                    raise SpeechFailure("Speech service stopping")
                if time.monotonic() >= deadline:
                    raise TimeoutError
                if any(p.is_file() and p.stat().st_size > MAX_SOURCE_BYTES for p in root.iterdir()):
                    raise SpeechFailure("Hermes speech exceeded the file limit")
                time.sleep(min(0.025, max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                raise TimeoutError
            if process.returncode:
                raise SpeechFailure("Configured Hermes provider failed; check its setup")
            path = root / "speech.wav"
            if path.stat().st_size > int(request["max_audio_seconds"] * 96000) + 4096:
                raise SpeechFailure("Hermes speech exceeded the audio limit")
            with wave.open(str(path), "rb") as audio:
                if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (
                    1,
                    2,
                    48000,
                ):
                    raise SpeechFailure("Invalid speech WAV")
                frames = audio.getnframes()
                if frames < 1 or frames > math.ceil(request["max_audio_seconds"] * 48000):
                    raise SpeechFailure("Invalid speech duration")
                if len(audio.readframes(frames)) != frames * 2:
                    raise SpeechFailure("Incomplete speech WAV")
            return path.read_bytes()
        finally:
            # Kill the group even after its leader exits; custom providers can spawn children.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=1)


class SpeechServer(ThreadingHTTPServer):
    daemon_threads = False

    def __init__(self, address, token, hermes_root, max_seconds=120):
        self.token = token
        self.hermes_root = hermes_root
        self.max_seconds = max_seconds
        self.busy = threading.Lock()
        self.stopping = threading.Event()
        super().__init__(address, SpeechHandler)

    def server_close(self):
        self.stopping.set()
        super().server_close()

    def handle_error(self, request, client_address):
        # HTTP tracebacks and upstream diagnostics must not expose text or secrets.
        print("Hermes speech request failed; diagnostics withheld", file=sys.stderr)


class SpeechHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass  # No request text, authorization headers, or URLs in access logs.

    def respond(self, status, body, kind="text/plain"):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        expected = ("Bearer " + self.server.token).encode()
        actual = self.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(actual, expected):
            self.respond(401, b"Service token required")
            return
        if self.path != "/v1/audio/speech":
            self.respond(404, b"Unknown speech endpoint")
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            length = int(lengths[0]) if len(lengths) == 1 else 0
            if self.headers.get("Transfer-Encoding") or not 0 < length <= MAX_REQUEST_BYTES:
                raise ValueError
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError
            request = validate_request(json.loads(self.rfile.read(length)), self.server.max_seconds)
        except (ValueError, OverflowError, TimeoutError, RecursionError):
            self.respond(400, b"Invalid speech request or limits")
            return
        if not self.server.busy.acquire(blocking=False):
            self.respond(503, b"Speech service busy; try again")
            return
        try:
            audio = generate(request, self.server.hermes_root, self.server.stopping)
        except TimeoutError:
            self.respond(504, b"Speech generation timed out")
        except Exception:
            self.respond(502, b"Configured Hermes speech provider failed; no fallback")
        else:
            self.respond(200, audio, "audio/wav")
        finally:
            self.server.busy.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-root", required=True, help="Hermes source directory")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8643)
    parser.add_argument("--token-env", default="API_SERVER_KEY")
    parser.add_argument("--max-audio-seconds", type=float, default=120)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.hermes_root).resolve()))
    # Same profile and credential store as the agent; never copy provider keys to Walkietalk.
    from dotenv import load_dotenv
    from hermes_constants import get_hermes_home

    load_dotenv(Path(get_hermes_home()) / ".env", override=False)
    if args.worker:
        try:
            synthesize_in_environment(json.loads(args.worker.read_text()), args.worker.parent)
            return 0
        except Exception:
            return 1
    token = os.environ.get(args.token_env, "")
    if len(token) < 16 or any(not 33 <= ord(c) <= 126 for c in token):
        parser.error(
            "Set a strong speech-service bearer token in the configured environment variable"
        )
    if not math.isfinite(args.max_audio_seconds) or args.max_audio_seconds <= 0:
        parser.error("--max-audio-seconds must be positive and finite")

    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    with SpeechServer(
        (args.host, args.port), token, args.hermes_root, args.max_audio_seconds
    ) as server:
        print(f"Hermes speech service listening on {args.host}:{args.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
