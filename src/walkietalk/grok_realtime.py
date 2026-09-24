"""Grok Voice speech-to-speech realtime client (Phases 1–2: no radio/PTT).

Explicit ``voice_agent.backend: grok_realtime`` path. Does not replace or fall
back into ``stt`` / ``agent`` / ``tts`` grok / grok_api adapters. SuperGrok
login is never used; auth is the billed ``XAI_API_KEY`` (or named env).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import numpy as np

from .audio import Wav
from .config import Config, WalkietalkError

DEFAULT_WEBSOCKET_URL = "wss://api.x.ai/v1/realtime"
DEFAULT_MODEL = "grok-voice-latest"
REALTIME_PCM_RATE = 24000
# ~100 ms chunks at 24 kHz mono PCM16.
APPEND_CHUNK_BYTES = REALTIME_PCM_RATE // 10 * 2

INPUT_TRANSCRIPT_EVENTS = (
    "conversation.item.input_audio_transcription.completed",
    "conversation.item.input_audio_transcription.updated",
)
OUTPUT_TRANSCRIPT_DELTA = "response.output_audio_transcript.delta"
OUTPUT_TRANSCRIPT_DONE = "response.output_audio_transcript.done"
RESPONSE_DONE = "response.done"

# Server events parents will use for PTT later; this client only parses them.
OUTPUT_AUDIO_DELTA = "response.output_audio.delta"
OUTPUT_AUDIO_DONE = "response.output_audio.done"
FUNCTION_CALL_ARGUMENTS_DONE = "response.function_call_arguments.done"
ERROR_EVENT = "error"

VOICE_AGENT_BACKENDS = ("off", "grok_realtime")


class RealtimeTransport(Protocol):
    """Minimal WebSocket surface for injectable fakes and the live client."""

    async def send(self, data: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


TransportFactory = Callable[[str, dict[str, str]], Awaitable[RealtimeTransport]]


@dataclass(frozen=True)
class RealtimeEvent:
    """Parsed server event. Audio/PTT decisions stay outside this module."""

    type: str
    data: dict[str, Any]
    is_first_output_audio_delta: bool = False
    audio_delta_b64: str | None = None
    function_name: str | None = None
    function_call_id: str | None = None
    function_arguments: str | None = None
    error_message: str | None = None


def valid_api_key(value: object) -> bool:
    return isinstance(value, str) and bool(value) and all(33 <= ord(c) <= 126 for c in value)


def build_realtime_url(base: str, model: str) -> str:
    """Attach ``model`` as a query parameter without dropping existing ones."""
    parts = urlsplit(base)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["model"] = model
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def open_voice_agent(
    config: Config,
    *,
    transport: RealtimeTransport | None = None,
    transport_factory: TransportFactory | None = None,
    api_key: str | None = None,
) -> GrokRealtimeClient | None:
    """Smallest registry hook: ``off`` → None; ``grok_realtime`` → client."""
    backend = config.voice_agent_backend
    if backend == "off":
        return None
    if backend == "grok_realtime":
        return GrokRealtimeClient(
            config,
            transport=transport,
            transport_factory=transport_factory,
            api_key=api_key,
        )
    raise WalkietalkError(
        "voice_agent.backend must be off or grok_realtime; "
        "other names are not implemented and never fall back to stt/agent/tts"
    )


class GrokRealtimeClient:
    """Realtime WebSocket session for Grok Voice; no serial, PortAudio, or PTT."""

    def __init__(
        self,
        config: Config,
        *,
        transport: RealtimeTransport | None = None,
        transport_factory: TransportFactory | None = None,
        api_key: str | None = None,
    ) -> None:
        self.config = config
        self._transport = transport
        self._transport_factory = transport_factory
        self._api_key = api_key
        self._connected = False
        self._saw_output_audio_delta = False
        self._closed = False

    def label(self) -> str:
        return (
            f"grok_realtime ({self.config.voice_agent_model}; "
            f"{self.config.voice_agent_voice}; "
            f"{self.config.voice_agent_api_key_env}; billed Speech to Speech API)"
        )

    def resolve_api_key(self) -> str:
        """Read billed API key; never SuperGrok login. Raises WalkietalkError."""
        if self._api_key is not None:
            if not valid_api_key(self._api_key):
                raise WalkietalkError(
                    f"voice_agent.backend grok_realtime requires a valid "
                    f"{self.config.voice_agent_api_key_env} for billed xAI Speech to Speech "
                    "API access; no SuperGrok login fallback"
                )
            return self._api_key
        token = os.environ.get(self.config.voice_agent_api_key_env)
        if not valid_api_key(token):
            raise WalkietalkError(
                f"voice_agent.backend grok_realtime requires "
                f"{self.config.voice_agent_api_key_env} for billed xAI Speech to Speech "
                "API access; no SuperGrok login fallback"
            )
        return token

    async def connect(self) -> None:
        """Open the transport with Bearer auth. Missing/invalid key fails loudly."""
        if self._connected:
            return
        key = self.resolve_api_key()
        headers = {"Authorization": f"Bearer {key}"}
        url = build_realtime_url(
            self.config.voice_agent_websocket_url, self.config.voice_agent_model
        )
        try:
            if self._transport is not None:
                pass  # Injected fake/live transport already provided.
            elif self._transport_factory is not None:
                self._transport = await asyncio.wait_for(
                    self._transport_factory(url, headers),
                    timeout=self.config.voice_agent_connect_timeout_seconds,
                )
            else:
                self._transport = await asyncio.wait_for(
                    _connect_websockets(url, headers),
                    timeout=self.config.voice_agent_connect_timeout_seconds,
                )
        except WalkietalkError:
            raise
        except TimeoutError as exc:
            raise WalkietalkError(
                "Grok realtime connection timed out; no stt/agent/tts fallback"
            ) from exc
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            lowered = message.lower()
            if any(token in lowered for token in ("401", "403", "unauthorized", "forbidden")):
                raise WalkietalkError(
                    "Grok realtime authentication failed; check billed "
                    f"{self.config.voice_agent_api_key_env}; no SuperGrok login fallback"
                ) from exc
            raise WalkietalkError(
                f"Grok realtime connection failed: {message}; no stt/agent/tts fallback"
            ) from exc
        self._connected = True
        self._closed = False

    async def session_update(
        self,
        *,
        instructions: str | None = None,
        voice: str | None = None,
        turn_detection: dict[str, Any] | None = None,
        extra_session: dict[str, Any] | None = None,
    ) -> None:
        """Send ``session.update`` after connect."""
        self._require_open()
        session: dict[str, Any] = {
            "voice": voice if voice is not None else self.config.voice_agent_voice,
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": 24000}},
                "output": {"format": {"type": "audio/pcm", "rate": 24000}},
            },
        }
        if instructions is not None:
            session["instructions"] = instructions
        if turn_detection is not None:
            session["turn_detection"] = turn_detection
        else:
            # Manual commit fits half-duplex unkey; parent commits after RX ends.
            session["turn_detection"] = None
        if extra_session:
            session.update(extra_session)
        await self._send({"type": "session.update", "session": session})

    async def append_audio(self, pcm16le: bytes) -> None:
        """Append base64 PCM16 little-endian audio to the input buffer."""
        self._require_open()
        if not pcm16le:
            return
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm16le).decode("ascii"),
            }
        )

    async def commit_audio(self) -> None:
        """Commit the input buffer as the end of the user turn (e.g. after unkey)."""
        self._require_open()
        await self._send({"type": "input_audio_buffer.commit"})

    async def create_response(self) -> None:
        """Request a model response after a manual commit (no server VAD)."""
        self._require_open()
        await self._send({"type": "response.create"})

    async def events(self) -> AsyncIterator[RealtimeEvent]:
        """Iterate parsed server events until the transport closes or idle timeout."""
        self._require_open()
        assert self._transport is not None
        idle = self.config.voice_agent_idle_timeout_seconds
        while not self._closed:
            try:
                raw = await asyncio.wait_for(self._transport.recv(), timeout=idle)
            except TimeoutError as exc:
                raise WalkietalkError(
                    "Grok realtime idle timeout waiting for server events; "
                    "no stt/agent/tts fallback"
                ) from exc
            except Exception as exc:
                if self._closed:
                    return
                name = exc.__class__.__name__.lower()
                # websockets ConnectionClosed* and fake-transport EOF end the stream.
                if "connectionclosed" in name or name in {"connectionerror", "eoferror"}:
                    return
                message = str(exc).strip() or exc.__class__.__name__
                raise WalkietalkError(
                    f"Grok realtime transport error: {message}; no stt/agent/tts fallback"
                ) from exc
            event = self._parse_event(raw)
            if event.type == ERROR_EVENT:
                detail = event.error_message or "unknown error"
                lowered = detail.lower()
                if any(
                    token in lowered
                    for token in ("auth", "unauthorized", "forbidden", "api key", "invalid key")
                ):
                    raise WalkietalkError(
                        f"Grok realtime authentication failed: {detail}; "
                        f"check billed {self.config.voice_agent_api_key_env}; "
                        "no SuperGrok login fallback"
                    )
                raise WalkietalkError(
                    f"Grok realtime protocol error: {detail}; no stt/agent/tts fallback"
                )
            yield event

    async def close(self) -> None:
        if self._transport is None or self._closed:
            self._closed = True
            self._connected = False
            return
        self._closed = True
        self._connected = False
        try:
            await self._transport.close()
        except Exception:
            pass

    def _require_open(self) -> None:
        if not self._connected or self._transport is None or self._closed:
            raise WalkietalkError("Grok realtime client is not connected")

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._transport is not None
        try:
            await self._transport.send(json.dumps(payload))
        except WalkietalkError:
            raise
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            raise WalkietalkError(
                f"Grok realtime send failed: {message}; no stt/agent/tts fallback"
            ) from exc

    def _parse_event(self, raw: str | bytes) -> RealtimeEvent:
        if isinstance(raw, bytes):
            # Binary audio frames are for a later transport mode; Phase 1 expects JSON.
            raise WalkietalkError(
                "Grok realtime received unexpected binary frame; configure JSON audio transport"
            )
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise WalkietalkError(
                "Grok realtime received malformed JSON; no stt/agent/tts fallback"
            ) from exc
        if not isinstance(data, dict):
            raise WalkietalkError(
                "Grok realtime received a non-object event; no stt/agent/tts fallback"
            )
        event_type = data.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise WalkietalkError("Grok realtime event missing type; no stt/agent/tts fallback")
        first_delta = False
        audio_b64 = None
        function_name = None
        call_id = None
        arguments = None
        error_message = None
        if event_type == OUTPUT_AUDIO_DELTA:
            audio = data.get("delta")
            if audio is None:
                audio = data.get("audio")
            if isinstance(audio, str):
                audio_b64 = audio
            if not self._saw_output_audio_delta:
                first_delta = True
                self._saw_output_audio_delta = True
        elif event_type == OUTPUT_AUDIO_DONE:
            self._saw_output_audio_delta = False
        elif event_type == FUNCTION_CALL_ARGUMENTS_DONE:
            function_name = data.get("name") if isinstance(data.get("name"), str) else None
            call_id = data.get("call_id") if isinstance(data.get("call_id"), str) else None
            arguments = data.get("arguments") if isinstance(data.get("arguments"), str) else None
            # Tool-call gap: parent should unkey; reset so next spoken audio is "first".
            self._saw_output_audio_delta = False
        elif event_type == ERROR_EVENT:
            err = data.get("error")
            if isinstance(err, dict):
                message = err.get("message") or err.get("code") or err
                error_message = str(message)
            else:
                error_message = str(data.get("message") or err or "error")
        return RealtimeEvent(
            type=event_type,
            data=data,
            is_first_output_audio_delta=first_delta,
            audio_delta_b64=audio_b64,
            function_name=function_name,
            function_call_id=call_id,
            function_arguments=arguments,
            error_message=error_message,
        )


@dataclass(frozen=True)
class OfflineVoiceResult:
    """Offline capture result: reply audio and any transcripts. No TX."""

    reply_wav: Wav | None
    input_transcript: str = ""
    output_transcript: str = ""
    event_types: tuple[str, ...] = ()


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int = REALTIME_PCM_RATE) -> bytes:
    """Resample mono PCM16 little-endian to the realtime session rate."""
    if src_rate <= 0 or dst_rate <= 0:
        raise WalkietalkError("Invalid PCM sample rate for voice agent")
    if not pcm:
        return b""
    if len(pcm) % 2:
        raise WalkietalkError("PCM16 audio length must be even")
    if src_rate == dst_rate:
        return pcm
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    n = int(round(samples.size * dst_rate / src_rate))
    if n < 1:
        return b""
    resampled = np.interp(
        np.linspace(0, 1, n, endpoint=False),
        np.linspace(0, 1, samples.size, endpoint=False),
        samples,
    )
    return np.rint(resampled).clip(-32768, 32767).astype("<i2").tobytes()


def pcm16_to_wav(pcm: bytes, rate: int = REALTIME_PCM_RATE) -> Wav | None:
    if not pcm:
        return None
    if len(pcm) % 2:
        raise WalkietalkError("Assembled reply PCM16 length must be even")
    duration = len(pcm) / 2 / rate
    return Wav(pcm, rate, duration)


def _transcript_from_event(event: RealtimeEvent) -> tuple[str | None, str | None]:
    """Return (input_transcript_update, output_transcript_update)."""
    data = event.data
    if event.type in INPUT_TRANSCRIPT_EVENTS:
        value = data.get("transcript")
        return (value if isinstance(value, str) else None, None)
    if event.type == OUTPUT_TRANSCRIPT_DONE:
        value = data.get("transcript")
        return (None, value if isinstance(value, str) else None)
    if event.type == OUTPUT_TRANSCRIPT_DELTA:
        value = data.get("delta")
        if value is None:
            value = data.get("transcript")
        return (None, value if isinstance(value, str) else None)
    return (None, None)


async def wait_for_event(
    client: GrokRealtimeClient,
    event_type: str,
    *,
    timeout: float,
    ignore_pings: bool = True,
) -> list[str]:
    """Drain events until ``event_type`` or timeout (pings ignored optionally)."""
    seen: list[str] = []
    deadline = time.monotonic() + timeout
    async for event in client.events():
        seen.append(event.type)
        if ignore_pings and event.type == "ping":
            if time.monotonic() >= deadline:
                raise WalkietalkError(
                    f"Grok realtime timed out waiting for {event_type} (pings only); "
                    "no stt/agent/tts fallback"
                )
            continue
        if event.type == event_type:
            return seen
        if time.monotonic() >= deadline:
            raise WalkietalkError(
                f"Grok realtime timed out waiting for {event_type}; no stt/agent/tts fallback"
            )
    raise WalkietalkError(f"Grok realtime closed before {event_type}; no stt/agent/tts fallback")


async def wait_for_session_updated(client: GrokRealtimeClient) -> list[str]:
    """Drain early events until session.updated (required before appending audio)."""
    return await wait_for_event(
        client,
        "session.updated",
        timeout=client.config.voice_agent_connect_timeout_seconds,
    )


async def run_offline_turn(
    client: GrokRealtimeClient,
    pcm16le: bytes,
    input_rate: int,
    *,
    instructions: str | None = None,
) -> OfflineVoiceResult:
    """One offline utterance: append/commit PCM, collect reply audio, never TX."""
    await client.connect()
    try:
        await client.session_update(instructions=instructions)
        seen = await wait_for_session_updated(client)
        session_pcm = resample_pcm16(pcm16le, input_rate, REALTIME_PCM_RATE)
        if not session_pcm:
            raise WalkietalkError("Voice agent input audio is empty after resampling")
        for offset in range(0, len(session_pcm), APPEND_CHUNK_BYTES):
            await client.append_audio(session_pcm[offset : offset + APPEND_CHUNK_BYTES])
        await client.commit_audio()
        seen.extend(
            await wait_for_event(
                client,
                "input_audio_buffer.committed",
                timeout=max(10.0, float(client.config.voice_agent_idle_timeout_seconds)),
            )
        )
        await client.create_response()

        audio_chunks: list[bytes] = []
        input_transcript = ""
        output_parts: list[str] = []
        output_final = ""
        # Pings must not keep a turn open forever.
        turn_deadline = time.monotonic() + max(
            15.0, float(client.config.voice_agent_idle_timeout_seconds)
        )
        async for event in client.events():
            if event.type == "ping":
                seen.append(event.type)
                if time.monotonic() >= turn_deadline:
                    raise WalkietalkError(
                        "Grok realtime turn timed out (pings only); no stt/agent/tts fallback"
                    )
                continue
            seen.append(event.type)
            if event.type == OUTPUT_AUDIO_DELTA and event.audio_delta_b64:
                try:
                    audio_chunks.append(base64.b64decode(event.audio_delta_b64, validate=True))
                except (ValueError, TypeError) as exc:
                    raise WalkietalkError(
                        "Grok realtime returned invalid audio delta; no stt/agent/tts fallback"
                    ) from exc
            in_update, out_update = _transcript_from_event(event)
            if in_update is not None:
                input_transcript = in_update
            if out_update is not None:
                if event.type == OUTPUT_TRANSCRIPT_DELTA:
                    output_parts.append(out_update)
                else:
                    output_final = out_update
            if event.type == RESPONSE_DONE:
                break
            if time.monotonic() >= turn_deadline:
                raise WalkietalkError(
                    "Grok realtime turn timed out waiting for response.done; "
                    "no stt/agent/tts fallback"
                )

        output_transcript = output_final or "".join(output_parts)
        reply = pcm16_to_wav(b"".join(audio_chunks), REALTIME_PCM_RATE)
        return OfflineVoiceResult(
            reply_wav=reply,
            input_transcript=input_transcript.strip(),
            output_transcript=output_transcript.strip(),
            event_types=tuple(seen),
        )
    finally:
        await client.close()


def offline_voice_check(
    config: Config,
    pcm16le: bytes,
    input_rate: int,
    *,
    transport: RealtimeTransport | None = None,
    transport_factory: TransportFactory | None = None,
    api_key: str | None = None,
    instructions: str | None = None,
) -> OfflineVoiceResult:
    """Sync entry for CLI/tests. Requires ``voice_agent.backend: grok_realtime``."""
    if config.voice_agent_backend != "grok_realtime":
        raise WalkietalkError(
            "voice-agent-check requires voice_agent.backend: grok_realtime; "
            "it never falls back to stt/agent/tts"
        )
    client = open_voice_agent(
        config,
        transport=transport,
        transport_factory=transport_factory,
        api_key=api_key,
    )
    if client is None:
        raise WalkietalkError(
            "voice-agent-check requires voice_agent.backend: grok_realtime; "
            "it never falls back to stt/agent/tts"
        )
    return asyncio.run(run_offline_turn(client, pcm16le, input_rate, instructions=instructions))


@dataclass
class _WebSocketTransport:
    """Thin adapter around the websockets library connection."""

    connection: Any
    _closed: bool = field(default=False, init=False)

    async def send(self, data: str) -> None:
        await self.connection.send(data)

    async def recv(self) -> str | bytes:
        return await self.connection.recv()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.connection.close()


async def _connect_websockets(url: str, headers: dict[str, str]) -> RealtimeTransport:
    try:
        import websockets
    except ImportError as exc:
        raise WalkietalkError(
            "Grok realtime requires the websockets package; install project dependencies"
        ) from exc
    try:
        connection = await websockets.connect(url, additional_headers=headers)
    except TypeError:
        # Older websockets used ``extra_headers``.
        connection = await websockets.connect(url, extra_headers=headers)
    return _WebSocketTransport(connection)
