"""Parent-owned supervised TX for Grok realtime (Phase 3).

Radio glue lives here — not in ``grok_realtime.py``. PTT keys when decoded PCM
crosses an audible energy threshold (with a short pre-roll), unkeys on
``response.output_audio.done`` after a local buffer drain, unkeys on tool-call
gaps, and re-keys when spoken audio resumes. Errors always attempt release.
Live SerialPTT requires an explicit allow_key / ``--transmit`` opt-in; DryPTT
is the default supervised dry path.
"""

from __future__ import annotations

import asyncio
import base64
import tempfile
import threading
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
    response_events,
    wait_for_event,
    wait_for_session_updated,
)

# Energy-gate: key closer to audible speech than first_output_audio_delta.
# RMS of PCM16 LE mono; silence ~0, soft noise tens, speech typically >> threshold.
PTT_ENERGY_THRESHOLD_RMS = 200.0
PTT_ENERGY_PRE_ROLL_MS = 150
PTT_ENERGY_PRE_ROLL_BYTES = int(REALTIME_PCM_RATE * PTT_ENERGY_PRE_ROLL_MS / 1000) * 2
from .session import uninterrupted_cleanup
from .tts import radio_wav, write_wav

PlaySegment = Callable[[bytes, int, float], None]


def pcm16_rms(pcm: bytes) -> float:
    """RMS of little-endian mono PCM16; empty → 0."""
    if len(pcm) < 2:
        return 0.0
    if len(pcm) % 2:
        pcm = pcm[:-1]
    samples = memoryview(pcm).cast("h")
    if not samples:
        return 0.0
    total = 0.0
    for sample in samples:
        total += float(sample) * float(sample)
    return (total / len(samples)) ** 0.5


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
    _pre_roll: bytearray = field(default_factory=bytearray)
    _energy_armed: bool = False
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

    def _reset_energy_gate(self) -> None:
        self._energy_armed = False
        self._pre_roll.clear()

    def _on_audio_delta(self, event: RealtimeEvent) -> None:
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
        if len(chunk) % 2:
            chunk = chunk[:-1]
        if not chunk:
            return

        if not self._energy_armed:
            self._pre_roll.extend(chunk)
            excess = len(self._pre_roll) - PTT_ENERGY_PRE_ROLL_BYTES
            if excess > 0:
                del self._pre_roll[: excess - (excess % 2)]
            if pcm16_rms(chunk) < PTT_ENERGY_THRESHOLD_RMS:
                return
            # Crossing threshold: key, then play pre-roll (includes this chunk).
            pre = bytes(self._pre_roll)
            self._pre_roll.clear()
            if self.keyed:
                self._finish_segment("rekey", play=True)
            self._energy_armed = True
            self._key("audible_audio_energy")
            self._buffer.extend(pre)
        else:
            self._buffer.extend(chunk)

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
            if len(self._buffer) > max_bytes:
                self._buffer = bytearray(self._buffer[: max_bytes - (max_bytes % 2)])
                self.truncated = True
                self._finish_segment("tx_cap", play=True)
                return

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
        self._reset_energy_gate()
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
                timeout=max(10.0, float(config.agent_realtime_idle_timeout_seconds)),
            )
        )
        await client.create_response()
        turn_deadline = None
        try:
            async for event in response_events(client):
                if event.type == "ping":
                    seen.append(event.type)
                    if turn_deadline is not None and time.monotonic() >= turn_deadline:
                        raise WalkietalkError(
                            "Grok realtime turn timed out (pings only); no stt/agent/tts fallback"
                        )
                    continue
                if turn_deadline is None:
                    turn_deadline = time.monotonic() + max(
                        15.0, float(config.agent_realtime_idle_timeout_seconds)
                    )
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
    """Sync entry: requires ``agent.backend: grok_realtime``."""
    if config.agent_backend != "grok_realtime":
        raise WalkietalkError(
            "supervised voice-agent TX requires agent.backend: grok_realtime; "
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
            "supervised voice-agent TX requires agent.backend: grok_realtime; "
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


async def run_supervised_text_turn(
    client: GrokRealtimeClient,
    config: Config,
    traffic: str,
    ptt: object,
    *,
    allow_key: bool,
    play_segment: PlaySegment | None = None,
    instructions: str | None = None,
) -> SupervisedTxResult:
    """Drive one talk turn from trusted STT traffic text (no utterance WAV upload)."""
    if not isinstance(traffic, str) or not traffic.strip():
        raise WalkietalkError("supervised text turn requires non-empty traffic text")
    if allow_key and play_segment is None:
        play_segment = play_segment_dry
    tx = SupervisedRealtimeTx(
        config,
        ptt,
        allow_key=allow_key,
        play_segment=play_segment if allow_key else None,
    )
    audio_chunks: list[bytes] = []
    output_parts: list[str] = []
    output_final = ""
    seen: list[str] = []
    await client.connect()
    try:
        await client.session_update(instructions=instructions)
        seen.extend(await wait_for_session_updated(client))
        await client.create_user_text_message(traffic.strip())
        await client.create_response()
        turn_deadline = None
        try:
            async for event in response_events(client):
                if event.type == "ping":
                    seen.append(event.type)
                    if turn_deadline is not None and time.monotonic() >= turn_deadline:
                        raise WalkietalkError(
                            "Grok realtime text turn timed out (pings only); "
                            "no stt/agent/tts fallback"
                        )
                    continue
                if turn_deadline is None:
                    turn_deadline = time.monotonic() + max(
                        15.0, float(config.agent_realtime_idle_timeout_seconds)
                    )
                seen.append(event.type)
                if event.type == OUTPUT_AUDIO_DELTA and event.audio_delta_b64:
                    try:
                        audio_chunks.append(base64.b64decode(event.audio_delta_b64, validate=True))
                    except (ValueError, TypeError) as exc:
                        tx.fail("invalid_audio_delta")
                        raise WalkietalkError(
                            "Grok realtime returned invalid audio delta; no stt/agent/tts fallback"
                        ) from exc
                _in_update, out_update = _transcript_from_event(event)
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
                        "Grok realtime text turn timed out waiting for response.done; "
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
        input_transcript=traffic.strip(),
        output_transcript=(output_final or "".join(output_parts)).strip(),
        event_types=tuple(seen),
        ptt_actions=tuple(tx.actions),
        truncated_by_tx_cap=tx.truncated,
    )


