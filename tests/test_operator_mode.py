"""Approval boundaries and delivery timing without accounts, providers, or radio hardware."""

import threading
import weakref
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
import yaml

from walkietalk import cli, operator_mode
from walkietalk.audio import Wav
from walkietalk.callsign import CallsignSession
from walkietalk.capture import Utterance
from walkietalk.config import Config, MessagingMode, WalkietalkError, load_config
from walkietalk.messaging import Inbound, MessageBridge, MessagePlayback
from walkietalk.ptt import SerialPTT
from walkietalk.wake import ListeningSession

PCM = Wav(b"\x01\x00" * 4800, 48000, 0.1)
CAPTURED = Wav(b"\x02\x00" * 1600, 16000, 0.1)
MODE = MessagingMode(wake="nana", to="123", sender_alias="Nana", listening_mode="conversation")


@pytest.mark.parametrize("value", [False, True, "omitted", "section omitted"])
def test_config_operator_mode_defaults_and_boolean(tmp_path, value):
    data = yaml.safe_load(Path("src/walkietalk/data/config.example.yaml").read_text())
    if value == "section omitted":
        del data["messaging"]
    elif value == "omitted":
        data["messaging"].pop("operator_mode")
    else:
        data["messaging"]["operator_mode"] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    assert load_config(path).messaging_operator_mode is (value is True)


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_config_operator_mode_rejects_non_boolean(tmp_path, value):
    data = yaml.safe_load(Path("src/walkietalk/data/config.example.yaml").read_text())
    data["messaging"]["operator_mode"] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WalkietalkError, match="messaging.operator_mode must be true or false"):
        load_config(path)


def test_review_is_fifo_across_services_and_buffered_commands_stay_bound():
    config = Config(messaging_operator_mode=True, whatsapp=MODE, signal=MODE)
    bridge = MessageBridge(config)
    review = bridge.operator
    first = Inbound("signal", "text", text="first", identity="1")
    second = Inbound("whatsapp", "text", text="second", identity="2")
    bridge.add(first)
    outgoing = review.add("signal", text="third")
    bridge.add(second)
    bridge.add(first)  # Duplicate service events must not create another review item.
    review.announce()
    review.submit("approve")
    review.submit("approve")
    command, item, stale = review.next_command()
    assert command == "approve" and not stale and item.incoming is first
    review.finish(item)
    assert review.approved(first)
    assert review.next_command() == ("approve", None, True)
    review.announce()
    review.submit("read")
    assert review.next_command() == ("read", outgoing, False)
    review.finish(outgoing)
    review.announce()
    review.submit("deny")
    _, item, stale = review.next_command()
    assert not stale and item.incoming is second


def test_failure_invalidates_buffered_commands_for_same_item():
    config = Config(messaging_operator_mode=True, signal=MODE)
    bridge = MessageBridge(config)
    incoming = Inbound("signal", "text", text="hello", identity="1")
    bridge.add(incoming)
    review = bridge.operator
    review.announce()
    review.submit("approve")
    _, item, _ = review.next_command()
    review.finish(item)
    review.hold(incoming)
    review.announce()
    review.submit("approve")
    review.submit("approve")
    _, item, _ = review.next_command()
    review.finish(item)
    review.hold(incoming)
    assert review.next_command() == ("approve", None, True)
    assert not review.approved(incoming)


@pytest.mark.parametrize("action", ["approve", "transmit"])
def test_new_transcript_invalidates_decisions_from_unread_snapshot(action):
    bridge = MessageBridge(Config(messaging_operator_mode=True, signal=MODE))
    bridge.add(Inbound("signal", "voice", audio_path=Path("voice.ogg"), identity="1"))
    review = bridge.operator
    displayed = review.snapshot()
    review.submit("read", revision=displayed["revision"])
    review.submit(action, revision=displayed["revision"])
    _, item, stale = review.next_command()
    assert not stale and not item.previewed
    review.mark_previewed(item, "The message's actual words.")
    review.complete_command(True)
    assert review.next_command() == (action, None, True)
    review.complete_command(False)
    fresh = review.snapshot()
    review.submit(action, revision=fresh["revision"])
    # Reading cached content does not invalidate a decision made after seeing it.
    review.mark_previewed(item, item.transcript)
    assert review.next_command() == (action, item, False)


@pytest.mark.parametrize("change", ["hold", "forget"])
@pytest.mark.parametrize("action", ["approve", "deny"])
def test_review_decision_survives_changes_behind_its_head(change, action):
    bridge = MessageBridge(Config(messaging_operator_mode=True, signal=MODE, whatsapp=MODE))
    first = Inbound("signal", "text", text="first", identity="1")
    later = Inbound("whatsapp", "text", text="later", identity="2")
    bridge.add(first)
    bridge.add(later)
    review = bridge.operator
    head, other = review._waiting
    review.finish(head)
    review.finish(other)
    review.hold(first)
    displayed = review.snapshot()
    review.submit(action, revision=displayed["revision"])
    # Return an approved later item to review, or remove that later waiting item.
    review.hold(later)
    if change == "forget":
        bridge.discard(later)
    assert review.snapshot()["revision"] == displayed["revision"]
    assert review.next_command() == (action, head, False)


def test_denial_can_remove_reply_behind_approved_sleeping_reply():
    bridge = MessageBridge(Config(messaging_operator_mode=True, signal=MODE))
    first = Inbound("signal", "text", text="first", identity="1")
    second = Inbound("signal", "text", text="second", identity="2")
    bridge.add(first)
    bridge.add(second)
    review = bridge.operator
    review.announce()
    review.submit("approve")
    review.finish(review.next_command()[1])
    review.announce()
    review.submit("deny")
    review.finish(review.next_command()[1])
    bridge.discard(second)
    assert bridge.peek("signal") is first
    assert review.approved(first)
    bridge.acknowledge(first)
    assert not bridge.has("signal") and not review.approved(first)


@pytest.mark.parametrize("change", ["finish", "dispatch", "cancel_dispatch", "hold", "forget"])
def test_approved_command_survives_changes_to_unrelated_items(change):
    bridge = MessageBridge(Config(messaging_operator_mode=True, signal=MODE, whatsapp=MODE))
    review = bridge.operator
    first = Inbound("signal", "text", text="first", identity="1")
    later = Inbound("whatsapp", "text", text="later", identity="2")
    bridge.add(first)
    bridge.add(later)
    head, other = review._waiting
    review.finish(head)
    if change != "finish":
        review.finish(other)
    if change == "cancel_dispatch":
        review.dispatch(other)
    displayed = review.snapshot(approved=True)
    review.submit("transmit", approved=True, revision=displayed["revision"])
    if change in {"hold", "forget"}:
        getattr(review, change)(later)
    elif change == "cancel_dispatch":
        review.cancel_dispatch()
    else:
        getattr(review, change)(other)
    assert review.snapshot(approved=True)["revision"] == displayed["revision"]
    assert review.next_command() == ("transmit", head, False)


def test_approved_command_is_stale_after_same_item_fails_and_is_reapproved():
    bridge = MessageBridge(Config(messaging_operator_mode=True, signal=MODE))
    incoming = Inbound("signal", "text", text="first", identity="1")
    bridge.add(incoming)
    review = bridge.operator
    item = review._waiting[0]
    review.finish(item)
    review.submit("transmit", approved=True, revision=review.snapshot(approved=True)["revision"])
    review.hold(incoming)
    review.finish(item)
    assert review.next_command() == ("transmit", None, True)


