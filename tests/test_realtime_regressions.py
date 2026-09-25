"""Observable regressions from PR #16; no xAI requests or radio hardware."""

import argparse
import asyncio
import base64
import json
import threading
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from walkietalk import cli, voice_agent_tx
from walkietalk.callsign import CallsignSession
from walkietalk.config import Config, WalkietalkError, load_config
from walkietalk.grok_realtime import GrokRealtimeClient, RealtimeEvent
from walkietalk.voice_agent_tx import (
    PttAction,
    RealtimeCaptureError,
    RealtimeTalkSession,
    RealtimeTranscriptTimeout,
    SupervisedRealtimeTx,
    SupervisedTxResult,
    run_committed_supervised_response,
)
from walkietalk.wake import ListeningSession

PCM = b"\x88\x13" * 2400


def config(**kwargs):
    return replace(
        Config(),
        agent_backend="grok_realtime",
        settle_seconds=0,
        agent_realtime_idle_timeout_seconds=0.2,
        **kwargs,
    )


def delta(pcm=PCM):
    encoded = base64.b64encode(pcm).decode()
    return RealtimeEvent(
        "response.output_audio.delta",
        {"type": "response.output_audio.delta", "delta": encoded},
        audio_delta_b64=encoded,
    )


class PTT:
    def __init__(self, *args):
        self.keyed = False
        self.released = threading.Event()
        self.actions = []

    def open(self):
        self.actions.append("open")

    def on(self):
        self.keyed = True
        self.actions.append("on")

    def off(self):
        self.keyed = False
        self.actions.append("off")
        self.released.set()

    def close(self):
        self.actions.append("close")


class Voice:
    """Respond only to client commands, correlating transcripts by item ID."""

    def __init__(self, transcripts=("charlotte hello",), reply=PCM, stall=False):
        self.transcripts = deque(transcripts)
        self.reply = reply
        self.stall = stall
        self.incoming = asyncio.Queue()
        self.sent = []
        self.buffer = bytearray()
        self.commits = []
        self.closed = False
        self.fail_append = False

    def emit(self, type_, **data):
        self.incoming.put_nowait(json.dumps({"type": type_, **data}))

    async def send(self, raw):
        if self.closed:
            raise OSError("connection lost")
        msg = json.loads(raw)
        self.sent.append(msg)
        kind = msg["type"]
        if kind == "session.update":
            self.emit("session.updated")
        elif kind == "input_audio_buffer.append":
            if self.fail_append:
                raise OSError("connection lost during upload")
            self.buffer.extend(base64.b64decode(msg["audio"]))
        elif kind == "input_audio_buffer.commit":
            self.commits.append(bytes(self.buffer))
            self.buffer.clear()
            item = f"user-{len(self.commits)}"
            self.emit("input_audio_buffer.committed", item_id=item)
            if self.transcripts:
                text = self.transcripts.popleft()
                if text is not None:
                    self.emit_transcript(item, text)

        elif kind == "conversation.item.delete":
            self.emit("conversation.item.deleted", item_id=msg["item_id"])
        elif kind == "input_audio_buffer.clear":
            self.buffer.clear()
            self.emit("input_audio_buffer.cleared")
        elif kind == "response.create":
            self.emit("response.created", response={"id": "reply"})
            self.emit("response.output_audio.delta", delta=base64.b64encode(self.reply).decode())
            if not self.stall:
                self.emit("response.output_audio.done")
                self.emit(
                    "response.done",
                    response={
                        "status": "completed",
                        "output": [{"id": f"assistant-{len(self.commits)}", "role": "assistant"}],
                    },
                )

    def emit_transcript(self, item: str, text: str) -> None:
        self.emit(
            "conversation.item.input_audio_transcription.completed",
            item_id=item,
            transcript=text,
        )

    async def recv(self):
        return await self.incoming.get()

    async def close(self):
        self.closed = True

    def types(self):
        return [e["type"] for e in self.sent]


def test_first_chunk_is_played_immediately_and_in_full():
    ptt = PTT()
    played = []
    tx = SupervisedRealtimeTx(config(), ptt, True, play_segment=lambda pcm, *_: played.append(pcm))
    try:
        tx.handle(delta(b"\0\0" * 12000))  # quiet pre-roll is bounded
        speech = PCM * 5  # 500 ms, larger than the old 150 ms clipping threshold
        tx.handle(delta(speech))
        assert played == [b"\0\0" * 3600 + speech]
        assert ptt.keyed
    finally:
        tx.close()


def test_watchdog_releases_radio_while_playback_blocks():
    ptt = PTT()
    entered = threading.Event()
    unblock = threading.Event()

    def play(*_):
        entered.set()
        assert unblock.wait(2)

    tx = SupervisedRealtimeTx(config(max_tx_seconds=0.05), ptt, True, play_segment=play)
    thread = threading.Thread(target=lambda: tx.handle(delta(PCM[:200])))
    thread.start()
    try:
        assert entered.wait(1)
        assert ptt.released.wait(0.5)
        assert thread.is_alive()  # Audio remains blocked; release was independent.
        assert not ptt.keyed
    finally:
        unblock.set()
        thread.join(1)
        tx.close()