def supervised_text_turn(
    config: Config,
    traffic: str,
    ptt: object,
    *,
    allow_key: bool,
    transport: RealtimeTransport | None = None,
    transport_factory: TransportFactory | None = None,
    api_key: str | None = None,
    instructions: str | None = None,
    play_segment: PlaySegment | None = None,
) -> SupervisedTxResult:
    """Sync talk entry: text traffic → realtime reply; parent-owned PTT; no WAV upload."""
    if config.agent_backend != "grok_realtime":
        raise WalkietalkError(
            "supervised text turn requires agent.backend: grok_realtime; "
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
            "supervised text turn requires agent.backend: grok_realtime; "
            "it never falls back to stt/agent/tts"
        )
    return asyncio.run(
        run_supervised_text_turn(
            client,
            config,
            traffic,
            ptt,
            allow_key=allow_key,
            play_segment=play_segment,
            instructions=instructions,
        )
    )


async def run_supervised_text_speak(
    client: GrokRealtimeClient,
    config: Config,
    text: str,
    ptt: object,
    *,
    allow_key: bool,
    play_segment: PlaySegment | None = None,
) -> SupervisedTxResult:
    """Speak ``text`` verbatim via realtime force_message with parent-owned PTT."""
    if allow_key and play_segment is None:
        play_segment = play_segment_dry
    tx = SupervisedRealtimeTx(
        config,
        ptt,
        allow_key=allow_key,
        play_segment=play_segment if allow_key else None,
    )
    audio_chunks: list[bytes] = []
    output_parts: list[str] = []
    output_final = ""
    seen: list[str] = []
    await client.connect()
    try:
        # Acks must not enable tools; speak the configured voice only.
        await client.session_update(
            instructions=(
                "You are a radio acknowledgement voice. "
                "Do not add preamble or commentary outside force_message."
            ),
            include_web_search=False,
        )
        seen.extend(await wait_for_session_updated(client))
        await client.create_force_message(text)
        turn_deadline = None
        try:
            async for event in response_events(client):
                if event.type == "ping":
                    seen.append(event.type)
                    if turn_deadline is not None and time.monotonic() >= turn_deadline:
                        raise WalkietalkError(
                            "Grok realtime ack timed out (pings only); no tts fallback"
                        )
                    continue
                if turn_deadline is None:
                    turn_deadline = time.monotonic() + max(
                        15.0, float(config.agent_realtime_idle_timeout_seconds)
                    )
                seen.append(event.type)
                if event.type == OUTPUT_AUDIO_DELTA and event.audio_delta_b64:
                    try:
                        audio_chunks.append(base64.b64decode(event.audio_delta_b64, validate=True))
                    except (ValueError, TypeError) as exc:
                        tx.fail("invalid_audio_delta")
                        raise WalkietalkError(
                            "Grok realtime returned invalid audio delta during ack; "
                            "no tts fallback"
                        ) from exc
                _in_update, out_update = _transcript_from_event(event)
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
                        "Grok realtime ack timed out waiting for response.done; no tts fallback"
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
        input_transcript="",
        output_transcript=(output_final or "".join(output_parts)).strip(),
        event_types=tuple(seen),
        ptt_actions=tuple(tx.actions),
        truncated_by_tx_cap=tx.truncated,
    )


