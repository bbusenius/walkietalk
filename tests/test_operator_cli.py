"""Real local CLI transport, with no radio, messaging accounts, or speech providers."""

import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

from walkietalk import cli, operator_mode
from walkietalk.config import Config, MessagingMode, WalkietalkError
from walkietalk.messaging import Inbound, MessageBridge
from walkietalk.operator_mode import OperatorServer, operator_request, operator_socket

MODE = MessagingMode(wake="nana", to="123", sender_alias="Nana")


@pytest.fixture
def runtime(monkeypatch):
    # Keep real Unix socket names below the platform length limit.
    with tempfile.TemporaryDirectory(prefix="wt-operator-test-") as directory:
        root = Path(directory)
        monkeypatch.setenv("XDG_RUNTIME_DIR", directory)
        config_path = root / "config.yaml"
        config_path.write_text("messaging:\n  operator_mode: true\n")
        yield root, config_path


@pytest.fixture
def control(runtime):
    _, config_path = runtime
    bridge = MessageBridge(Config(messaging_operator_mode=True, signal=MODE))
    server = OperatorServer(bridge.operator, config_path)
    server.start()
    try:
        yield bridge, server, config_path
    finally:
        server.close()


def watch_commands(monkeypatch, review):
    queued = threading.Event()
    requests = []
    submit = review.submit

    def observed(*args, **kwargs):
        request = submit(*args, **kwargs)
        requests.append(request)
        queued.set()
        return request

    monkeypatch.setattr(review, "submit", observed)
    return queued, requests


def connect(server):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(2)
    connection.connect(str(server.path))
    response = connection.makefile("rb")
    snapshot = json.loads(response.readline())["snapshot"]
    return connection, response, snapshot


def request(connection, action):
    connection.sendall(json.dumps({"action": action}).encode() + b"\n")


def test_status_is_immediate_and_does_not_change_or_preview_messages(control):
    bridge, _, config_path = control
    bridge.add(Inbound("signal", "text", text="hello", identity="1"))
    log = Mock()
    assert operator_request(config_path, "status", timeout=2, log=log)
    assert ("reply", "hello") in [call.args for call in log.call_args_list]
    assert "1 waiting" in log.call_args_list[0].args[1]
    assert bridge.has("signal") and not bridge.operator.has_commands()
    assert not bridge.operator.approved(bridge.peek("signal"))


def test_status_counts_previously_approved_sleeping_replies(control):
    bridge, _, config_path = control
    item = Inbound("signal", "text", text="held", identity="1")
    bridge.add(item)
    review = bridge.operator
    review.announce()
    review.submit("approve")
    review.finish(review.next_command()[1])
    review.complete_command(True)
    log = Mock()
    assert operator_request(config_path, "status", timeout=2, log=log)
    assert "0 waiting; 1 approved" in log.call_args_list[0].args[1]
    assert bridge.peek("signal") is item


@pytest.mark.parametrize("approved", [False, True])
def test_sleep_is_a_session_command_even_when_displayed_queue_changes(
    control, monkeypatch, approved
):
    bridge, server, _ = control
    old = bridge.operator.add("signal", text="old review head")
    queued, _ = watch_commands(monkeypatch, bridge.operator)
    connection, response, snapshot = connect(server)
    try:
        bridge.operator.finish(old)
        replacement = bridge.operator.add("signal", text="new review head")
        connection.sendall(
            json.dumps(
                {"action": "sleep", "revision": snapshot["revision"], "approved": approved}
            ).encode()
            + b"\n"
        )
        assert queued.wait(2)
        assert bridge.operator.next_command() == ("sleep", None, False)
        assert bridge.operator._waiting == [replacement]
        bridge.operator.complete_command(True)
        assert "Command queued" in json.loads(response.readline())["message"]
        assert json.loads(response.readline()) == {"done": True, "ok": True}
    finally:
        response.close()
        connection.close()


@pytest.mark.parametrize("approved", [False, True])
def test_sleep_cli_does_not_need_any_review_or_approved_message(control, monkeypatch, approved):
    bridge, _, config_path = control
    queued, _ = watch_commands(monkeypatch, bridge.operator)
    log = Mock()
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(
            operator_request,
            config_path,
            "sleep",
            approved=approved,
            revision=None,
            timeout=2,
            log=log,
        )
        assert queued.wait(2)
        assert bridge.operator.next_command() == ("sleep", None, False)
        bridge.operator.respond("status", "Sleep requested by operator; waiting for wake.")
        bridge.operator.complete_command(True)
        assert result.result(timeout=2)
    assert not bridge.operator._waiting and not bridge.operator._approved
    assert not any("queue empty" in call.args[1].lower() for call in log.call_args_list)


