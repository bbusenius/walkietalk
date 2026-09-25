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
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from .audio import Wav
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
from .session import uninterrupted_cleanup

# Energy-gate: key closer to audible speech than first_output_audio_delta.
# RMS of PCM16 LE mono; silence ~0, soft noise tens, speech typically >> threshold.
PTT_ENERGY_THRESHOLD_RMS = 200.0
PTT_ENERGY_PRE_ROLL_MS = 150
PTT_ENERGY_PRE_ROLL_BYTES = int(REALTIME_PCM_RATE * PTT_ENERGY_PRE_ROLL_MS / 1000) * 2
# Noise/silent carriers can pass energy VAD without producing a transcript.
# Do not hold the receiver closed for the much longer response/tool timeout.
INPUT_TRANSCRIPT_TIMEOUT_SECONDS = 5.0
# An empty completed event can arrive a moment before the real transcript.
# Keep listening briefly so that text is not thrown away as silence.
EMPTY_TRANSCRIPT_GRACE_SECONDS = 0.4

PlaySegment = Callable[[bytes, int, float], None]


def _remembered_transcript(partials: dict[str, str], item_id: str | None) -> str:
    if item_id is None:
        return ""
    return partials.get(item_id, "")


def _finish_gate_wait(partials: dict[str, str], item_id: str | None, empty_at: float | None) -> str:
    """End a transcript wait. A seen empty completion is silence, not a timeout."""
    remembered = _remembered_transcript(partials, item_id)
    if remembered or empty_at is not None:
        return remembered
    raise RealtimeTranscriptTimeout("Realtime input transcript timed out; turn discarded")


def _gate_transcript_event(event: RealtimeEvent) -> tuple[str | None, str, bool]:
    """Return ``(item_id, stripped text, is_final)`` for an input-transcript event.

    ``is_final`` is true only for ``transcription.completed``. Empty completed
    text is ``""`` with ``is_final`` true. Other events contribute text only.
    """
    if event.type in {
        "conversation.item.input_audio_transcription.completed",
        "conversation.item.input_audio_transcription.updated",
    }:
        item_id = event.data.get("item_id")
        transcript = event.data.get("transcript")
        if not isinstance(item_id, str) or not isinstance(transcript, str):
            return None, "", False
        final = event.type.endswith(".completed")
        return item_id, transcript.strip(), final
    if event.type != "conversation.item.added":
        return None, "", False
    item = event.data.get("item")
    if not isinstance(item, dict):
        return None, "", False
    item_id = item.get("id")
    content = item.get("content")
    if not isinstance(item_id, str) or not isinstance(content, list):
        return None, "", False
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and isinstance(part.get("transcript"), str):
            parts.append(part["transcript"].strip())
    text = " ".join(part for part in parts if part)
    if not text:
        return None, "", False
    return item_id, text, False


class RealtimeCaptureError(WalkietalkError):
    """A network failure discarded the current recording; a fresh turn may retry."""


class RealtimeHardwareError(WalkietalkError):
    """PTT control failed; stop instead of treating it as a recoverable API error."""


