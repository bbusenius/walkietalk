"""Message-loss and lifecycle regressions from REVIEW-messaging.md; no real accounts."""

import io
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import yaml

from walkietalk import cli, messaging
from walkietalk.agent import validate_reply
from walkietalk.audio import Wav
from walkietalk.capture import Utterance
from walkietalk.config import Config, MessagingMode, WalkietalkError, load_config
from walkietalk.messaging import Inbound, MessageBridge, MessagePlayback
from walkietalk.wake import ListeningSession, transcription_keyterms

MODE = MessagingMode(wake="nana", to="123", sender_alias="Nana")
PCM = Wav(b"\x01\x00" * 4800, 48000, 0.1)


@pytest.mark.parametrize("service", ["signal", "whatsapp"])
@pytest.mark.parametrize(
    "body",
    [
        "Ok 🤷‍♀️ see you\u00a0at 5\u202fpm",
        "one\u2028two\u2029three\tmore\r\nwords",
        "hello\u200e there\u200f soft\u00adhyphen",
        "before\x1b[31mred\x1b[0m\x1b]52;c;data\x07after",
    ],
)
def test_incoming_unicode_is_speakable_and_terminal_safe(service, body):
    if service == "signal":
        item = messaging.parse_signal_message(
            {"sourceNumber": "123", "timestamp": 1, "dataMessage": {"message": body}}, MODE
        )
    else:
        item = messaging.parse_whatsapp_row({"msg_id": "1", "chat_jid": "123", "text": body}, MODE)
    assert item is not None
    assert validate_reply(item.text, 600)
    assert all(char.isprintable() for char in item.text)
    if "one" in body:
        assert "one two three" in item.text


def database(path):
    connection = sqlite3.connect(path / "wacli.db")
    connection.execute(
        "CREATE TABLE messages (msg_id, chat_jid, from_me, text, "
        "display_text, media_type, local_path, ts)"
    )
    return connection


def insert(db, identity, timestamp, *, media="", path="", text="hello"):
    db.execute(
        "INSERT INTO messages VALUES (?, '123', 0, ?, '', ?, ?, ?)",
        (identity, text, media, str(path), timestamp),
    )
    db.commit()


@pytest.mark.parametrize("initial", ["missing", "broken", "ready"])
def test_startup_history_is_ignored_across_sync_batches(tmp_path, initial):
    bridge = MessageBridge(Config(whatsapp=MODE))
    bridge.whatsapp_store = tmp_path
    if initial == "broken":
        (tmp_path / "wacli.db").write_bytes(b"not sqlite")
    db = database(tmp_path) if initial == "ready" else None
    if db:
        insert(db, "existing", bridge._started_at - 100)
    bridge._poll_whatsapp(prime=True)
    if db is None:
        (tmp_path / "wacli.db").unlink(missing_ok=True)
        db = database(tmp_path)
    with db:
        insert(db, "history1", bridge._started_at - 90)
        insert(db, "live1", bridge._started_at + 1)
        bridge._poll_whatsapp()
        assert bridge.peek("whatsapp").identity == "live1"
        bridge.acknowledge(bridge.peek("whatsapp"))
        insert(db, "history2", bridge._started_at - 200)
        insert(db, "live2", bridge._started_at + 2)
        bridge._poll_whatsapp()
        assert bridge.peek("whatsapp").identity == "live2"
        assert len(bridge.queues["whatsapp"]) == 1
    db.close()


def test_delayed_media_gets_download_time_from_first_observation(tmp_path, monkeypatch, capsys):
    clock = [100.0]
    monkeypatch.setattr(messaging.time, "monotonic", lambda: clock[0])
    bridge = MessageBridge(Config(whatsapp=MODE))
    bridge.whatsapp_store = tmp_path
    db = database(tmp_path)
    # Sent during this run, but first observed several minutes later.
    timestamp = bridge._started_at + 1
    insert(db, "download", timestamp, media="audio")
    insert(db, "missing", timestamp, media="audio")
    bridge._poll_whatsapp()
    assert not bridge.has("whatsapp")
    assert "whatsapp:download" not in bridge.seen
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"fake media")
    db.execute("UPDATE messages SET local_path = ? WHERE msg_id = 'download'", (str(audio),))
    db.commit()
    clock[0] += 59
    bridge._poll_whatsapp()
    assert bridge.peek("whatsapp").audio_path == audio
    clock[0] += 2
    bridge._poll_whatsapp()
    assert "whatsapp:missing" in bridge.seen
    assert "download timed out" in capsys.readouterr().err
    db.close()


