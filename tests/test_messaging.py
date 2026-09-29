import json
import subprocess
import sys
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from walkietalk import cli, messaging
from walkietalk.audio import Wav
from walkietalk.config import Config, MessagingMode, WalkietalkError, load_config
from walkietalk.messaging import (
    Inbound,
    MessageBridge,
    parse_signal_message,
    parse_whatsapp_row,
    signal_send_failure,
    spoken_text,
)
from walkietalk.wake import ListeningSession


def drain(bridge, service):
    items = []
    while item := bridge.peek(service):
        items.append(item)
        bridge.acknowledge(item)
    return items


def write_config(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture
def config_data():
    return yaml.safe_load(Path("src/walkietalk/data/config.example.yaml").read_text())


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


def test_example_messaging_is_disabled(tmp_path, config_data):
    config = load_config(write_config(tmp_path, config_data))
    assert config.whatsapp == MessagingMode()
    assert config.signal == MessagingMode()
    assert not config.whatsapp.enabled()
    assert not config.signal.enabled()


def test_omitted_messaging_section_stays_off(tmp_path, config_data):
    del config_data["messaging"]
    config = load_config(write_config(tmp_path, config_data))
    assert not config.signal.enabled()


def test_enabled_signal_contact(tmp_path, config_data):
    config_data["messaging"]["signal"] = {
        "wake": "grandma",
        "aliases": ["grand ma"],
        "to": "+15551212",
        "empty_queue_phrase": "Nothing from grandma.",
    }
    config = load_config(write_config(tmp_path, config_data))
    assert config.signal.enabled()
    assert config.signal.to == "+15551212"
    assert config.signal.aliases == ("grand ma",)


@pytest.mark.parametrize("phrase", ["charlotte", "go to sleep"])
def test_messaging_phrase_collisions_rejected(tmp_path, config_data, phrase):
    config_data["messaging"]["signal"]["wake"] = phrase
    config_data["messaging"]["signal"]["to"] = "+15551212"
    with pytest.raises(WalkietalkError, match="must differ"):
        load_config(write_config(tmp_path, config_data))


def test_services_can_share_empty_queue_phrase(tmp_path, config_data):
    for service, wake in (("whatsapp", "nana"), ("signal", "grandma")):
        config_data["messaging"][service].update(
            wake=wake, to="+15551212", empty_queue_phrase="Nothing yet."
        )
    config = load_config(write_config(tmp_path, config_data))
    assert config.whatsapp.empty_queue_phrase == config.signal.empty_queue_phrase == "Nothing yet."


@pytest.mark.parametrize("notice_service", ["whatsapp", "signal"])
def test_empty_queue_phrase_still_cannot_match_other_service_wake(
    tmp_path, config_data, notice_service
):
    for service, wake in (("whatsapp", "nana"), ("signal", "grandma")):
        config_data["messaging"][service].update(wake=wake, to="+15551212")
    other = "signal" if notice_service == "whatsapp" else "whatsapp"
    config_data["messaging"][notice_service]["empty_queue_phrase"] = config_data["messaging"][
        other
    ]["wake"]
    with pytest.raises(WalkietalkError, match="must differ"):
        load_config(write_config(tmp_path, config_data))


def test_signal_wake_sleeps_the_agent_and_agent_wake_returns(tmp_path, config_data):
    clock = FakeClock()
    config_data["listening"]["mode"] = "conversation"
    config_data["listening"]["conversation_timeout_seconds"] = 30
    config_data["messaging"]["signal"] = {
        "wake": "grandma",
        "aliases": [],
        "to": "+15551212",
        "empty_queue_phrase": "",
    }
    session = ListeningSession(load_config(write_config(tmp_path, config_data)), clock=clock)
    opened = session.decide("grandma we are on our way", 0)
    assert opened.accepted
    assert opened.destination == "signal"
    assert opened.traffic == "we are on our way"
    clock.now = 1
    session.complete_turn()
    follow = session.decide("tell her I love her", 2)
    assert follow.destination == "signal"
    assert follow.kind == "follow_up"
    agent = session.decide("charlotte what is rain", 3)
    assert agent.accepted
    assert agent.destination == "agent"
    assert agent.traffic == "what is rain"
    asleep = session.decide("go to sleep", 4)
    assert asleep.kind == "sleep"
    assert session.destination == ""


def test_shutdown_still_wins_with_a_messaging_prefix(tmp_path, config_data):
    from walkietalk.shutdown import ShutdownSession

    config_data["shutdown"]["enabled"] = True
    config_data["shutdown"]["phrase"] = "bridge shutdown"
    config_data["shutdown"]["code"] = "alpha seven"
    config_data["shutdown"]["arm_confirmation_phrase"] = "Shutdown armed."
    config_data["shutdown"]["confirmation_phrase"] = "Walkietalk shutting down."
    config_data["messaging"]["signal"] = {
        "wake": "grandma",
        "aliases": [],
        "to": "+15551212",
        "empty_queue_phrase": "Nothing yet.",
    }
    config = load_config(write_config(tmp_path, config_data))
    decision = ShutdownSession(config).decide("grandma bridge shutdown", 0)
    assert decision.kind == "armed"


def test_signal_outgoing_sync_copy_is_not_an_incoming_reply():
    mode = MessagingMode(wake="fire 9", to="+15551212")
    payload = {
        "envelope": {
            "sourceNumber": "+15551212",
            "timestamp": 20,
            "syncMessage": {
                "sentMessage": {
                    "destinationNumber": "+15551212",
                    "message": "On my way",
                }
            },
        }
    }
    parsed = parse_signal_message(payload, mode)
    assert parsed is None
    other = {
        "envelope": {
            "timestamp": 21,
            "syncMessage": {"sentMessage": {"destinationNumber": "+1999", "message": "Nope"}},
        }
    }
    assert parse_signal_message(other, mode) is None


def test_unregistered_signal_number_is_named():
    payload = {
        "error": {
            "code": -1,
            "message": "Failed to send message",
            "data": {"response": {"results": [{"type": "UNREGISTERED_FAILURE"}]}},
        }
    }
    assert signal_send_failure(payload) == "Message not sent. That number is not a Signal account."
    assert signal_send_failure({"result": {"timestamp": 1}}) is None


def test_spoken_text_appends_over():
    assert spoken_text("  we are leaving ") == "we are leaving, over"


def test_signal_parser_keeps_text_and_drops_calls():
    mode = MessagingMode(wake="grandma", to="+15551212")
    payload = {
        "envelope": {
            "sourceNumber": "+15551212",
            "timestamp": 5,
            "dataMessage": {"message": "Hi"},
        }
    }
    text = parse_signal_message(payload, mode)
    assert text is not None and text.kind == "text" and text.text == "Hi"
    dropped = parse_signal_message(
        {"envelope": {"sourceNumber": "+15551212", "callMessage": {"offer": "x"}}},
        mode,
    )
    assert dropped is None
    other = parse_signal_message(
        {"envelope": {"sourceNumber": "+1999", "dataMessage": {"message": "Hi"}}},
        mode,
    )
    assert other is None


def test_whatsapp_parser_reads_text_and_ignores_self(tmp_path):
    mode = MessagingMode(wake="grandma", to="15551212")
    row = {
        "msg_id": "m1",
        "chat_jid": "15551212@s.whatsapp.net",
        "from_me": 0,
        "text": "Love you",
        "display_text": "",
        "media_type": None,
        "local_path": "",
        "ts_unix": 10,
    }
    parsed = parse_whatsapp_row(row, mode)
    assert parsed is not None and parsed.text == "Love you"
    row["from_me"] = 1
    assert parse_whatsapp_row(row, mode) is None
    audio = Path(tmp_path / "note.m4a")
    audio.write_bytes(b"audio")
    voice = parse_whatsapp_row(
        {
            "msg_id": "m2",
            "chat_jid": "15551212@s.whatsapp.net",
            "from_me": 0,
            "text": "",
            "media_type": "audio",
            "local_path": str(audio),
            "ts_unix": 12,
        },
        mode,
    )
    assert voice is not None and voice.kind == "voice" and voice.audio_path == audio


@pytest.mark.parametrize("account", ["", "+15550001111"])
def test_signal_send_uses_a_running_daemon(monkeypatch, account):
    # Local daemon requests must ignore proxies even when no_proxy misses 127.0.0.1.
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("no_proxy", "localhost")
    monkeypatch.setenv("NO_PROXY", "localhost")
    received = []
    subscriptions = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/v1/check":
                self.send_response(200)
                self.end_headers()
                return
            if urlsplit(self.path).path == "/api/v1/events":
                subscriptions.append(parse_qs(urlsplit(self.path).query))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                note = {
                    "jsonrpc": "2.0",
                    "method": "receive",
                    "params": {
                        "account": account or "+15550001111",
                        "envelope": {
                            "sourceNumber": "+15551212",
                            "timestamp": 9,
                            "dataMessage": {"message": "Hello"},
                        },
                    },
                }
                self.wfile.write(f"data: {json.dumps(note)}\n\n".encode())
                self.wfile.flush()
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            received.append(json.loads(self.rfile.read(length).decode()))
            body = b'{"jsonrpc":"2.0","result":{"timestamp":1},"id":1}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(messaging, "SIGNAL_HTTP", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(messaging.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("lock"))
    config = replace(
        Config(),
        signal=MessagingMode(
            wake="grandma",
            to="+15551212",
            account=account,
        ),
    )
    bridge = MessageBridge(config)
    try:
        bridge.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not bridge.has("signal"):
            time.sleep(0.02)
        assert [item.text for item in drain(bridge, "signal")] == ["Hello"]
        bridge.send_text("signal", "we are on our way")
    finally:
        bridge.close()
        server.shutdown()
        server.server_close()
    assert received[0]["method"] == "send"
    assert received[0]["params"]["message"] == "we are on our way"
    assert received[0]["params"]["recipient"] == ["+15551212"]
    if account:
        assert received[0]["params"]["account"] == account
        assert subscriptions[0] == {"account": [account]}
    else:
        assert "account" not in received[0]["params"]
        assert subscriptions[0] == {}


@pytest.mark.parametrize("account", ["", "+15550001111"])
def test_signal_without_a_daemon_uses_one_jsonrpc_process(monkeypatch, tmp_path, account):
    script = tmp_path / "signal_fake.py"
    record = tmp_path / "request.json"
    script.write_text(
        "import json,sys,pathlib\n"
        "for line in sys.stdin:\n"
        "    request=json.loads(line)\n"
        f"    pathlib.Path({str(record)!r}).write_text(line)\n"
        "    sys.stdout.write(json.dumps({'jsonrpc':'2.0','result':{'timestamp':1},"
        "'id':request['id']})+'\\n')\n"
        "    sys.stdout.flush()\n"
    )
    monkeypatch.setattr(messaging, "signal_daemon_ready", lambda base=messaging.SIGNAL_HTTP: False)
    config = replace(
        Config(),
        signal=MessagingMode(
            wake="grandma",
            to="+15551212",
            account=account,
        ),
    )
    bridge = MessageBridge(config)
    command = bridge._signal_jsonrpc_command()
    if account:
        assert command[:4] == ["signal-cli", "-a", account, "jsonRpc"]
    else:
        assert command[:2] == ["signal-cli", "jsonRpc"]
    bridge._signal_jsonrpc_command = lambda: [__import__("sys").executable, str(script)]
    spawned = []
    real_popen = messaging.subprocess.Popen

    def popen(command, **kwargs):
        spawned.append(command)
        return real_popen(command, **kwargs)

    monkeypatch.setattr(messaging.subprocess, "Popen", popen)
    try:
        bridge.start()
        bridge.send_text("signal", "hello")
    finally:
        bridge.close()
    assert len(spawned) == 1
    assert spawned[0][-1].endswith("signal_fake.py")
    params = json.loads(record.read_text())["params"]
    assert params["recipient"] == ["+15551212"]
    if account:
        assert params["account"] == account
    else:
        assert "account" not in params


def test_queue_drains_once():
    bridge = MessageBridge(Config())
    bridge.add(Inbound("signal", "text", text="Hi", identity="1"))
    bridge.add(Inbound("signal", "text", text="Hi", identity="1"))
    assert [item.text for item in drain(bridge, "signal")] == ["Hi"]
    assert drain(bridge, "signal") == []


def test_talk_sends_signal_text_and_skips_the_agent(monkeypatch, tmp_path, config_data, capsys):
    config_data["listening"]["mode"] = "wake_phrase"
    config_data["messaging"]["signal"] = {
        "wake": "grandma",
        "aliases": [],
        "to": "+15551212",
        "empty_queue_phrase": "Nothing yet.",
    }
    sent = []

    class FakeBridge:
        def start(self):
            return None

        def close(self):
            return None

        def mode(self, service):
            return MessagingMode(wake="grandma", to="+15551212", empty_queue_phrase="Nothing yet.")

        def has(self, service):
            return False

        def peek(self, service):
            return None

        def send_text(self, service, text):
            sent.append((service, text))

    monkeypatch.setattr(cli, "open_messaging", lambda config: FakeBridge())
    monkeypatch.setattr(cli, "preflight", lambda config, **kwargs: 0)
    monkeypatch.setattr(
        cli,
        "open_stt",
        lambda config: type(
            "L",
            (),
            {
                "label": lambda self: "fake",
                "prepare": lambda self: None,
                "transcribe": lambda self, pcm, rate: "grandma we are on our way",
            },
        )(),
    )
    monkeypatch.setattr(
        cli,
        "capture_from_device",
        lambda *args, **kwargs: __import__("walkietalk.capture", fromlist=["Utterance"]).Utterance(
            b"\x00\x40" * 320, 16000, 0.24, 0.24, 0.5, "silence", started_at=1.0
        ),
    )
    path = write_config(tmp_path, config_data)
    assert cli.main(["-c", str(path), "talk", "--capture", "--once"]) == 0
    assert sent == [("signal", "we are on our way")]
    output = capsys.readouterr().out
    assert "Reply: This is a pretend answer." not in output
    assert "Traffic: we are on our way" in output


def test_empty_queue_phrase_is_silent_when_blank(monkeypatch, tmp_path, config_data, capsys):
    config_data["messaging"]["whatsapp"] = {
        "wake": "nana",
        "aliases": [],
        "to": "15551212",
        "empty_queue_phrase": "",
    }

    class FakeBridge:
        def close(self):
            return None

        def mode(self, service):
            return MessagingMode(wake="nana", to="15551212", empty_queue_phrase="")

        def has(self, service):
            return False

        def peek(self, service):
            return None

        def send_text(self, service, text):
            raise AssertionError("nothing to send")

    monkeypatch.setattr(cli, "open_messaging", lambda config: FakeBridge())
    monkeypatch.setattr(cli, "preflight", lambda config, **kwargs: 0)
    monkeypatch.setattr(
        cli,
        "open_stt",
        lambda config: type(
            "L",
            (),
            {
                "label": lambda self: "fake",
                "prepare": lambda self: None,
                "transcribe": lambda self, pcm, rate: "nana",
            },
        )(),
    )
    monkeypatch.setattr(
        cli,
        "capture_from_device",
        lambda *args, **kwargs: __import__("walkietalk.capture", fromlist=["Utterance"]).Utterance(
            b"\x00\x40" * 320, 16000, 0.24, 0.24, 0.5, "silence", started_at=1.0
        ),
    )
    path = write_config(tmp_path, config_data)
    assert cli.main(["-c", str(path), "talk", "--capture", "--once"]) == 0
    assert "Wake heard" in capsys.readouterr().out


def test_text_reply_and_voice_reply_use_the_station_id(monkeypatch):
    played = []
    synthesized = []

    class Voice:
        def synthesize(self, text, truncate=True):
            synthesized.append(text)
            return Wav(b"\x01\x00" * 480, 48000, 0.01)

    class Signs:
        def due(self):
            return True

        def mark(self):
            synthesized.append("marked")

    monkeypatch.setattr(
        cli, "transmit_speech", lambda speech, config, **kwargs: played.append(speech)
    )
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    config = Config(
        callsign="TEST1ID",
        callsign_mode="end_of_reply",
        settle_seconds=0.2,
        max_tx_seconds=10,
    )
    speech = Wav(b"\x01\x00" * 4800, 48000, 0.1)
    cli.transmit_with_callsign(speech, config, Voice(), Signs())
    assert synthesized[0] == "TEST1ID"
    assert played
    assert spoken_text("Love you") == "Love you, over"


@pytest.mark.parametrize(
    "agent_mode,message_mode", [("wake_phrase", "conversation"), ("conversation", "wake_phrase")]
)
def test_independent_listening_and_global_sleep(agent_mode, message_mode):
    clock = FakeClock()
    config = Config(
        listening_mode=agent_mode,
        conversation_timeout_seconds=99,
        sleep_primary="go to sleep",
        signal=MessagingMode(
            wake="grandma",
            to="+15551212",
            listening_mode=message_mode,
            conversation_timeout_seconds=7,
        ),
    )
    session = ListeningSession(config, clock)
    assert session.decide("grandma", 0).kind == "wake_only"
    session.complete_turn()
    assert session.awake_until == (7 if message_mode == "conversation" else None)
    assert session.decide("hello", 1).accepted == (message_mode == "conversation")
    clock.now = 8
    session.expire_if_needed()
    assert session.destination == "signal"  # Incoming replies remain eligible after timeout.
    assert not session.decide("hello", 8).accepted
    session.decide("go to sleep", 8)
    assert session.destination == ""
    assert not session.decide("hello", 8).accepted
    session.decide("charlotte", 8)
    session.complete_turn()
    assert session.awake_until == (107 if agent_mode == "conversation" else None)


def test_config_loads_messaging_listening_and_sender_alias(tmp_path, config_data):
    config_data["messaging"]["signal"].update(
        wake="contact",
        to="+15551212",
        sender_alias="Grandma",
        listening={"mode": "wake_phrase", "conversation_timeout_seconds": 23},
    )
    mode = load_config(write_config(tmp_path, config_data)).signal
    assert mode.sender_alias == "Grandma"
    assert mode.listening_mode == "wake_phrase"
    assert mode.conversation_timeout_seconds == 23


def test_queue_peek_preserves_pending_and_ids_are_service_scoped():
    bridge = MessageBridge(Config())
    first = Inbound("signal", "text", text="First", identity="1")
    second = Inbound("signal", "text", text="Second", identity="2")
    bridge.add(first)
    bridge.add(second)
    bridge.add(Inbound("whatsapp", "text", text="Other", identity="1"))
    assert bridge.peek("signal") == first
    assert bridge.peek("signal") == first
    bridge.acknowledge(first)
    assert bridge.peek("signal") == second
    assert bridge.has("whatsapp")


@pytest.mark.parametrize("kind", ["text", "voice"])
def test_playback_retries_then_listens_before_next_message(
    monkeypatch, tmp_path, config_data, kind
):
    from walkietalk.capture import Utterance

    config_data["messaging"]["signal"].update(
        wake="contact",
        to="+15551212",
        sender_alias="Grandma",
        listening={"mode": "wake_phrase"},
    )
    config_data["radio"]["callsign_mode"] = "off"
    path = write_config(tmp_path, config_data)
    bridge = MessageBridge(load_config(path))
    first = Inbound(
        "signal",
        kind,
        text="Hello",
        identity="1",
        audio_path=tmp_path / "voice.wav" if kind == "voice" else None,
    )
    second = Inbound("signal", "text", text="Later", identity="2")
    bridge.add(first)
    bridge.add(second)
    monkeypatch.setattr(cli, "open_messaging", lambda config: bridge)
    monkeypatch.setattr(cli, "preflight", lambda *args, **kwargs: 0)
    clock = FakeClock(100)
    monkeypatch.setattr(cli.time, "monotonic", clock)
    utterances = iter(["contact", "go to sleep"])
    listener = type(
        "Listener",
        (),
        {
            "label": lambda self: "fake",
            "prepare": lambda self: None,
            "transcribe": lambda self, *args: next(utterances),
        },
    )()
    monkeypatch.setattr(cli, "open_stt", lambda config: listener)
    spoken = []
    attempts = []
    monkeypatch.setattr(cli, "voice_wav", lambda *args: Wav(b"\x01\x00" * 480, 48000, 0.01))

    class Voice:
        def label(self):
            return "fake"

        def prepare(self):
            pass

        def synthesize(self, text, **kwargs):
            attempts.append(text)
            if len(attempts) == 1:
                raise WalkietalkError("temporary failure")
            spoken.append(text)
            return Wav(b"\x01\x00" * 480, 48000, 0.01)

    monkeypatch.setattr(cli, "open_tts", lambda config: Voice())
    monkeypatch.setattr(cli, "transmit_speech", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "wait_post_tx_mute", lambda config: None)
    captures = 0

    def capture(*args, on_wait, **kwargs):
        nonlocal captures
        captures += 1
        if captures == 2:
            assert bridge.peek("signal") == first  # Failed preparation retained it.
            clock.now += 6
            on_wait()  # Delayed reply works even in wake_phrase mode.
            pytest.fail("Expected queued reply to end idle capture")
        if captures == 3:
            assert bridge.peek("signal") == second
            on_wait()  # One-second gap prevents another immediate transmission.
        if captures == 4:
            clock.now += 10
            on_wait()  # Sleep holds the second message even after the gap ends.
            assert bridge.peek("signal") == second
            raise KeyboardInterrupt
        return Utterance(b"\x00\x40" * 320, 16000, 0.24, 0.24, 0.5, "silence", started_at=clock.now)

    monkeypatch.setattr(cli, "capture_from_device", capture)
    assert cli.main(["-c", str(path), "talk", "--capture", "--transmit"]) == 130
    expected = "Grandma says: Hello, over" if kind == "text" else "Grandma says:"
    assert spoken.count(expected) == 1
    assert "Grandma says: Later, over" not in spoken
    assert captures == 4


@pytest.mark.parametrize("layout", ["fresh", "legacy", "xdg", "override"])
def test_whatsapp_store_and_commands_agree(monkeypatch, tmp_path, layout):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("WACLI_STORE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(messaging.sys, "platform", "linux")
    expected = tmp_path / ".local/state/wacli"
    if layout == "legacy":
        expected = tmp_path / ".wacli"
        expected.mkdir()
    elif layout == "xdg":
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        expected = tmp_path / "state/wacli"
    elif layout == "override":
        (tmp_path / ".wacli").mkdir()
        expected = tmp_path / "custom"
        monkeypatch.setenv("WACLI_STORE_DIR", str(expected))
    bridge = MessageBridge(Config(whatsapp=MessagingMode(wake="nana", to="123")))
    assert bridge.whatsapp_store == expected
    commands = []
    monkeypatch.setattr(bridge, "_spawn", lambda command, **kwargs: commands.append(command))
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)

    def run(command, **kwargs):
        commands.append(command)
        return 0, b'{"success":true}', b""

    monkeypatch.setattr(messaging, "run_cli", run)
    bridge.start()
    bridge.send_text("whatsapp", "hello")
    assert len(commands) == 2
    assert all(command[:3] == ["wacli", "--store", str(expected)] for command in commands)


@pytest.mark.parametrize("body", ["One sentence. Another sentence here.", "x" * 100, "a! b? c. d"])
@pytest.mark.parametrize("budget", [1, 3, 15, 30])
def test_split_preserves_text_and_budget(body, budget):
    remaining = body
    parts = []
    while remaining:
        part, remaining = messaging.split_text(remaining, budget)
        assert 0 < len(part) <= budget
        parts.append(part)
    assert "".join("".join(parts).split()) == "".join(body.split())


def test_text_chunks_obey_character_and_audio_limits():
    from walkietalk.agent import validate_reply
    from walkietalk.config import OutputTooLarge

    config = Config(agent_max_reply_chars=80, max_tx_seconds=1, settle_seconds=0.2)
    body = "First sentence with several words. Second sentence with more words. " * 10
    progress = messaging.MessagePlayback(body.strip())
    attempts, accepted = [], []

    class Voice:
        def synthesize(self, text, *, truncate):
            assert truncate is False
            validate_reply(text, config.agent_max_reply_chars)
            attempts.append(text)
            # Simulate a provider rejecting audio that is too long, even within text limits.
            if len(text) > 45:
                raise OutputTooLarge("too long")
            accepted.append(text)
            return Wav(b"\x01\x00" * (len(text) * 600), 48000, len(text) / 80)

    while progress.remaining:
        old = progress.remaining
        speech, rest = progress.prepare_text(Voice(), "Grandma", config)
        assert progress.remaining == old  # Preparing is not acknowledging.
        assert speech.duration <= 0.8
        progress.remaining = rest
    assert len(attempts) > len(accepted) > 2
    assert all(not text.endswith(", over") for text in accepted[:-1])
    assert accepted[-1].endswith(", over")
    reconstructed = " ".join(text.removeprefix("Grandma says: ") for text in accepted)
    assert reconstructed.removesuffix(", over").rstrip(".") == body.strip().rstrip(".")


@pytest.mark.parametrize("permanent", [False, True])
def test_chunk_progress_retries_sleep_and_queue_advancement(
    monkeypatch, tmp_path, config_data, permanent, capsys
):
    from walkietalk.capture import Utterance

    config_data["messaging"]["signal"].update(
        wake="contact", to="+15551212", sender_alias="Grandma"
    )
    config_data["agent"]["max_reply_chars"] = 40
    path = write_config(tmp_path, config_data)
    bridge = MessageBridge(load_config(path))
    bridge.add(Inbound("signal", "text", text="First words here. Second words here.", identity="1"))
    bridge.add(Inbound("signal", "text", text="Later", identity="2"))
    monkeypatch.setattr(cli, "open_messaging", lambda config: bridge)
    monkeypatch.setattr(cli, "preflight", lambda *args, **kwargs: None)
    clock = FakeClock(100)
    monkeypatch.setattr(cli.time, "monotonic", clock)
    transcripts = iter(["contact", "go to sleep", "contact"])
    listener = type(
        "Listener",
        (),
        {
            "label": lambda self: "fake",
            "prepare": lambda self: None,
            "transcribe": lambda self, *args: next(transcripts),
        },
    )()
    monkeypatch.setattr(cli, "open_stt", lambda config: listener)
    attempts, played = [], []

    class Voice:
        def prepare(self):
            pass

        def label(self):
            return "fake"

        def synthesize(self, text, *, truncate=False):
            assert not truncate
            attempts.append(text)
            if text == "Grandma says: Second words here, over":
                if permanent or attempts.count(text) == 1:
                    raise WalkietalkError("temporary failure")
            return Wav(text.encode(), 48000, 0.01)

    # Use real chunk sizing but a tagged WAV to track successful transmissions.
    monkeypatch.setattr(messaging, "radio_wav", lambda wav, maximum: wav)
    monkeypatch.setattr(cli, "open_tts", lambda config: Voice())
    monkeypatch.setattr(
        cli, "transmit_with_callsign", lambda speech, *args: played.append(speech.frames.decode())
    )
    captures = 0

    def capture(*args, on_wait, **kwargs):
        nonlocal captures
        captures += 1
        assert captures < 12
        if captures == 2:
            on_wait()  # Listen before the next chunk; sleep now holds its remainder.
        elif captures == 3:
            clock.now += 10
            on_wait()  # Sleeping still suppresses playback.
        elif captures > 3:
            if not bridge.has("signal"):
                raise KeyboardInterrupt
            clock.now += 6
            on_wait()
            pytest.fail("Expected queued playback")
        return Utterance(b"\x00\x40" * 320, 16000, 0.24, 0.24, 0.5, "silence", started_at=clock.now)

    # Keep the sleep acknowledgement out of this playback test.
    config_data["sleep"]["confirmation_phrase"] = ""
    write_config(tmp_path, config_data)
    monkeypatch.setattr(cli, "capture_from_device", capture)
    assert cli.main(["-c", str(path), "talk", "--capture", "--transmit"]) == 130
    assert played.count("Grandma says: First words here.") == 1
    assert played.count("Grandma says: Second words here, over") == (0 if permanent else 1)
    assert played[-1] == "Grandma says: Later, over"
    assert ("Skipping unplayable" in capsys.readouterr().err) == permanent


@pytest.mark.parametrize("backend", ["grok", "hermes"])
def test_tts_worker_preserves_splittable_error(monkeypatch, backend):
    import sys

    from walkietalk import grok_tts, hermes_tts
    from walkietalk.config import OutputTooLarge

    module = grok_tts if backend == "grok" else hermes_tts
    voice_type = module.GrokTts if backend == "grok" else module.HermesTts
    monkeypatch.setattr(voice_type, "prepare", lambda self: None)
    if backend == "hermes":
        monkeypatch.setattr(voice_type, "_token", lambda self: "test-token")
    original = module.run_cli
    program = f"""
from walkietalk import {module.__name__.split(".")[-1]} as voice_module
from walkietalk.config import OutputTooLarge

def synthesize(*args, **kwargs):
    raise OutputTooLarge('speech exceeds limit')

voice_module.{voice_type.__name__}._synthesize_direct = synthesize
raise SystemExit(voice_module.main())
"""

    def run(command, **kwargs):
        return original([sys.executable, "-c", program, command[-1]], **kwargs)

    monkeypatch.setattr(module, "run_cli", run)
    config = Config(tts_backend=backend)
    with pytest.raises(OutputTooLarge, match="speech exceeds limit"):
        voice_type(config).synthesize("Hello", truncate=False)


def test_config_selects_local_signal_account(tmp_path, config_data):
    config_data["messaging"]["signal"]["account"] = "+15550001111"
    assert load_config(write_config(tmp_path, config_data)).signal.account == "+15550001111"


@pytest.mark.parametrize("account", [None, 123, True, "+123\n", "--help", "15551212", ""])
def test_signal_account_validation(tmp_path, config_data, account):
    config_data["messaging"]["signal"]["account"] = account
    if account == "":
        assert load_config(write_config(tmp_path, config_data)).signal.account == ""
    else:
        with pytest.raises(WalkietalkError, match="messaging.signal.account"):
            load_config(write_config(tmp_path, config_data))


def test_whatsapp_rejects_signal_account_setting(tmp_path, config_data):
    config_data["messaging"]["whatsapp"]["account"] = "+15550001111"
    with pytest.raises(WalkietalkError):
        load_config(write_config(tmp_path, config_data))


@pytest.mark.parametrize("transport", ["http", "rpc"])
def test_signal_filters_other_accounts_before_deduplication(transport):
    bridge = MessageBridge(
        Config(
            signal=MessagingMode(
                wake="contact",
                to="+15551212",
                account="+15550001111",
            )
        )
    )
    envelope = {"sourceNumber": "+15551212", "timestamp": 1, "dataMessage": {"message": "hello"}}
    for account in ["+15550002222", None, "+15550001111"]:
        event = {"envelope": envelope}
        if account is not None:
            event["account"] = account
        if transport == "rpc":
            event = {"method": "receive", "params": event}
        bridge._accept_signal_payload(event)
        assert bridge.has("signal") == (account == "+15550001111")
    assert len(drain(bridge, "signal")) == 1


@pytest.mark.parametrize("failure", [WalkietalkError("missing signal-cli"), KeyboardInterrupt()])
def test_partial_startup_reaps_started_process(monkeypatch, failure):
    mode = MessagingMode(wake="contact", to="+15551212")
    config = Config(whatsapp=mode, signal=mode)
    spawned = []
    original_spawn = MessageBridge._spawn

    def spawn(self, command, **kwargs):
        if command[0] == "signal-cli":
            raise failure
        process = original_spawn(self, [sys.executable, "-c", "import time; time.sleep(60)"])
        spawned.append(process)
        return process

    monkeypatch.setattr(MessageBridge, "_poll_whatsapp", lambda *a, **kw: None)
    monkeypatch.setattr(MessageBridge, "_spawn", spawn)
    monkeypatch.setattr(messaging, "signal_daemon_ready", lambda *a: False)
    try:
        with pytest.raises(type(failure)):
            messaging.open_messaging(config)
        assert len(spawned) == 1
        assert spawned[0].poll() is not None
    finally:
        for process in spawned:
            if process.poll() is None:
                process.kill()
            process.wait()


def test_close_kills_every_child_after_shared_grace_period(monkeypatch):
    from unittest.mock import Mock

    bridge = MessageBridge(Config())
    children = [Mock(pid=101), Mock(pid=102)]
    for child in children:
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("child", 2), 0]
    bridge._processes = children
    times = iter([0, 0, 3])
    monkeypatch.setattr(messaging.time, "monotonic", lambda: next(times))
    signals = []
    monkeypatch.setattr(messaging, "_signal_group", lambda pid, sig: signals.append((pid, sig)))
    bridge.close()
    assert signals == [
        (101, messaging.signal.SIGTERM),
        (102, messaging.signal.SIGTERM),
        (101, messaging.signal.SIGKILL),
        (102, messaging.signal.SIGKILL),
    ]
    assert children[1].wait.call_args_list[0].kwargs == {"timeout": 0}