@pytest.mark.parametrize("action", ["status", "read", "transmit", "deny"])
def test_approved_commands_display_and_bind_to_approved_head(control, monkeypatch, action):
    bridge, _, config_path = control
    incoming = Inbound("signal", "text", text="approved message", identity="1")
    bridge.add(incoming)
    review = bridge.operator
    review.finish(review._waiting[0])
    outgoing = review.add("signal", text="different review head")
    queued, _ = watch_commands(monkeypatch, review)
    log = Mock()
    if action == "status":
        assert operator_request(config_path, action, approved=True, timeout=2, log=log)
        assert not review.has_commands()
    else:
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(
                operator_request, config_path, action, approved=True, timeout=2, log=log
            )
            assert queued.wait(2)
            command, item, stale = review.next_command()
            assert command == action and item.incoming is incoming and not stale
            if action == "read":
                review.respond("reply", incoming.text)
            elif action == "transmit":
                review.dispatch(item)
            else:
                bridge.discard(incoming)
            review.complete_command(True)
            assert result.result(timeout=2)
    assert ("reply", "approved message") in [call.args for call in log.call_args_list]
    assert ("reply", "different review head") not in [call.args for call in log.call_args_list]
    assert review._waiting == [outgoing]
    assert review.dispatch_service() == ("signal" if action == "transmit" else "")


def test_approved_command_cannot_authorize_head_delivered_after_connecting(control, monkeypatch):
    bridge, server, _ = control
    first = Inbound("signal", "text", text="first", identity="1")
    second = Inbound("signal", "text", text="second", identity="2")
    bridge.add(first)
    bridge.add(second)
    review = bridge.operator
    review.finish(review._waiting[0])
    review.finish(review._waiting[0])
    queued, _ = watch_commands(monkeypatch, review)
    connection, response, snapshot = connect(server)
    try:
        assert snapshot["approved_item"]["text"] == "first"
        bridge.acknowledge(first)
        connection.sendall(b'{"action": "transmit", "approved": true}\n')
        assert queued.wait(2)
        assert review.next_command() == ("transmit", None, True)
        review.complete_command(False)
        response.readline()
        assert json.loads(response.readline())["ok"] is False
        assert not review.dispatch_service() and review.approved(second)
    finally:
        response.close()
        connection.close()


def test_panel_approved_revision_cannot_release_replacement_head(control):
    bridge, _, config_path = control
    first = Inbound("signal", "text", text="first", identity="1")
    second = Inbound("signal", "text", text="second", identity="2")
    bridge.add(first)
    bridge.add(second)
    review = bridge.operator
    review.finish(review._waiting[0])
    review.finish(review._waiting[0])
    displayed = review.snapshot(approved=True)
    bridge.acknowledge(first)
    assert not operator_request(
        config_path,
        "transmit",
        approved=True,
        revision=displayed["revision"],
        show_snapshot=False,
        timeout=2,
        log=Mock(),
    )
    assert not review.has_commands() and not review.dispatch_service()


def test_empty_approved_head_cannot_authorize_new_arrival(control):
    bridge, server, _ = control
    connection, response, snapshot = connect(server)
    try:
        assert snapshot["approved_item"] is None
        incoming = Inbound("signal", "text", text="new", identity="1")
        bridge.add(incoming)
        bridge.operator.finish(bridge.operator._waiting[0])
        connection.sendall(b'{"action": "transmit", "approved": true}\n')
        assert json.loads(response.readline())["ok"] is False
        assert not bridge.operator.has_commands() and not bridge.operator.dispatch_service()
    finally:
        response.close()
        connection.close()


@pytest.mark.parametrize("approved", [None, 1, "true", []])
def test_invalid_approved_view_never_queues_an_action(control, approved):
    bridge, server, _ = control
    bridge.add(Inbound("signal", "text", text="held", identity="1"))
    connection, response, _ = connect(server)
    try:
        connection.sendall(
            json.dumps({"action": "transmit", "approved": approved}).encode() + b"\n"
        )
        assert json.loads(response.readline())["ok"] is False
        assert not bridge.operator.has_commands()
    finally:
        response.close()
        connection.close()