def test_blocked_announcement_does_not_block_bridge_polling(monkeypatch):
    bridge = MessageBridge(Config(messaging_operator_mode=True, signal=MODE))
    bridge.add(Inbound("signal", "text", text="first", identity="1"))
    entered, release, added = (threading.Event() for _ in range(3))

    def blocked_emit(*args):
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(operator_mode, "emit", blocked_emit)
    announcement = threading.Thread(target=bridge.operator.announce)
    incoming = Inbound("signal", "text", text="later", identity="2")

    def poll():
        bridge.add(incoming)
        assert bridge.peek("signal").text == "first"
        added.set()

    polling = threading.Thread(target=poll)
    try:
        announcement.start()
        assert entered.wait(1)
        polling.start()
        assert added.wait(1), "Blocked terminal output held up bridge polling"
    finally:
        release.set()
        announcement.join(2)
        if polling.ident is not None:
            polling.join(2)


class Talk:
    def __init__(self, monkeypatch, *, mode=MODE, service="signal", realtime=False, **settings):
        self.config = Config(
            messaging_operator_mode=True,
            sleep_primary="go to sleep",
            agent_backend="grok_realtime" if realtime else "stub",
            **{service: mode},
            **settings,
        )
        self.service = service
        self.now = [100.0]
        self.bridge = MessageBridge(self.config)
        self.review = self.bridge.operator
        self.session = ListeningSession(self.config, lambda: self.now[0])
        self.listener = Mock()
        self.voice = Mock()
        self.voice.synthesize.return_value = PCM
        self.realtime = Mock(warm_connected=True)
        self.controls = SimpleNamespace(
            closed=threading.Event(), acquire=Mock(), start=Mock(), close=Mock()
        )
        self.bridge.send_text = Mock()
        self.bridge.send_voice = Mock()
        self.transmit = Mock()
        self.events = []
        self.captures = 0
        monkeypatch.setattr(cli, "load_config", lambda path: self.config)
        monkeypatch.setattr(cli, "open_messaging", lambda config: self.bridge)
        monkeypatch.setattr(cli, "open_stt", lambda config: self.listener)
        monkeypatch.setattr(cli, "open_tts", lambda config: self.voice)
        monkeypatch.setattr(cli, "ListeningSession", lambda config: self.session)
        monkeypatch.setattr(cli, "RealtimeTalkSession", lambda config: self.realtime)
        monkeypatch.setattr(cli, "OperatorServer", lambda review, config_path: self.controls)
        monkeypatch.setattr(cli, "preflight", lambda *a, **kw: None)
        monkeypatch.setattr(cli, "transmit_with_callsign", self.transmit)
        monkeypatch.setattr(cli, "voice_wav", Mock(return_value=PCM))
        monkeypatch.setattr(cli.time, "monotonic", lambda: self.now[0])
        monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
        monkeypatch.setattr(cli, "capture_from_device", self.capture)
        for name in ("SerialPTT", "Playback"):
            monkeypatch.setattr(cli, name, lambda *a, **kw: pytest.fail("real radio access"))

    def capture(self, *args, on_wait, **kwargs):
        self.captures += 1
        assert self.captures < 50, "operator loop failed to make progress"
        while self.events:
            event = self.events.pop(0)
            if isinstance(event, str):
                self.listener.transcribe.return_value = event
                self.realtime.gate_transcript.return_value = event
                return Utterance(
                    CAPTURED.frames, CAPTURED.rate, 0.2, 0.2, 0.1, "silence", self.now[0]
                )
            event(on_wait)
        raise KeyboardInterrupt

    def command(self, command, *, approved=False):
        def submit(on_wait):
            if approved:
                self.review.submit(
                    command, approved=True, revision=self.review.snapshot(approved=True)["revision"]
                )
            else:
                self.review.submit(command)
            on_wait()
            pytest.fail("operator command must release capture")

        return submit

    def tick(self, seconds=2):
        def idle(on_wait):
            self.now[0] += seconds
            on_wait()

        return idle

    def run(self, transmit=True):
        args = ["-c", "unused", "talk", "--capture"]
        if transmit:
            args.append("--transmit")
        result = cli.main(args)
        self.controls.close.assert_called_once()
        return result


@pytest.mark.parametrize("realtime", [False, True])
@pytest.mark.parametrize("listening", ["conversation", "wake_phrase"])
@pytest.mark.parametrize("destination", ["", "agent", "signal", "whatsapp"])
def test_operator_sleep_closes_any_conversation_and_retains_queues(
    monkeypatch, realtime, listening, destination
):
    talk = Talk(
        monkeypatch,
        realtime=realtime,
        listening_mode=listening,
        mode=replace(MODE, listening_mode=listening),
        whatsapp=replace(MODE, wake="friend", listening_mode=listening),
    )
    if destination:
        wake = {"agent": "charlotte", "signal": "nana", "whatsapp": "friend"}[destination]
        talk.session.decide(wake, talk.now[0])
        talk.session.complete_turn()
    approved = Inbound("signal", "text", text="approved reply", identity="1")
    waiting = Inbound("signal", "text", text="waiting reply", identity="2")
    talk.bridge.add(approved)
    talk.review.finish(talk.review._waiting[0])
    talk.bridge.add(waiting)
    outgoing = talk.review.add("whatsapp", text="outgoing waiting for review")
    talk.events = [talk.command("sleep"), talk.tick(70), "nearby chatter"]
    assert talk.run() == 130
    assert talk.session.destination == "" and talk.session.awake_until is None
    assert talk.bridge.queues["signal"] == [approved, waiting]
    assert talk.review.approved(approved) and not talk.review.approved(waiting)
    assert talk.review._waiting[1] is outgoing
    talk.transmit.assert_not_called()
    talk.bridge.send_text.assert_not_called()
    talk.bridge.send_voice.assert_not_called()


@pytest.mark.parametrize("realtime", [False, True])
@pytest.mark.parametrize("transmit", [False, True])
@pytest.mark.parametrize("result", ["spoken", "silent", "failed"])
def test_operator_sleep_uses_the_existing_confirmation_path(
    monkeypatch, realtime, transmit, result
):
    talk = Talk(monkeypatch, realtime=realtime, sleep_confirmation_phrase="Standing by.")
    talk.session.decide("nana", talk.now[0])
    talk.session.complete_turn()

    def confirm(config, voice, text, **options):
        assert talk.session.destination == "" and talk.session.awake_until is None
        assert text == "Standing by."
        assert options["realtime"] is realtime and options["transmit"] is transmit
        return result

    confirmation = Mock(side_effect=confirm)
    mute = Mock()
    monkeypatch.setattr(cli, "acknowledge", confirmation)
    monkeypatch.setattr(cli, "wait_post_tx_mute", mute)
    talk.events = [talk.command("sleep"), talk.tick()]
    assert talk.run(transmit=transmit) == 130
    confirmation.assert_called_once()
    assert mute.call_count == int(result == "spoken")
    assert talk.session.destination == "" and talk.session.awake_until is None


def test_operator_sleep_works_without_a_configured_voice_sleep_phrase(monkeypatch):
    talk = Talk(monkeypatch)
    talk.config = replace(talk.config, sleep_primary="", sleep_aliases=())
    talk.session.config = talk.config
    talk.session.decide("nana", talk.now[0])
    talk.session.complete_turn()
    talk.events = [talk.command("sleep"), talk.tick()]
    assert talk.run(transmit=False) == 130
    assert talk.session.destination == "" and talk.session.awake_until is None
    talk.transmit.assert_not_called()