def test_idle_whatsapp_poll_does_not_reparse_history(tmp_path, monkeypatch):
    bridge = MessageBridge(Config(whatsapp=MODE))
    bridge.whatsapp_store = tmp_path
    db = database(tmp_path)
    for index in range(10):
        insert(db, str(index), bridge._started_at + index + 1)
    parse = Mock(wraps=messaging.parse_whatsapp_row)
    monkeypatch.setattr(messaging, "parse_whatsapp_row", parse)
    bridge._poll_whatsapp()
    assert parse.call_count == 10
    bridge._poll_whatsapp()
    assert parse.call_count == 10
    # A newly inserted row with an older timestamp is still visited.
    insert(db, "late", bridge._started_at + 0.5)
    bridge._poll_whatsapp()
    assert parse.call_count == 11
    db.close()


@pytest.mark.parametrize("override", [False, True])
def test_signal_attachments_respect_xdg_and_explicit_directory(tmp_path, monkeypatch, override):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    mode = replace(MODE, attachments_dir=str(tmp_path / "custom") if override else "")
    item = messaging.parse_signal_message(
        {
            "sourceNumber": "123",
            "timestamp": 1,
            "dataMessage": {"attachments": [{"contentType": "audio/ogg", "id": "note"}]},
        },
        mode,
    )
    root = tmp_path / "custom" if override else tmp_path / "xdg/signal-cli/attachments"
    assert item.audio_path == root / "note"


def test_missing_signal_media_does_not_block_rpc_responses(tmp_path, monkeypatch, capsys):
    bridge = MessageBridge(Config(signal=MODE))
    clock = [100.0]
    monkeypatch.setattr(messaging.time, "monotonic", lambda: clock[0])
    path = tmp_path / "note"
    bridge._remember(Inbound("signal", "voice", audio_path=path, identity="voice"))
    pending = {"event": threading.Event(), "error": None}
    bridge._pending[7] = pending
    bridge._accept_signal_line('{"id":7,"result":{}}')
    assert pending["event"].is_set()
    path.write_bytes(b"audio")
    bridge._poll_media()
    assert bridge.peek("signal").audio_path == path
    bridge._remember(Inbound("signal", "voice", audio_path=tmp_path / "missing", identity="bad"))
    clock[0] += messaging.VOICE_WAIT_SECONDS
    bridge._poll_media()
    assert "attachments_dir" in capsys.readouterr().err
    assert not bridge._media_pending


@pytest.mark.parametrize("output", [b"\xff", b"[]", b"null", b"42", b"{}", b"bad json"])
def test_malformed_whatsapp_send_result_is_a_normal_failure(monkeypatch, output):
    monkeypatch.setattr(messaging, "run_cli", lambda *a, **kw: (0, output, b""))
    with pytest.raises(WalkietalkError, match="^Message not sent\\.$"):
        MessageBridge(Config(whatsapp=MODE)).send_text("whatsapp", "hello")


@pytest.mark.parametrize("reply", [b"\xff", b"[]", b"null", b"{}", "truncated"])
def test_malformed_signal_http_reply_is_a_normal_failure(monkeypatch, reply):
    def post(*args, **kwargs):
        if reply == "truncated":
            raise httpx.RemoteProtocolError("incomplete HTTP response")
        return httpx.Response(200, content=reply, request=httpx.Request("POST", "http://localhost"))

    monkeypatch.setattr(messaging.httpx, "post", post)
    bridge = MessageBridge(Config(signal=MODE))
    bridge._signal_base = "http://localhost"
    with pytest.raises(WalkietalkError, match="Message not sent"):
        bridge.send_text("signal", "hello")


def test_closed_signal_stdin_is_a_normal_failure():
    bridge = MessageBridge(Config(signal=MODE))
    stream = io.BytesIO()
    stream.close()
    bridge._signal_process = Mock(stdin=stream)
    bridge._signal_process.poll.return_value = None
    with pytest.raises(WalkietalkError, match="Message not sent"):
        bridge.send_text("signal", "hello")
    assert not bridge._pending


