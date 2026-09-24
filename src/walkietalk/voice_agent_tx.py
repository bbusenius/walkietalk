"""Parent-owned supervised TX for Grok realtime (Phase 3).

Radio glue lives here — not in ``grok_realtime.py``. PTT keys on the first AI
audio delta, unkeys on ``response.output_audio.done`` after a local buffer drain,
unkeys on tool-call gaps, and re-keys when spoken audio resumes. Errors always
attempt release. Live SerialPTT requires an explicit allow_key / ``--transmit``
opt-in; DryPTT is the default supervised dry path.
"""

from __future__ import annotations

import asyncio
import base64
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .audio import Playback, Wav
from .config import Config, WalkietalkError
from .grok_realtime import (
    APPEND_CHUNK_BYTES,
    FUNCTION_CALL_ARGUMENTS_DONE,
    OUTPUT_AUDIO_DELTA,
    OUTPUT_AUDIO_DONE,
    REALTIME_PCM_RATE,
    RESPONSE_DONE,
    GrokRealtimeClient,
    RealtimeEvent,
    RealtimeTransport,
    TransportFactory,
    _transcript_from_event,
    open_voice_agent,
    pcm16_to_wav,
    resample_pcm16,
    wait_for_event,
    wait_for_session_updated,
)
from .session import uninterrupted_cleanup
from .tts import radio_wav, write_wav

PlaySegment = Callable[[bytes, int, float], None]


@dataclass(frozen=True)
class PttAction:
    """Recorded key/unkey for tests and dry-run observability."""

    kind: str  # "key" or "unkey"
    reason: str


@dataclass(frozen=True)
class SupervisedTxResult:
    """Outcome of one supervised realtime turn (TX optional)."""

    reply_wav: Wav | None
    input_transcript: str = ""
    output_transcript: str = ""
    event_types: tuple[str, ...] = ()
    ptt_actions: tuple[PttAction, ...] = ()
    truncated_by_tx_cap: bool = False


@dataclass
class SupervisedRealtimeTx:
    """Drive PTT from realtime events. Does not open WebSockets itself."""

    config: Config
    ptt: object
    allow_key: bool
    play_segment: PlaySegment | None = None
    clock: Callable[[], float] = time.monotonic
    actions: list[PttAction] = field(default_factory=list)
    keyed: bool = False
    opened: bool = False
    closed: bool = False
    truncated: bool = False
    _buffer: bytearray = field(default_factory=bytearray)
    _tx_started_at: float | None = None
    _airtime_used: float = 0.0

    def handle(self, event: RealtimeEvent) -> None:
        if self.closed:
            return
        if self.truncated and event.type == OUTPUT_AUDIO_DELTA:
            return
        if event.type == OUTPUT_AUDIO_DELTA:
            self._on_audio_delta(event)
        elif event.type in {
            OUTPUT_AUDIO_DONE,
            FUNCTION_CALL_ARGUMENTS_DONE,
            RESPONSE_DONE,
        }:
            reason = {
                OUTPUT_AUDIO_DONE: "output_audio.done",
                FUNCTION_CALL_ARGUMENTS_DONE: "tool_call_gap",
                RESPONSE_DONE: "response.done",
            }[event.type]
            self._finish_segment(reason)

    def fail(self, reason: str = "error") -> None:
        """Unkey on errors; safe to call multiple times."""
        try:
            self._finish_segment(reason, play=False)
        finally:
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.keyed:
                self._unkey("close")
        finally:
            if self.opened:
                with uninterrupted_cleanup():
                    self.ptt.close()
                self.opened = False

    def _on_audio_delta(self, event: RealtimeEvent) -> None:
        if event.is_first_output_audio_delta:
            if self.keyed:
                self._finish_segment("rekey")
            self._key("first_output_audio_delta")
        if not event.audio_delta_b64:
            return
        try:
            chunk = base64.b64decode(event.audio_delta_b64, validate=True)
        except (ValueError, TypeError) as exc:
            self.fail("invalid_audio_delta")
            raise WalkietalkError(
                "Grok realtime returned invalid audio delta during supervised TX; "
                "no stt/agent/tts fallback"
            ) from exc
        if self.allow_key and not self.keyed:
            self._key("output_audio_delta")
        budget = self._remaining_airtime()
        if self.allow_key and budget <= 0:
            self.truncated = True
            self._finish_segment("tx_cap", play=False)
            return
        if self.allow_key:
            max_bytes = int(budget * REALTIME_PCM_RATE) * 2
            max_bytes -= max_bytes % 2
            if max_bytes <= 0:
                self.truncated = True
                self._finish_segment("tx_cap", play=False)
                return
            if len(self._buffer) + len(chunk) > max_bytes:
                keep = max_bytes - len(self._buffer)
                if keep > 0:
                    self._buffer.extend(chunk[: keep - (keep % 2)])
                self.truncated = True
                self._finish_segment("tx_cap", play=True)
                return
        self._buffer.extend(chunk)

    def _remaining_airtime(self) -> float:
        cap = max(0.0, self.config.max_tx_seconds - self.config.settle_seconds)
        remaining_total = cap - self._airtime_used
        if self.keyed and self._tx_started_at is not None:
            elapsed = self.clock() - self._tx_started_at
            return max(0.0, min(remaining_total, cap - elapsed))
        return max(0.0, remaining_total)

    def _ensure_open(self) -> None:
        if not self.opened:
            self.ptt.open()
            self.opened = True

    def _key(self, reason: str) -> None:
        if self.keyed or not self.allow_key:
            return
        self._ensure_open()
        self.ptt.on()
        self.keyed = True
        self._tx_started_at = self.clock()
        self.actions.append(PttAction("key", reason))

    def _unkey(self, reason: str) -> None:
        if not self.keyed:
            return
        if self._tx_started_at is not None:
            self._airtime_used += max(0.0, self.clock() - self._tx_started_at)
        self._tx_started_at = None
        with uninterrupted_cleanup():
            self.ptt.off()
        self.keyed = False
        self.actions.append(PttAction("unkey", reason))

    def _finish_segment(self, reason: str, *, play: bool = True) -> None:
        pcm = bytes(self._buffer)
        self._buffer.clear()
        if (
            play
            and pcm
            and self.allow_key
            and self.play_segment is not None
            and (self.keyed or reason.startswith("tx_cap"))
        ):
            if not self.keyed:
                self._key(reason)
            deadline = self.clock() + max(0.05, self._remaining_airtime())
            try:
                settle = min(self.config.settle_seconds, max(0.0, deadline - self.clock()))
                if settle > 0:
                    time.sleep(settle)
                self.play_segment(pcm, REALTIME_PCM_RATE, deadline)
            except Exception:
                self._unkey("playback_error")
                raise
        if self.keyed:
            self._unkey(reason)