def test_cancelled_operator_sleep_does_not_close_conversation(monkeypatch):
    talk = Talk(monkeypatch)
    talk.session.decide("nana", talk.now[0])
    talk.session.complete_turn()
    deadline = talk.session.awake_until

    def cancelled(on_wait):
        talk.review.submit("sleep").cancelled.set()
        on_wait()

    talk.events = [cancelled, talk.tick()]
    assert talk.run() == 130
    assert talk.session.destination == "signal" and talk.session.awake_until == deadline


def test_sleep_confirmation_transmission_fault_stops_operator_talk(monkeypatch):
    talk = Talk(monkeypatch, sleep_confirmation_phrase="Standing by.")
    talk.session.decide("nana", talk.now[0])
    confirmation = Mock(side_effect=WalkietalkError("Sleep confirmation: PTT release failed"))
    monkeypatch.setattr(cli, "acknowledge", confirmation)
    talk.events = [talk.command("sleep"), talk.tick()]
    assert talk.run() == 1
    assert talk.captures == 1
    assert talk.session.destination == "" and talk.session.awake_until is None


def test_operator_sleep_cancels_pending_shutdown(monkeypatch):
    talk = Talk(
        monkeypatch,
        shutdown_enabled=True,
        shutdown_phrase="bridge shutdown",
        shutdown_code="confirm alpha nine",
    )
    talk.events = ["bridge shutdown", talk.command("sleep"), "confirm alpha nine", talk.tick()]
    assert talk.run() == 130  # The stale confirmation code must not stop talk.
    assert talk.session.destination == "" and talk.session.awake_until is None


@pytest.mark.parametrize("realtime", [False, True])
@pytest.mark.parametrize("listening", ["conversation", "wake_phrase"])
def test_incoming_approval_during_sleep_then_contact_wake(monkeypatch, realtime, listening):
    talk = Talk(monkeypatch, realtime=realtime, mode=replace(MODE, listening_mode=listening))
    item = Inbound("signal", "text", text="hello", identity="1")
    talk.bridge.add(item)

    def held(on_wait):
        talk.transmit.assert_not_called()
        assert talk.review.approved(item) and talk.session.destination == ""
        on_wait()

    talk.events = [
        "nana",  # Wake and idle paths cannot deliver an unapproved message.
        talk.tick(),
        "go to sleep",
        talk.command("approve"),
        held,
        "charlotte",  # The agent wake cannot release it either.
        talk.tick(),
        "nana",
    ]
    assert talk.run() == 130
    talk.transmit.assert_called_once()
    assert not talk.bridge.has("signal")


@pytest.mark.parametrize("listening", ["conversation", "wake_phrase"])
def test_late_approval_uses_mode_specific_receive_timing(monkeypatch, listening):
    talk = Talk(monkeypatch, mode=replace(MODE, listening_mode=listening))
    talk.bridge.add(Inbound("signal", "text", text="late reply", identity="1"))

    def after_timeout(on_wait):
        if listening == "conversation":
            on_wait()
            talk.transmit.assert_not_called()
            assert talk.session.awake_until is None
        else:
            on_wait()  # Active contact still receives in wake_phrase mode.

    talk.events = ["nana", talk.tick(61), talk.command("approve"), after_timeout, "nana"]
    assert talk.run() == 130
    talk.transmit.assert_called_once()


def test_wake_phrase_keeps_receiving_multiple_approved_replies(monkeypatch):
    talk = Talk(monkeypatch, mode=replace(MODE, listening_mode="wake_phrase"))
    talk.bridge.add(Inbound("signal", "text", text="first", identity="1"))
    talk.bridge.add(Inbound("signal", "text", text="second", identity="2"))
    talk.events = [talk.command("approve"), talk.command("approve"), "nana", talk.tick(2)]
    assert talk.run() == 130
    assert talk.transmit.call_count == 2
    assert not talk.bridge.has("signal")


@pytest.mark.parametrize("service", ["signal", "whatsapp"])
@pytest.mark.parametrize("listening", ["conversation", "wake_phrase"])
@pytest.mark.parametrize("realtime", [False, True])
@pytest.mark.parametrize("transmit", [False, True])
def test_operator_transmit_releases_one_message_while_asleep(
    monkeypatch, service, listening, realtime, transmit
):
    talk = Talk(
        monkeypatch,
        service=service,
        realtime=realtime,
        mode=replace(MODE, listening_mode=listening),
    )
    first = Inbound(service, "text", text="first", identity="1")
    second = Inbound(service, "text", text="second", identity="2")
    talk.bridge.add(first)
    talk.bridge.add(second)
    talk.events = [talk.command("transmit"), talk.tick(), talk.tick(70)]
    assert talk.run(transmit=transmit) == 130
    assert talk.transmit.call_count == int(transmit)
    assert talk.session.destination == "" and talk.session.awake_until is None
    assert talk.bridge.peek(service) is second
    assert talk.review.snapshot()["waiting"] == 1
    assert not talk.review.approved(second) and not talk.review.dispatch_service()
    talk.bridge.send_text.assert_not_called()
    talk.bridge.send_voice.assert_not_called()


def test_operator_can_release_previously_approved_message_without_draining_backlog(monkeypatch):
    talk = Talk(monkeypatch)
    first = Inbound("signal", "text", text="first", identity="1")
    second = Inbound("signal", "text", text="second", identity="2")
    talk.bridge.add(first)
    talk.bridge.add(second)
    outgoing = talk.review.add("signal", text="outgoing waiting for review")
    talk.events = [
        talk.command("approve"),
        talk.command("approve"),
        talk.command("transmit", approved=True),
        talk.tick(),
        talk.tick(70),
    ]
    assert talk.run() == 130
    talk.transmit.assert_called_once()
    assert talk.bridge.peek("signal") is second and talk.review.approved(second)
    assert talk.review._waiting == [outgoing]
    assert talk.session.destination == "" and talk.session.awake_until is None


@pytest.mark.parametrize("wake", ["charlotte", "friend"])
def test_operator_transmit_preserves_another_conversation_and_its_timer(monkeypatch, wake):
    talk = Talk(monkeypatch, listening_mode="conversation", whatsapp=replace(MODE, wake="friend"))
    talk.bridge.add(Inbound("signal", "text", text="manual reply", identity="1"))
    talk.session.decide(wake, talk.now[0])
    talk.session.complete_turn()
    destination, deadline = talk.session.destination, talk.session.awake_until
    talk.events = [talk.command("transmit"), talk.tick()]
    assert talk.run() == 130
    talk.transmit.assert_called_once()
    assert (talk.session.destination, talk.session.awake_until) == (destination, deadline)


def test_operator_transmit_bypasses_expired_receive_window_without_reopening_it(monkeypatch):
    talk = Talk(monkeypatch)
    talk.bridge.add(Inbound("signal", "text", text="late reply", identity="1"))
    talk.session.decide("nana", talk.now[0])
    talk.session.complete_turn()
    talk.now[0] += 61
    talk.events = [talk.command("transmit"), talk.tick()]
    assert talk.run() == 130
    talk.transmit.assert_called_once()
    assert talk.session.destination == "signal" and talk.session.awake_until is None