def speak_text_via_realtime(
    config: Config,
    text: str,
    ptt: object,
    *,
    allow_key: bool,
    transport: RealtimeTransport | None = None,
    transport_factory: TransportFactory | None = None,
    api_key: str | None = None,
    play_segment: PlaySegment | None = None,
) -> SupervisedTxResult:
    """Sync helper: speak a fixed phrase with realtime voice and parent-owned PTT."""
    if config.agent_backend != "grok_realtime":
        raise WalkietalkError(
            "speak_text_via_realtime requires agent.backend: grok_realtime; "
            "it never falls back to tts"
        )
    client = open_voice_agent(
        config,
        transport=transport,
        transport_factory=transport_factory,
        api_key=api_key,
    )
    if client is None:
        raise WalkietalkError(
            "speak_text_via_realtime requires agent.backend: grok_realtime; "
            "it never falls back to tts"
        )
    return asyncio.run(
        run_supervised_text_speak(
            client,
            config,
            text,
            ptt,
            allow_key=allow_key,
            play_segment=play_segment,
        )
    )


async def run_committed_supervised_response(
    client: GrokRealtimeClient,
    config: Config,
    ptt: object,
    *,
    allow_key: bool,
    play_segment: PlaySegment | None = None,
    close_client: bool = False,
) -> SupervisedTxResult:
    """Commit already-appended input audio and drive parent-owned PTT playback.

    Does not upload PCM and does not send ``input_text`` / user text items.
    """
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
    try:
        await client.commit_audio()
        seen.extend(
            await wait_for_event(
                client,
                "input_audio_buffer.committed",
                timeout=max(10.0, float(config.agent_realtime_idle_timeout_seconds)),
            )
        )
        await client.create_response()
        turn_deadline = None
        try:
            async for event in response_events(client):
                if event.type == "ping":
                    seen.append(event.type)
                    if turn_deadline is not None and time.monotonic() >= turn_deadline:
                        raise WalkietalkError(
                            "Grok realtime turn timed out (pings only); no stt/agent/tts fallback"
                        )
                    continue
                if turn_deadline is None:
                    turn_deadline = time.monotonic() + max(
                        15.0, float(config.agent_realtime_idle_timeout_seconds)
                    )
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
        if close_client:
            await client.close()

    return SupervisedTxResult(
        reply_wav=pcm16_to_wav(b"".join(audio_chunks), REALTIME_PCM_RATE),
        input_transcript=input_transcript.strip(),
        output_transcript=(output_final or "".join(output_parts)).strip(),
        event_types=tuple(seen),
        ptt_actions=tuple(tx.actions),
        truncated_by_tx_cap=tx.truncated,
    )