@pytest.mark.parametrize("text", ["a" * 70000, "中" * 11000], ids=["ascii", "unicode"])
@pytest.mark.parametrize("action", ["status", "read", "approve", "deny"])
def test_long_text_can_be_reviewed_and_next_item_remains_accessible(
    control, monkeypatch, text, action
):
    bridge, _, config_path = control
    first = Inbound("signal", "text", text=text, identity="1")
    second = Inbound("signal", "text", text="next message", identity="2")
    bridge.add(first)
    bridge.add(second)
    review = bridge.operator
    queued, _ = watch_commands(monkeypatch, review)
    log = Mock()
    if action == "status":
        assert operator_request(config_path, action, timeout=2, log=log)
        assert not review.has_commands()
    else:
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(operator_request, config_path, action, timeout=2, log=log)
            assert queued.wait(2)
            command, item, stale = review.next_command()
            assert command == action and item.incoming is first and not stale
            if action == "read":
                review.respond("reply", text)
            else:
                review.finish(item)
                if action == "deny":
                    bridge.discard(first)
            review.complete_command(True)
            assert result.result(timeout=2)
    assert ("reply", text) in [call.args for call in log.call_args_list]
    expected = first if action in {"status", "read"} else second
    assert review.snapshot()["item"]["text"] == expected.text
    assert review.approved(first) is (action == "approve")
    log.reset_mock()
    assert operator_request(config_path, "status", timeout=2, log=log)
    assert ("reply", expected.text) in [call.args for call in log.call_args_list]


def test_panel_can_deny_long_text_and_reveal_next_item(control, monkeypatch):
    bridge, _, config_path = control
    first = Inbound("signal", "text", text="中" * 11000, identity="1")
    bridge.add(first)
    bridge.add(Inbound("signal", "text", text="next message", identity="2"))
    review = bridge.operator
    displayed = review.snapshot()
    queued, _ = watch_commands(monkeypatch, review)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(
            operator_request,
            config_path,
            "deny",
            timeout=2,
            revision=displayed["revision"],
            show_snapshot=False,
            log=Mock(),
        )
        assert queued.wait(2)
        command, item, stale = review.next_command()
        assert command == "deny" and item.incoming is first and not stale
        review.finish(item)
        bridge.discard(first)
        review.complete_command(True)
        assert result.result(timeout=2)
    assert review.snapshot()["item"]["text"] == "next message"
    assert bridge.peek("signal").identity == "2"


def test_long_cached_voice_transcript_is_complete_in_snapshot_and_preview(control, monkeypatch):
    bridge, _, config_path = control
    review = bridge.operator
    item = review.add("signal", audio=Mock())
    transcript = "voice words " * 7000
    review.mark_previewed(item, transcript)
    queued, _ = watch_commands(monkeypatch, review)
    log = Mock()
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(operator_request, config_path, "read", timeout=2, log=log)
        assert queued.wait(2)
        assert review.next_command() == ("read", item, False)
        review.respond("reply", f"Voice transcript: {transcript}")
        review.complete_command(True)
        assert result.result(timeout=2)
    assert [call.args for call in log.call_args_list].count(
        ("reply", f"Voice transcript: {transcript}")
    ) == 2
    assert review.snapshot()["waiting"] == 1


@pytest.mark.parametrize("phase", ["snapshot", "response"])
@pytest.mark.parametrize("payload", [b"", b'{"done": true, "ok": true}', b'{"done":'])
def test_incomplete_operator_response_is_reported_as_closed(monkeypatch, phase, payload):
    connection, peer = socket.socketpair()
    monkeypatch.setattr(operator_mode, "_operator_connection", lambda *args: connection)
    with peer:
        if phase == "response":
            peer.sendall(b'{"snapshot": {"item": null, "waiting": 0, "approved": 0}}\n')
        peer.sendall(payload)
        peer.shutdown(socket.SHUT_WR)
        with pytest.raises(WalkietalkError, match="connection closed; check status"):
            operator_request(None, "status", timeout=2, log=Mock())


@pytest.mark.parametrize("phase", ["snapshot", "response"])
def test_stalled_operator_response_obeys_command_timeout(monkeypatch, phase):
    connection, peer = socket.socketpair()
    connection.settimeout(2)
    monkeypatch.setattr(operator_mode, "_operator_connection", lambda *args: connection)
    with peer:
        if phase == "response":
            peer.sendall(b'{"snapshot": {"item": null, "waiting": 0, "approved": 0}}\n')
        started = time.monotonic()
        with pytest.raises(WalkietalkError, match="timed out.*check operator status"):
            operator_request(None, "status", timeout=0.05, log=Mock())
        assert time.monotonic() - started < 0.5


