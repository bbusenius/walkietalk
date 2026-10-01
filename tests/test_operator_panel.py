"""Fixed terminal controls, including an actual curses/PTY run with fake capture."""

import curses
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from walkietalk import cli, operator_panel
from walkietalk.config import Config, MessagingMode, WalkietalkError
from walkietalk.messaging import Inbound
from walkietalk.operator_mode import OperatorQueue
from walkietalk.operator_panel import OperatorPanel, wrap_lines
from walkietalk.term import emit, route_logs

MODE = MessagingMode(wake="nana", to="123", sender_alias="Nana")


def review():
    return OperatorQueue(Config(messaging_operator_mode=True, signal=MODE))


class Screen:
    def __init__(self, rows=24, columns=80):
        self.size = rows, columns
        self.lines = {}

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.lines.clear()

    def border(self):
        pass

    def hline(self, *args):
        pass

    def addstr(self, row, column, text, attributes):
        assert row < self.size[0] and column + len(text) < self.size[1]
        self.lines[row] = text

    def refresh(self):
        pass


@pytest.fixture
def screen(monkeypatch):
    monkeypatch.setattr(curses, "ACS_HLINE", ord("-"), raising=False)
    return Screen()


def test_fixed_layout_counts_identity_and_live_meter(screen):
    queue = review()
    queue.status_provider = lambda: "asleep; waiting for wake"
    queue.add("signal", text="I'll be there around four.")
    queue.record_log("event", "Listening on capture")
    queue.record_log("meter", "RMS 0.003 (threshold 0.020)")
    queue.record_log("meter", "RMS 0.004 (threshold 0.020)")
    panel = OperatorPanel(queue, Path("unused"))
    assert panel._draw(screen, queue.snapshot()) == 4
    text = "\n".join(screen.lines.values())
    for expected in (
        "Radio log",
        "Operator",
        "asleep",
        "Waiting for review: 1",
        "Approved for delivery: 0",
        "Item 1 | Outgoing | Signal | Nana | Text",
        "I'll be there around four.",
        "[R] Read",
        "[A] Approve",
        "[D] Deny",
    ):
        assert expected in text
    assert "Listening on capture" in text and "RMS 0.004" in text
    assert "RMS 0.003" not in text
    assert "[S] Sleep" in screen.lines[22]


def test_long_message_and_transcript_can_be_scrolled_and_resize_keeps_all_words(screen):
    queue = review()
    item = queue.add("signal", audio=Mock())
    transcript = "\n".join(f"Transcript line {i}" for i in range(30))
    queue.mark_previewed(item, transcript)
    panel = OperatorPanel(queue, Path("unused"))
    snapshot = queue.snapshot()
    rows = panel._draw(screen, snapshot)
    assert "Transcript line 0" in screen.lines.values()
    assert "Transcript line 29" not in screen.lines.values()
    panel._dispatch(curses.KEY_END, snapshot, rows)
    panel._draw(screen, snapshot)
    assert "Transcript line 29" in screen.lines.values()
    assert "Transcript line 0" not in screen.lines.values()
    screen.size = 30, 100
    panel._draw(screen, snapshot)
    assert panel._body == transcript.splitlines()
    assert panel._scroll == 0
    screen.size = 10, 40
    assert panel._draw(screen, snapshot) == 0
    assert "Resize" in screen.lines[0]


def test_voice_approval_is_disabled_until_preview(monkeypatch):
    queue = review()
    item = queue.add("signal", audio=Mock())
    request = Mock(return_value=True)
    monkeypatch.setattr(operator_panel, "operator_request", request)
    panel = OperatorPanel(queue, Path("unused"))
    panel._dispatch(ord("A"), queue.snapshot(), 4)
    assert "Press R" in panel._result[1]
    request.assert_not_called()
    queue.mark_previewed(item, "The voice message transcript.")
    panel._dispatch(ord("A"), queue.snapshot(), 4)
    panel._worker.join(timeout=2)
    assert not panel._worker.is_alive()
    request.assert_called_once()
    assert request.call_args.kwargs["revision"] == queue.snapshot()["revision"]
    assert not request.call_args.kwargs["show_snapshot"]