def test_network_stall_unkeys_at_tx_cap_and_cancels():
    async def run():
        cfg = config(max_tx_seconds=0.05)
        remote = Voice(reply=PCM[:200], stall=True)
        client = GrokRealtimeClient(cfg, transport=remote, api_key="fake")
        await client.connect()
        ptt = PTT()
        result = await asyncio.wait_for(
            run_committed_supervised_response(client, cfg, ptt, allow_key=True), 1
        )
        assert result.truncated_by_tx_cap
        assert not ptt.keyed
        assert remote.closed
        assert "response.cancel" in remote.types()

    asyncio.run(run())


@pytest.mark.parametrize("reject", [False, True])
def test_native_gate_never_substitutes_text_or_requests_rejected_reply(reject):
    remote = Voice(transcripts=("not addressed" if reject else "charlotte hello",))
    session = RealtimeTalkSession(config(), transport=remote, api_key="fake")
    try:
        session.warm()
        session.on_frame(PCM, 24000)
        assert session.gate_transcript() == ("not addressed" if reject else "charlotte hello")
        assert "response.create" not in remote.types()
        if reject:
            session.clear_input()
            assert "conversation.item.delete" in remote.types()
            assert "response.create" not in remote.types()
        else:
            session.commit_and_respond(PTT(), allow_key=True)
            assert remote.types().count("input_audio_buffer.commit") == 1
            assert remote.types().count("response.create") == 1
        assert "conversation.item.create" not in remote.types()
    finally:
        session.close()


@pytest.mark.parametrize("failure", ["gate", "append", "cap"])
def test_failed_or_truncated_turn_reconnects_with_clean_input(failure):
    cfg = config(max_tx_seconds=0.05 if failure == "cap" else 10)
    first = Voice(transcripts=(None,), reply=PCM * 4)
    second = Voice()
    remotes = deque([first, second])

    async def factory(*_):
        return remotes.popleft()

    session = RealtimeTalkSession(cfg, transport_factory=factory, api_key="fake")
    try:
        session.warm()
        if failure == "append":
            first.fail_append = True
            with pytest.raises(RealtimeCaptureError):
                session.on_frame(PCM, 24000)
        else:
            session.on_frame(PCM, 24000)
            if failure == "gate":
                with pytest.raises(WalkietalkError, match="transcript timed out"):
                    session.gate_transcript()
            else:
                assert session.commit_and_respond(PTT(), allow_key=True).truncated_by_tx_cap
        assert first.closed
        session.on_frame(PCM[:480], 24000)
        assert session.gate_transcript() == "charlotte hello"
        assert second.commits == [PCM[:480]]
    finally:
        session.close()


@pytest.mark.parametrize(
    "shutdown_enabled, transcript, followup, should_reply",
    [
        (False, "charlotte hello", False, True),
        (False, "not addressed", False, False),
        (False, None, True, False),
        (False, "", True, False),
        (False, "charlotte", True, False),
        (True, "charlotte", True, False),
        (False, "what about tomorrow", True, True),
        (True, "bird seven", True, False),
        (True, "bird", True, False),
        (True, "what about tomorrow", True, True),
        (True, "", True, False),
        (False, "", False, False),
    ],
)
def test_cli_uses_native_controls_without_opening_stt(
    monkeypatch, shutdown_enabled, transcript, followup, should_reply
):
    cfg = config(
        listening_mode="conversation",
        shutdown_enabled=shutdown_enabled,
        shutdown_phrase="bird",
        shutdown_code="seven",
    )
    remote = Voice(transcripts=(transcript,))
    session = RealtimeTalkSession(cfg, transport=remote, api_key="fake")
    gate = ListeningSession(cfg)
    if followup:
        gate.complete_turn()
    monkeypatch.setattr(cli, "load_config", lambda _: cfg)
    monkeypatch.setattr(cli, "RealtimeTalkSession", lambda _: session)
    monkeypatch.setattr(cli, "ListeningSession", lambda _: gate)
    for name in ("open_stt", "open_agent", "open_tts"):
        monkeypatch.setattr(cli, name, lambda *_: pytest.fail("separate backend opened"))
    monkeypatch.setattr(cli, "preflight", lambda *_, **__: None)

    def capture(*_, on_frame, **__):
        on_frame(PCM, 24000)
        return SimpleNamespace(pcm=PCM, rate=24000, started_at=time.monotonic())

    monkeypatch.setattr(cli, "capture_from_device", capture)
    assert cli.main(["-c", "/tmp/fake.yaml", "--no-env-file", "talk", "--capture", "--once"]) == 0
    assert ("response.create" in remote.types()) == should_reply
    if not should_reply and transcript is not None:
        assert "conversation.item.delete" in remote.types()