class RealtimeTalkSession:
    """Warm realtime WS for live talk: stream capture frames, commit on accept.

    Sync façade over a background asyncio loop so ``capture`` callbacks can
    ``append`` while frames arrive. Rejected turns ``clear``; accepted turns
    ``commit`` + ``response.create`` + parent energy-gate PTT. Never sends
    ``decision.traffic`` as ``input_text``.
    """

    def __init__(
        self,
        config: Config,
        *,
        transport: RealtimeTransport | None = None,
        transport_factory: TransportFactory | None = None,
        api_key: str | None = None,
    ) -> None:
        if config.agent_backend != "grok_realtime":
            raise WalkietalkError(
                "RealtimeTalkSession requires agent.backend: grok_realtime; "
                "it never falls back to stt/agent/tts"
            )
        self.config = config
        self._transport = transport
        self._transport_factory = transport_factory
        self._api_key = api_key
        self._client: GrokRealtimeClient | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="walkietalk-realtime-talk", daemon=True
        )
        self._thread.start()
        self._warm = False
        self._instructions: str | None = None
        self._capture_rate: int | None = None
        self._pending = bytearray()
        self._appended_bytes = 0
        self._lock = threading.Lock()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, coro, *, timeout: float = 120.0):
        if not self._thread.is_alive():
            raise WalkietalkError("Realtime talk session loop is not running")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout)
        except Exception:
            fut.cancel()
            raise

    def warm(self, *, instructions: str | None = None) -> None:
        """Connect early and apply session.update; reuse across follow-up turns."""
        self._instructions = instructions
        self._call(self._async_warm(instructions=instructions))

    async def _async_warm(self, *, instructions: str | None) -> None:
        if self._client is None:
            client = open_voice_agent(
                self.config,
                transport=self._transport,
                transport_factory=self._transport_factory,
                api_key=self._api_key,
            )
            if client is None:
                raise WalkietalkError(
                    "RealtimeTalkSession requires agent.backend: grok_realtime; "
                    "it never falls back to stt/agent/tts"
                )
            self._client = client
        assert self._client is not None
        if not self._warm:
            await self._client.connect()
            await self._client.session_update(instructions=instructions)
            await wait_for_session_updated(self._client)
            self._warm = True
            self._instructions = instructions

    def begin_utterance(self, capture_rate: int) -> None:
        """Reset per-utterance streaming state before capture starts."""
        if capture_rate <= 0:
            raise WalkietalkError("Realtime talk capture rate must be positive")
        with self._lock:
            self._capture_rate = capture_rate
            self._pending.clear()
            self._appended_bytes = 0

    def on_frame(self, pcm: bytes, rate: int | None = None) -> None:
        """Append capture-rate PCM as frames arrive (resampled to session rate)."""
        if not pcm:
            return
        if rate is not None and self._capture_rate != rate:
            self.begin_utterance(rate)
        if self._capture_rate is None:
            raise WalkietalkError("RealtimeTalkSession.begin_utterance must run before on_frame")
        if not self._warm:
            self.warm(instructions=self._instructions)
        self._call(self._async_on_frame(pcm))

    async def _async_on_frame(self, pcm: bytes) -> None:
        assert self._client is not None
        rate = self._capture_rate
        if rate is None:
            raise WalkietalkError("RealtimeTalkSession.begin_utterance must run before on_frame")
        with self._lock:
            self._pending.extend(pcm)
            pending = bytes(self._pending)
            self._pending.clear()
        session_pcm = resample_pcm16(pending, rate, REALTIME_PCM_RATE)
        if not session_pcm:
            # Hold odd byte / sub-frame remainder at capture rate for next frame.
            if len(pending) % 2:
                with self._lock:
                    self._pending.extend(pending[-1:])
            return
        # Keep leftover capture sample if odd length after earlier extend.
        # resample_pcm16 consumes all even bytes; stash nothing further.
        for offset in range(0, len(session_pcm), APPEND_CHUNK_BYTES):
            chunk = session_pcm[offset : offset + APPEND_CHUNK_BYTES]
            await self._client.append_audio(chunk)
            self._appended_bytes += len(chunk)

    def on_reset(self) -> None:
        """VAD discarded a short noise burst — clear uncommitted server audio."""
        self.clear_input()

    def clear_input(self) -> None:
        """Reject path: do not commit streamed audio as a question."""
        with self._lock:
            self._pending.clear()
            had = self._appended_bytes > 0
            self._appended_bytes = 0
        if not self._warm or not had:
            return
        self._call(self._async_clear())

    async def _async_clear(self) -> None:
        assert self._client is not None
        await self._client.clear_audio()

    def commit_and_respond(
        self,
        ptt: object,
        *,
        allow_key: bool,
        play_segment: PlaySegment | None = None,
    ) -> SupervisedTxResult:
        """Accept path: flush pending audio, commit, response.create, energy-gate PTT."""
        if not self._warm:
            raise WalkietalkError("Realtime talk session is not warm")
        return self._call(
            self._async_commit_and_respond(ptt, allow_key=allow_key, play_segment=play_segment)
        )

    async def _async_commit_and_respond(
        self,
        ptt: object,
        *,
        allow_key: bool,
        play_segment: PlaySegment | None,
    ) -> SupervisedTxResult:
        assert self._client is not None
        rate = self._capture_rate
        with self._lock:
            pending = bytes(self._pending)
            self._pending.clear()
        if pending and rate is not None:
            session_pcm = resample_pcm16(pending, rate, REALTIME_PCM_RATE)
            for offset in range(0, len(session_pcm), APPEND_CHUNK_BYTES):
                chunk = session_pcm[offset : offset + APPEND_CHUNK_BYTES]
                await self._client.append_audio(chunk)
                self._appended_bytes += len(chunk)
        if self._appended_bytes <= 0:
            raise WalkietalkError(
                "Realtime talk accepted a turn with no streamed audio; "
                "no stt/agent/tts fallback"
            )
        result = await run_committed_supervised_response(
            self._client,
            self.config,
            ptt,
            allow_key=allow_key,
            play_segment=play_segment,
            close_client=False,
        )
        self._appended_bytes = 0
        return result

    @property
    def appended_bytes(self) -> int:
        return self._appended_bytes

    def close(self) -> None:
        try:
            if self._client is not None and self._warm:
                self._call(self._client.close(), timeout=30.0)
        finally:
            self._warm = False
            self._client = None
            if self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5.0)


def stream_pcm_frames_for_tests(
    session: RealtimeTalkSession,
    pcm16le: bytes,
    input_rate: int,
) -> list[int]:
    """Simulate live capture for WAV/--once: frame-sized appends in order.

    Returns the cumulative appended-byte counts after each frame flush (for tests).
    """
    from .vad import frame_samples

    session.begin_utterance(input_rate)
    width = frame_samples(input_rate) * 2
    counts: list[int] = []
    offset = 0
    while offset < len(pcm16le):
        chunk = pcm16le[offset : offset + width]
        if len(chunk) < width:
            chunk = chunk + b"\x00" * (width - len(chunk))
        session.on_frame(chunk)
        counts.append(session.appended_bytes)
        offset += width
    return counts
