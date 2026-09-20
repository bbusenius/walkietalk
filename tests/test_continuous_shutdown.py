import array
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from walkietalk import cli
from walkietalk.agent import Turn
from walkietalk.capture import Utterance, collect_utterance
from walkietalk.config import Config, WalkietalkError, load_config
from walkietalk.shutdown import ShutdownSession
from walkietalk.wake import ListeningSession


@pytest.fixture
def config():
    return replace(
        Config(),
        listening_mode="conversation",
        wake_primary="code nine",
        wake_aliases=("code 9",),
        shutdown_enabled=True,
        shutdown_phrase="bridge shutdown",
        shutdown_phrase_aliases=("bridge stop",),
        shutdown_code="confirm alpha nine",
        shutdown_code_aliases=("confirm alpha 9",),
        shutdown_confirmation_seconds=30,
    )


@pytest.fixture(autouse=True)
def no_radio_transmission(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("continuous listening or shutdown touched PTT/playback")

    for name in ("SerialPTT", "DryPTT", "Playback", "transmit"):
        monkeypatch.setattr(cli, name, forbidden)


def test_shutdown_accepts_two_utterances_and_never_uses_wake_window(config):
    now = [100]
    shutdown = ShutdownSession(config, clock=lambda: now[0])
    assert shutdown.decide("confirm alpha 9", 100).kind == "rejected"
    assert shutdown.armed_until is None
    assert shutdown.decide("Bridge STOP!", 100).kind == "armed"
    now[0] = 110
    assert shutdown.decide("Confirm alpha 9.", 110).kind == "confirmed"
    assert shutdown.armed_until is None


@pytest.mark.parametrize(
    "text",
    [
        "bridge shutdown confirm alpha nine",
        "Bridge STOP: confirm alpha 9!",
        "Code nine, bridge shutdown. Confirm alpha nine.",
        "Code 9: bridge stop, confirm alpha 9.",
    ],
)
@pytest.mark.parametrize("armed_at", [None, 99, 1])
def test_combined_command_confirms_with_aliases_and_any_prior_arming_state(config, text, armed_at):
    shutdown = ShutdownSession(config, clock=lambda: 100)
    shutdown.armed_until = None if armed_at is None else armed_at + 30
    assert shutdown.decide(text, 100).kind == "confirmed"
    assert shutdown.armed_until is None


@pytest.mark.parametrize(
    "text",
    [
        "bridge shutdown wrong code",
        "bridge shutdown confirm alpha",
        "bridge shutdown confirm alpha nine please",
        "bridge shutdown now confirm alpha nine",
        "bridge shutdowns confirm alpha nine",
        "please bridge shutdown confirm alpha nine",
        "confirm alpha nine bridge shutdown",
    ],
)
def test_inexact_combined_command_does_not_arm_or_shutdown(config, text):
    shutdown = ShutdownSession(config, clock=lambda: 100)
    assert shutdown.decide(text, 100).kind in {"none", "rejected"}
    assert shutdown.armed_until is None


@pytest.mark.parametrize("text", ["wrong code", "", "code nine hello", "confirm alpha nine extra"])
def test_wrong_or_empty_next_utterance_disarms(config, text):
    shutdown = ShutdownSession(config, clock=lambda: 100)
    shutdown.decide("bridge shutdown", 100)
    assert shutdown.decide(text, 101).kind == "rejected"
    assert shutdown.armed_until is None
    assert shutdown.decide("confirm alpha nine", 102).kind == "rejected"


def test_code_may_finish_transcription_after_deadline_if_speech_started_inside(config):
    now = [100]
    shutdown = ShutdownSession(config, clock=lambda: now[0])
    shutdown.decide("bridge shutdown", 100)
    now[0] = 140
    assert shutdown.decide("confirm alpha nine", 129.9).kind == "confirmed"
    now[0] = 150
    shutdown.decide("bridge shutdown", 150)
    assert shutdown.decide("confirm alpha nine", 180).kind == "rejected"


def test_expiry_is_reported_once_and_disarms(config):
    now = [100]
    shutdown = ShutdownSession(config, clock=lambda: now[0])
    shutdown.decide("bridge shutdown", 100)
    now[0] = 129.9
    assert shutdown.expire_if_needed() is None
    now[0] = 130
    assert "expired" in shutdown.expire_if_needed()
    assert shutdown.expire_if_needed() is None
    assert shutdown.decide("confirm alpha nine", 131).kind == "rejected"


def test_optional_wake_prefix_and_punctuation(config):
    shutdown = ShutdownSession(config, clock=lambda: 100)
    assert shutdown.decide("Code 9. Bridge shutdown!", 100).kind == "armed"
    assert shutdown.decide("CODE NINE: Confirm, alpha-nine.", 101).kind == "confirmed"


@pytest.mark.parametrize(
    "text", ["discuss bridge shutdown", "bridge shutdowns", "a confirm alpha nine"]
)
def test_no_substring_or_fuzzy_shutdown(config, text):
    assert ShutdownSession(config).decide(text, 100).kind == "none"


def test_disabled_shutdown_leaves_all_traffic_alone(config):
    shutdown = ShutdownSession(replace(config, shutdown_enabled=False))
    assert shutdown.decide("bridge shutdown", 100).kind == "none"
    assert shutdown.decide("confirm alpha nine", 101).kind == "none"
    assert shutdown.decide("bridge shutdown confirm alpha nine", 102).kind == "none"
    assert shutdown.armed_until is None


def install_fakes(monkeypatch, config, texts, *, answers=None, starts=None):
    listener = Mock()
    listener.label.return_value = "fake STT"
    listener.transcribe.side_effect = texts
    agent = Mock()
    agent.label.return_value = "fake agent"
    agent.reply.side_effect = answers
    agent.reply.return_value = "A short answer."
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "open_stt", lambda config: listener)
    monkeypatch.setattr(cli, "open_agent", lambda config: agent)
    monkeypatch.setattr(cli, "preflight", lambda *args, **kwargs: None)
    calls = []

    def capture(device, cfg, wait, **kwargs):
        calls.append(wait)
        if len(calls) > len(texts):
            raise KeyboardInterrupt
        start = starts[len(calls) - 1] if starts else None
        return Utterance(b"\x00\x40" * 320, 16000, 0.5, 0.5, 1, "silence", start)

    monkeypatch.setattr(cli, "capture_from_device", capture)
    return listener, agent, calls