def test_signal_reader_recovers_after_non_json_line_and_reports_send_detail(tmp_path):
    script = tmp_path / "signal.py"
    script.write_text("""import json, sys
print("non-JSON startup banner", flush=True)
for line in sys.stdin:
    req = json.loads(line)
    print(json.dumps({"id": req["id"], "error": {"data": {"response": {
        "results": [{"type": "UNREGISTERED_FAILURE"}]}}}}), flush=True)
""")
    bridge = MessageBridge(Config(signal=MODE))
    process = bridge._spawn([sys.executable, str(script)], stdin=subprocess.PIPE)
    bridge._signal_process = process
    thread = threading.Thread(target=bridge._watch_signal, args=(process,), daemon=True)
    bridge._threads.append(thread)
    thread.start()
    try:
        with pytest.raises(WalkietalkError, match="That number is not a Signal account"):
            bridge.send_text("signal", "hello")
    finally:
        bridge.close()


def test_cleanup_ignores_second_interrupt_and_still_kills_child(monkeypatch):
    bridge = MessageBridge(Config())
    child = Mock(pid=101)
    child.poll.return_value = None
    calls = []

    def wait(**kwargs):
        if not calls:
            calls.append("wait")
            os.kill(os.getpid(), signal.SIGINT)
            raise subprocess.TimeoutExpired("child", 2)
        return 0

    child.wait.side_effect = wait
    bridge._processes = [child]
    sent = []
    monkeypatch.setattr(messaging, "_signal_group", lambda pid, sig: sent.append(sig))
    handler = signal.getsignal(signal.SIGINT)
    bridge.close()
    assert signal.SIGKILL in sent
    assert signal.getsignal(signal.SIGINT) == handler


def test_subprocesses_receive_only_required_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("WALKIETALK_HERMES_TOKEN", "must-not-leak")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    bridge = MessageBridge(Config(whatsapp=MODE))
    observed = []

    def run(command, **kwargs):
        observed.append(kwargs["env"])
        if command[0] == "ffmpeg":
            from walkietalk.tts import write_wav

            write_wav(Path(command[-1]), PCM)
        return 0, b'{"success":true}', b""

    monkeypatch.setattr(messaging, "run_cli", run)
    bridge.send_text("whatsapp", "hello")
    messaging.voice_wav(tmp_path / "input", 1)
    child = Mock()

    def popen(command, **kwargs):
        observed.append(kwargs["env"])
        return child

    monkeypatch.setattr(messaging.subprocess, "Popen", popen)
    bridge._spawn(["signal-cli"])
    for env in observed:
        assert env["XDG_DATA_HOME"] == str(tmp_path)
        assert not {"XAI_API_KEY", "ANTHROPIC_API_KEY", "WALKIETALK_HERMES_TOKEN"} & env.keys()
    assert len(observed) == 3


@pytest.mark.parametrize("error", ["Piper timed out", "Hermes TTS HTTP 502"])
def test_chunk_retry_reduces_work_after_timeout_or_old_service_error(error):
    progress = MessagePlayback("word " * 200)
    voice = Mock()
    voice.synthesize.side_effect = [WalkietalkError(error), PCM]
    with pytest.raises(WalkietalkError):
        progress.prepare_text(voice, "Nana", Config())
    first = voice.synthesize.call_args.args[0]
    progress.prepare_text(voice, "Nana", Config())
    second = voice.synthesize.call_args.args[0]
    assert len(second) < len(first)
    assert progress.remaining == "word " * 200


def test_chunk_sizing_learns_without_resynthesizing_each_large_chunk():
    progress = MessagePlayback("Words to say. " * 80)
    calls = []

    def synthesize(text, **kwargs):
        calls.append(text)
        samples = len(text) * 3200  # 15 characters per second
        return Wav(b"\x01\x00" * samples, 48000, samples / 48000)

    chunks = 0
    while progress.remaining:
        _, progress.remaining = progress.prepare_text(Mock(synthesize=synthesize), "Nana", Config())
        chunks += 1
    assert len(calls) <= chunks + 1


