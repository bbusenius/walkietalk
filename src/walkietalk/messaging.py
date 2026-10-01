"""WhatsApp and Signal conversations. Stored messages only; never a live call."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import httpx

from .agent_process import _signal_group, run_cli
from .audio import Wav, read_wav
from .config import MESSAGING_SERVICES, Config, MessagingMode, OutputTooLarge, WalkietalkError
from .operator_mode import OperatorQueue
from .session import uninterrupted_cleanup
from .term import emit
from .tts import TtsBackend, radio_wav, write_wav

SIGNAL_HTTP = "http://127.0.0.1:8080"
# signal-cli sends an SSE keepalive every 15 seconds, even when no messages arrive.
SIGNAL_STREAM_TIMEOUT_SECONDS = 30
VOICE_MEDIA = {"audio", "ptt", "voice"}
SEND_TIMEOUT_SECONDS = 60
VOICE_WAIT_SECONDS = 60
PLAYBACK_ATTEMPTS = 3
MAX_TRANSCRIBE_SECONDS = 300
MAX_VOICE_UPLOAD_BYTES = 1024 * 1024


def messaging_environment() -> dict[str, str]:
    """Keep CLI login/storage locations, without unrelated provider credentials."""
    allowed = {
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "TZ",
        "JAVA_HOME",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def signal_attachments(mode: MessagingMode) -> Path:
    if mode.attachments_dir:
        return Path(mode.attachments_dir).expanduser()
    data = os.environ.get("XDG_DATA_HOME", "")
    root = Path(data) if data and Path(data).is_absolute() else Path.home() / ".local/share"
    return root / "signal-cli/attachments"


def normalize_message(text: str) -> str:
    """Accept phone Unicode as speech while keeping terminal controls out."""
    return "".join(
        " " if char.isspace() else char for char in text if char.isspace() or char.isprintable()
    ).strip()


def whatsapp_store() -> Path:
    """Resolve once, then pin both wacli and polling to the same store."""
    override = os.environ.get("WACLI_STORE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    legacy = Path.home() / ".wacli"
    if legacy.exists() or not sys.platform.startswith("linux"):
        return legacy
    state = os.environ.get("XDG_STATE_HOME")
    root = Path(state) if state and Path(state).is_absolute() else Path.home() / ".local/state"
    return root / "wacli"


def split_text(text: str, maximum: int) -> tuple[str, str]:
    """Prefer a complete sentence, then a word; split oversized words as a last resort."""
    if maximum < 1:
        raise WalkietalkError("Sender introduction leaves no room for message text")
    if len(text) <= maximum:
        return text, ""
    boundaries = [
        match
        for match in re.finditer(r"[.!?](?=\s)", text[: maximum + 1])
        if match.end() <= maximum
    ]
    if boundaries:
        end = boundaries[-1].end()
    else:
        spaces = list(re.finditer(r"\s", text[: maximum + 1]))
        end = spaces[-1].start() if spaces else maximum
    return text[:end].rstrip(), text[end:].lstrip()


@dataclass
class MessagePlayback:
    """Parent-owned progress; commit remaining text only after successful transmission."""

    remaining: str
    failures: int = 0
    chunk_chars: int | None = None
    transcript: str | None = None
    speech: Wav | None = None
    preview_audio: Wav | None = None

    def prepare_text(self, voice: TtsBackend, alias: str, config: Config) -> tuple[Wav, str]:
        prefix = f"{alias} says: "
        maximum = config.agent_max_reply_chars - len(prefix) - len(", over")
        limit = config.max_tx_seconds - config.settle_seconds
        # Start at a conservative speech rate; learn the provider's rate as we go.
        budget = min(maximum, self.chunk_chars or max(24, int(limit * 12) - len(prefix) - 6))
        while True:
            body, rest = split_text(self.remaining, budget)
            line = prefix + (body if rest else spoken_text(body))
            try:
                # Providers must return all requested speech, never silently crop this chunk.
                speech = radio_wav(voice.synthesize(line, truncate=False), limit)
                estimate = int(len(line) * limit / speech.duration * 0.9) - len(prefix) - 6
                self.chunk_chars = max(1, min(maximum, estimate))
                return speech, rest
            except OutputTooLarge:
                if len(body) <= 1:
                    raise
                budget = max(1, len(body) // 2)
                self.chunk_chars = budget
            except WalkietalkError as exc:
                # Retry a smaller chunk after a timeout or an older service's
                # generic size error; progress is still committed only on TX.
                if "timed out" in str(exc).lower() or "Hermes TTS HTTP 502" in str(exc):
                    self.chunk_chars = max(1, len(body) // 2)
                raise


class MessagingReady(Exception):
    """An inbound message is waiting for the open conversation. Capture must release the device."""

    def __init__(self, service: str) -> None:
        self.service = service
        super().__init__(service)


@dataclass(frozen=True)
class Inbound:
    service: str
    kind: str
    text: str = ""
    audio_path: Path | None = None
    identity: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", normalize_message(self.text))


def _digits(value: str) -> str:
    return "".join(char for char in value if char.isdigit())


def _same_destination(configured: str, candidate: str) -> bool:
    if configured.strip() == candidate.strip():
        return True
    configured_digits = _digits(configured)
    candidate_digits = _digits(candidate.split("@", 1)[0])
    return bool(configured_digits) and configured_digits == candidate_digits


def signal_daemon_ready(base: str = SIGNAL_HTTP) -> bool:
    """True when a signal-cli daemon is already serving this account.

    A second signal-cli process waits forever on the account lock.
    """
    try:
        response = httpx.get(base.rstrip("/") + "/api/v1/check", timeout=0.5, trust_env=False)
        return response.status_code == 200
    except (OSError, httpx.HTTPError):
        return False


def signal_payload(line: str) -> dict | None:
    """One JSON object from jsonRpc stdout or a daemon event-stream line."""
    text = line.strip()
    if text.startswith("data:"):
        text = text[5:].strip()
    if not text or text.startswith(":"):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def signal_send_failure(payload: dict) -> str | None:
    """A sentence when Signal refused the send, or None when it was accepted."""
    if not isinstance(payload, dict) or not payload.get("error"):
        return None
    error = payload["error"]
    results = []
    if isinstance(error, dict):
        data = error.get("data")
        response = data.get("response") if isinstance(data, dict) else None
        if isinstance(response, dict) and isinstance(response.get("results"), list):
            results = response["results"]
    kinds = {item.get("type") for item in results if isinstance(item, dict)}
    if "UNREGISTERED_FAILURE" in kinds:
        return "Message not sent. That number is not a Signal account."
    return "Message not sent."


def spoken_text(body: str) -> str:
    """Text replies end with the proword that hands the conversation back."""
    words = body.strip().rstrip(" ,.!?")
    return f"{words}, over"


def _inbound_from_content(data: dict, identity: str, mode: MessagingMode) -> Inbound | None:
    """A text or voice note inside a data message. Reactions and groups are dropped."""
    if not isinstance(data, dict) or data.get("groupInfo") or data.get("reaction"):
        return None
    attachments = data.get("attachments") or []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        content_type = str(attachment.get("contentType") or "")
        voice = bool(attachment.get("isVoiceNote")) or content_type.startswith("audio/")
        if not voice:
            continue
        file_id = str(attachment.get("id") or "")
        if not file_id or Path(file_id).name != file_id or file_id in {".", ".."}:
            continue
        return Inbound(
            "signal",
            "voice",
            audio_path=signal_attachments(mode) / file_id,
            identity=f"{identity}:{file_id}",
        )
    text = str(data.get("message") or "").strip()
    if not text:
        return None
    return Inbound("signal", "text", text=text, identity=identity or text)


def parse_signal_message(payload: dict, mode: MessagingMode) -> Inbound | None:
    """Return a stored text or voice message. Calls, groups, and other senders are dropped.

    Only incoming data messages from the configured contact are eligible.
    Linked-device sent-message sync copies are outgoing traffic, not replies.
    """
    envelope = payload.get("envelope", payload)
    if not isinstance(envelope, dict):
        return None
    if envelope.get("callMessage") or envelope.get("typingMessage"):
        return None
    identity = str(envelope.get("timestamp") or "")
    data = envelope.get("dataMessage")
    if isinstance(data, dict):
        source = str(envelope.get("sourceNumber") or envelope.get("source") or "")
        if _same_destination(mode.to, source):
            found = _inbound_from_content(data, identity or source, mode)
            if found is not None:
                return found
    return None


def parse_whatsapp_row(row: dict, mode: MessagingMode) -> Inbound | None:
    """Return a stored message; the poller retries rows whose media is not ready."""
    if row.get("from_me"):
        return None
    chat = str(row.get("chat_jid") or "")
    if not _same_destination(mode.to, chat):
        return None
    identity = str(row.get("msg_id") or "")
    if not identity:
        return None
    media = str(row.get("media_type") or "")
    if media in VOICE_MEDIA:
        local = str(row.get("local_path") or "")
        path = Path(local) if local else None
        if path is not None and path.is_file():
            return Inbound("whatsapp", "voice", audio_path=path, identity=identity)
        return None
    if media:
        return Inbound("whatsapp", "text", text="", identity=identity)
    text = str(row.get("text") or row.get("display_text") or "").strip()
    if not text:
        return Inbound("whatsapp", "text", text="", identity=identity)
    return Inbound("whatsapp", "text", text=text, identity=identity)


def voice_wav(path: Path, maximum: float, *, rate: int = 48000, truncate: bool = True) -> Wav:
    """Convert a stored voice note to the mono PCM16 WAV the radio already plays."""
    maximum_samples = int(maximum * rate)
    if maximum_samples < 1:
        raise OutputTooLarge("Voice message has no room within the audio limit")
    with tempfile.TemporaryDirectory(prefix="walkietalk-voice-") as directory:
        output = Path(directory) / "voice.wav"
        try:
            code, _, _ = run_cli(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(path.resolve()),
                    "-af",
                    # One extra sample detects overlong notes without decoding
                    # unbounded media or silently losing the transcript's tail.
                    f"aresample={rate},atrim=end_sample={maximum_samples + 1}",
                    "-ac",
                    "1",
                    "-ar",
                    str(rate),
                    "-c:a",
                    "pcm_s16le",
                    str(output),
                ],
                prompt=b"",
                cwd=directory,
                env=messaging_environment(),
                deadline=time.monotonic() + 30,
                final_path=output,
                max_final_bytes=int(maximum * rate * 2) + 65536,
                name="ffmpeg",
                executable_setting="PATH",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WalkietalkError(f"Cannot play voice message: {exc}") from exc
        if code:
            raise WalkietalkError("Cannot play voice message: ffmpeg failed")
        audio = read_wav(output, (maximum_samples + 1) / rate)
        if len(audio.frames) // 2 <= maximum_samples:
            return audio
        if not truncate:
            raise OutputTooLarge(f"Voice message exceeds the {maximum:g}s transcription limit")
        emit("warn", "Voice note cut to fit the transmit limit.")
        frames = audio.frames[: maximum_samples * 2]
        return Wav(frames, audio.rate, len(frames) / (audio.rate * 2))


def encode_voice_note(audio: Wav, output: Path, maximum: float) -> None:
    """Encode bounded captured speech as the OGG/Opus both messengers accept."""
    audio = radio_wav(audio, maximum)
    source = output.with_suffix(".wav")
    write_wav(source, audio)
    code, _, _ = run_cli(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-ac",
            "1",
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            "-application",
            "voip",
            str(output),
        ],
        prompt=b"",
        cwd=str(output.parent),
        env=messaging_environment(),
        deadline=time.monotonic() + 30,
        final_path=output,
        max_final_bytes=MAX_VOICE_UPLOAD_BYTES,
        name="ffmpeg",
        executable_setting="PATH",
    )
    if code or not output.is_file() or not 0 < output.stat().st_size <= MAX_VOICE_UPLOAD_BYTES:
        raise WalkietalkError("Cannot encode voice message")


class MessageBridge:
    """One queue per service. Real CLIs are started only by ``start``."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.whatsapp_store = whatsapp_store()
        self.queues: dict[str, list[Inbound]] = {service: [] for service in MESSAGING_SERVICES}
        self.operator = OperatorQueue(config) if config.messaging_operator_mode else None
        self.seen: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._processes: list[subprocess.Popen[bytes]] = []
        self._threads: list[threading.Thread] = []
        self._signal_base = ""
        self._signal_process: subprocess.Popen[bytes] | None = None
        self._signal_send_lock = threading.Lock()
        self._pending: dict[int, dict] = {}
        self._next_id = 0
        # wacli timestamps have whole-second precision.
        self._started_at = int(time.time())
        self._whatsapp_rowid = 0
        self._whatsapp_pending: dict[int, float] = {}
        self._media_pending: dict[str, tuple[Inbound, float]] = {}

    def mode(self, service: str) -> MessagingMode:
        if service not in MESSAGING_SERVICES:
            raise WalkietalkError(f"Unknown messaging service: {service}")
        return getattr(self.config, service)

    def start(self) -> None:
        try:
            self._start()
        except BaseException:
            # Also clean up a partially started bridge on Ctrl+C.
            self.close()
            raise

    def _start(self) -> None:
        if self.config.whatsapp.enabled():
            self._poll_whatsapp(prime=True)
            self._spawn(
                [
                    "wacli",
                    "--store",
                    str(self.whatsapp_store),
                    "sync",
                    "--follow",
                    "--download-media",
                ],
                stdout=subprocess.DEVNULL,
            )
            self._threads.append(threading.Thread(target=self._watch_whatsapp, daemon=True))
        if self.config.signal.enabled():
            # signal-cli locks the account. Sending with a second process waits
            # until the first one exits, which is the freeze on the radio.
            if signal_daemon_ready(SIGNAL_HTTP):
                self._signal_base = SIGNAL_HTTP
                self._threads.append(threading.Thread(target=self._watch_signal_http, daemon=True))
            else:
                process = self._spawn(self._signal_jsonrpc_command(), stdin=subprocess.PIPE)
                self._signal_process = process
                self._threads.append(
                    threading.Thread(target=self._watch_signal, args=(process,), daemon=True)
                )
        if self.config.signal.enabled():
            self._threads.append(threading.Thread(target=self._watch_media, daemon=True))
        for thread in self._threads:
            thread.start()

    def close(self) -> None:
        with uninterrupted_cleanup():
            self._stop.set()
            for process in self._processes:
                if process.poll() is None and process.pid:
                    _signal_group(process.pid, signal.SIGTERM)
            deadline = time.monotonic() + 2
            for process in self._processes:
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    if process.pid:
                        _signal_group(process.pid, signal.SIGKILL)
                    process.wait()
            for thread in self._threads:
                if thread.is_alive():
                    thread.join(timeout=1)
            for process in self._processes:
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        with suppress(OSError):
                            pipe.close()

    def has(self, service: str) -> bool:
        with self._lock:
            return bool(self.queues[service])

    def add(self, item: Inbound) -> None:
        if not item.identity:
            return
        with self._lock:
            if f"{item.service}:{item.identity}" in self.seen:
                return
            self.seen.add(f"{item.service}:{item.identity}")
            if item.text or item.audio_path:
                self.queues[item.service].append(item)
                if self.operator is not None:
                    self.operator.add(item.service, incoming=item)

    def peek(self, service: str) -> Inbound | None:
        with self._lock:
            return self.queues[service][0] if self.queues[service] else None

    def acknowledge(self, item: Inbound) -> None:
        """Remove a message only after successful playback (or receive-only display)."""
        with self._lock:
            if self.queues[item.service] and self.queues[item.service][0] == item:
                self.queues[item.service].pop(0)
                if self.operator is not None:
                    self.operator.forget(item)

    def discard(self, item: Inbound) -> None:
        """Denial can remove an item behind an approved reply waiting for its contact."""
        with self._lock:
            if item in self.queues[item.service]:
                self.queues[item.service].remove(item)
            if self.operator is not None:
                self.operator.forget(item)

    def send_text(self, service: str, text: str) -> None:
        mode = self.mode(service)
        if service == "signal":
            self._send_signal(text)
            return
        self._send_whatsapp(["text", "--to", mode.to, "--message", text])

    def send_voice(self, service: str, audio: Wav) -> None:
        mode = self.mode(service)
        try:
            with tempfile.TemporaryDirectory(prefix="walkietalk-send-voice-") as directory:
                path = Path(directory) / "voice.ogg"
                # Capture may include its final VAD frame. This is a message
                # upload, so the radio's outbound TX cap does not apply.
                encode_voice_note(audio, path, self.config.max_utterance_seconds + 1)
                if service == "signal":
                    self._send_signal("", attachment=path)
                else:
                    self._send_whatsapp(
                        [
                            "voice",
                            "--to",
                            mode.to,
                            "--file",
                            str(path),
                            "--mime",
                            "audio/ogg; codecs=opus",
                        ]
                    )
        except (OSError, ValueError, WalkietalkError) as exc:
            # Preserve Signal's helpful unregistered-recipient notice.
            if isinstance(exc, WalkietalkError) and str(exc).startswith("Message not sent."):
                raise
            raise WalkietalkError("Message not sent. Could not prepare voice message.") from exc

    def _send_whatsapp(self, arguments: list[str]) -> None:
        command = ["wacli", "--store", str(self.whatsapp_store), "--json", "send", *arguments]
        try:
            code, stdout, _ = run_cli(
                command,
                prompt=b"",
                cwd=os.getcwd(),
                env=messaging_environment(),
                deadline=time.monotonic() + SEND_TIMEOUT_SECONDS,
                name="wacli",
                executable_setting="PATH",
            )
            payload = json.loads(stdout)
            if code or not isinstance(payload, dict) or payload.get("success") is not True:
                raise WalkietalkError("Message not sent.")
        except (OSError, ValueError, RecursionError, WalkietalkError) as exc:
            raise WalkietalkError("Message not sent.") from exc

    def _signal_jsonrpc_command(self) -> list[str]:
        return [
            "signal-cli",
            *(["-a", self.config.signal.account] if self.config.signal.account else []),
            "jsonRpc",
            "--receive-mode",
            "on-start",
            "--ignore-stories",
            "--ignore-avatars",
            "--ignore-stickers",
        ]

    def _send_signal(self, text: str, *, attachment: Path | None = None) -> None:
        if self._signal_base:
            self._send_signal_http(text, attachment=attachment)
        else:
            self._send_signal_rpc(text, attachment=attachment)

    def _signal_send_params(self, text: str, attachment: Path | None = None) -> dict:
        params = {"recipient": [self.config.signal.to], "message": text}
        if attachment is not None:
            if self._signal_base:
                # A separately running daemon may not share our temporary paths.
                with attachment.open("rb") as stream:
                    data = stream.read(MAX_VOICE_UPLOAD_BYTES + 1)
                if not data or len(data) > MAX_VOICE_UPLOAD_BYTES:
                    raise WalkietalkError("Message not sent. Voice message exceeds upload limit.")
                encoded = base64.b64encode(data).decode("ascii")
                value = "data:audio/ogg;filename=voice.ogg;base64," + encoded
            else:
                value = str(attachment)
            params.pop("message")
            params.update(attachments=[value], voiceNote=True)
        if self.config.signal.account:
            params["account"] = self.config.signal.account
        return params

    def _send_signal_http(self, text: str, *, attachment: Path | None = None) -> None:
        request = {
            "jsonrpc": "2.0",
            "method": "send",
            "params": self._signal_send_params(text, attachment),
            "id": 1,
        }
        try:
            response = httpx.post(
                self._signal_base.rstrip("/") + "/api/v1/rpc",
                json=request,
                timeout=SEND_TIMEOUT_SECONDS,
                trust_env=False,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Expected JSON object")
        except (OSError, httpx.HTTPError, ValueError, RecursionError) as exc:
            raise WalkietalkError("Message not sent.") from exc
        failure = signal_send_failure(payload)
        if failure:
            raise WalkietalkError(failure)
        if "result" not in payload:
            raise WalkietalkError("Message not sent.")

    def _send_signal_rpc(self, text: str, *, attachment: Path | None = None) -> None:
        process = self._signal_process
        if process is None or process.poll() is not None or process.stdin is None:
            raise WalkietalkError("Message not sent.")
        with self._signal_send_lock:
            self._next_id += 1
            request_id = self._next_id
            pending: dict = {"event": threading.Event(), "error": None}
            self._pending[request_id] = pending
            line = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "send",
                    "params": self._signal_send_params(text, attachment),
                    "id": request_id,
                }
            )
            try:
                process.stdin.write(line.encode() + b"\n")
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                self._pending.pop(request_id, None)
                raise WalkietalkError("Message not sent.") from exc
        if not pending["event"].wait(SEND_TIMEOUT_SECONDS):
            with self._signal_send_lock:
                self._pending.pop(request_id, None)
            raise WalkietalkError("Message not sent.")
        if pending["error"] is not None:
            raise WalkietalkError(
                signal_send_failure({"error": pending["error"]}) or "Message not sent."
            )

    def _finish_signal_request(self, request_id: object, error: object) -> None:
        if not isinstance(request_id, int):
            return
        with self._signal_send_lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return
        pending["error"] = error
        pending["event"].set()

    def _accept_signal_payload(self, payload: dict) -> None:
        if payload.get("method") == "receive" or "envelope" in payload:
            params = payload.get("params") if payload.get("method") == "receive" else payload
            if isinstance(params, dict):
                account = self.config.signal.account
                if account and params.get("account") != account:
                    return
                self._remember(parse_signal_message(params, self.config.signal))
            return
        if "id" in payload:
            self._finish_signal_request(payload.get("id"), payload.get("error"))

    def _accept_signal_line(self, line: str) -> None:
        payload = signal_payload(line)
        if payload is not None:
            self._accept_signal_payload(payload)

    def _spawn(
        self,
        command: list[str],
        *,
        stdin: int | None = subprocess.DEVNULL,
        stdout: int = subprocess.PIPE,
    ) -> subprocess.Popen[bytes]:
        try:
            process = subprocess.Popen(
                command,
                stdin=stdin,
                stdout=stdout,
                stderr=subprocess.DEVNULL,
                env=messaging_environment(),
                start_new_session=True,
            )
        except OSError as exc:
            raise WalkietalkError(f"Cannot start {' '.join(command[:2])}: {exc}") from exc
        self._processes.append(process)
        return process

    def _remember(self, item: Inbound | None, *, prime: bool = False) -> None:
        if item is None or not item.identity:
            return
        if prime:
            with self._lock:
                self.seen.add(f"{item.service}:{item.identity}")
            return
        if item.kind == "voice" and item.audio_path is not None and not item.audio_path.is_file():
            with self._lock:
                key = f"{item.service}:{item.identity}"
                if key not in self.seen:
                    self._media_pending.setdefault(
                        key, (item, time.monotonic() + VOICE_WAIT_SECONDS)
                    )
            return
        self.add(item)

    def _watch_media(self) -> None:
        while not self._stop.is_set():
            self._poll_media()
            self._stop.wait(0.25)

    def _poll_media(self) -> None:
        with self._lock:
            pending = list(self._media_pending.items())
        for key, (item, deadline) in pending:
            if item.audio_path.is_file():
                self.add(item)
            elif time.monotonic() < deadline:
                continue
            else:
                emit(
                    "error",
                    "Signal voice message unavailable; check "
                    "messaging.signal.attachments_dir and daemon file access.",
                    file=sys.stderr,
                )
                with self._lock:
                    self.seen.add(key)
            with self._lock:
                self._media_pending.pop(key, None)

    def _watch_whatsapp(self) -> None:
        while not self._stop.is_set():
            self._poll_whatsapp()
            self._stop.wait(1)

    def _poll_whatsapp(self, *, prime: bool = False) -> None:
        database = self.whatsapp_store / "wacli.db"
        if not database.is_file():
            return
        mode = self.config.whatsapp
        connection = None
        try:
            with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
                connection.row_factory = sqlite3.Row
                columns = (
                    "rowid AS sequence, msg_id, chat_jid, from_me, text, display_text, "
                    "media_type, local_path, ts AS ts_unix"
                )
                rows = list(
                    connection.execute(
                        f"SELECT {columns} FROM messages WHERE rowid > ? ORDER BY rowid",
                        (self._whatsapp_rowid,),
                    )
                )
                for rowid in self._whatsapp_pending:
                    rows.extend(
                        connection.execute(
                            f"SELECT {columns} FROM messages WHERE rowid = ?",
                            (rowid,),
                        )
                    )
        except sqlite3.Error:
            return
        finally:
            if connection is not None:
                connection.close()
        now = time.monotonic()
        for row in sorted(rows, key=lambda row: (row["ts_unix"] or 0, row["sequence"])):
            row = dict(row)
            rowid = row["sequence"]
            self._whatsapp_rowid = max(self._whatsapp_rowid, rowid)
            if (
                not row["msg_id"]
                or row["from_me"]
                or not _same_destination(mode.to, str(row["chat_jid"] or ""))
            ):
                continue
            # History can be synced in batches after startup. A timestamp cutoff
            # applies to every batch, not just the first successful database read.
            if prime or float(row["ts_unix"] or 0) < self._started_at:
                self._remember(Inbound("whatsapp", "text", identity=str(row["msg_id"])), prime=True)
                continue
            item = parse_whatsapp_row(row, mode)
            if item is None and row["media_type"] in VOICE_MEDIA:
                first_seen = self._whatsapp_pending.setdefault(rowid, now)
                if now - first_seen < VOICE_WAIT_SECONDS:
                    continue
                emit("error", "WhatsApp voice message download timed out.", file=sys.stderr)
                item = Inbound("whatsapp", "text", identity=str(row["msg_id"]))
            self._whatsapp_pending.pop(rowid, None)
            self._remember(item)

    def _watch_signal(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stdout is not None
        while not self._stop.is_set():
            line = process.stdout.readline()
            if not line:
                return
            self._accept_signal_line(line.decode(errors="replace"))

    def _watch_signal_http(self) -> None:
        asyncio.run(self._watch_signal_http_async())

    async def _watch_signal_http_async(self) -> None:
        # Keep cancellation and response closure in the reader's thread. Closing
        # a buffered HTTP response from another thread races with pending reads.
        receiver = asyncio.create_task(self._receive_signal_http())
        try:
            while not self._stop.is_set() and not receiver.done():
                await asyncio.sleep(0.1)
            if receiver.done():
                await receiver
        finally:
            receiver.cancel()
            with suppress(asyncio.CancelledError):
                await receiver

    async def _receive_signal_http(self) -> None:
        url = self._signal_base.rstrip("/") + "/api/v1/events"
        if self.config.signal.account:
            url += "?" + urllib.parse.urlencode({"account": self.config.signal.account})
        timeout = httpx.Timeout(SIGNAL_STREAM_TIMEOUT_SECONDS, connect=5)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            while not self._stop.is_set():
                try:
                    async with client.stream("GET", url) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            self._accept_signal_line(line)
                            if self._stop.is_set():
                                return
                except (httpx.HTTPError, OSError):
                    # A missed keepalive or transport failure needs a new stream.
                    pass
                await asyncio.sleep(1)


def open_messaging(config: Config) -> MessageBridge:
    bridge = MessageBridge(config)
    if config.whatsapp.enabled() or config.signal.enabled():
        bridge.start()
    return bridge