@pytest.mark.parametrize("pause", [None, "go to sleep", "charlotte", "operator sleep"])
def test_operator_transmit_covers_all_chunks_and_radio_controls_can_pause_it(monkeypatch, pause):
    talk = Talk(monkeypatch, agent_max_reply_chars=38)
    incoming = Inbound("signal", "text", text="First sentence. Second sentence.", identity="1")
    talk.bridge.add(incoming)
    talk.events = [talk.command("transmit"), talk.tick()]
    if pause:

        def held(on_wait):
            assert talk.transmit.call_count == 1
            assert talk.review.approved(incoming) and not talk.review.dispatch_service()
            on_wait()

        pause_event = talk.command("sleep") if pause == "operator sleep" else pause
        talk.events += [pause_event, held, talk.tick(70), talk.command("transmit", approved=True)]
    talk.events.append(talk.tick())
    assert talk.run() == 130
    assert talk.transmit.call_count == 2
    assert not talk.bridge.has("signal") and not talk.review.dispatch_service()
    assert talk.session.destination == ("agent" if pause == "charlotte" else "")
    assert talk.session.awake_until is None
    assert talk.voice.synthesize.call_args_list == [
        call("Nana says: First sentence.", truncate=False),
        call("Nana says: Second sentence, over", truncate=False),
    ]


def test_failed_operator_transmission_requires_new_approval_and_keeps_completed_chunks(monkeypatch):
    talk = Talk(monkeypatch, agent_max_reply_chars=38)
    incoming = Inbound("signal", "text", text="First sentence. Second sentence.", identity="1")
    talk.bridge.add(incoming)
    talk.voice.synthesize.side_effect = [PCM, WalkietalkError("TTS failed"), PCM]

    def held(on_wait):
        assert talk.transmit.call_count == 1
        assert not talk.review.approved(incoming) and not talk.review.dispatch_service()
        on_wait()

    talk.events = [
        talk.command("transmit"),
        talk.tick(),
        talk.tick(),
        held,
        talk.command("transmit", approved=True),
        held,
        talk.command("transmit"),
        talk.tick(6),
    ]
    assert talk.run() == 130
    assert talk.transmit.call_count == 2
    assert not talk.bridge.has("signal")
    assert talk.voice.synthesize.call_args_list == [
        call("Nana says: First sentence.", truncate=False),
        call("Nana says: Second sentence, over", truncate=False),
        call("Nana says: Second sentence, over", truncate=False),
    ]


@pytest.mark.parametrize("as_text", [False, True])
def test_operator_transmit_requires_voice_preview_and_uses_cached_delivery(
    monkeypatch, tmp_path, as_text
):
    talk = Talk(monkeypatch, mode=replace(MODE, transcribe_voice=as_text))
    talk.bridge.add(Inbound("signal", "voice", audio_path=tmp_path / "voice.ogg", identity="1"))
    talk.listener.transcribe.return_value = "voice transcript"
    talk.events = [
        talk.command("transmit"),
        talk.command("read"),
        talk.command("transmit"),
        talk.tick(),
    ]
    assert talk.run() == 130
    talk.transmit.assert_called_once()
    talk.listener.transcribe.assert_called_once_with(PCM.frames, PCM.rate)
    assert cli.voice_wav.call_count == (1 if as_text else 2)
    assert not talk.bridge.has("signal")
    if as_text:
        talk.voice.synthesize.assert_called_once_with(
            "Nana says: voice transcript, over", truncate=False
        )
    else:
        assert talk.transmit.call_args.args[0].frames == PCM.frames + PCM.frames
        talk.voice.synthesize.assert_called_once_with("Nana says:", truncate=False)


def test_operator_transmit_cannot_send_an_outgoing_message(monkeypatch):
    talk = Talk(monkeypatch)
    talk.events = ["nana hello", talk.command("transmit"), talk.tick()]
    assert talk.run() == 130
    talk.bridge.send_text.assert_not_called()
    talk.bridge.send_voice.assert_not_called()
    talk.transmit.assert_not_called()
    assert talk.review.snapshot()["item"]["text"] == "hello"


def test_operator_transmit_cannot_skip_an_earlier_message_for_same_contact(monkeypatch):
    talk = Talk(monkeypatch)
    first = Inbound("signal", "text", text="first", identity="1")
    second = Inbound("signal", "text", text="second", identity="2")
    talk.bridge.add(first)
    talk.bridge.add(second)

    def blocked(on_wait):
        assert talk.review.snapshot()["item"]["text"] == "second"
        assert not talk.review.approved(second) and not talk.review.dispatch_service()
        talk.transmit.assert_not_called()
        on_wait()

    talk.events = [
        talk.command("approve"),
        talk.command("transmit"),
        blocked,
        talk.command("transmit", approved=True),
        talk.tick(),
        talk.command("transmit"),
        talk.tick(),
    ]
    assert talk.run() == 130
    assert talk.transmit.call_count == 2
    assert not talk.bridge.has("signal")


def test_failed_contact_head_does_not_block_another_services_approved_delivery(monkeypatch):
    talk = Talk(monkeypatch, whatsapp=replace(MODE, wake="friend"))
    failed = Inbound("whatsapp", "text", text="failed", identity="1")
    blocked = Inbound("whatsapp", "text", text="blocked", identity="2")
    deliverable = Inbound("signal", "text", text="deliverable", identity="3")
    for incoming in (failed, blocked, deliverable):
        talk.bridge.add(incoming)
        talk.review.finish(talk.review._waiting[0])
    talk.review.hold(failed)
    assert talk.review.snapshot(approved=True)["item"]["text"] == "deliverable"
    talk.events = [talk.command("transmit", approved=True), talk.tick()]
    assert talk.run() == 130
    talk.transmit.assert_called_once()
    assert not talk.bridge.has("signal")
    assert talk.bridge.peek("whatsapp") is failed
    assert talk.review.approved(blocked)
    assert talk.review.snapshot(approved=True)["item"] is None
    talk.bridge.discard(failed)
    assert talk.review.snapshot(approved=True)["item"]["text"] == "blocked"


def test_concurrent_approved_transmit_commands_cannot_release_next_message(monkeypatch):
    talk = Talk(monkeypatch)
    talk.bridge.add(Inbound("signal", "text", text="first", identity="1"))
    second = Inbound("signal", "text", text="second", identity="2")
    talk.bridge.add(second)

    def double_transmit(on_wait):
        revision = talk.review.snapshot(approved=True)["revision"]
        talk.review.submit("transmit", revision=revision, approved=True)
        talk.review.submit("transmit", revision=revision, approved=True)
        on_wait()

    talk.events = [
        talk.command("approve"),
        talk.command("approve"),
        double_transmit,
        talk.tick(),
        talk.tick(70),
    ]
    assert talk.run() == 130
    talk.transmit.assert_called_once()
    assert talk.bridge.peek("signal") is second and talk.review.approved(second)


def test_operator_transmit_is_held_during_shutdown_confirmation(monkeypatch):
    talk = Talk(
        monkeypatch,
        shutdown_enabled=True,
        shutdown_phrase="bridge shutdown",
        shutdown_code="confirm alpha nine",
    )
    incoming = Inbound("signal", "text", text="held", identity="1")
    talk.bridge.add(incoming)
    talk.events = ["bridge shutdown", talk.command("transmit"), talk.tick()]
    assert talk.run() == 130
    talk.transmit.assert_not_called()
    assert talk.review.waiting(talk.review._waiting[0])
    assert not talk.review.approved(incoming) and not talk.review.dispatch_service()


def test_outgoing_approval_preserves_newly_selected_conversation(monkeypatch):
    talk = Talk(monkeypatch, whatsapp=replace(MODE, wake="friend"))
    talk.events = ["nana hello", "friend", talk.tick(20), talk.command("approve")]
    assert talk.run() == 130
    talk.bridge.send_text.assert_called_once_with("signal", "hello")
    assert talk.session.destination == "whatsapp"
    assert talk.session.awake_until == 160