def test_contact_wake_hints_and_recognition_terms():
    config = Config(signal=replace(MODE, aliases=("nan na",)))
    clock = [0.0]
    session = ListeningSession(config, lambda: clock[0])
    session.decide("nana", 0)
    session.complete_turn()
    clock[0] = 61
    assert '"nana"' in session.expire_if_needed()
    assert '"nana / nan na"' in session.status_line()
    assert '"nana"' in session.decide("hello", 61).message
    assert {"charlotte", "nana", "nan na"} <= set(transcription_keyterms(config))


@pytest.mark.parametrize("field,value", [("wake", "charlotte go"), ("aliases", ["nan\u200bna"])])
def test_invalid_contact_phrases_rejected(tmp_path, field, value):
    data = yaml.safe_load(Path("src/walkietalk/data/config.example.yaml").read_text())
    data["messaging"]["signal"].update(wake="nana", to="123")
    data["messaging"]["signal"][field] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WalkietalkError):
        load_config(path)


def setup_talk(monkeypatch, config):
    bridge = MessageBridge(config)
    voice = Mock()
    voice.synthesize.return_value = PCM
    realtime = Mock(warm_connected=True)
    listener = Mock()
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "open_messaging", lambda config: bridge)
    monkeypatch.setattr(cli, "open_tts", lambda config: voice)
    monkeypatch.setattr(cli, "open_stt", lambda config: listener)
    monkeypatch.setattr(cli, "RealtimeTalkSession", lambda config: realtime)
    monkeypatch.setattr(cli, "preflight", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "transmit_with_callsign", Mock())
    for name in ("SerialPTT", "Playback"):
        monkeypatch.setattr(cli, name, lambda *a, **kw: pytest.fail("real hardware access"))
    return bridge, voice, realtime, listener


def test_realtime_messaging_prepares_tts_before_bridge_or_hardware(monkeypatch):
    config = Config(agent_backend="grok_realtime", signal=MODE)
    _, voice, realtime, _ = setup_talk(monkeypatch, config)
    voice.prepare.side_effect = WalkietalkError("missing TTS")
    monkeypatch.setattr(cli, "open_messaging", lambda config: pytest.fail("bridge started"))
    monkeypatch.setattr(cli, "preflight", lambda *a, **kw: pytest.fail("hardware opened"))
    assert cli.main(["-c", "unused", "talk", "--capture", "--transmit"]) == 1
    voice.prepare.assert_called_once()
    realtime.warm.assert_not_called()


def test_realtime_is_closed_when_bridge_start_fails(monkeypatch):
    config = Config(agent_backend="grok_realtime", signal=MODE)
    _, _, realtime, _ = setup_talk(monkeypatch, config)

    def fail(config):
        raise WalkietalkError("missing wacli")

    monkeypatch.setattr(cli, "open_messaging", fail)
    assert cli.main(["-c", "unused", "talk", "--capture"]) == 1
    realtime.close.assert_called_once()


@pytest.mark.parametrize("realtime", [False, True])
@pytest.mark.parametrize("wav", [False, True])
def test_send_failure_has_nonzero_exit_for_once_and_wav(monkeypatch, realtime, wav):
    config = Config(agent_backend="grok_realtime" if realtime else "stub", signal=MODE)
    bridge, _, session, listener = setup_talk(monkeypatch, config)
    bridge.send_text = Mock(side_effect=WalkietalkError("Message not sent."))
    listener.transcribe.return_value = session.gate_transcript.return_value = "nana hello"
    utterance = Utterance(b"\x00\x40" * 320, 16000, 0.24, 0.24, 0.5, "silence", started_at=1)
    monkeypatch.setattr(cli, "capture_from_device", lambda *a, **kw: utterance)
    monkeypatch.setattr(cli, "capture_from_wav", lambda *a, **kw: utterance)
    args = ["-c", "unused", "talk", *(["input.wav"] if wav else ["--capture", "--once"])]
    assert cli.main(args) == 1