class RealtimeTranscriptTimeout(WalkietalkError):
    """No usable transcript arrived in time; discard the turn without replying."""


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
    _pre_roll: bytearray = field(default_factory=bytearray)
    _energy_armed: bool = False
    _tx_started_at: float | None = None
    _airtime_used: float = 0.0
    _segment_bytes_left: int = 0
    _deadline: float = 0.0
    _watchdog: threading.Timer | None = None
    _watchdog_error: Exception | None = None
    _generation: int = 0
    _state_lock: threading.RLock = field(default_factory=threading.RLock)

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
            try:
                close = getattr(self.play_segment, "close", None)
                if close is not None:
                    close()
            finally:
                if self.opened:
                    with uninterrupted_cleanup():
                        try:
                            self.ptt.close()
                        except Exception as exc:
                            raise RealtimeHardwareError(f"PTT close failed: {exc}") from exc
                    self.opened = False

    def _reset_energy_gate(self) -> None:
        self._energy_armed = False
        self._pre_roll.clear()

    def prepare_playback(self) -> None:
        prepare = getattr(self.play_segment, "prepare", None)
        if prepare is not None:
            prepare()
        # Cancellation may close the turn while a worker is being prepared.
        if self.closed:
            close = getattr(self.play_segment, "close", None)
            if close is not None:
                close()

    def _on_audio_delta(self, event: RealtimeEvent) -> None:
        if not event.audio_delta_b64:
            return
        try:
            chunk = base64.b64decode(event.audio_delta_b64, validate=True)
        except (ValueError, TypeError) as exc:
            self.fail("invalid_audio_delta")
            raise WalkietalkError("Grok realtime returned invalid audio delta") from exc
        if len(chunk) % 2:
            self.fail("invalid_audio_delta")
            raise WalkietalkError("Grok realtime returned an incomplete PCM16 sample")
        if not chunk:
            return

        if not self._energy_armed:
            if pcm16_rms(chunk) < PTT_ENERGY_THRESHOLD_RMS:
                self._pre_roll.extend(chunk)
                del self._pre_roll[:-PTT_ENERGY_PRE_ROLL_BYTES]
                return
            # Bound only the preceding quiet audio, never the triggering speech.
            chunk = bytes(self._pre_roll) + chunk
            self._pre_roll.clear()
            if self.allow_key:
                if self._remaining_airtime() <= self.config.settle_seconds:
                    self.truncated = True
                    return
                self.prepare_playback()  # PTT is off during device/worker setup.
                if self.closed:
                    close = getattr(self.play_segment, "close", None)
                    if close is not None:
                        close()
                    return
                self._key("audible_audio_energy")
                self._segment_bytes_left = (
                    int(
                        max(0, self._remaining_airtime() - self.config.settle_seconds)
                        * REALTIME_PCM_RATE
                    )
                    * 2
                )
                if self.config.settle_seconds:
                    time.sleep(self.config.settle_seconds)
            self._energy_armed = True
        if not self.allow_key or self.truncated or self.closed:
            return
        keep = min(len(chunk), self._segment_bytes_left)
        keep -= keep % 2
        self._segment_bytes_left -= keep
        if keep and self.play_segment is not None:
            self.play_segment(chunk[:keep], REALTIME_PCM_RATE, self._deadline)
        if keep < len(chunk):
            self.truncated = True
            self._finish_segment("tx_cap")

    def _remaining_airtime(self) -> float:
        remaining = self.config.max_tx_seconds - self._airtime_used
        if self.keyed and self._tx_started_at is not None:
            remaining -= max(0, self.clock() - self._tx_started_at)
        return max(0, remaining)

    def _ensure_open(self) -> None:
        if not self.opened:
            try:
                self.ptt.open()
            except Exception as exc:
                raise RealtimeHardwareError(f"PTT open failed: {exc}") from exc
            self.opened = True

    def _key(self, reason: str) -> None:
        with self._state_lock:
            if self.keyed or not self.allow_key or self.closed:
                return
            self._ensure_open()
            # Mark before on(): even a partially failed assertion needs release.
            self.keyed = True
            self._tx_started_at = self.clock()
            self._deadline = self.clock() + self._remaining_airtime()
            self._generation += 1
            self._watchdog = threading.Timer(
                self._remaining_airtime(), self._expire, args=(self._generation,)
            )
            self._watchdog.daemon = True
            self._watchdog.start()
            try:
                self.ptt.on()
            except Exception as exc:
                raise RealtimeHardwareError(f"PTT assertion failed: {exc}") from exc
            self.actions.append(PttAction("key", reason))

    def _expire(self, generation: int) -> None:
        # Independent of network events, the asyncio loop, and blocking audio I/O.
        with self._state_lock:
            if not self.keyed or generation != self._generation:
                return
            self.truncated = True
            try:
                self._unkey("tx_cap")
            except Exception as exc:
                self._watchdog_error = exc
        close = getattr(self.play_segment, "close", None)
        if close is not None:
            close()

    def _unkey(self, reason: str) -> None:
        with self._state_lock:
            if not self.keyed:
                return
            if self._watchdog is not None:
                self._watchdog.cancel()
                self._watchdog = None
            if self._tx_started_at is not None:
                self._airtime_used += max(0.0, self.clock() - self._tx_started_at)
            self._tx_started_at = None
            with uninterrupted_cleanup():
                try:
                    self.ptt.off()
                except Exception as exc:
                    raise RealtimeHardwareError(f"PTT release failed: {exc}") from exc
            self.keyed = False
            self.actions.append(PttAction("unkey", reason))

    def _finish_segment(self, reason: str, *, play: bool = True) -> None:
        self._reset_energy_gate()
        try:
            finish = getattr(self.play_segment, "finish", None)
            if play and self.keyed and finish is not None:
                finish(self._deadline)
        finally:
            try:
                if self.keyed:
                    self._unkey(reason)
            finally:
                close = getattr(self.play_segment, "close", None)
                if close is not None:
                    close()
        if self._watchdog_error is not None:
            raise self._watchdog_error