@pytest.mark.parametrize("action", ["read", "approve", "deny"])
def test_cli_actions_wait_for_main_loop_and_stream_its_response(control, monkeypatch, action):
    bridge, _, config_path = control
    item = Inbound("signal", "text", text="hello", identity="1")
    bridge.add(item)
    review = bridge.operator
    queued, _ = watch_commands(monkeypatch, review)
    log = Mock()
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(operator_request, config_path, action, timeout=2, log=log)
        assert queued.wait(2)
        assert not result.done() and not review.approved(item)
        assert review.next_command()[0:2] == (action, review._waiting[0])
        if action == "read":
            review.respond("reply", "reviewed text")
        else:
            review.finish(review._waiting[0])
        review.respond("status", "action completed")
        review.complete_command(True)
        assert result.result(timeout=2)
    assert ("status", "action completed") in [call.args for call in log.call_args_list]
    if action == "read":
        assert ("reply", "reviewed text") in [call.args for call in log.call_args_list]
        assert not review.approved(item)


def test_cli_failure_is_returned_and_keeps_item_held(control, monkeypatch):
    bridge, _, config_path = control
    item = Inbound("signal", "voice", audio_path=Path("unused.ogg"), identity="1")
    bridge.add(item)
    queued, _ = watch_commands(monkeypatch, bridge.operator)
    log = Mock()
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(operator_request, config_path, "read", timeout=2, log=log)
        assert queued.wait(2)
        bridge.operator.next_command()
        bridge.operator.respond("error", "Voice preview failed")
        bridge.operator.complete_command(False)
        assert result.result(timeout=2) is False
    assert ("error", "Voice preview failed") in [call.args for call in log.call_args_list]
    assert bridge.peek("signal") is item and not bridge.operator.approved(item)


def test_command_uses_head_shown_when_connection_opens(control, monkeypatch):
    bridge, server, _ = control
    first = Inbound("signal", "text", text="first", identity="1")
    second = Inbound("signal", "text", text="second", identity="2")
    bridge.add(first)
    bridge.add(second)
    queued, _ = watch_commands(monkeypatch, bridge.operator)
    connection, response, snapshot = connect(server)
    try:
        assert snapshot["item"]["text"] == "first"
        bridge.operator.announce()
        bridge.operator.submit("approve")
        bridge.operator.finish(bridge.operator.next_command()[1])
        bridge.operator.complete_command(True)
        queued.clear()
        request(connection, "approve")
        assert queued.wait(2)
        assert bridge.operator.next_command() == ("approve", None, True)
        bridge.operator.complete_command(False)
        assert json.loads(response.readline())["message"].startswith("Command queued")
        assert json.loads(response.readline()) == {"done": True, "ok": False}
        assert not bridge.operator.approved(second)
    finally:
        response.close()
        connection.close()


def test_empty_head_cannot_authorize_new_arrival(control):
    bridge, server, _ = control
    connection, response, snapshot = connect(server)
    try:
        assert snapshot["item"] is None
        item = Inbound("signal", "text", text="new", identity="1")
        bridge.add(item)
        request(connection, "approve")
        assert json.loads(response.readline())["ok"] is False
        assert not bridge.operator.has_commands() and not bridge.operator.approved(item)
    finally:
        response.close()
        connection.close()


def test_panel_revision_cannot_approve_head_changed_before_connecting(control):
    bridge, _, config_path = control
    first = bridge.operator.add("signal", text="first")
    bridge.operator.add("signal", text="second")
    displayed = bridge.operator.snapshot()
    bridge.operator.finish(first)
    log = Mock()
    assert not operator_request(
        config_path,
        "approve",
        timeout=2,
        revision=displayed["revision"],
        show_snapshot=False,
        log=log,
    )
    assert ("status", "Displayed item changed; review it again.") in [
        call.args for call in log.call_args_list
    ]
    assert not bridge.operator.has_commands()
    assert bridge.operator.snapshot()["item"]["text"] == "second"


@pytest.mark.parametrize("revision", [None, True, "1", [], {}])
def test_invalid_explicit_revision_never_queues_an_action(control, revision):
    bridge, _, config_path = control
    bridge.operator.add("signal", text="held")
    assert not operator_request(
        config_path, "approve", timeout=2, revision=revision, show_snapshot=False, log=Mock()
    )
    assert not bridge.operator.has_commands()