def test_shutdown_confirmation_suppresses_queued_playback(monkeypatch):
    config = Config(
        signal=MODE,
        shutdown_enabled=True,
        shutdown_phrase="bridge shutdown",
        shutdown_code="alpha seven",
    )
    bridge, _, _, listener = setup_talk(monkeypatch, config)
    session = ListeningSession(config)
    session.decide("nana", time.monotonic())
    monkeypatch.setattr(cli, "ListeningSession", lambda config: session)
    listener.transcribe.side_effect = ["bridge shutdown", "alpha seven"]
    captures = []

    def capture(*a, on_wait, **kw):
        if captures:
            bridge.add(Inbound("signal", "text", text="queued", identity="1"))
            on_wait()  # Must leave room for the shutdown code.
        captures.append(1)
        return Utterance(
            b"\x00\x40" * 320, 16000, 0.24, 0.24, 0.5, "silence", started_at=time.monotonic()
        )

    monkeypatch.setattr(cli, "capture_from_device", capture)
    assert cli.main(["-c", "unused", "talk", "--capture"]) == 0
    assert len(captures) == 2
    assert bridge.has("signal")


def test_queued_reply_plays_during_realtime_outage(monkeypatch):
    config = Config(agent_backend="grok_realtime", signal=MODE)
    bridge, voice, realtime, _ = setup_talk(monkeypatch, config)
    session = ListeningSession(config)
    session.decide("nana", time.monotonic())
    monkeypatch.setattr(cli, "ListeningSession", lambda config: session)
    bridge.add(Inbound("signal", "text", text="still here", identity="1"))
    realtime.warm_connected = False
    realtime.warm.side_effect = [WalkietalkError("offline"), KeyboardInterrupt()]
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    assert cli.main(["-c", "unused", "talk", "--capture", "--transmit"]) == 130
    assert not bridge.has("signal")
    voice.synthesize.assert_called_once_with("Nana says: still here, over", truncate=False)
    cli.transmit_with_callsign.assert_called_once()


def test_whatsapp_accepts_messages_in_startup_second(tmp_path, monkeypatch):
    monkeypatch.setattr(messaging.time, "time", lambda: 100.9)
    bridge = MessageBridge(Config(whatsapp=MODE))
    bridge.whatsapp_store = tmp_path
    bridge._poll_whatsapp(prime=True)
    db = database(tmp_path)
    insert(db, "new", 100)
    bridge._poll_whatsapp()
    assert bridge.peek("whatsapp").identity == "new"
    db.close()


def test_signal_config_accepts_attachment_override_and_full_timeout(tmp_path):
    data = yaml.safe_load(Path("src/walkietalk/data/config.example.yaml").read_text())
    data["messaging"]["signal"].update(
        wake="nana",
        to="123",
        attachments_dir=str(tmp_path),
        listening={"mode": "conversation", "conversation_timeout_seconds": 600},
    )
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    config = load_config(path)
    assert config.signal.attachments_dir == str(tmp_path)
    assert config.signal.conversation_timeout_seconds == 600
    data["messaging"]["signal"]["attachments_dir"] = "relative/path"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WalkietalkError, match="absolute path"):
        load_config(path)


def test_control_with_an_explicit_agent_prefix_remains_valid(tmp_path):
    data = yaml.safe_load(Path("src/walkietalk/data/config.example.yaml").read_text())
    data["sleep"]["primary"] = "charlotte go to sleep"
    data["sleep"]["aliases"] = []
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    session = ListeningSession(load_config(path))
    assert session.decide("charlotte go to sleep", 0).kind == "sleep"


def test_whatsapp_sync_cannot_block_on_unread_stdout(monkeypatch):
    bridge = MessageBridge(Config(whatsapp=MODE))
    monkeypatch.setattr(bridge, "_poll_whatsapp", Mock())
    spawn = Mock()
    monkeypatch.setattr(bridge, "_spawn", spawn)
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    bridge.start()
    assert spawn.call_args.kwargs["stdout"] == subprocess.DEVNULL


@pytest.mark.parametrize("rate", [8000, 48000])
@pytest.mark.parametrize("maximum", [1, 0.123456, 0.333333])
def test_ffmpeg_trims_to_whole_samples_within_the_budget(tmp_path, rate, maximum):
    import shutil

    from walkietalk.tts import write_wav

    if not shutil.which("ffmpeg"):
        pytest.skip("optional ffmpeg dependency is not installed")
    source = tmp_path / "note.wav"
    write_wav(source, Wav(b"\x01\x00" * (rate * 2), rate, 2))
    result = messaging.voice_wav(source, maximum)
    assert result.rate == 48000
    assert len(result.frames) // 2 == int(maximum * 48000)
    assert result.duration <= maximum