def play_segment_dry(pcm: bytes, rate: int, deadline: float) -> None:
    """Dry drain without opening audio devices (tests / DryPTT)."""
    del deadline  # Deadline enforced by the parent; dry path does not block on audio.
    if not pcm or rate <= 0:
        return


async def _receive_supervised_response(
    client: GrokRealtimeClient,
    config: Config,
    ptt: object,
    *,
    allow_key: bool,
    play_segment: PlaySegment | None,
    seen: list[str] | None = None,
) -> SupervisedTxResult:
    """One response lifecycle, shared by audio turns, text turns, and fixed phrases."""
    tx = SupervisedRealtimeTx(
        config,
        ptt,
        allow_key=allow_key,
        play_segment=(play_segment or play_segment_dry) if allow_key else None,
    )
    chunks: list[bytes] = []
    input_transcript = ""
    output_parts: list[str] = []
    output_final = ""
    seen = list(seen or [])
    events = response_events(client)
    deadline = time.monotonic() + max(15.0, config.agent_realtime_idle_timeout_seconds)
    completed = False
    try:
        # Worker preparation/drain must not block cancellation on the event loop.
        await asyncio.to_thread(tx.prepare_playback)
        while True:
            timeout = deadline - time.monotonic()
            if tx.keyed:
                timeout = min(timeout, tx._remaining_airtime())
            if tx.truncated:
                break
            try:
                event = await asyncio.wait_for(anext(events), timeout=max(0, timeout))
            except TimeoutError as exc:
                if tx.keyed or tx.truncated:
                    tx.truncated = True
                    tx.fail("tx_cap")
                    break
                raise WalkietalkError("Grok realtime response timed out") from exc
            except StopAsyncIteration as exc:
                raise WalkietalkError("Grok realtime closed before response.done") from exc
            seen.append(event.type)
            if event.type == OUTPUT_AUDIO_DELTA and event.audio_delta_b64:
                try:
                    chunks.append(base64.b64decode(event.audio_delta_b64, validate=True))
                except (ValueError, TypeError) as exc:
                    raise WalkietalkError("Grok realtime returned invalid audio delta") from exc
            in_update, out_update = _transcript_from_event(event)
            if in_update is not None:
                input_transcript = in_update
            if out_update is not None:
                if event.type.endswith(".delta"):
                    output_parts.append(out_update)
                else:
                    output_final = out_update
            try:
                await asyncio.to_thread(tx.handle, event)
            except RealtimeHardwareError:
                raise
            except (WalkietalkError, OSError):
                if not tx.truncated:
                    raise
            if event.type == RESPONSE_DONE:
                status = event.data.get("response", {}).get("status", "completed")
                if status != "completed":
                    raise WalkietalkError(f"Grok realtime response ended with status {status}")
                completed = True
                break
            if tx.truncated:
                break
    finally:
        try:
            tx.close()  # Release radio before any network cleanup.
        finally:
            await events.aclose()
            if not completed or tx.truncated:
                # Discard an interrupted session rather than retain speech the
                # listener never heard. A subsequent turn starts fresh.
                try:
                    await asyncio.wait_for(client.cancel_response(), timeout=1)
                except Exception:
                    pass
                await client.close()
    if tx._watchdog_error is not None:
        raise tx._watchdog_error
    return SupervisedTxResult(
        reply_wav=pcm16_to_wav(b"".join(chunks), REALTIME_PCM_RATE),
        input_transcript=input_transcript.strip(),
        output_transcript=(output_final or "".join(output_parts)).strip(),
        event_types=tuple(seen),
        ptt_actions=tuple(tx.actions),
        truncated_by_tx_cap=tx.truncated,
    )