@pytest.mark.parametrize("approved", [False, True])
def test_transmit_shortcut_uses_the_displayed_incoming_item_and_view(screen, monkeypatch, approved):
    queue = review()
    queue.add("signal", incoming=Inbound("signal", "text", text="incoming reply", identity="1"))
    if approved:
        queue.finish(queue._waiting[0])
        queue.add("signal", text="different outgoing review head")
    request = Mock(return_value=True)
    monkeypatch.setattr(operator_panel, "operator_request", request)
    panel = OperatorPanel(queue, Path("unused"))
    displayed = queue.snapshot(approved=approved)
    rows = panel._draw(screen, displayed)
    assert "incoming reply" in screen.lines.values()
    assert "[T] Transmit" in "\n".join(screen.lines.values())
    panel._dispatch(ord("T"), displayed, rows)
    panel._worker.join(timeout=2)
    assert request.call_args.args == (Path("unused"), "transmit")
    assert request.call_args.kwargs["revision"] == displayed["revision"]
    assert request.call_args.kwargs.get("approved", False) is approved


def test_tab_selects_approved_messages_and_all_controls_fit_minimum_terminal(screen):
    queue = review()
    queue.add("signal", incoming=Inbound("signal", "text", text="approved reply", identity="1"))
    queue.finish(queue._waiting[0])
    queue.add("signal", text="outgoing review head")
    panel = OperatorPanel(queue, Path("unused"))
    screen.size = 20, 60
    displayed = queue.snapshot()
    rows = panel._draw(screen, displayed)
    assert "outgoing review head" in screen.lines.values()
    assert "[T] Transmit" in screen.lines[17]
    assert "[S] Sleep" in screen.lines[18] and "[Ctrl+C] Stop" in screen.lines[18]
    panel._dispatch(9, displayed, rows)
    assert panel._approved_view
    panel._draw(screen, queue.snapshot(approved=panel._approved_view))
    assert "approved reply" in screen.lines.values()
    assert "Tab: Review" in "\n".join(screen.lines.values())


def test_transmit_on_empty_review_points_to_approved_messages(screen, monkeypatch):
    queue = review()
    incoming = Inbound("signal", "text", text="approved reply", identity="1")
    queue.add("signal", incoming=incoming)
    queue.finish(queue._waiting[0])
    request = Mock()
    monkeypatch.setattr(operator_panel, "operator_request", request)
    panel = OperatorPanel(queue, Path("unused"))
    displayed = queue.snapshot()
    rows = panel._draw(screen, displayed)
    panel._dispatch(ord("T"), displayed, rows)
    panel._draw(screen, displayed)
    assert "Press Tab to Approved, then T to transmit." in screen.lines.values()
    request.assert_not_called()
    assert queue.approved(incoming) and not queue.dispatch_service()


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("content", ["empty", "outgoing", "voice"])
@pytest.mark.parametrize("columns", [60, 80])
def test_sleep_shortcut_works_in_either_view_without_a_preview(
    screen, monkeypatch, approved, content, columns
):
    queue = review()
    if content == "outgoing":
        queue.add("signal", text="held outgoing")
    elif content == "voice":
        queue.add(
            "signal",
            incoming=Inbound("signal", "voice", audio_path=Path("voice.ogg"), identity="1"),
        )
    request = Mock(return_value=True)
    monkeypatch.setattr(operator_panel, "operator_request", request)
    panel = OperatorPanel(queue, Path("unused"))
    screen.size = 20, columns
    displayed = queue.snapshot(approved=approved)
    rows = panel._draw(screen, displayed)
    assert "[S] Sleep" in screen.lines[18]
    panel._dispatch(ord("S"), displayed, rows)
    panel._worker.join(timeout=2)
    request.assert_called_once()
    assert request.call_args.args == (Path("unused"), "sleep")
    assert request.call_args.kwargs["revision"] is None
    assert not request.call_args.kwargs.get("approved", False)