def test_status_shows_cached_transcript_and_conversation(control):
    bridge, _, config_path = control
    item = bridge.operator.add("signal", audio=Mock())
    bridge.operator.mark_previewed(item, "This is the cached transcript.")
    bridge.operator.status_provider = lambda: "asleep; waiting for wake"
    log = Mock()
    assert operator_request(config_path, "status", timeout=2, log=log)
    assert ("reply", "Voice transcript: This is the cached transcript.") in [
        call.args for call in log.call_args_list
    ]
    assert ("status", "Conversation: asleep; waiting for wake") in [
        call.args for call in log.call_args_list
    ]


@pytest.mark.parametrize("action", ["approve", "sleep"])
def test_disconnected_client_cancels_command_before_execution(control, monkeypatch, action):
    bridge, server, _ = control
    bridge.add(Inbound("signal", "text", text="held", identity="1"))
    queued, commands = watch_commands(monkeypatch, bridge.operator)
    connection, response, _ = connect(server)
    request(connection, action)
    assert queued.wait(2)
    assert "Command queued" in json.loads(response.readline())["message"]
    response.close()
    connection.close()
    assert commands[0].cancelled.wait(2)
    _, _, stale = bridge.operator.next_command()
    assert stale and not bridge.operator.approved(bridge.peek("signal"))
    bridge.operator.complete_command(False)


def test_timeout_reports_uncertainty_and_cancels_unstarted_command(control, monkeypatch):
    bridge, _, config_path = control
    bridge.add(Inbound("signal", "text", text="held", identity="1"))
    _, commands = watch_commands(monkeypatch, bridge.operator)
    with pytest.raises(WalkietalkError, match="check operator status before retrying"):
        operator_request(config_path, "approve", timeout=0.2, log=Mock())
    assert commands[0].cancelled.wait(2)
    assert bridge.operator.next_command()[2]
    bridge.operator.complete_command(False)


@pytest.mark.parametrize("action", ["bogus", [], None])
def test_invalid_command_never_reaches_main_loop(control, action):
    bridge, server, _ = control
    connection, response, _ = connect(server)
    try:
        request(connection, action)
        assert json.loads(response.readline())["ok"] is False
        assert not bridge.operator.has_commands()
    finally:
        response.close()
        connection.close()


def test_socket_shutdown_unblocks_pending_clients_and_cleans_endpoint(control, monkeypatch):
    bridge, server, _ = control
    bridge.add(Inbound("signal", "text", text="held", identity="1"))
    queued, commands = watch_commands(monkeypatch, bridge.operator)
    connection, response, _ = connect(server)
    try:
        request(connection, "read")
        assert queued.wait(2)
        response.readline()  # Queued acknowledgement.
        server.close()
        assert json.loads(response.readline())["ok"] is False
        assert commands[0].cancelled.wait(2)
        assert not server.path.exists() and not server._thread.is_alive()
    finally:
        response.close()
        connection.close()