def test_read_invalid_command_and_denial_do_not_send_or_transmit(monkeypatch):
    talk = Talk(monkeypatch)
    talk.bridge.add(Inbound("signal", "text", text="hello", identity="1"))
    talk.events = [talk.command("read"), talk.command("invalid"), talk.command("deny"), "nana"]
    assert talk.run() == 130
    assert not talk.bridge.has("signal")
    talk.transmit.assert_not_called()
    talk.bridge.send_text.assert_not_called()
    talk.bridge.send_voice.assert_not_called()


def test_denial_does_not_invalidate_queued_transmit_for_another_contact(monkeypatch):
    talk = Talk(monkeypatch, whatsapp=MODE)
    failed = Inbound("signal", "text", text="denied", identity="1")
    approved = Inbound("whatsapp", "text", text="delivered", identity="2")
    talk.bridge.add(failed)
    talk.bridge.add(approved)
    talk.review.finish(talk.review._waiting[0])
    talk.review.finish(talk.review._waiting[0])
    talk.review.hold(failed)

    def queued(on_wait):
        talk.review.submit("deny", revision=talk.review.snapshot()["revision"])
        talk.review.submit(
            "transmit", approved=True, revision=talk.review.snapshot(approved=True)["revision"]
        )
        on_wait()

    talk.events = [queued, talk.tick()]
    assert talk.run() == 130
    talk.transmit.assert_called_once()
    assert not talk.bridge.has("signal") and not talk.bridge.has("whatsapp")


def test_approval_queued_before_voice_read_requires_fresh_action(monkeypatch):
    talk = Talk(monkeypatch)
    incoming = Inbound("signal", "voice", audio_path=Path("voice.ogg"), identity="1")
    talk.bridge.add(incoming)
    talk.listener.transcribe.return_value = "The message's actual words."

    def queued(on_wait):
        revision = talk.review.snapshot()["revision"]
        talk.review.submit("read", revision=revision)
        talk.review.submit("approve", revision=revision)
        on_wait()

    def reviewed(on_wait):
        assert talk.review.snapshot()["item"]["previewed"]
        assert not talk.review.approved(incoming)
        assert not talk.review.snapshot()["approved"]
        on_wait()

    talk.events = [queued, reviewed, talk.command("approve")]
    assert talk.run(transmit=False) == 130
    assert talk.review.approved(incoming)
    cli.voice_wav.assert_called_once()
    talk.transmit.assert_not_called()


def test_partial_transmission_mutes_and_delays_another_authorized_delivery(monkeypatch):
    transmit_with_callsign = cli.transmit_with_callsign
    talk = Talk(monkeypatch, whatsapp=MODE, post_tx_mute_seconds=2)
    first = Inbound("signal", "text", text="partial", identity="1")
    second = Inbound("whatsapp", "text", text="next", identity="2")
    talk.bridge.add(first)
    talk.bridge.add(second)
    head = talk.review._waiting[0]
    talk.review.finish(head)
    talk.review.finish(talk.review._waiting[0])
    talk.review.dispatch(head)
    players = [Mock(), Mock()]
    players[0].play.side_effect = OSError("partial playback failed")
    ptt = Mock()
    key_times = []
    ptt.on.side_effect = lambda: key_times.append(talk.now[0])

    def mute(config):
        talk.now[0] += config.post_tx_mute_seconds

    def before_retry(on_wait):
        assert key_times == [100]
        assert not talk.review.approved(first)
        on_wait()

    monkeypatch.setattr(cli, "transmit_with_callsign", transmit_with_callsign)
    monkeypatch.setattr(cli, "Playback", Mock(side_effect=players))
    monkeypatch.setattr(cli, "SerialPTT", Mock(return_value=ptt))
    monkeypatch.setattr(cli, "wait_post_tx_mute", Mock(side_effect=mute))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    talk.events = [
        talk.tick(0),
        talk.command("transmit", approved=True),
        talk.tick(4),
        before_retry,
        talk.tick(1),
    ]
    assert talk.run() == 130
    assert key_times == [100, 107]
    assert cli.wait_post_tx_mute.call_count == 2
    assert ptt.off.call_count == ptt.close.call_count == 2
    assert talk.bridge.peek("signal") is first
    assert not talk.bridge.has("whatsapp")


@pytest.mark.parametrize("service", ["signal", "whatsapp"])
@pytest.mark.parametrize("realtime", [False, True])
@pytest.mark.parametrize("send_voice", [False, True])
@pytest.mark.parametrize("transmit", [False, True])
def test_outgoing_is_held_then_approved_while_sleeping(
    monkeypatch, service, realtime, send_voice, transmit
):
    talk = Talk(
        monkeypatch,
        service=service,
        realtime=realtime,
        mode=replace(MODE, send_as_voice=send_voice),
    )

    def held(on_wait):
        talk.bridge.send_text.assert_not_called()
        talk.bridge.send_voice.assert_not_called()
        # A contact reply that arrives after the outgoing item also stays unapproved.
        talk.bridge.add(Inbound(service, "text", text="incoming", identity="1"))
        on_wait()

    talk.events = ["nana hello there", "go to sleep", held]
    talk.events += [talk.command("approve"), talk.tick()]
    assert talk.run(transmit=transmit) == 130
    assert talk.session.destination == "" and talk.session.awake_until is None
    talk.transmit.assert_not_called()
    if send_voice:
        talk.bridge.send_voice.assert_called_once_with(service, CAPTURED)
        talk.bridge.send_text.assert_not_called()
    else:
        talk.bridge.send_text.assert_called_once_with(service, "hello there")
        talk.bridge.send_voice.assert_not_called()


def test_realtime_reconnection_cannot_play_unapproved_message(monkeypatch):
    talk = Talk(monkeypatch, realtime=True)
    talk.session.decide("nana", talk.now[0])
    talk.session.complete_turn()
    talk.bridge.add(Inbound("signal", "text", text="held", identity="1"))
    talk.realtime.warm_connected = False
    talk.realtime.warm.side_effect = lambda **kw: setattr(talk.realtime, "warm_connected", True)
    assert talk.run() == 130
    talk.realtime.warm.assert_called_once()
    talk.transmit.assert_not_called()


@pytest.mark.parametrize("transmit", [False, True])
def test_voice_transcript_preview_is_cached_for_approval(monkeypatch, tmp_path, transmit):
    talk = Talk(monkeypatch, mode=replace(MODE, transcribe_voice=True))
    incoming = Inbound("signal", "voice", audio_path=tmp_path / "voice.ogg", identity="1")
    talk.bridge.add(incoming)
    talk.listener.transcribe.return_value = "approved transcript"
    talk.events = [
        talk.command("approve"),  # Voice cannot be approved without a successful preview.
        talk.command("read"),
        talk.command("read"),
        talk.command("approve"),
        "nana",
    ]
    assert talk.run(transmit=transmit) == 130
    assert talk.listener.transcribe.call_args_list.count(call(PCM.frames, PCM.rate)) == 1
    cli.voice_wav.assert_called_once_with(incoming.audio_path, 300, rate=16000, truncate=False)
    assert not talk.bridge.has("signal")
    if transmit:
        assert talk.voice.synthesize.call_args.args[0] == "Nana says: approved transcript, over"
        talk.transmit.assert_called_once()
    else:
        talk.transmit.assert_not_called()


def test_failed_voice_preview_stays_held_without_audio_fallback(monkeypatch, tmp_path):
    talk = Talk(monkeypatch, mode=replace(MODE, transcribe_voice=True))
    item = Inbound("signal", "voice", audio_path=tmp_path / "voice.ogg", identity="1")
    talk.bridge.add(item)
    talk.listener.transcribe.side_effect = WalkietalkError("transcription failed")
    talk.events = [talk.command("read"), talk.command("approve"), talk.tick()]
    assert talk.run() == 130
    assert talk.bridge.peek("signal") is item and not talk.review.approved(item)
    talk.transmit.assert_not_called()
    talk.voice.synthesize.assert_not_called()