@pytest.mark.parametrize("release", ["transmit", "wake"])
def test_playback_failure_stays_visible_and_explicit_transmit_can_retry(
    screen, monkeypatch, capsys, release
):
    from test_operator_mode import Talk

    transmit_with_callsign = cli.transmit_with_callsign
    talk = Talk(monkeypatch)
    incoming = Inbound("signal", "text", text="Ok", identity="1")
    talk.bridge.add(incoming)
    panel = OperatorPanel(talk.review, Path("unused"))
    monkeypatch.setattr(panel, "start", Mock())
    monkeypatch.setattr(panel, "close", Mock())
    monkeypatch.setattr(operator_panel, "validate_terminal", lambda: None)
    monkeypatch.setattr(operator_panel, "OperatorPanel", lambda *args: panel)
    monkeypatch.setattr(cli, "transmit_with_callsign", transmit_with_callsign)
    playback = Mock()
    playback.prepare.side_effect = [
        WalkietalkError("Audio preparation failed; PTT was not asserted: output device found 0"),
        None,
    ]
    monkeypatch.setattr(cli, "Playback", Mock(return_value=playback))
    ptt = Mock()
    monkeypatch.setattr(cli, "SerialPTT", ptt)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)

    def failed(on_wait):
        assert talk.bridge.peek("signal") is incoming
        assert not talk.review.approved(incoming) and not talk.review.dispatch_service()
        ptt.assert_not_called()
        playback.play.assert_not_called()
        # The socket worker can report scheduling after actual playback has failed.
        panel._progress("status", "One message scheduled for operator transmission.")
        panel._draw(screen, talk.review.snapshot())
        assert "Delivery failed; retained for review. See radio log." in screen.lines.values()
        on_wait()
        assert playback.prepare.call_count == 1  # No automatic retry.

    def completed(on_wait):
        assert not talk.bridge.has("signal")
        ptt.assert_called_once()
        playback.play.assert_called_once()
        panel._progress("status", "One message scheduled for operator transmission.")
        panel._draw(screen, talk.review.snapshot())
        assert "Message transmitted; conversation unchanged." in screen.lines.values()
        on_wait()

    initial = (
        [talk.command("transmit"), talk.tick()]
        if release == "transmit"
        else [talk.command("approve"), "nana"]
    )
    talk.events = initial + [failed, talk.command("transmit"), talk.tick(5), completed]
    assert cli.main(["-c", "unused", "talk", "--capture", "--transmit", "--panel"]) == 130
    output = capsys.readouterr()
    assert "PTT was not asserted and nothing was transmitted" in output.out
    assert "partial transmission may have been heard" not in output.out


def test_transmit_shortcut_requires_voice_preview_and_rejects_outgoing_messages(monkeypatch):
    queue = review()
    incoming = Inbound("signal", "voice", audio_path=Path("unused.ogg"), identity="1")
    queue.add("signal", incoming=incoming)
    request = Mock(return_value=True)
    monkeypatch.setattr(operator_panel, "operator_request", request)
    panel = OperatorPanel(queue, Path("unused"))
    panel._dispatch(ord("t"), queue.snapshot(), 4)
    assert "Press R" in panel._result[1]
    request.assert_not_called()
    queue.forget(incoming)
    queue.add("signal", text="outgoing message")
    panel._dispatch(ord("t"), queue.snapshot(), 4)
    assert "incoming" in panel._result[1]
    request.assert_not_called()


def test_visible_capture_transcript_can_be_approved_without_read(screen, monkeypatch):
    queue = review()
    queue.add("signal", audio=Mock(), text="Nana, I'll be there at four.")
    request = Mock(return_value=True)
    monkeypatch.setattr(operator_panel, "operator_request", request)
    panel = OperatorPanel(queue, Path("unused"))
    displayed = queue.snapshot()
    rows = panel._draw(screen, displayed)
    assert "Nana, I'll be there at four." in screen.lines.values()
    assert "Ready for review" in screen.lines.values()
    panel._dispatch(ord("a"), displayed, rows)
    panel._worker.join(timeout=2)
    assert not panel._worker.is_alive()
    assert request.call_args.args == (Path("unused"), "approve")
    assert request.call_args.kwargs["revision"] == displayed["revision"]


def test_voice_caption_is_not_displayed_as_a_transcript_or_allowed_for_approval(
    screen, monkeypatch
):
    queue = review()
    queue.add(
        "signal", incoming=Inbound("signal", "voice", text="Attachment caption", identity="1")
    )
    request = Mock()
    monkeypatch.setattr(operator_panel, "operator_request", request)
    panel = OperatorPanel(queue, Path("unused"))
    displayed = queue.snapshot()
    rows = panel._draw(screen, displayed)
    assert "Attachment caption" not in "\n".join(screen.lines.values())
    assert "Voice message. Press R to read its transcript." in screen.lines.values()
    panel._dispatch(ord("a"), displayed, rows)
    request.assert_not_called()


def test_busy_panel_ignores_additional_actions_and_surfaces_progress(monkeypatch):
    queue = review()
    queue.add("signal", text="held")
    request = Mock()
    monkeypatch.setattr(operator_panel, "operator_request", request)
    panel = OperatorPanel(queue, Path("unused"))
    panel._busy = True
    panel._dispatch(ord("d"), queue.snapshot(), 4)
    request.assert_not_called()
    panel._progress("status", "Waiting for the radio to be idle.")
    assert panel._result[1] == "Waiting for the radio to be idle."