@pytest.mark.parametrize(
    "texts",
    [["bridge shutdown", "confirm alpha nine"], ["bridge shutdown confirm alpha nine"]],
)
def test_continuous_shutdown_exits_successfully_without_agent_or_code_logging(
    config, monkeypatch, capsys, texts
):
    _, agent, calls = install_fakes(monkeypatch, config, texts)
    assert cli.main(["-c", "unused", "talk", "--capture"]) == 0
    assert calls == [None] * len(texts)
    agent.reply.assert_not_called()
    output = capsys.readouterr()
    assert ("Shutdown armed" in output.out) == (len(texts) == 2)
    assert "Shutdown confirmed" in output.out
    assert "confirm alpha nine" not in output.out + output.err
    assert "Reply:" not in output.out


@pytest.mark.parametrize(
    "failure",
    [
        WalkietalkError("timed out"),
        WalkietalkError("login expired"),
        OSError("network unavailable"),
        "x" * 601,
    ],
)
def test_agent_failure_recovers_requires_wake_and_preserves_only_completed_pairs(
    config, monkeypatch, capsys, failure
):
    texts = [
        "code nine first",
        "second",
        "unaddressed retry",
        "code nine third",
        "bridge shutdown",
        "confirm alpha nine",
    ]
    _, agent, calls = install_fakes(
        monkeypatch, config, texts, answers=["First answer.", failure, "Third answer."]
    )
    assert cli.main(["-c", "unused", "talk", "--capture"]) == 0
    assert all(wait is None for wait in calls)
    assert [call.args[0] for call in agent.reply.call_args_list] == ["first", "second", "third"]
    contexts = [call.args[1] for call in agent.reply.call_args_list]
    assert contexts[0].session_id == contexts[1].session_id
    assert contexts[2].session_id != contexts[1].session_id
    assert contexts[2].history == (Turn("first", "First answer."),)
    output = capsys.readouterr()
    assert "Agent failed:" in output.err
    assert output.out.count("Reply:") == 2