async def run_committed_supervised_response(
    client: GrokRealtimeClient,
    config: Config,
    ptt: object,
    *,
    allow_key: bool,
    play_segment: PlaySegment | None = None,
    close_client: bool = False,
    input_committed: bool = False,
) -> SupervisedTxResult:
    """Finish streamed input and request speech; never send a text substitute."""
    try:
        seen = []
        if not input_committed:
            await client.commit_audio()
            seen = await wait_for_event(
                client,
                "input_audio_buffer.committed",
                timeout=config.agent_realtime_idle_timeout_seconds,
            )
        await client.create_response()
        return await _receive_supervised_response(
            client,
            config,
            ptt,
            allow_key=allow_key,
            play_segment=play_segment,
            seen=seen,
        )
    finally:
        if close_client:
            await client.close()


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
    await client.connect()
    try:
        await client.session_update(instructions=instructions)
        await wait_for_session_updated(client)
        pcm = resample_pcm16(pcm16le, input_rate, REALTIME_PCM_RATE)
        if not pcm:
            raise WalkietalkError("Voice agent input audio is empty after resampling")
        for offset in range(0, len(pcm), APPEND_CHUNK_BYTES):
            await client.append_audio(pcm[offset : offset + APPEND_CHUNK_BYTES])
        return await run_committed_supervised_response(
            client,
            config,
            ptt,
            allow_key=allow_key,
            play_segment=play_segment,
        )
    finally:
        await client.close()


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
    if not isinstance(traffic, str) or not traffic.strip():
        raise WalkietalkError("supervised text turn requires non-empty traffic text")
    await client.connect()
    try:
        await client.session_update(instructions=instructions)
        await wait_for_session_updated(client)
        await client.create_user_text_message(traffic.strip())
        await client.create_response()
        result = await _receive_supervised_response(
            client,
            config,
            ptt,
            allow_key=allow_key,
            play_segment=play_segment,
        )
        return replace(result, input_transcript=traffic.strip())
    finally:
        await client.close()


async def run_supervised_text_speak(
    client: GrokRealtimeClient,
    config: Config,
    text: str,
    ptt: object,
    *,
    allow_key: bool,
    play_segment: PlaySegment | None = None,
) -> SupervisedTxResult:
    await client.connect()
    try:
        await client.session_update(include_web_search=False)
        await wait_for_session_updated(client)
        await client.create_force_message(text)
        return await _receive_supervised_response(
            client,
            config,
            ptt,
            allow_key=allow_key,
            play_segment=play_segment,
        )
    finally:
        await client.close()


def _client_for_check(config, *, transport=None, transport_factory=None, api_key=None):
    client = open_voice_agent(
        config,
        transport=transport,
        transport_factory=transport_factory,
        api_key=api_key,
    )
    if client is None:
        raise WalkietalkError("Voice check requires agent.backend: grok_realtime")
    return client