def test_output_restored_when_curses_initialization_fails(monkeypatch):
    stdout, stderr = sys.stdout, sys.stderr
    monkeypatch.setattr(curses, "wrapper", Mock(side_effect=RuntimeError("terminal failed")))
    panel = OperatorPanel(review(), Path("unused"))
    try:
        with pytest.raises(WalkietalkError, match="terminal failed"):
            panel.start()
        assert sys.stdout is stdout and sys.stderr is stderr
    finally:
        panel.close()
    assert sys.stdout is stdout and sys.stderr is stderr
    assert not panel._thread.is_alive()


def test_panel_failure_stops_talk_and_cleans_controls_without_delivery(monkeypatch):
    from test_operator_mode import Talk

    talk = Talk(monkeypatch)
    item = talk.review.add("signal", text="held")
    talk.review.finish(item)
    talk.session.decide("nana", talk.now[0])
    talk.session.complete_turn()
    panel = Mock()
    panel.closed = SimpleNamespace(is_set=lambda: True)
    panel.check.side_effect = WalkietalkError("Operator panel closed: terminal failed")
    monkeypatch.setattr(operator_panel, "validate_terminal", lambda: None)
    monkeypatch.setattr(operator_panel, "OperatorPanel", lambda *a: panel)
    assert cli.main(["-c", "unused", "talk", "--capture", "--transmit", "--panel"]) == 1
    panel.close.assert_called_once()
    talk.controls.close.assert_called_once()
    talk.transmit.assert_not_called()
    assert talk.captures == 0


def test_all_python_output_routes_to_logs_and_regular_logging_is_restored(capsys):
    queue = review()
    stream = operator_panel._LogStream(sys.stdout, queue.record_log, "event")
    stream.write("PTT ")
    stream.write("OFF\n")
    with route_logs(queue.record_log):
        emit("reply", "Reviewed message")
    emit("status", "Normal output")
    assert queue.recent_logs() == [("event", "PTT OFF"), ("reply", "Reviewed message")]
    assert capsys.readouterr().out == "Normal output\n"


def test_wrapping_preserves_words_newlines_and_wide_characters():
    text = "A long sentence with words.\n中文中文中文\n\nThe end."
    lines = wrap_lines(text, 8)
    assert "".join(lines).replace(" ", "") == text.replace("\n", "").replace(" ", "")
    assert "" in lines
    assert wrap_lines("中文", 1) == ["中", "文"]
    assert wrap_lines("\x1b[31mred\x1b[0m\x00", 20) == ["red"]


@pytest.mark.parametrize(
    "text", ["hello world again", "hello  world   again", "hello world again "]
)
def test_wrapping_at_word_boundary_does_not_insert_blank_rows(text):
    assert wrap_lines(text, 5) == ["hello", "world", "again"]
    assert wrap_lines("hello\n\nworld", 5) == ["hello", "", "world"]


def test_idle_redraw_reuses_logs_and_invalidates_on_meter_rollover_and_resize(screen, monkeypatch):
    queue = review()
    for number in range(499):
        queue.record_log("event", f"entry {number}")
    queue.record_log("meter", "RMS 0.001")
    panel = OperatorPanel(queue, Path("unused"))
    wrapping = Mock(wraps=wrap_lines)
    copying = Mock(wraps=queue.changed_logs)
    monkeypatch.setattr(operator_panel, "wrap_lines", wrapping)
    monkeypatch.setattr(queue, "changed_logs", copying)
    snapshot = queue.snapshot()
    panel._draw(screen, snapshot)
    calls = wrapping.call_count
    assert "RMS 0.001" in screen.lines.values()
    panel._draw(screen, snapshot)
    assert wrapping.call_count == calls
    assert copying.call_args.args == (queue._log_revision,)
    queue.record_log("meter", "RMS 0.002")  # Replaces the meter at the same log count.
    panel._draw(screen, snapshot)
    assert wrapping.call_count > calls
    assert "RMS 0.002" in screen.lines.values()
    queue.record_log("event", "rolled over")  # The bounded deque still has 500 entries.
    panel._draw(screen, snapshot)
    assert "rolled over" in screen.lines.values()
    screen.size = (30, 60)
    calls = wrapping.call_count
    panel._draw(screen, snapshot)
    assert wrapping.call_count > calls