@pytest.mark.parametrize("mode, count", [("end_of_reply", 2), ("interval", 1), ("off", 0)])
def test_realtime_station_id_policy(monkeypatch, mode, count):
    cfg = config(callsign="TEST123", callsign_mode=mode)
    callsigns = CallsignSession(cfg, time.monotonic)
    result = SupervisedTxResult(
        None, output_transcript="reply", ptt_actions=(PttAction("key", "test"),)
    )
    talk = SimpleNamespace(commit_and_respond=lambda *_, **__: result)
    spoken = []

    def identify(config, text, *_, **__):
        spoken.append(text)
        return result

    monkeypatch.setattr(cli, "SerialPTT", PTT)
    monkeypatch.setattr(cli, "StreamingPlayback", lambda _: lambda *_: None)
    monkeypatch.setattr(cli, "speak_text_via_realtime", identify)
    monkeypatch.setattr(cli, "IDENT_GAP_SECONDS", 0)
    args = argparse.Namespace(transmit=True, once=True, wav=None)
    for _ in range(2):
        assert cli._realtime_commit_reply(
            talk_realtime=talk,
            config=cfg,
            args=args,
            session=ListeningSession(cfg),
            callsigns=callsigns,
        )
    assert spoken == ["TEST123"] * count


def test_old_config_loads_and_partial_realtime_overrides(tmp_path):
    import yaml

    data = yaml.safe_load(Path("config.example.yaml").read_text())
    del data["agent"]["realtime"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    assert load_config(path).agent_realtime_voice == "eve"
    data["agent"]["realtime"] = {"voice": "ara"}
    path.write_text(yaml.safe_dump(data))
    cfg = load_config(path)
    assert cfg.agent_realtime_voice == "ara"
    assert cfg.agent_realtime_model == "grok-voice-latest"


def test_cancellation_during_drain_releases_ptt_without_waiting_for_cap():
    async def run():
        remote = Voice()
        cfg = config(max_tx_seconds=10)
        client = GrokRealtimeClient(cfg, transport=remote, api_key="fake")
        await client.connect()
        entered = threading.Event()
        drained = threading.Event()
        ptt = PTT()

        class Sink:
            def __call__(self, *_):
                pass

            def finish(self, _):
                entered.set()
                assert drained.wait(2)

            def close(self):
                drained.set()

        task = asyncio.create_task(
            run_committed_supervised_response(
                client,
                cfg,
                ptt,
                allow_key=True,
                play_segment=Sink(),
            )
        )
        assert await asyncio.to_thread(entered.wait, 1)
        assert ptt.keyed
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert not ptt.keyed
        assert remote.closed

    asyncio.run(run())


def test_ptt_failure_attempts_release_and_stops_continuous_cli(monkeypatch):
    from walkietalk.voice_agent_tx import RealtimeHardwareError

    class BadPTT(PTT):
        def on(self):
            super().on()
            raise OSError("partial assertion failure")

    async def run():
        remote = Voice()
        cfg = config()
        client = GrokRealtimeClient(cfg, transport=remote, api_key="fake")
        await client.connect()
        ptt = BadPTT()
        with pytest.raises(RealtimeHardwareError, match="assertion"):
            await run_committed_supervised_response(client, cfg, ptt, allow_key=True)
        assert "off" in ptt.actions
        assert "close" in ptt.actions
        assert not ptt.keyed

    asyncio.run(run())

    def failed(*_, **__):
        raise RealtimeHardwareError("release failed")

    with pytest.raises(RealtimeHardwareError):
        cli._realtime_commit_reply(
            talk_realtime=SimpleNamespace(commit_and_respond=failed),
            config=config(),
            args=argparse.Namespace(transmit=False, once=False, wav=None),
            session=ListeningSession(config()),
        )


def test_audio_cleanup_failure_still_closes_ptt():
    ptt = PTT()

    class Sink:
        def __call__(self, *_):
            pass

        def close(self):
            raise OSError("audio close failed")

    tx = SupervisedRealtimeTx(config(), ptt, True, play_segment=Sink())
    tx.handle(delta())
    with pytest.raises(OSError, match="audio close"):
        tx.close()
    assert not ptt.keyed
    assert ptt.actions[-2:] == ["off", "close"]


@pytest.mark.parametrize("server_behavior", ["silent", "pings", "blocked_commit"])
def test_missing_transcript_has_its_own_deadline_and_never_replies(monkeypatch, server_behavior):
    monkeypatch.setattr(voice_agent_tx, "INPUT_TRANSCRIPT_TIMEOUT_SECONDS", 0.05)

    class SilentCarrier(Voice):
        async def send(self, raw):
            if server_behavior == "blocked_commit" and json.loads(raw)["type"] == (
                "input_audio_buffer.commit"
            ):
                await asyncio.Event().wait()
            await super().send(raw)

        async def recv(self):
            if server_behavior == "pings" and self.incoming.empty():
                await asyncio.sleep(0.002)
                return json.dumps({"type": "ping"})
            return await super().recv()

    # Use the actual long session timeout, not the short fake-test timeout.
    cfg = replace(config(), agent_realtime_idle_timeout_seconds=60)
    remote = SilentCarrier(transcripts=(None,))
    session = RealtimeTalkSession(cfg, transport=remote, api_key="fake")
    try:
        session.warm()
        session.on_frame(PCM, 24000)  # Radio noise can exceed the energy threshold.
        started = time.monotonic()
        with pytest.raises(RealtimeTranscriptTimeout):
            session.gate_transcript()
        assert time.monotonic() - started < 1
        assert remote.closed
        assert session.appended_bytes == 0
        assert "response.create" not in remote.types()
    finally:
        session.close()


@pytest.mark.parametrize("followup", [False, True])
def test_cli_silent_carrier_preserves_window_and_accepts_next_turn(monkeypatch, capsys, followup):
    monkeypatch.setattr(voice_agent_tx, "INPUT_TRANSCRIPT_TIMEOUT_SECONDS", 0.05)
    cfg = replace(
        config(listening_mode="conversation", shutdown_enabled=True),
        agent_realtime_idle_timeout_seconds=60,
    )
    silence = Voice(transcripts=(None,))
    spoken = Voice(transcripts=("hello" if followup else "charlotte hello",))
    remotes = deque([silence, spoken])

    async def factory(*_):
        return remotes.popleft()

    session = RealtimeTalkSession(cfg, transport_factory=factory, api_key="fake")
    gate = ListeningSession(cfg)
    if followup:
        gate.complete_turn()
    initial_deadline = gate.awake_until
    monkeypatch.setattr(cli, "load_config", lambda _: cfg)
    monkeypatch.setattr(cli, "RealtimeTalkSession", lambda _: session)
    monkeypatch.setattr(cli, "ListeningSession", lambda _: gate)
    monkeypatch.setattr(cli, "preflight", lambda *_, **__: None)
    for name in ("open_stt", "open_agent", "open_tts"):
        monkeypatch.setattr(cli, name, lambda *_: pytest.fail("separate backend opened"))
    captures = []

    def capture(*_, on_frame, **__):
        if len(captures) == 2:
            raise KeyboardInterrupt  # Stop continuous listening after recovery.
        if len(captures) == 1:
            # Empty input neither closes nor extends the listening window.
            assert gate.awake_until == initial_deadline
        pcm = PCM if not captures else PCM[:480]
        captures.append(pcm)
        on_frame(pcm, 24000)
        return SimpleNamespace(pcm=pcm, rate=24000, started_at=time.monotonic())

    monkeypatch.setattr(cli, "capture_from_device", capture)
    # main's deliberate Ctrl+C exit status is independent of the recovery check.
    cli.main(["-c", "/tmp/fake.yaml", "--no-env-file", "talk", "--capture"])
    assert len(captures) == 2
    assert "response.create" not in silence.types()
    assert "response.create" in spoken.types()
    assert spoken.commits == [PCM[:480]]
    output = capsys.readouterr().out
    assert "Ignored (empty transcript). Window unchanged." in output
    assert "No words received" not in output


def test_empty_transcript_reconnects_before_the_next_capture(monkeypatch, capsys):
    """A blank carrier must not leave the next wake on the same socket.

    Reconnecting inside the capture callback overflows the device and drops
    the start of the wake phrase, which then looks like another empty transcript.
    """
    monkeypatch.setattr(voice_agent_tx, "EMPTY_TRANSCRIPT_GRACE_SECONDS", 0.05)
    cfg = replace(config(listening_mode="conversation"), agent_realtime_idle_timeout_seconds=60)
    silence = Voice(transcripts=("",))
    spoken = Voice(transcripts=("charlotte hello",))
    remotes = deque([silence, spoken])

    async def factory(*_):
        return remotes.popleft()

    session = RealtimeTalkSession(cfg, transport_factory=factory, api_key="fake")
    gate = ListeningSession(cfg)
    monkeypatch.setattr(cli, "load_config", lambda _: cfg)
    monkeypatch.setattr(cli, "RealtimeTalkSession", lambda _: session)
    monkeypatch.setattr(cli, "ListeningSession", lambda _: gate)
    monkeypatch.setattr(cli, "preflight", lambda *_, **__: None)
    for name in ("open_stt", "open_agent", "open_tts"):
        monkeypatch.setattr(cli, name, lambda *_: pytest.fail("separate backend opened"))
    captures = []

    def capture(*_, on_frame, **__):
        if len(captures) == 2:
            raise KeyboardInterrupt
        if len(captures) == 1:
            assert silence.closed
            assert "session.update" in spoken.types()
            assert "input_audio_buffer.append" not in spoken.types()
            session_update = next(item for item in spoken.sent if item["type"] == "session.update")
            assert (
                "charlotte"
                in session_update["session"]["audio"]["input"]["transcription"]["keyterms"]
            )
        pcm = PCM if not captures else PCM[:480]
        captures.append(pcm)
        on_frame(pcm, 24000)
        return SimpleNamespace(pcm=pcm, rate=24000, started_at=time.monotonic())

    monkeypatch.setattr(cli, "capture_from_device", capture)
    cli.main(["-c", "/tmp/fake.yaml", "--no-env-file", "talk", "--capture"])
    assert len(captures) == 2
    assert spoken.commits == [PCM[:480]]
    assert "response.create" in spoken.types()
    output = capsys.readouterr().out
    assert "Ignored (empty transcript). Window unchanged." in output
    assert "Connecting voice session before the next listen." in output
    assert "Accepted (wake name)." in output


def test_streaming_transcript_counts_when_completed_is_empty_or_missing(monkeypatch):
    monkeypatch.setattr(voice_agent_tx, "INPUT_TRANSCRIPT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(voice_agent_tx, "EMPTY_TRANSCRIPT_GRACE_SECONDS", 0.05)
    cfg = replace(config(), agent_realtime_idle_timeout_seconds=60)

    class UpdatedOnly(Voice):
        def emit_transcript(self, item: str, text: str) -> None:
            self.emit(
                "conversation.item.input_audio_transcription.updated",
                item_id=item,
                transcript=text,
            )

    class AddedThenEmpty(Voice):
        def emit_transcript(self, item: str, text: str) -> None:
            self.emit(
                "conversation.item.added",
                item={
                    "id": item,
                    "content": [{"type": "input_audio", "transcript": text}],
                },
            )
            self.emit(
                "conversation.item.input_audio_transcription.completed",
                item_id=item,
                transcript="",
            )

    updated = UpdatedOnly(transcripts=("charlotte hello",))
    session = RealtimeTalkSession(cfg, transport=updated, api_key="fake")
    try:
        session.warm()
        session.on_frame(PCM, 24000)
        assert session.gate_transcript() == "charlotte hello"
        assert not updated.closed
    finally:
        session.close()

    added = AddedThenEmpty(transcripts=("charlotte hello",))
    session = RealtimeTalkSession(cfg, transport=added, api_key="fake")
    try:
        session.warm()
        session.on_frame(PCM, 24000)
        assert session.gate_transcript() == "charlotte hello"
    finally:
        session.close()


def test_close_serializes_with_in_progress_ptt_open():
    entered, release, closed = (threading.Event() for _ in range(3))

    class SlowPTT(PTT):
        def open(self):
            entered.set()
            assert release.wait(2)
            super().open()

    ptt = SlowPTT()
    tx = SupervisedRealtimeTx(config(max_tx_seconds=10), ptt, True)
    worker = threading.Thread(target=lambda: tx.handle(delta()))

    def close():
        tx.close()
        closed.set()

    closer = threading.Thread(target=close)
    worker.start()
    try:
        assert entered.wait(1)
        closer.start()
        assert not closed.wait(0.03)  # Cleanup must wait for the in-flight open/key.
        release.set()
        closer.join(1)
        worker.join(1)
        assert closed.is_set()
        assert not ptt.keyed
        assert ptt.actions[-2:] == ["off", "close"]
        actions = ptt.actions[:]
        tx.handle(delta())
        assert ptt.actions == actions
    finally:
        release.set()
        worker.join(1)
        closer.join(1)
        tx.close()


def test_sync_deadline_waits_for_reset_before_next_capture():
    remotes = deque([Voice(), Voice()])

    async def factory(*_):
        return remotes.popleft()

    session = RealtimeTalkSession(config(), transport_factory=factory, api_key="fake")
    cleaned = threading.Event()

    async def slow_turn():
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.03)
            await session._async_reset()
            cleaned.set()

    try:
        session.warm()
        with pytest.raises(WalkietalkError, match="timed out after 0.01s"):
            session._call(slow_turn(), timeout=0.01)
        assert cleaned.is_set()
        session.ensure_warm()
        session.on_frame(PCM, 24000)
        assert session.gate_transcript() == "charlotte hello"
    finally:
        session.close()


def test_warm_reader_drains_idle_events_and_does_not_expire_idle_session():
    remote = Voice()
    session = RealtimeTalkSession(config(), transport=remote, api_key="fake")

    async def idle_events():
        for _ in range(100):
            remote.emit("ping")
        for _ in range(40):
            remote.emit("rate_limits.updated")
        await asyncio.sleep(0.25)  # Longer than the per-operation idle timeout.
        assert remote.incoming.empty()

    try:
        session.warm()
        session._call(idle_events())
        assert session.warm_connected
        session.on_frame(PCM, 24000)
        assert session.gate_transcript() == "charlotte hello"
        result = session.commit_and_respond(PTT(), allow_key=True)
        assert result.ptt_actions
    finally:
        session.close()


def test_warm_reader_overflow_discards_session_without_unbounded_queue():
    remote = Voice()
    session = RealtimeTalkSession(config(), transport=remote, api_key="fake")

    async def flood():
        for _ in range(200):
            remote.emit("rate_limits.updated")
        await asyncio.sleep(0.01)
        assert session._client._inbox.qsize() <= 128

    try:
        session.warm()
        session._call(flood())
        assert not session.warm_connected
        assert remote.closed
        assert "response.create" not in remote.types()
    finally:
        session.close()


def test_remote_history_prunes_complete_turns_including_tools():
    class ToolVoice(Voice):
        async def send(self, raw):
            if json.loads(raw)["type"] == "response.create":
                self.emit(
                    "conversation.item.added",
                    item={"id": f"tool-{len(self.commits)}", "type": "function_call"},
                )
            await super().send(raw)

    remote = ToolVoice(transcripts=("charlotte hello",) * 3)
    session = RealtimeTalkSession(config(agent_history_turns=2), transport=remote, api_key="fake")
    try:
        session.warm()
        for _ in range(3):
            session.on_frame(PCM, 24000)
            session.gate_transcript()
            session.commit_and_respond(PTT(), allow_key=True)
        deleted = [
            msg["item_id"] for msg in remote.sent if msg["type"] == "conversation.item.delete"
        ]
        assert deleted == ["user-1", "tool-1", "assistant-1"]
        assert session.warm_connected
        assert len(session._client.conversation_items) == 6
    finally:
        session.close()


@pytest.mark.parametrize("reply", [b"", b"\0\0" * 2400])
def test_silent_reply_does_not_open_followup_or_send_station_id(monkeypatch, capsys, reply):
    cfg = config(listening_mode="conversation", callsign="TEST123", callsign_mode="end_of_reply")
    remote = Voice(reply=reply)
    realtime = RealtimeTalkSession(cfg, transport=remote, api_key="fake")
    gate = ListeningSession(cfg)
    monkeypatch.setattr(cli, "load_config", lambda _: cfg)
    monkeypatch.setattr(cli, "RealtimeTalkSession", lambda _: realtime)
    monkeypatch.setattr(cli, "ListeningSession", lambda _: gate)
    monkeypatch.setattr(cli, "preflight", lambda *_, **__: None)
    monkeypatch.setattr(cli, "SerialPTT", PTT)
    monkeypatch.setattr(cli, "StreamingPlayback", lambda _: lambda *_: None)
    monkeypatch.setattr(
        cli, "speak_text_via_realtime", lambda *_, **__: pytest.fail("station ID without reply")
    )

    def capture(*_, on_frame, **__):
        on_frame(PCM, 24000)
        return SimpleNamespace(pcm=PCM, rate=24000, started_at=time.monotonic())

    monkeypatch.setattr(cli, "capture_from_device", capture)
    assert (
        cli.main(
            ["-c", "/tmp/fake.yaml", "--no-env-file", "talk", "--capture", "--once", "--transmit"]
        )
        == 1
    )
    assert gate.awake_until is None
    assert remote.closed
    assert "no audible reply" in capsys.readouterr().err


def test_check_realtime_reports_native_backend_without_opening_unused_backends(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda _: config())
    monkeypatch.setattr(cli, "preflight", lambda *_: None)
    for name in ("open_stt", "open_agent", "open_tts"):
        monkeypatch.setattr(cli, name, lambda *_: pytest.fail("unused backend opened"))
    assert cli.main(["-c", "/tmp/fake.yaml", "--no-env-file", "check"]) == 0
    assert "Voice agent: grok_realtime" in capsys.readouterr().out


@pytest.mark.parametrize("supervised", [True, False])
@pytest.mark.parametrize("ending", ["eof", "null", "failed"])
def test_response_requires_valid_successful_completion(supervised, ending):
    from walkietalk.grok_realtime import run_offline_turn

    class BrokenVoice(Voice):
        def emit(self, type_, **data):
            if type_ == "response.done":
                if ending == "eof":
                    self.incoming.put_nowait(ConnectionError("server closed"))
                    return
                data["response"] = None if ending == "null" else {"status": "failed"}
            super().emit(type_, **data)

        async def recv(self):
            item = await super().recv()
            if isinstance(item, Exception):
                raise item
            return item

    async def run():
        remote = BrokenVoice()
        client = GrokRealtimeClient(config(), transport=remote, api_key="fake")
        ptt = PTT()
        with pytest.raises(WalkietalkError):
            if supervised:
                await client.connect()
                await run_committed_supervised_response(client, config(), ptt, allow_key=True)
            else:
                await run_offline_turn(client, PCM, 24000)
        assert not ptt.keyed
        assert remote.closed

    asyncio.run(run())


def test_event_deadline_bounds_silent_transport_and_matches_delete_id():
    from walkietalk.grok_realtime import wait_for_event

    async def run():
        remote = Voice()
        client = GrokRealtimeClient(config(), transport=remote, api_key="fake")
        await client.connect()
        try:
            remote.emit("conversation.item.deleted", item_id="old")
            with pytest.raises(WalkietalkError, match="timed out waiting"):
                await asyncio.wait_for(
                    wait_for_event(
                        client, "conversation.item.deleted", timeout=0.01, item_id="new"
                    ),
                    0.1,
                )
            remote.emit("conversation.item.deleted", item_id="new")
            await wait_for_event(client, "conversation.item.deleted", timeout=0.01, item_id="new")
        finally:
            await client.close()

    asyncio.run(run())


def test_continuous_retries_connection_and_failed_rejected_item_cleanup(monkeypatch, capsys):
    class FailedDelete(Voice):
        async def send(self, raw):
            if json.loads(raw)["type"] == "conversation.item.delete":
                raise OSError("connection lost during deletion")
            await super().send(raw)

    bad = FailedDelete(transcripts=("not addressed",))
    good = Voice()
    attempts = [OSError("temporary DNS failure"), bad, good]

    async def factory(*_):
        value = attempts.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    cfg = config()
    session = RealtimeTalkSession(cfg, transport_factory=factory, api_key="fake")
    monkeypatch.setattr(cli, "load_config", lambda _: cfg)
    monkeypatch.setattr(cli, "RealtimeTalkSession", lambda _: session)
    monkeypatch.setattr(cli, "preflight", lambda *_, **__: None)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    captures = []

    def capture(*_, on_frame, **__):
        if len(captures) == 2:
            raise KeyboardInterrupt
        captures.append(1)
        on_frame(PCM, 24000)
        return SimpleNamespace(pcm=PCM, rate=24000, started_at=time.monotonic())

    monkeypatch.setattr(cli, "capture_from_device", capture)
    cli.main(["-c", "/tmp/fake.yaml", "--no-env-file", "talk", "--capture"])
    assert len(captures) == 2
    assert "response.create" not in bad.types()
    assert "response.create" in good.types()
    err = capsys.readouterr().err
    assert "temporary DNS failure" in err
    assert "connection lost during deletion" in err


def test_idle_disconnect_reconnects_before_uploading_next_wake(monkeypatch):
    class DisconnectingVoice(Voice):
        async def recv(self):
            item = await super().recv()
            if isinstance(item, Exception):
                raise item
            return item

    old, good = DisconnectingVoice(), Voice()
    remotes = deque([old, good])

    async def factory(*_):
        return remotes.popleft()

    cfg = config()
    session = RealtimeTalkSession(cfg, transport_factory=factory, api_key="fake")
    monkeypatch.setattr(cli, "load_config", lambda _: cfg)
    monkeypatch.setattr(cli, "RealtimeTalkSession", lambda _: session)
    monkeypatch.setattr(cli, "preflight", lambda *_, **__: None)
    captures = []

    async def disconnect():
        old.incoming.put_nowait(ConnectionError("idle socket expired"))
        await asyncio.sleep(0.01)

    def capture(*_, on_frame, on_wait, **__):
        captures.append(1)
        if len(captures) == 1:
            session._call(disconnect())
            on_wait()  # Must abandon idle capture, without waiting for a speech frame.
            pytest.fail("idle disconnect was not detected")
        if len(captures) == 3:
            raise KeyboardInterrupt
        assert "session.update" in good.types()
        on_frame(PCM, 24000)
        return SimpleNamespace(pcm=PCM, rate=24000, started_at=time.monotonic())

    monkeypatch.setattr(cli, "capture_from_device", capture)
    cli.main(["-c", "/tmp/fake.yaml", "--no-env-file", "talk", "--capture"])
    assert "input_audio_buffer.append" not in old.types()
    assert good.commits == [PCM]
    assert "response.create" in good.types()


def test_failed_history_pruning_resets_without_failing_delivered_reply():
    class LostDelete(Voice):
        async def send(self, raw):
            if json.loads(raw)["type"] == "conversation.item.delete":
                raise OSError("delete unavailable")
            await super().send(raw)

    remote = LostDelete(transcripts=("charlotte hello",) * 2)
    session = RealtimeTalkSession(config(agent_history_turns=1), transport=remote, api_key="fake")
    try:
        session.warm()
        for _ in range(2):
            session.on_frame(PCM, 24000)
            session.gate_transcript()
            result = session.commit_and_respond(PTT(), allow_key=True)
            assert any(action.kind == "key" for action in result.ptt_actions)
        assert not session.warm_connected
        assert remote.closed
    finally:
        session.close()


def test_continuous_authentication_failure_stops_instead_of_retrying(monkeypatch, capsys):
    from walkietalk.grok_realtime import RealtimeAuthenticationError

    cfg = config()
    monkeypatch.setattr(cli, "load_config", lambda _: cfg)
    monkeypatch.setattr(cli, "preflight", lambda *_, **__: None)

    class Unauthorized:
        warm_connected = False

        def warm(self, **_):
            raise RealtimeAuthenticationError("Grok realtime authentication failed")

        def close(self):
            pass

    monkeypatch.setattr(cli, "RealtimeTalkSession", lambda _: Unauthorized())
    monkeypatch.setattr(cli.time, "sleep", lambda _: pytest.fail("retrying invalid credentials"))
    assert cli.main(["-c", "/tmp/fake.yaml", "--no-env-file", "talk", "--capture"]) == 1
    assert "authentication failed" in capsys.readouterr().err


@pytest.mark.parametrize("custom", [False, True])
def test_talk_supplies_wake_routing_guidance_without_assigning_identity(monkeypatch, custom):
    from walkietalk.agent import render_guidance

    cfg = config(
        wake_primary="Maple",
        wake_aliases=("May pull",),
        agent_instructions=(
            "Your name is Atlas. Answer in French, using {max_words} words." if custom else ""
        ),
        shutdown_code="private-code-not-for-prompts",
    )
    remote = Voice(transcripts=("May pull who are you?",))
    session = RealtimeTalkSession(cfg, transport=remote, api_key="fake")
    monkeypatch.setattr(cli, "load_config", lambda _: cfg)
    monkeypatch.setattr(cli, "RealtimeTalkSession", lambda _: session)
    monkeypatch.setattr(cli, "preflight", lambda *_, **__: None)

    def capture(*_, on_frame, **__):
        on_frame(PCM, 24000)
        return SimpleNamespace(pcm=PCM, rate=24000, started_at=time.monotonic())

    monkeypatch.setattr(cli, "capture_from_device", capture)
    assert cli.main(["-c", "/tmp/fake.yaml", "--no-env-file", "talk", "--capture", "--once"]) == 0
    settings = next(msg["session"] for msg in remote.sent if msg["type"] == "session.update")
    prompt = settings["instructions"]
    assert prompt.startswith(render_guidance(cfg, spoken=True))
    assert json.dumps([cfg.wake_primary, *cfg.wake_aliases]) in prompt
    assert "Answer the request that follows directly" in prompt
    assert "Do not repeat, acknowledge, explain, or correct the wake phrase" in prompt
    assert "They do not define your name, identity, or persona" in prompt
    assert "interpret the request as if that routing prefix had been removed" in prompt
    assert "never infer an identity from the wake configuration" in prompt
    assert "'Who are you?'" in prompt
    assert "wake phrases address you" not in prompt
    assert "when the user explicitly asks about it" in prompt
    assert cfg.shutdown_code not in prompt
    assert settings["audio"]["input"]["transcription"]["keyterms"] == ["Maple", "May pull"]
    assert remote.commits == [PCM]  # Preserve native audio, including the spoken wake.
    assert "response.create" in remote.types()
    assert "conversation.item.create" not in remote.types()


def test_reconnect_preserves_wake_guidance_without_accumulating_it():
    first, second = Voice(), Voice()
    remotes = deque([first, second])

    async def factory(*_):
        return remotes.popleft()

    session = RealtimeTalkSession(config(), transport_factory=factory, api_key="fake")
    try:
        session.warm(instructions="Keep answers brief.")
        session.reset()
        session.ensure_warm()
        prompts = [
            next(
                msg["session"]["instructions"]
                for msg in remote.sent
                if msg["type"] == "session.update"
            )
            for remote in (first, second)
        ]
        assert prompts[0] == prompts[1]
        assert prompts[1].count("## Radio routing") == 1
    finally:
        session.close()


@pytest.mark.parametrize("transmit", [False, True])
def test_sleep_deletes_control_audio_preserves_history_and_waits_for_wake(monkeypatch, transmit):
    cfg = config(
        listening_mode="conversation",
        sleep_primary="go to sleep",
        sleep_aliases=("stop listening",),
        sleep_confirmation_phrase="Standing by.",
    )
    remote = Voice(
        transcripts=(
            "charlotte first question",
            "Charlotte, stop listening!",
            "talking to another person",
            "charlotte next question",
        )
    )
    session = RealtimeTalkSession(cfg, transport=remote, api_key="fake")
    gate = ListeningSession(cfg, clock=lambda: 100)
    monkeypatch.setattr(cli, "load_config", lambda _: cfg)
    monkeypatch.setattr(cli, "ListeningSession", lambda _: gate)
    monkeypatch.setattr(cli, "RealtimeTalkSession", lambda _: session)
    monkeypatch.setattr(cli, "preflight", lambda *_, **__: None)
    monkeypatch.setattr(cli, "SerialPTT", PTT)
    monkeypatch.setattr(cli, "StreamingPlayback", lambda _: lambda *_: None)
    acks = []

    def speak(config, text, ptt, **kwargs):
        assert gate.awake_until is None
        assert text == cfg.sleep_confirmation_phrase
        # The original session retains only the completed question/answer pair.
        assert session.warm_connected
        assert [m["item_id"] for m in remote.sent if m["type"] == "conversation.item.delete"] == [
            "user-2"
        ]
        acks.append(text)
        return SupervisedTxResult(None, ptt_actions=(PttAction("key", "audible_audio_energy"),))

    monkeypatch.setattr(cli, "speak_text_via_realtime", speak)
    captures = 0

    def capture(*_, on_frame, **__):
        nonlocal captures
        captures += 1
        if captures > 4:
            raise KeyboardInterrupt
        if captures in (3, 4):
            assert gate.awake_until is None
        on_frame(PCM, 24000)
        return SimpleNamespace(pcm=PCM, rate=24000, started_at=100)

    monkeypatch.setattr(cli, "capture_from_device", capture)
    args = ["-c", "/tmp/fake.yaml", "--no-env-file", "talk", "--capture"]
    assert cli.main(args + (["--transmit"] if transmit else [])) == 130
    assert acks == (["Standing by."] if transmit else [])
    assert remote.types().count("session.update") == 1
    assert remote.types().count("response.create") == 2
    assert [m["item_id"] for m in remote.sent if m["type"] == "conversation.item.delete"] == [
        "user-2",
        "user-3",
    ]
    settings = next(m["session"] for m in remote.sent if m["type"] == "session.update")
    terms = settings["audio"]["input"]["transcription"]["keyterms"]
    assert "go to sleep" in terms
    assert "stop listening" in terms
    assert gate.awake_until == 100 + cfg.conversation_timeout_seconds