@pytest.mark.parametrize("service", ["signal", "whatsapp"])
@pytest.mark.parametrize("realtime", [False, True])
def test_read_transcribes_full_note_and_delivery_trims_original_audio(
    monkeypatch, tmp_path, capsys, service, realtime
):
    talk = Talk(monkeypatch, service=service, realtime=realtime)
    item = Inbound(service, "voice", audio_path=tmp_path / "voice.ogg", identity="1")
    talk.bridge.add(item)
    excerpt = Wav(b"\x03\x00" * 4800, 48000, 0.1)
    full_note = Wav(b"\x04\x00" * 16000, 16000, 1)
    cli.voice_wav.side_effect = [full_note, excerpt]
    talk.listener.transcribe.return_value = "full transcript"

    def reviewed(on_wait):
        head = talk.review.snapshot()["item"]
        assert head["transcript"] == "full transcript" and head["previewed"]
        assert not talk.review.approved(item)
        # Reviewing original audio must not turn its delivery into TTS.
        assert talk.review._waiting[0].incoming is item
        talk.transmit.assert_not_called()
        talk.voice.synthesize.assert_not_called()
        assert "cut to fit" not in capsys.readouterr().out
        talk.bridge.send_text.assert_not_called()
        talk.bridge.send_voice.assert_not_called()
        on_wait()

    talk.events = [
        talk.command("read"),
        reviewed,
        talk.command("read"),
        talk.command("approve"),
        "nana",
    ]
    assert talk.run() == 130
    assert (
        talk.listener.transcribe.call_args_list.count(call(full_note.frames, full_note.rate)) == 1
    )
    speech = talk.transmit.call_args.args[0]
    assert speech.frames == PCM.frames + excerpt.frames
    assert cli.voice_wav.call_count == 2
    output = capsys.readouterr().out
    assert "Voice transcript: full transcript" in output
    talk.voice.synthesize.assert_called_once_with("Nana says:", truncate=False)


def test_receive_only_voice_preview_needs_no_tts(monkeypatch, tmp_path):
    talk = Talk(monkeypatch)
    incoming = Inbound("signal", "voice", audio_path=tmp_path / "voice.ogg", identity="1")
    talk.bridge.add(incoming)
    monkeypatch.setattr(cli, "open_tts", lambda config: pytest.fail("receive-only TTS request"))
    talk.listener.transcribe.return_value = "voice transcript"
    talk.events = [
        talk.command("read"),
        talk.command("read"),
        talk.command("approve"),
        "nana",
    ]
    assert talk.run(transmit=False) == 130
    cli.voice_wav.assert_called_once_with(
        incoming.audio_path,
        300,
        rate=16000,
        truncate=False,
    )
    talk.voice.prepare.assert_not_called()
    talk.voice.synthesize.assert_not_called()
    talk.transmit.assert_not_called()
    assert not talk.bridge.has("signal")


@pytest.mark.parametrize("transmit", [False, True])
def test_raw_voice_read_ignores_tx_limit_and_tts_failure(monkeypatch, tmp_path, transmit):
    talk = Talk(monkeypatch, max_tx_seconds=1)
    incoming = Inbound("signal", "voice", audio_path=tmp_path / "voice.ogg", identity="1")
    talk.bridge.add(incoming)
    full_note = Wav(b"\x01\x00" * 16000 * 20, 16000, 20)
    cli.voice_wav.return_value = full_note
    talk.voice.synthesize.side_effect = WalkietalkError("TTS unavailable")
    talk.listener.transcribe.return_value = "full note transcript"
    talk.events = [talk.command("read"), talk.command("read"), talk.command("approve")]
    assert talk.run(transmit=transmit) == 130
    cli.voice_wav.assert_called_once_with(incoming.audio_path, 300, rate=16000, truncate=False)
    talk.listener.transcribe.assert_called_once_with(full_note.frames, full_note.rate)
    talk.voice.synthesize.assert_not_called()
    assert talk.review.approved(incoming)


def test_successful_raw_voice_review_releases_decoded_audio(monkeypatch):
    talk = Talk(monkeypatch)
    talk.bridge.add(Inbound("signal", "voice", audio_path=Path("voice.ogg"), identity="1"))
    talk.listener.transcribe.return_value = "A cached transcript."
    decoded = []

    def decode(*args, **kwargs):
        audio = Wav(PCM.frames, PCM.rate, PCM.duration)
        decoded.append(weakref.ref(audio))
        return audio

    def released(on_wait):
        assert len(decoded) == 1 and decoded[0]() is None
        on_wait()

    monkeypatch.setattr(cli, "voice_wav", decode)
    talk.events = [talk.command("read"), released, talk.command("read"), released]
    assert talk.run(transmit=False) == 130
    talk.listener.transcribe.assert_called_once()


def test_failed_chunk_requires_fresh_approval_and_preserves_completed_chunks(monkeypatch):
    talk = Talk(monkeypatch, agent_max_reply_chars=38)
    item = Inbound("signal", "text", text="First sentence. Second sentence.", identity="1")
    talk.bridge.add(item)
    talk.voice.synthesize.side_effect = [PCM, WalkietalkError("TTS failed"), PCM]

    def failed(on_wait):
        assert talk.bridge.peek("signal") is item and not talk.review.approved(item)
        assert talk.transmit.call_count == 1
        assert talk.voice.synthesize.call_count == 2
        on_wait()  # Failure must not trigger an automatic retry.

    talk.events = [
        talk.command("approve"),
        "nana",
        talk.tick(2),
        failed,
        talk.command("approve"),
        talk.tick(6),
    ]
    assert talk.run() == 130
    assert talk.transmit.call_count == 2
    assert not talk.bridge.has("signal")
    assert talk.voice.synthesize.call_args_list == [
        call("Nana says: First sentence.", truncate=False),
        call("Nana says: Second sentence, over", truncate=False),
        call("Nana says: Second sentence, over", truncate=False),
    ]


def test_transmission_failure_returns_item_to_review(monkeypatch):
    talk = Talk(monkeypatch)
    item = Inbound("signal", "text", text="hello", identity="1")
    talk.bridge.add(item)
    talk.transmit.side_effect = WalkietalkError("worker failed")
    talk.events = [talk.command("approve"), "nana", talk.tick(10)]
    assert talk.run() == 130
    assert talk.bridge.peek("signal") is item and not talk.review.approved(item)
    talk.transmit.assert_called_once()