def test_one_instance_per_config_and_private_socket(control):
    _, server, config_path = control
    assert stat.S_IMODE(server.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(server.path.parent.stat().st_mode) == 0o700
    other = OperatorServer(operator_mode.OperatorQueue(Config()), config_path)
    with pytest.raises(WalkietalkError, match="already running"):
        other.start()
    other.close()
    assert server.path.exists()
    assert operator_request(config_path, "status", timeout=2, log=Mock())


def test_second_talk_instance_is_rejected_before_opening_messaging(control, monkeypatch):
    from test_operator_mode import Talk

    _, server, config_path = control
    Talk(monkeypatch)
    monkeypatch.setattr(cli, "OperatorServer", OperatorServer)
    messaging = Mock(side_effect=AssertionError("messaging started before locking"))
    monkeypatch.setattr(cli, "open_messaging", messaging)
    assert cli.main(["-c", str(config_path), "talk", "--capture"]) == 1
    messaging.assert_not_called()
    assert server.path.exists()
    assert operator_request(config_path, "status", timeout=2, log=Mock())


def test_messaging_startup_failure_releases_reserved_config(runtime, monkeypatch):
    from test_operator_mode import Talk

    _, config_path = runtime
    Talk(monkeypatch)
    monkeypatch.setattr(cli, "OperatorServer", OperatorServer)
    monkeypatch.setattr(cli, "open_messaging", Mock(side_effect=OSError("startup failed")))
    assert cli.main(["-c", str(config_path), "talk", "--capture"]) == 1
    other = OperatorServer(None, config_path)
    try:
        other.acquire()
        assert not other.path.exists()
    finally:
        other.close()


def test_config_remains_locked_until_messaging_shutdown_finishes(runtime, monkeypatch):
    from test_operator_mode import Talk

    _, config_path = runtime
    talk = Talk(monkeypatch)
    monkeypatch.setattr(cli, "OperatorServer", OperatorServer)
    other = OperatorServer(None, config_path)

    def close_bridge():
        with pytest.raises(WalkietalkError, match="already running"):
            other.acquire()

    talk.bridge.close = Mock(side_effect=close_bridge)
    try:
        assert cli.main(["-c", str(config_path), "talk", "--capture"]) == 130
        talk.bridge.close.assert_called_once()
        other.acquire()
    finally:
        other.close()


def test_commands_without_panel_work_when_curses_is_unavailable():
    script = """
import importlib.abc
import sys
from types import SimpleNamespace
from unittest.mock import Mock

class NoCurses(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname in {"curses", "_curses"}:
            raise ModuleNotFoundError("curses unavailable")

sys.meta_path.insert(0, NoCurses())
from walkietalk import cli
cli.audio_devices = lambda: []
assert cli.main(["--no-env-file", "devices"]) == 0
cli.open_stt = lambda config: Mock(transcribe=Mock(return_value="charlotte hello"))
cli.capture_from_wav = lambda *args, **kwargs: SimpleNamespace(
    pcm=b"", rate=16000, started_at=None
)
assert cli.main(["--no-env-file", "talk", "unused.wav"]) == 0
assert "walkietalk.operator_panel" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ordinary_commands_import_without_unix_socket_support():
    script = """
import socket
import socketserver
del socket.AF_UNIX
del socketserver.UnixStreamServer
from walkietalk import cli
from walkietalk.config import WalkietalkError
from walkietalk.operator_mode import operator_directory
cli.audio_devices = lambda: []
assert cli.main(["--no-env-file", "devices"]) == 0
try:
    operator_directory()
except WalkietalkError as exc:
    assert "requires Unix domain sockets" in str(exc)
else:
    raise AssertionError("operator mode accepted unsupported platform")
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("invalid", ["permissions", "symlink", "owner", "missing", "relative"])
def test_operator_runtime_rejects_untrusted_directories(monkeypatch, tmp_path, invalid):
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    if invalid == "permissions":
        root.chmod(0o755)
    elif invalid == "symlink":
        link = tmp_path / "link"
        link.symlink_to(root, target_is_directory=True)
        root = link
    elif invalid == "owner":
        monkeypatch.setattr(operator_mode.os, "getuid", lambda: os.stat(root).st_uid + 1)
    elif invalid == "missing":
        root = tmp_path / "missing"
    elif invalid == "relative":
        monkeypatch.chdir(tmp_path)
        root = Path("runtime")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(root))
    with pytest.raises(WalkietalkError, match="operator runtime directory|Operator runtime"):
        operator_mode.operator_directory()


def test_operator_runtime_fallback_is_independent_of_tmpdir(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    exists = Path.exists
    system_runtime = Path(f"/run/user/{os.getuid()}")
    monkeypatch.setattr(Path, "exists", lambda path: path != system_runtime and exists(path))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "one"))
    first = operator_mode.operator_directory()
    monkeypatch.setenv("TMPDIR", str(tmp_path / "two"))
    assert operator_mode.operator_directory() == first
    assert first == home / ".cache" / "walkietalk" / "operator"
    assert stat.S_IMODE(first.stat().st_mode) == 0o700


def test_missing_or_different_config_reports_no_running_instance(control):
    _, _, config_path = control
    with pytest.raises(WalkietalkError, match="No operator-mode talk instance"):
        operator_request(config_path.with_name("another.yaml"), "read", timeout=2, log=Mock())


def test_socket_identity_resolves_config_symlinks(runtime):
    root, config_path = runtime
    alias = root / "alias.yaml"
    alias.symlink_to(config_path)
    assert operator_socket(alias) == operator_socket(config_path)