def supervised_voice_check(
    config: Config,
    pcm16le: bytes,
    input_rate: int,
    ptt: object,
    *,
    allow_key: bool,
    transport=None,
    transport_factory=None,
    api_key=None,
    instructions=None,
    play_segment=None,
) -> SupervisedTxResult:
    client = _client_for_check(
        config,
        transport=transport,
        transport_factory=transport_factory,
        api_key=api_key,
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


def supervised_text_turn(
    config: Config,
    traffic: str,
    ptt: object,
    *,
    allow_key: bool,
    transport=None,
    transport_factory=None,
    api_key=None,
    instructions=None,
    play_segment=None,
) -> SupervisedTxResult:
    client = _client_for_check(
        config,
        transport=transport,
        transport_factory=transport_factory,
        api_key=api_key,
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


def speak_text_via_realtime(
    config: Config,
    text: str,
    ptt: object,
    *,
    allow_key: bool,
    transport=None,
    transport_factory=None,
    api_key=None,
    play_segment=None,
) -> SupervisedTxResult:
    client = _client_for_check(
        config,
        transport=transport,
        transport_factory=transport_factory,
        api_key=api_key,
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
        self._input_committed = False
        self._committed_item_id: str | None = None

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, coro, *, timeout: float = 120.0):
        if not self._thread.is_alive():
            raise WalkietalkError("Realtime talk session loop is not running")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout)
        except BaseException:
            fut.cancel()
            raise

    def warm(self, *, instructions: str | None = None) -> None:
        """Connect early and apply session.update; reuse across follow-up turns."""
        self._instructions = instructions
        try:
            self._call(self._async_warm(instructions=instructions))
        except BaseException:
            self.reset()
            raise

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
            terms = (self.config.wake_primary, *self.config.wake_aliases)
            await self._client.session_update(
                instructions=instructions, transcription_keyterms=terms
            )
            await wait_for_session_updated(self._client)
            self._warm = True
            self._instructions = instructions

    def ensure_warm(self) -> None:
        """Connect before capture so the handshake is not inside the audio read."""
        if self._warm:
            return
        self.warm(instructions=self._instructions)

    @property
    def warm_connected(self) -> bool:
        return self._warm

    def discard_turn(self) -> None:
        """Delete a rejected empty turn and drop the socket that produced it.

        Reusing that socket left later wake audio untranscribed. The next
        capture reconnects before it reads from the device.
        """
        try:
            self.clear_input()
        finally:
            self.reset()

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
        try:
            if not self._warm:
                self.warm(instructions=self._instructions)
            self._call(self._async_on_frame(pcm))
        except (WalkietalkError, OSError) as exc:
            self.reset()
            raise RealtimeCaptureError(str(exc)) from exc

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
        try:
            self.clear_input()
        except (WalkietalkError, OSError) as exc:
            raise RealtimeCaptureError(str(exc)) from exc

    def clear_input(self) -> None:
        """Reject path: do not commit streamed audio as a question."""
        with self._lock:
            self._pending.clear()
            had = self._appended_bytes > 0
            self._appended_bytes = 0
        if not self._warm or not (had or self._input_committed):
            return
        try:
            self._call(self._async_clear())
        except BaseException:
            self.reset()
            raise

    async def _async_clear(self) -> None:
        assert self._client is not None
        if self._input_committed:
            assert self._committed_item_id is not None
            await self._client.delete_item(self._committed_item_id)
            await wait_for_event(
                self._client,
                "conversation.item.deleted",
                timeout=self.config.agent_realtime_idle_timeout_seconds,
            )
        else:
            await self._client.clear_audio()
        self._input_committed = False
        self._committed_item_id = None

    def gate_transcript(self) -> str:
        """Use this voice session's transcript for deterministic wake/control gates.

        Committing audio does not request a response in manual-turn mode. Rejected
        or control-only items are deleted by clear_input before any response.create.
        """
        try:
            return self._call(self._async_gate_transcript())
        except BaseException:
            self.reset()
            raise

    async def _async_gate_transcript(self) -> str:
        if self._client is None or not self._warm or self._appended_bytes <= 0:
            raise WalkietalkError("Realtime gate has no captured audio")
        if self._input_committed:
            raise WalkietalkError("Realtime input was already committed")
        # Text seen on .updated or conversation.item.added before the final event.
        partials: dict[str, str] = {}
        events = self._client.events()
        try:
            timeout = min(
                INPUT_TRANSCRIPT_TIMEOUT_SECONDS, self.config.agent_realtime_idle_timeout_seconds
            )
            # Absolute deadline so pings cannot extend the wait, and so an empty
            # completed event can still notice a transcript queued just after it.
            deadline = time.monotonic() + timeout
            empty_at: float | None = None
            try:
                await asyncio.wait_for(
                    self._client.commit_audio(), timeout=max(0.0, deadline - time.monotonic())
                )
            except TimeoutError as exc:
                raise RealtimeTranscriptTimeout(
                    "Realtime input transcript timed out; turn discarded"
                ) from exc
            while True:
                now = time.monotonic()
                if now >= deadline:
                    return _finish_gate_wait(partials, self._committed_item_id, empty_at)
                wait = deadline - now
                if empty_at is not None:
                    grace_left = EMPTY_TRANSCRIPT_GRACE_SECONDS - (now - empty_at)
                    if grace_left <= 0:
                        return _remembered_transcript(partials, self._committed_item_id)
                    wait = min(wait, grace_left)
                try:
                    event = await asyncio.wait_for(anext(events), timeout=wait)
                except TimeoutError:
                    return _finish_gate_wait(partials, self._committed_item_id, empty_at)
                except StopAsyncIteration as exc:
                    remembered = _remembered_transcript(partials, self._committed_item_id)
                    if remembered or empty_at is not None:
                        return remembered
                    raise WalkietalkError(
                        "Realtime input transcript unavailable; turn discarded"
                    ) from exc
                if event.type == "input_audio_buffer.committed":
                    item_id = event.data.get("item_id")
                    if not isinstance(item_id, str) or not item_id:
                        raise WalkietalkError("Realtime commit acknowledgement lacks item_id")
                    self._committed_item_id = item_id
                    self._input_committed = True
                    continue
                if event.type == "conversation.item.input_audio_transcription.failed":
                    raise WalkietalkError("Realtime input transcript failed; turn discarded")
                item_id, transcript, final = _gate_transcript_event(event)
                if item_id is not None and transcript:
                    partials[item_id] = transcript
                if not (
                    final
                    and self._input_committed
                    and item_id is not None
                    and item_id == self._committed_item_id
                ):
                    continue
                if transcript:
                    return transcript
                if item_id in partials:
                    return partials[item_id]
                empty_at = time.monotonic()
        finally:
            await events.aclose()

    def reset(self) -> None:
        """Discard a failed/partial remote conversation; reconnect on the next capture."""
        self._call(self._async_reset(), timeout=10)

    async def _async_reset(self) -> None:
        try:
            if self._client is not None:
                await self._client.close()
        finally:
            self._client = None
            self._warm = False
            self._input_committed = False
            self._committed_item_id = None
            self._appended_bytes = 0
            self._pending.clear()

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
                "Realtime talk accepted a turn with no streamed audio; no stt/agent/tts fallback"
            )
        try:
            result = await run_committed_supervised_response(
                self._client,
                self.config,
                ptt,
                allow_key=allow_key,
                play_segment=play_segment,
                close_client=False,
                input_committed=self._input_committed,
            )
        except BaseException:
            await self._async_reset()
            raise
        if result.truncated_by_tx_cap:
            await self._async_reset()
        self._input_committed = False
        self._committed_item_id = None
        self._appended_bytes = 0
        return result

    @property
    def appended_bytes(self) -> int:
        return self._appended_bytes

    async def _async_shutdown(self) -> None:
        # Cancellation must run each turn's finally block (PTT/audio cleanup)
        # before stopping its event loop, including Ctrl+C in Future.result().
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._async_reset()
        await self._loop.shutdown_asyncgens()

    def close(self) -> None:
        if not self._thread.is_alive():
            return
        try:
            self._call(self._async_shutdown(), timeout=10.0)
        finally:
            self._warm = False
            self._client = None
            if self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5.0)
            if not self._thread.is_alive():
                self._loop.close()


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