def test_native_logs_capture_both_descriptors_and_restore_on_failure(capfd):
    queue = review()
    before = [os.fstat(descriptor) for descriptor in (1, 2)]
    with (
        pytest.raises(RuntimeError, match="display failed"),
        operator_panel._native_logs(queue.record_log),
    ):
        os.write(1, b"native stdout\n")
        os.write(2, b"native stderr\npartial stderr")
        raise RuntimeError("display failed")
    assert ("event", "native stdout") in queue.recent_logs()
    assert ("error", "native stderr") in queue.recent_logs()
    assert ("error", "partial stderr") in queue.recent_logs()
    captured = capfd.readouterr()
    assert captured.out == captured.err == ""
    after = [os.fstat(descriptor) for descriptor in (1, 2)]
    assert [(s.st_dev, s.st_ino) for s in after] == [(s.st_dev, s.st_ino) for s in before]
    os.write(1, b"restored stdout\n")
    os.write(2, b"restored stderr\n")
    captured = capfd.readouterr()
    assert captured.out == "restored stdout\n" and captured.err == "restored stderr\n"


@pytest.mark.parametrize("enabled,tty", [(False, True), (True, False)])
def test_panel_requirements_fail_before_any_provider_or_device(monkeypatch, enabled, tty):
    monkeypatch.setattr(cli, "load_config", lambda path: Config(messaging_operator_mode=enabled))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: tty))
    monkeypatch.setattr(
        sys, "stdout", SimpleNamespace(isatty=lambda: tty, write=lambda s: None, flush=lambda: None)
    )
    monkeypatch.setattr(cli, "open_agent", lambda config: pytest.fail("provider opened"))
    monkeypatch.setattr(cli, "preflight", lambda *a, **kw: pytest.fail("device opened"))
    with pytest.raises(WalkietalkError, match="operator_mode" if not enabled else "interactive"):
        cli.talk_command(cli.parser().parse_args(["-c", "unused", "talk", "--capture", "--panel"]))


PANEL_PROGRAM = """
import fcntl, os, sys, termios, time
from pathlib import Path
from types import SimpleNamespace
from walkietalk import cli
from walkietalk.config import Config, MessagingMode
from walkietalk.messaging import Inbound, MessageBridge
from walkietalk.audio import Wav

fcntl.ioctl(0, termios.TIOCSCTTY, 0)
before = termios.tcgetattr(0)
config = Config(messaging_operator_mode=True,
                signal=MessagingMode(wake="nana", to="123", sender_alias="Nana"))
bridge = MessageBridge(config)
as_voice = sys.argv[2] == "voice"
release = sys.argv[3]
sent = []
if as_voice:
    recording = Wav(b"\\x01\\x00" * 1600, 16000, 0.1)
    bridge.operator.add("signal", text="FIRSTMESSAGE", audio=recording)
    def send_voice(service, audio):
        assert service == "signal" and audio is recording
        sent.append(audio)
    bridge.send_voice = send_voice
else:
    bridge.add(Inbound("signal", "text", text="FIRSTMESSAGE", identity="1"))
bridge.add(Inbound("signal", "text", text="SECONDMESSAGE", identity="2"))
backend = SimpleNamespace(label=lambda: "test stub", prepare=lambda: None)
cli.load_config = lambda path: config
cli.open_agent = lambda config: backend
cli.open_stt = lambda config: backend
cli.open_messaging = lambda config: bridge
cli.preflight = lambda *a, **kw: None
session = cli.ListeningSession(config)
session.decide("nana", time.monotonic())
session.complete_turn()
cli.ListeningSession = lambda config: session
native_written = False
def capture(*a, on_wait, log, **kw):
    global native_written
    assert not os.isatty(1) and not os.isatty(2)
    if not native_written:
        os.write(1, b"NATIVE_STDOUT\\n")
        os.write(2, b"NATIVE_STDERR\\n")
        native_written = True
    log("Listening on fake capture")
    while True:
        log("RMS 0.003 (threshold 0.020)")
        on_wait()
        time.sleep(0.02)
cli.capture_from_device = capture
result = cli.main(["-c", sys.argv[1], "talk", "--capture", "--panel"])
after = termios.tcgetattr(0)
assert result == 130, result
assert before == after, (before, after)
assert os.isatty(1) and os.isatty(2)
assert ("event", "NATIVE_STDOUT") in bridge.operator.recent_logs()
assert ("error", "NATIVE_STDERR") in bridge.operator.recent_logs()
assert bridge.operator.snapshot()["waiting"] == 0
assert bridge.operator.snapshot()["approved"] == (0 if as_voice or release != "approve" else 1)
assert not as_voice or sent == [recording]
print("TERMINAL_RESTORED", flush=True)
"""