def play_segment_via_playback(config: Config, pcm: bytes, rate: int, deadline: float) -> None:
    """Play one keyed PCM burst through the isolated Playback worker."""
    if not pcm:
        return
    maximum = max(0.05, deadline - time.monotonic())
    wav = radio_wav(Wav(pcm, rate, len(pcm) / 2 / rate), maximum)
    with tempfile.TemporaryDirectory(prefix="walkietalk-voice-agent-") as directory:
        path = Path(directory) / "burst.wav"
        write_wav(path, wav)
        playback = Playback(path, config.output_device, config.gain, maximum)
        try:
            playback.prepare()
            playback.play(deadline)
        finally:
            with uninterrupted_cleanup():
                playback.close()


def play_segment_dry(pcm: bytes, rate: int, deadline: float) -> None:
    """Dry drain without opening audio devices (tests / DryPTT)."""
    del deadline  # Deadline enforced by the parent; dry path does not block on audio.
    if not pcm or rate <= 0:
        return


async def run_supervised_turn(
    client: GrokRealtimeClient,
    config: Config,
    pcm16le: bytes,
    input_rate: int,
    ptt: object,
    *,
    allow_key: bool,
    play_segment: PlaySegment | None = None,
    instructions: str | None = None,
) -> SupervisedTxResult:
    """Feed one utterance into realtime and apply parent-owned PTT policy."""
    if allow_key and play_segment is None:
        play_segment = play_segment_dry
    tx = SupervisedRealtimeTx(
        config,
        ptt,
        allow_key=allow_key,
        play_segment=play_segment if allow_key else None,
    )
    audio_chunks: list[bytes] = []
    input_transcript = ""
    output_parts: list[str] = []
    output_final = ""
    seen: list[str] = []
    await client.connect()
    try:
        await client.session_update(instructions=instructions)
        seen.extend(await wait_for_session_updated(client))
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
                timeout=max(10.0, float(config.voice_agent_idle_timeout_seconds)),
            )
        )
        await client.create_response()
        turn_deadline = time.monotonic() + max(15.0, float(config.voice_agent_idle_timeout_seconds))
        try:
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
                        tx.fail("invalid_audio_delta")
                        raise WalkietalkError(
                            "Grok realtime returned invalid audio delta; no stt/agent/tts fallback"
                        ) from exc
                in_update, out_update = _transcript_from_event(event)
                if in_update is not None:
                    input_transcript = in_update
                if out_update is not None:
                    if event.type.endswith(".delta"):
                        output_parts.append(out_update)
                    else:
                        output_final = out_update
                tx.handle(event)
                if event.type == RESPONSE_DONE or tx.truncated:
                    break
                if time.monotonic() >= turn_deadline:
                    raise WalkietalkError(
                        "Grok realtime turn timed out waiting for response.done; "
                        "no stt/agent/tts fallback"
                    )
        except WalkietalkError:
            tx.fail("protocol_error")
            raise
        finally:
            tx.close()
    finally:
        await client.close()

    return SupervisedTxResult(
        reply_wav=pcm16_to_wav(b"".join(audio_chunks), REALTIME_PCM_RATE),
        input_transcript=input_transcript.strip(),
        output_transcript=(output_final or "".join(output_parts)).strip(),
        event_types=tuple(seen),
        ptt_actions=tuple(tx.actions),
        truncated_by_tx_cap=tx.truncated,
    )


def supervised_voice_check(
    config: Config,
    pcm16le: bytes,
    input_rate: int,
    ptt: object,
    *,
    allow_key: bool,
    transport: RealtimeTransport | None = None,
    transport_factory: TransportFactory | None = None,
    api_key: str | None = None,
    instructions: str | None = None,
    play_segment: PlaySegment | None = None,
) -> SupervisedTxResult:
    """Sync entry: requires ``voice_agent.backend: grok_realtime``."""
    if config.voice_agent_backend != "grok_realtime":
        raise WalkietalkError(
            "supervised voice-agent TX requires voice_agent.backend: grok_realtime; "
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
            "supervised voice-agent TX requires voice_agent.backend: grok_realtime; "
            "it never falls back to stt/agent/tts"
        )
    return asyncio.run(
        run_supervised_turn(
            client,
            config,
            pcm16le,
            input_rate,
            ptt,
            allow_key=allow_key,
            play_segment=play_segment,
            instructions=instructions,
        )
    )