def test_stt_failure_disarms_shutdown_and_returns_to_listening(config, monkeypatch, capsys):
    texts = [
        "bridge shutdown",
        WalkietalkError("STT timed out"),
        "confirm alpha nine",
        "bridge shutdown",
        "confirm alpha nine",
    ]
    _, agent, _ = install_fakes(monkeypatch, config, texts)
    assert cli.main(["-c", "unused", "talk", "--capture"]) == 0
    agent.reply.assert_not_called()
    output = capsys.readouterr()
    assert "Transcription failed:" in output.err
    assert "shutdown is not armed" in output.out


def test_wrong_confirmation_is_not_forwarded_even_when_conversation_open(config, monkeypatch):
    texts = [
        "code nine hello",
        "bridge shutdown",
        "code nine incorrect code",
        "why is that",
        "bridge shutdown",
        "confirm alpha nine",
    ]
    _, agent, _ = install_fakes(monkeypatch, config, texts)
    assert cli.main(["-c", "unused", "talk", "--capture"]) == 0
    assert [call.args[0] for call in agent.reply.call_args_list] == ["hello"]


def test_waiting_hours_expire_followup_and_shutdown_but_not_program(config, monkeypatch, capsys):
    now = [100]
    gate = ListeningSession(config, clock=lambda: now[0])
    shutdown = ShutdownSession(config, clock=lambda: now[0])
    texts = [
        "code nine hello",
        "bridge shutdown",
        "why is that",
        "code nine again",
        "bridge shutdown",
        "confirm alpha nine",
    ]
    _, agent, calls = install_fakes(
        monkeypatch, config, texts, starts=[100, 101, 10000, 10001, 10002, 10003]
    )
    original = cli.capture_from_device

    def capture(*args, **kwargs):
        if len(calls) == 2:
            now[0] = 10000
            kwargs["on_wait"]()
        return original(*args, **kwargs)

    monkeypatch.setattr(cli, "capture_from_device", capture)
    monkeypatch.setattr(cli, "ListeningSession", lambda config: gate)
    monkeypatch.setattr(cli, "ShutdownSession", lambda config: shutdown)
    assert cli.main(["-c", "unused", "talk", "--capture"]) == 0
    assert [call.args[0] for call in agent.reply.call_args_list] == ["hello", "again"]
    assert "expired" in capsys.readouterr().out


def test_capture_accepts_speech_after_hours_of_quiet_with_bounded_recording(monkeypatch):
    now = [100]
    monkeypatch.setattr("walkietalk.capture.time.monotonic", lambda: now[0])
    quiet = b"\x00\x00" * 320
    loud = array.array("h", [6000] * 320).tobytes()

    def frames():
        for n in range(100):
            now[0] = 100 + n * 3600
            yield quiet, False
        yield from [(loud, False)] * 15
        yield from [(quiet, False)] * 25

    utterance = collect_utterance(
        frames(),
        rate=16000,
        energy_threshold=0.02,
        hangover_ms=400,
        max_utterance_seconds=12,
        wait_deadline=None,
        log=lambda line: None,
    )
    assert utterance.started_at > 3600
    assert utterance.duration < 2
    assert utterance.end_reason == "silence"