def test_stale_socket_is_recovered_but_unexpected_file_is_preserved(runtime):
    _, config_path = runtime
    path = operator_socket(config_path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
        stale.bind(str(path))
    server = OperatorServer(operator_mode.OperatorQueue(Config()), config_path)
    try:
        server.start()
        assert operator_request(config_path, "status", timeout=2, log=Mock())
    finally:
        server.close()
    path.write_text("keep this file")
    with pytest.raises(WalkietalkError, match="unexpected file"):
        OperatorServer(operator_mode.OperatorQueue(Config()), config_path).start()
    assert path.read_text() == "keep this file"


@pytest.mark.parametrize("action", [None, "status"])
@pytest.mark.parametrize("use_config", [False, True])
def test_actual_cli_status_runs_in_second_process_without_hardware(
    control, runtime, action, use_config
):
    bridge, _, config_path = control
    root, _ = runtime
    bridge.add(Inbound("signal", "text", text="hello from local queue", identity="1"))
    arguments = [sys.executable, "-m", "walkietalk", "--no-env-file"]
    if use_config:
        arguments.extend(["-c", str(config_path)])
    arguments.append("operator")
    if action:
        arguments.append(action)
    result = subprocess.run(
        arguments,
        env={**os.environ, "TMPDIR": str(root)},
        capture_output=True,
        text=True,
        timeout=5,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stderr
    assert "Item 1: incoming signal, Nana, text." in result.stdout
    assert "hello from local queue" in result.stdout
    assert not bridge.operator.has_commands()


@pytest.mark.parametrize("config_args", [[], ["-c", "unused"]])
def test_cli_validates_timeout_before_connecting(monkeypatch, config_args):
    monkeypatch.setattr(cli, "operator_request", lambda *a, **kw: pytest.fail("connected"))
    assert cli.main([*config_args, "operator", "read", "--timeout", "-1"]) == 1


@pytest.mark.parametrize(
    ("action", "voice_failure", "expected_code"),
    [("read", False, 0), ("approve", False, 0), ("deny", False, 0), ("read", True, 1)],
)
@pytest.mark.parametrize("use_config", [False, True])
def test_actual_cli_controls_live_talk_loop(
    monkeypatch, runtime, action, voice_failure, expected_code, use_config
):
    from test_operator_mode import CAPTURED, Talk

    root, config_path = runtime
    talk = Talk(monkeypatch)
    monkeypatch.setattr(cli, "OperatorServer", OperatorServer)
    monkeypatch.setattr(cli.sys, "stdin", Mock(spec=[], name="radio stdin stays unused"))
    talk.review.add(
        "signal",
        text="" if voice_failure else "held outgoing message",
        audio=CAPTURED if voice_failure else None,
    )
    if voice_failure:
        talk.listener.transcribe.side_effect = WalkietalkError("transcription unavailable")
    queued, _ = watch_commands(monkeypatch, talk.review)
    processes = []

    def launch(on_wait):
        arguments = [sys.executable, "-m", "walkietalk", "--no-env-file"]
        if use_config:
            arguments.extend(["-c", str(config_path)])
        arguments.extend(["operator", action, "--timeout", "5"])
        processes.append(
            subprocess.Popen(
                arguments,
                env={**os.environ, "TMPDIR": str(root)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                stdin=subprocess.DEVNULL,
            )
        )
        assert queued.wait(3)
        on_wait()

    def finished(on_wait):
        stdout, stderr = processes[0].communicate(timeout=5)
        assert processes[0].returncode == expected_code, stdout + stderr
        if voice_failure:
            assert "transcription unavailable" in stdout
            assert talk.review.snapshot()["waiting"] == 1
        elif action == "read":
            assert "held outgoing message" in stdout and "Preview complete" in stdout
            assert talk.review.snapshot()["waiting"] == 1
        else:
            assert talk.review.snapshot()["waiting"] == 0
        on_wait()

    talk.events = [launch, finished]
    try:
        assert cli.main(["-c", str(config_path), "talk", "--capture"]) == 130
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
    talk.transmit.assert_not_called()
    if action == "approve":
        talk.bridge.send_text.assert_called_once_with("signal", "held outgoing message")
    else:
        talk.bridge.send_text.assert_not_called()


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("transmit", [False, True])
def test_actual_transmit_cli_releases_one_message_in_sleeping_talk_loop(
    monkeypatch, runtime, approved, transmit
):
    from test_operator_mode import Talk

    root, config_path = runtime
    talk = Talk(monkeypatch)
    first = Inbound("signal", "text", text="requested message", identity="1")
    second = Inbound("signal", "text", text="held message", identity="2")
    talk.bridge.add(first)
    talk.bridge.add(second)
    if approved:
        talk.review.finish(talk.review._waiting[0])
    monkeypatch.setattr(cli, "OperatorServer", OperatorServer)
    queued, _ = watch_commands(monkeypatch, talk.review)
    processes = []

    def launch(on_wait):
        arguments = [sys.executable, "-m", "walkietalk", "--no-env-file", "operator", "transmit"]
        if approved:
            arguments.append("--approved")
        processes.append(
            subprocess.Popen(
                arguments,
                env={**os.environ, "TMPDIR": str(root)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                stdin=subprocess.DEVNULL,
            )
        )
        assert queued.wait(3)
        on_wait()

    def scheduled(on_wait):
        stdout, stderr = processes[0].communicate(timeout=5)
        assert processes[0].returncode == 0, stdout + stderr
        assert "requested message" in stdout and "One message scheduled" in stdout
        assert "held message" not in stdout
        on_wait()

    def delivered(on_wait):
        assert talk.bridge.peek("signal") is second and not talk.review.approved(second)
        assert talk.session.destination == "" and talk.session.awake_until is None
        on_wait()

    talk.events = [launch, scheduled, delivered]
    try:
        arguments = ["-c", str(config_path), "talk", "--capture"]
        if transmit:
            arguments.append("--transmit")
        assert cli.main(arguments) == 130
        assert talk.transmit.call_count == int(transmit)
        assert not operator_socket(config_path).exists()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)


@pytest.mark.parametrize("ok", [True, False])
@pytest.mark.parametrize("config_path", [None, Path("unused")])
def test_cli_returns_exit_status_from_main_loop_result(monkeypatch, ok, config_path):
    control = Mock(return_value=ok)
    monkeypatch.setattr(cli, "operator_request", control)
    for name in ("open_agent", "open_stt", "open_tts", "preflight"):
        monkeypatch.setattr(cli, name, lambda *a, **kw: pytest.fail("hardware or provider opened"))
    config_args = ["-c", str(config_path)] if config_path is not None else []
    assert cli.main([*config_args, "operator", "read"]) == (0 if ok else 1)
    control.assert_called_once_with(config_path, "read", timeout=120)


def test_actual_sleep_cli_closes_running_conversation_without_a_message(monkeypatch, runtime):
    from test_operator_mode import Talk

    root, config_path = runtime
    talk = Talk(monkeypatch)
    talk.session.decide("nana", talk.now[0])
    talk.session.complete_turn()
    monkeypatch.setattr(cli, "OperatorServer", OperatorServer)
    queued, _ = watch_commands(monkeypatch, talk.review)
    processes = []

    def launch(on_wait):
        processes.append(
            subprocess.Popen(
                [sys.executable, "-m", "walkietalk", "--no-env-file", "operator", "sleep"],
                env={**os.environ, "TMPDIR": str(root)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                stdin=subprocess.DEVNULL,
            )
        )
        assert queued.wait(3)
        on_wait()

    def slept(on_wait):
        stdout, stderr = processes[0].communicate(timeout=5)
        assert processes[0].returncode == 0, stdout + stderr
        assert "Sleep requested by operator" in stdout
        assert "No message waiting" not in stdout
        assert talk.session.destination == "" and talk.session.awake_until is None
        on_wait()

    talk.events = [launch, slept, "nearby chatter"]
    try:
        assert cli.main(["-c", str(config_path), "talk", "--capture"]) == 130
        talk.transmit.assert_not_called()
        assert not operator_socket(config_path).exists()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)


def test_discovery_ignores_stale_sockets_and_other_files(control):
    bridge, server, _ = control
    directory = server.path.parent
    stale_path = directory / "stale.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
        stale.bind(str(stale_path))
    regular_path = directory / "regular.sock"
    regular_path.write_text("keep this file")
    alias = directory / "alias.sock"
    alias.symlink_to(server.path)
    assert operator_request(None, "status", timeout=2, log=Mock())
    assert not bridge.operator.has_commands()
    assert stale_path.exists() and regular_path.read_text() == "keep this file"
    assert alias.is_symlink()


def test_discovery_reports_when_no_instance_is_running(runtime):
    with pytest.raises(WalkietalkError, match="No operator-mode talk instance is running"):
        operator_request(None, "status", timeout=2, log=Mock())


def test_discovery_does_not_choose_arbitrarily_between_live_instances(control):
    bridge, _, config_path = control
    other_review = operator_mode.OperatorQueue(Config(signal=MODE))
    other_path = config_path.with_name("another.yaml")
    other = OperatorServer(other_review, other_path)
    bridge.operator.add("signal", text="first instance")
    other_review.add("signal", text="second instance")
    other.start()
    try:
        with pytest.raises(WalkietalkError, match="More than one.*Use --config"):
            operator_request(None, "approve", timeout=2, log=Mock())
        assert not bridge.operator.has_commands() and not other_review.has_commands()
        log = Mock()
        assert operator_request(config_path, "status", timeout=2, log=log)
        assert ("reply", "first instance") in [call.args for call in log.call_args_list]
    finally:
        other.close()