@pytest.mark.parametrize("failure_at", ["open", "on", "off", "close"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_ptt_fault_stops_operator_talk_and_closes_controls(
    monkeypatch, capsys, failure_at, cleanup_fails
):
    transmit_with_callsign = cli.transmit_with_callsign
    talk = Talk(monkeypatch)
    talk.bridge.add(Inbound("signal", "text", text="hello", identity="1"))
    ptt = Mock()
    if failure_at == "off":

        class DisconnectedSerial:
            is_open = True

            def __setattr__(self, name, value):
                if name == "dtr":
                    raise OSError("disconnected")
                object.__setattr__(self, name, value)

            def close(self):
                pass

        native_ptt = SerialPTT("unused")
        native_ptt.serial = DisconnectedSerial()
        ptt.off.side_effect = native_ptt.off
    else:
        getattr(ptt, failure_at).side_effect = OSError("disconnected")
    playback = Mock()
    if cleanup_fails:
        playback.close.side_effect = OSError("audio cleanup failed")
    monkeypatch.setattr(cli, "transmit_with_callsign", transmit_with_callsign)
    monkeypatch.setattr(cli, "SerialPTT", Mock(return_value=ptt))
    monkeypatch.setattr(cli, "Playback", Mock(return_value=playback))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    talk.events = [talk.command("approve"), "nana", talk.tick(10)]
    assert talk.run() == 1
    assert talk.captures == 2
    ptt.off.assert_called_once()
    ptt.close.assert_called_once()
    playback.close.assert_called_once()
    assert "disconnected" in capsys.readouterr().err


def test_receive_only_agent_reply_after_raw_voice_review_stays_off_air(monkeypatch, tmp_path):
    talk = Talk(monkeypatch)
    talk.bridge.add(Inbound("signal", "voice", audio_path=tmp_path / "voice.ogg", identity="1"))
    talk.listener.transcribe.return_value = "reviewed voice note"
    talk.events = [talk.command("read"), "charlotte what time is it", "charlotte hello again"]
    assert talk.run(transmit=False) == 130
    talk.transmit.assert_not_called()
    talk.voice.synthesize.assert_not_called()
    assert talk.review.snapshot()["item"]["transcript"] == "reviewed voice note"


def test_send_failure_retains_message_and_invalidates_buffered_approval(monkeypatch):
    talk = Talk(monkeypatch)
    talk.bridge.send_text.side_effect = WalkietalkError("send timed out")

    def double_approve(on_wait):
        talk.review.submit("approve")
        talk.review.submit("approve")
        on_wait()

    talk.events = ["nana hello", double_approve, talk.tick()]
    assert talk.run() == 130
    talk.bridge.send_text.assert_called_once_with("signal", "hello")
    talk.review.submit("read")
    assert talk.review.next_command()[1].text == "hello"


def test_closed_operator_controls_stop_before_approved_delivery(monkeypatch):
    talk = Talk(monkeypatch)
    talk.bridge.add(Inbound("signal", "text", text="held", identity="1"))
    talk.review.announce()
    talk.review.submit("approve")
    talk.review.finish(talk.review.next_command()[1])
    talk.session.decide("nana", talk.now[0])
    talk.session.complete_turn()
    talk.controls.closed.set()
    assert talk.run() == 1
    talk.transmit.assert_not_called()
    assert talk.captures == 0


def test_operator_controls_closing_during_preparation_prevent_keying(monkeypatch):
    talk = Talk(monkeypatch)
    talk.bridge.add(Inbound("signal", "text", text="held", identity="1"))

    def synthesize(*args, **kwargs):
        talk.controls.closed.set()
        return PCM

    talk.voice.synthesize.side_effect = synthesize
    talk.events = [talk.command("approve"), "nana"]
    assert talk.run() == 1
    talk.transmit.assert_not_called()


def test_window_expiring_during_preparation_holds_approved_reply(monkeypatch):
    talk = Talk(monkeypatch)
    item = Inbound("signal", "text", text="held", identity="1")
    talk.bridge.add(item)

    def synthesize(*args, **kwargs):
        talk.now[0] += 61
        return PCM

    talk.voice.synthesize.side_effect = synthesize
    talk.events = [talk.command("approve"), "nana", talk.tick()]
    assert talk.run() == 130
    assert talk.bridge.peek("signal") is item and talk.review.approved(item)
    talk.voice.synthesize.assert_called_once()
    talk.transmit.assert_not_called()


@pytest.mark.parametrize("failure_at", ["prepare", "play"])
@pytest.mark.parametrize("chunked", [False, True])
def test_failed_separate_station_id_does_not_repeat_delivered_message(
    monkeypatch, capsys, failure_at, chunked
):
    transmit_with_callsign = cli.transmit_with_callsign
    talk = Talk(
        monkeypatch,
        callsign="TEST1ID",
        callsign_mode="interval",
        max_tx_seconds=1,
        settle_seconds=0.1,
        post_tx_mute_seconds=0.7,
        agent_max_reply_chars=38,
    )
    tracker = CallsignSession(talk.config, lambda: talk.now[0])
    text = "First sentence. Second sentence." if chunked else "hello"
    progress = MessagePlayback(text)
    monkeypatch.setattr(cli, "MessagePlayback", lambda text: progress)
    incoming = Inbound("signal", "text", text=text, identity="1")
    talk.bridge.add(incoming)
    ident = Wav(b"\x00\x30" * 43200, 48000, 0.9)
    chunks = 2 if chunked else 1
    talk.voice.synthesize.side_effect = [PCM, ident] * chunks
    players = [Mock() for _ in range(chunks * 2)]
    for player in players[1::2]:
        getattr(player, failure_at).side_effect = WalkietalkError("ID output unavailable")
    ptt = Mock()
    mute = Mock()
    monkeypatch.setattr(cli, "CallsignSession", lambda *args: tracker)
    monkeypatch.setattr(cli, "transmit_with_callsign", transmit_with_callsign)
    monkeypatch.setattr(cli, "Playback", Mock(side_effect=players))
    monkeypatch.setattr(cli, "SerialPTT", Mock(return_value=ptt))
    monkeypatch.setattr(cli, "wait_post_tx_mute", mute)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    talk.events = [talk.command("approve"), "nana", talk.command("approve"), talk.tick(10)]
    assert talk.run() == 1
    players[0].play.assert_called_once()
    assert cli.Playback.call_count == 2
    assert not talk.review._waiting
    assert talk.bridge.has("signal") is chunked
    if chunked:
        assert talk.review.approved(incoming)
        assert talk.voice.synthesize.call_count == 2
        assert progress.remaining == "Second sentence."
    assert tracker.due() and tracker.last_id_at is None
    assert mute.call_args_list == [call(talk.config)]
    assert "Station ID failed" in capsys.readouterr().err


@pytest.mark.parametrize("failure_at", ["radio_wav", "TemporaryDirectory", "write_wav", "prepare"])
def test_failure_before_keying_reports_nothing_transmitted(monkeypatch, capsys, failure_at):
    transmit_with_callsign = cli.transmit_with_callsign
    talk = Talk(monkeypatch)
    incoming = Inbound("signal", "text", text="held", identity="1")
    talk.bridge.add(incoming)
    player = Mock()
    ptt = Mock()
    monkeypatch.setattr(cli, "transmit_with_callsign", transmit_with_callsign)
    monkeypatch.setattr(cli, "Playback", Mock(return_value=player))
    monkeypatch.setattr(cli, "SerialPTT", ptt)
    if failure_at == "TemporaryDirectory":
        monkeypatch.setattr(cli.tempfile, failure_at, Mock(side_effect=OSError("cannot prepare")))
    elif failure_at == "radio_wav":
        # Text chunk preparation succeeds, then transmit_speech's validation fails.
        monkeypatch.setattr(cli, failure_at, Mock(side_effect=WalkietalkError("cannot prepare")))
    elif failure_at == "prepare":
        player.prepare.side_effect = WalkietalkError("cannot prepare")
    else:
        monkeypatch.setattr(cli, failure_at, Mock(side_effect=OSError("cannot prepare")))
    talk.events = [talk.command("approve"), "nana", talk.tick(10)]
    assert talk.run() == 130
    assert talk.bridge.peek("signal") is incoming and not talk.review.approved(incoming)
    ptt.assert_not_called()
    output = capsys.readouterr().out
    assert "nothing was transmitted" in output
    assert "partial transmission" not in output


def test_audio_cleanup_failure_after_delivery_does_not_requeue_or_repeat(monkeypatch, capsys):
    transmit_with_callsign = cli.transmit_with_callsign
    talk = Talk(monkeypatch)
    incoming = Inbound("signal", "text", text="once", identity="1")
    talk.bridge.add(incoming)
    playback = Mock()
    playback.close.side_effect = OSError("audio cleanup failed")
    ptt = Mock()
    monkeypatch.setattr(cli, "transmit_with_callsign", transmit_with_callsign)
    monkeypatch.setattr(cli, "Playback", Mock(return_value=playback))
    monkeypatch.setattr(cli, "SerialPTT", Mock(return_value=ptt))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    talk.events = [talk.command("approve"), "nana", talk.command("approve"), talk.tick(10)]
    assert talk.run() == 130
    playback.play.assert_called_once()
    playback.close.assert_called_once()
    ptt.off.assert_called_once()
    assert not talk.bridge.has("signal")
    assert talk.review.snapshot()["waiting"] == 0
    assert "Audio cleanup failed after transmission" in capsys.readouterr().err


@pytest.mark.parametrize("realtime", [False, True])
@pytest.mark.parametrize("queued", [False, True])
def test_contact_wake_announces_no_deliverable_messages(monkeypatch, realtime, queued):
    talk = Talk(
        monkeypatch, realtime=realtime, mode=replace(MODE, empty_queue_phrase="No messages ready")
    )
    incoming = Inbound("signal", "text", text="unapproved", identity="1")
    if queued:
        talk.bridge.add(incoming)
    notice = Mock(return_value="spoken")
    monkeypatch.setattr(cli, "acknowledge", notice)
    talk.events = ["nana"]
    assert talk.run() == 130
    notice.assert_called_once()
    assert notice.call_args.args[2] == "No messages ready"
    assert talk.session.destination == "signal" and talk.session.awake_until == 160
    talk.transmit.assert_not_called()
    if queued:
        assert talk.bridge.peek("signal") is incoming and not talk.review.approved(incoming)


@pytest.mark.parametrize("operator", [False, True])
@pytest.mark.parametrize("result", ["spoken", "failed", "silent"])
def test_empty_queue_followup_starts_after_notice_and_post_tx_mute(monkeypatch, operator, result):
    talk = Talk(monkeypatch, mode=replace(MODE, conversation_timeout_seconds=10))
    talk.config = replace(talk.config, messaging_operator_mode=operator)

    def notice(*args, **kwargs):
        assert talk.session.awake_until is None
        talk.now[0] += 5
        return result

    def mute(config):
        assert talk.session.awake_until is None
        talk.now[0] += 2

    monkeypatch.setattr(cli, "acknowledge", notice)
    monkeypatch.setattr(cli, "wait_post_tx_mute", mute)
    talk.events = ["nana"]
    if operator:
        assert talk.run() == 130
    else:
        assert cli.main(["-c", "unused", "talk", "--capture", "--transmit"]) == 130
    assert talk.session.awake_until == talk.now[0] + 10
    assert talk.now[0] == (107 if result == "spoken" else 105)


@pytest.mark.parametrize("failure_at", ["bridge", "controls", "realtime"])
def test_shutdown_attempts_every_close_when_one_fails(monkeypatch, failure_at):
    from walkietalk import operator_panel

    talk = Talk(monkeypatch, realtime=True)
    panel = Mock()
    panel.closed.is_set.return_value = False
    monkeypatch.setattr(operator_panel, "validate_terminal", lambda: None)
    monkeypatch.setattr(operator_panel, "OperatorPanel", lambda *args: panel)
    talk.bridge.close = Mock()
    resource = getattr(talk, failure_at)
    resource.close.side_effect = OSError("cleanup failed")
    assert cli.main(["-c", "unused", "talk", "--capture", "--transmit", "--panel"]) == 1
    talk.bridge.close.assert_called_once()
    talk.controls.close.assert_called_once()
    talk.realtime.close.assert_called_once()
    panel.close.assert_called_once()


@pytest.mark.parametrize("args", [["talk", "unused.wav"], ["talk", "--capture", "--once"]])
def test_operator_mode_rejects_wav_or_single_capture(monkeypatch, args):
    monkeypatch.setattr(cli, "load_config", lambda path: Config(messaging_operator_mode=True))
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(cli, "open_agent", lambda config: pytest.fail("started before validation"))
    assert cli.main(["-c", "unused", *args]) == 1


def test_operator_mode_does_not_read_the_radio_terminals_stdin(monkeypatch):
    talk = Talk(monkeypatch)
    monkeypatch.setattr(cli.sys, "stdin", Mock(spec=[], name="stdin must stay unused"))
    assert talk.run() == 130
    talk.controls.start.assert_called_once()


@pytest.mark.parametrize("failure", [WalkietalkError("transcription failed"), ""])
def test_raw_voice_failed_transcription_does_not_allow_approval(monkeypatch, tmp_path, failure):
    talk = Talk(monkeypatch)
    incoming = Inbound("signal", "voice", audio_path=tmp_path / "voice.ogg", identity="1")
    talk.bridge.add(incoming)
    if isinstance(failure, Exception):
        talk.listener.transcribe.side_effect = failure
    else:
        talk.listener.transcribe.return_value = failure
    talk.events = [talk.command("read"), talk.command("approve"), "nana", talk.tick()]
    assert talk.run() == 130
    head = talk.review.snapshot()["item"]
    assert not head["previewed"] and head["transcript"] is None
    assert talk.bridge.peek("signal") is incoming and not talk.review.approved(incoming)
    talk.transmit.assert_not_called()


def test_realtime_raw_voice_review_retries_transcription_setup_without_rebuilding_audio(
    monkeypatch, tmp_path
):
    talk = Talk(monkeypatch, realtime=True)
    talk.bridge.add(Inbound("signal", "voice", audio_path=tmp_path / "voice.ogg", identity="1"))
    talk.listener.prepare.side_effect = [WalkietalkError("STT unavailable"), None]
    talk.listener.transcribe.return_value = "voice transcript"
    talk.events = [talk.command("read"), talk.command("approve"), talk.command("read")]
    assert talk.run(transmit=False) == 130
    assert talk.listener.prepare.call_count == 2
    cli.voice_wav.assert_called_once()
    talk.voice.synthesize.assert_not_called()
    assert talk.review.snapshot()["item"]["transcript"] == "voice transcript"
    talk.transmit.assert_not_called()


@pytest.mark.parametrize("realtime", [False, True])
@pytest.mark.parametrize("read_first", [False, True])
def test_outgoing_voice_review_uses_full_capture_transcript_and_sends_same_recording(
    monkeypatch, capsys, realtime, read_first
):
    talk = Talk(monkeypatch, realtime=realtime, mode=replace(MODE, send_as_voice=True))

    def held(on_wait):
        head = talk.review.snapshot()["item"]
        assert head["text"] == head["transcript"] == "nana hello there" and head["previewed"]
        talk.bridge.send_voice.assert_not_called()
        on_wait()

    talk.events = ["nana hello there", held]
    if read_first:
        talk.events += [talk.command("read"), talk.command("read")]
    talk.events.append(talk.command("approve"))
    assert talk.run() == 130
    talk.bridge.send_voice.assert_called_once_with("signal", CAPTURED)
    assert "Voice transcript: nana hello there" in capsys.readouterr().out
    # Reuse the capture transcript, without a second speech request.
    assert talk.listener.transcribe.call_count == (0 if realtime else 1)
    talk.transmit.assert_not_called()