def test_device_failure_remains_fatal(config, monkeypatch, capsys):
    install_fakes(monkeypatch, config, [])
    monkeypatch.setattr(
        cli, "capture_from_device", Mock(side_effect=WalkietalkError("AIOC disconnected"))
    )
    assert cli.main(["-c", "unused", "talk", "--capture"]) == 1
    assert "AIOC disconnected" in capsys.readouterr().err


def test_once_retains_bounded_wait_and_failure_exit(config, monkeypatch):
    _, _, calls = install_fakes(
        monkeypatch, config, ["code nine hello"], answers=[WalkietalkError("timed out")]
    )
    assert cli.main(["-c", "unused", "talk", "--capture", "--once", "--timeout", "2"]) == 1
    assert calls == [2]


def test_timeout_in_continuous_mode_has_clear_usage_error(config, monkeypatch, capsys):
    _, agent, calls = install_fakes(monkeypatch, config, [])
    assert cli.main(["-c", "unused", "talk", "--capture", "--timeout", "300"]) == 1
    assert calls == []
    agent.reply.assert_not_called()
    assert "omit it for continuous listening" in capsys.readouterr().err


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", "yes"),
        ("phrase", ""),
        ("code", ""),
        ("phrase", "!!!"),
        ("code", 123),
        ("phrase_aliases", [""]),
        ("code_aliases", [False]),
        ("phrase_aliases", "bridge stop"),
        ("confirmation_seconds", 0),
        ("confirmation_seconds", 301),
        ("confirmation_seconds", True),
        ("phrase", "charlotte"),
        ("code", "bridge shutdown"),
        ("code_aliases", ["bridge shutdown"]),
    ],
)
def test_invalid_shutdown_configuration(tmp_path, field, value):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["shutdown"].update(enabled=True, phrase="bridge shutdown", code="confirm alpha nine")
    data["shutdown"][field] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WalkietalkError, match="[Ss]hutdown"):
        load_config(path)


@pytest.mark.parametrize("interrupt", [False, True])
def test_unbounded_device_capture_and_interrupt_close_stream(monkeypatch, interrupt):
    from walkietalk.capture import capture_from_device

    quiet = b"\x00\x00" * 320
    loud = array.array("h", [6000] * 320).tobytes()
    readings = [(quiet, False)] * 100
    readings += [KeyboardInterrupt()] if interrupt else [(loud, False)] * 15 + [(quiet, False)] * 25
    stream = Mock()
    stream.read.side_effect = readings
    monkeypatch.setattr("walkietalk.capture.audio_devices", lambda: [{"default_samplerate": 16000}])
    monkeypatch.setattr("walkietalk.capture.resolve_device", lambda *args: 0)
    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(
            check_input_settings=lambda **kwargs: None,
            RawInputStream=lambda **kwargs: stream,
            PortAudioError=type("PortAudioError", (Exception,), {}),
        ),
    )
    logs = []
    if interrupt:
        with pytest.raises(KeyboardInterrupt):
            capture_from_device("fake AIOC", Config(), None, log=logs.append)
    else:
        assert (
            capture_from_device("fake AIOC", Config(), None, log=logs.append).end_reason
            == "silence"
        )
    stream.close.assert_called_once()
    assert any("no idle time limit" in line for line in logs)


def test_shutdown_section_is_required_and_disabled_blank_example_loads(tmp_path):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    assert not load_config(Path("config.example.yaml")).shutdown_enabled
    del data["shutdown"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WalkietalkError, match="shutdown"):
        load_config(path)


def test_configured_commands_can_include_the_wake_prefix(config):
    config = replace(config, shutdown_phrase="code nine stop", shutdown_code="code nine confirm")
    session = ShutdownSession(config, clock=lambda: 100)
    assert session.decide("Code nine stop.", 100).kind == "armed"
    assert session.decide("Code nine confirm.", 101).kind == "confirmed"