@pytest.mark.parametrize(
    "voice,release",
    [
        (False, "approve"),
        (True, "approve"),
        (False, "transmit"),
        (False, "approved-transmit"),
    ],
)
def test_actual_panel_shortcuts_resize_and_ctrl_c_restore_terminal(voice, release):
    """Exercise the real talk loop and socket commands, without devices or accounts."""
    with tempfile.TemporaryDirectory(prefix="wt-panel-test-") as directory:
        config_path = Path(directory) / "config.yaml"
        config_path.write_text("messaging:\n  operator_mode: true\n")
        script = Path(directory) / "panel_demo.py"
        script.write_text(PANEL_PROGRAM)
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        process = subprocess.Popen(
            [sys.executable, str(script), str(config_path), "voice" if voice else "text", release],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            env={**os.environ, "TERM": "xterm-256color", "TMPDIR": directory},
        )
        os.close(slave)
        output = bytearray()
        cursor = 0

        def expect(text, *, after=None):
            nonlocal cursor
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                position = output.find(text.encode(), cursor if after is None else after)
                if position >= 0:
                    cursor = max(cursor, position + len(text))
                    return
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError:
                        break
            pytest.fail(f"Panel did not show {text!r}: {output.decode(errors='replace')}")

        try:
            expect("FIRSTMESSAGE")
            expect("Ready for review")
            os.write(master, b"s")
            expect("Asleep; queued messages retained.")
            if not voice:
                os.write(master, b"r")
                expect("Preview complete")
                expect("Read [A] Approve")
            release_start = cursor
            os.write(master, b"t" if release == "transmit" else b"a")
            expect("SECONDMESSAGE")
            if release == "approved-transmit":
                os.write(master, b"\t")
                expect("Approved; T transmits")
                os.write(master, b"t")
                expect("Approved queue empty")
                os.write(master, b"\t")
                expect("Ready for review")
            elif release == "transmit":
                expect("Operator delivery complete", after=release_start)
            # Resizing must leave the controls usable and the radio loop running.
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
            os.kill(process.pid, signal.SIGWINCH)
            expect("SECONDMESSAGE")
            os.write(master, b"d")
            expect("Review queue empty")
            os.write(master, b"s")
            expect("Asleep; queued messages retained.")
            os.write(master, b"\x03")
            expect("TERMINAL_RESTORED")
            assert process.wait(timeout=5) == 0
            assert not list((Path(directory) / f"walkietalk-operator-{os.getuid()}").glob("*.sock"))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            os.close(master)


@pytest.mark.parametrize("chained", [False, True])
def test_display_failure_restores_native_streams_and_terminal(tmp_path, chained):
    script = tmp_path / "panel_failure.py"
    script.write_text("""
import fcntl, os, sys, termios
from pathlib import Path
from walkietalk.config import Config, WalkietalkError
from walkietalk.operator_mode import OperatorQueue
from walkietalk.operator_panel import OperatorPanel

fcntl.ioctl(0, termios.TIOCSCTTY, 0)
before = termios.tcgetattr(0)
panel = OperatorPanel(OperatorQueue(Config()), Path("unused"))
def nested(screen):
    raise ValueError("nested failure")
def fail(screen):
    assert not os.isatty(1) and not os.isatty(2)
    if sys.argv[1] == "chained":
        try:
            nested(screen)
        except ValueError as exc:
            raise RuntimeError("display failed") from exc
    raise RuntimeError("display failed")
panel._display = fail
try:
    panel.start()
except WalkietalkError as exc:
    assert "display failed" in str(exc)
else:
    raise AssertionError("Expected display failure")
panel.close()
assert termios.tcgetattr(0) == before
assert os.isatty(1) and os.isatty(2)
print("FAILURE_RESTORED", flush=True)
""")
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, str(script), "chained" if chained else "direct"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    os.close(slave)
    output = bytearray()
    try:
        returncode = process.wait(timeout=5)
        while select.select([master], [], [], 0.1)[0]:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
        assert returncode == 0, output.decode(errors="replace")
        assert b"FAILURE_RESTORED" in output, output.decode(errors="replace")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master)
