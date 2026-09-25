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
                self.emit("response.done", response={"status": "completed"})

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
        (False, None, True, True),  # No gate transcript is needed on this path.
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
    if not should_reply:
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
    assert "Reconnecting voice session before the next listen." in output
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