def test_signal_stream_reconnects_without_reusing_timed_out_reader(monkeypatch):
    import httpx

    bridge = MessageBridge(Config(signal=MessagingMode(wake="contact", to="+15551212")))
    bridge._signal_base = "http://localhost"
    reads = []
    closed = []

    class Stream(httpx.AsyncByteStream):
        def __init__(self, index):
            self.index = index

        async def __aiter__(self):
            reads.append(self.index)
            if self.index == 0:
                raise httpx.ReadTimeout("missed keepalive")
            bridge._stop.set()
            yield (
                b'data: {"envelope":{"sourceNumber":"+15551212","timestamp":1,'
                b'"dataMessage":{"message":"reconnected"}}}\n'
            )

        async def aclose(self):
            closed.append(self.index)

    requests = []

    def respond(request):
        index = len(requests)
        requests.append(request)
        return httpx.Response(200, stream=Stream(index))

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        messaging.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            **kwargs,
            transport=httpx.MockTransport(respond),
        ),
    )
    bridge._watch_signal_http()
    assert reads == [0, 1]
    assert closed == [0, 1]
    assert bridge.peek("signal").text == "reconnected"


def test_signal_stream_keeps_subscription_across_idle_period():
    connections = []
    finished = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            connections.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            if finished.wait(5.5):
                return
            event = {
                "envelope": {
                    "sourceNumber": "+15551212",
                    "timestamp": 1,
                    "dataMessage": {"message": "after idle"},
                }
            }
            try:
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
            except OSError:
                pass
            finished.wait(2)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    bridge = MessageBridge(Config(signal=MessagingMode(wake="contact", to="+15551212")))
    bridge._signal_base = f"http://127.0.0.1:{server.server_port}"
    reader = threading.Thread(target=bridge._watch_signal_http, daemon=True)
    bridge._threads.append(reader)
    reader.start()
    try:
        deadline = time.monotonic() + 7
        while not bridge.has("signal") and time.monotonic() < deadline:
            time.sleep(0.02)
        assert bridge.has("signal")
        assert bridge.peek("signal").text == "after idle"
        assert len(connections) == 1
    finally:
        bridge.close()
        finished.set()
        server.shutdown()
        server.server_close()
    assert not reader.is_alive()
