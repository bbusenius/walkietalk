from pathlib import Path

import pytest
import yaml

from walkietalk import cli
from walkietalk.capture import Utterance
from walkietalk.config import WalkietalkError, load_config
from walkietalk.term import style
from walkietalk.wake import ListeningSession, strip_wake


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def config_data():
    return yaml.safe_load(Path("config.example.yaml").read_text())


def write_config(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def session_for(tmp_path, config_data, *, mode="wake_phrase", timeout=10, clock=None):
    config_data["listening"]["mode"] = mode
    config_data["listening"]["conversation_timeout_seconds"] = timeout
    config = load_config(write_config(tmp_path, config_data))
    return ListeningSession(config, clock=clock or FakeClock())


def fake_utterance():
    return Utterance(b"\x00\x40" * 320, 16000, 0.24, 0.24, 0.5, "silence", started_at=50.0)


def test_valid_wake_and_listening_config(tmp_path, config_data):
    config = load_config(write_config(tmp_path, config_data))
    assert config.listening_mode == "wake_phrase"
    assert config.conversation_timeout_seconds == 60
    assert config.wake_primary == "charlotte"
    assert config.wake_aliases == ("charlot", "sharlot")


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("listening", "mode", "always_on"),
        ("listening", "conversation_timeout_seconds", 0),
        ("listening", "conversation_timeout_seconds", 601),
        ("wake", "primary", ""),
        ("wake", "aliases", "charlot"),
        ("wake", "aliases", [""]),
    ],
)
def test_invalid_wake_listening_rejected(tmp_path, config_data, section, field, value):
    config_data[section][field] = value
    with pytest.raises(WalkietalkError):
        load_config(write_config(tmp_path, config_data))


def test_agent_and_stt_backend_fields_still_rejected(tmp_path, config_data):
    extra = dict(config_data, agent={"backend": "stub"})
    with pytest.raises(WalkietalkError, match="exactly"):
        load_config(write_config(tmp_path, extra))
    config_data["stt"]["backend"] = "faster-whisper"
    with pytest.raises(WalkietalkError):
        load_config(write_config(tmp_path, config_data))


@pytest.mark.parametrize(
    ("text", "matched", "rest"),
    [
        ("hello kids", False, "hello kids"),
        ("charlotte what is rain", True, "what is rain"),
        ("Charlotte, what is rain?", True, "what is rain?"),
        ("CHARLOTTE: why", True, "why"),
        ("charlot what time", True, "what time"),
        ("sharlot, hello", True, "hello"),
        ("charlotte", True, ""),
        ("charlotte. what is rain", True, "what is rain"),
        ("charlotteanne hello", False, "charlotteanne hello"),
        ("hey charlotte hello", False, "hey charlotte hello"),
    ],
)
def test_strip_wake_prefix_and_aliases(text, matched, rest):
    assert strip_wake(text, "charlotte", ["charlot", "sharlot"]) == (matched, rest)


def test_strip_wake_allows_period_after_alias():
    matched, rest = strip_wake(
        "Code 9. Now you will listen, yes?",
        "code nine",
        ["code 9", "gold nine", "gold 9", "hold it tight", "court 9"],
    )
    assert matched is True
    assert rest == "Now you will listen, yes?"


def test_wake_phrase_mode_requires_name_each_time(tmp_path, config_data):
    clock = FakeClock(0)
    session = session_for(tmp_path, config_data, mode="wake_phrase", clock=clock)
    ignored = session.decide("hello kids", 0)
    assert not ignored.accepted
    assert "charlotte" in ignored.message
    accepted = session.decide("charlotte what is the moon", 1)
    assert accepted.accepted
    assert accepted.traffic == "what is the moon"
    session.complete_turn()
    again = session.decide("why does that happen", 2)
    assert not again.accepted
    assert session.state() == "waiting_for_wake"


def test_conversation_follow_up_before_deadline(tmp_path, config_data):
    clock = FakeClock(100)
    session = session_for(tmp_path, config_data, mode="conversation", timeout=10, clock=clock)
    first = session.decide("charlotte what is rain", 100)
    assert first.accepted
    clock.now = 105
    session.complete_turn()
    assert session.state() == "awake"
    follow = session.decide("why does that happen", 114.9)
    assert follow.accepted
    assert follow.kind == "follow_up"
    assert follow.traffic == "why does that happen"


def test_conversation_deadline_requires_wake_again(tmp_path, config_data):
    clock = FakeClock(100)
    session = session_for(tmp_path, config_data, mode="conversation", timeout=10, clock=clock)
    session.decide("charlotte hello", 100)
    clock.now = 101
    session.complete_turn()
    ignored = session.decide("why", 111)
    assert not ignored.accepted
    named = session.decide("charlotte why", 112)
    assert named.accepted
    assert named.traffic == "why"


def test_expire_if_needed_announces_once(tmp_path, config_data):
    clock = FakeClock(0)
    session = session_for(tmp_path, config_data, mode="conversation", timeout=5, clock=clock)
    session.decide("charlotte hello", 0)
    clock.now = 1
    session.complete_turn()
    assert session.expire_if_needed() is None
    clock.now = 6
    message = session.expire_if_needed()
    assert message is not None
    assert "Follow-up window ended" in message
    assert session.state() == "waiting_for_wake"
    assert session.expire_if_needed() is None


def test_speech_starting_at_deadline_requires_wake(tmp_path, config_data):
    clock = FakeClock(50)
    session = session_for(tmp_path, config_data, mode="conversation", timeout=10, clock=clock)
    session.decide("charlotte hello", 50)
    clock.now = 50
    session.complete_turn()
    boundary = session.decide("follow up", 60)
    assert not boundary.accepted
    just_before = session.decide("follow up", 59.999)
    assert just_before.accepted


def test_empty_transcript_does_not_open_window(tmp_path, config_data):
    clock = FakeClock(0)
    session = session_for(tmp_path, config_data, mode="conversation", timeout=10, clock=clock)
    assert not session.decide("   ", 0).accepted
    assert session.state() == "waiting_for_wake"
    session.decide("charlotte hello", 2)
    clock.now = 3
    session.complete_turn()
    assert session.state() == "awake"
    session.decide("", 4)
    assert session.state() == "awake"


def test_conversation_wake_only_opens_follow_up_window(tmp_path, config_data):
    clock = FakeClock(0)
    session = session_for(tmp_path, config_data, mode="conversation", timeout=10, clock=clock)
    check_in = session.decide("charlotte", 0)
    assert check_in.kind == "wake_only"
    assert not check_in.accepted
    clock.now = 1
    session.complete_turn()
    assert session.state() == "awake"
    follow = session.decide("turn on the light", 2)
    assert follow.accepted
    assert follow.kind == "follow_up"
    assert follow.traffic == "turn on the light"


def test_wake_phrase_wake_only_does_not_open_window(tmp_path, config_data):
    clock = FakeClock(0)
    session = session_for(tmp_path, config_data, mode="wake_phrase", timeout=10, clock=clock)
    check_in = session.decide("charlotte", 0)
    assert check_in.kind == "empty"
    session.complete_turn()
    assert session.state() == "waiting_for_wake"
    assert not session.decide("turn on the light", 1).accepted


def test_timeout_duration_is_taken_from_config(tmp_path, config_data):
    clock = FakeClock(0)
    short = session_for(tmp_path, config_data, mode="conversation", timeout=5, clock=clock)
    short.decide("charlotte hello", 0)
    clock.now = 1
    short.complete_turn()
    assert short.decide("next", 5.9).accepted
    assert not short.decide("next", 6).accepted
    clock.now = 0
    long = session_for(tmp_path, config_data, mode="conversation", timeout=20, clock=clock)
    long.decide("charlotte hello", 0)
    clock.now = 1
    long.complete_turn()
    assert long.decide("next", 20.9).accepted
    assert not long.decide("next", 21).accepted


def test_talk_once_does_not_open_ptt(monkeypatch, tmp_path, config_data, capsys):
    config_path = write_config(tmp_path, config_data)

    def forbidden(*args, **kwargs):
        pytest.fail("talk opened PTT or playback")

    monkeypatch.setattr(cli, "preflight", lambda config, **kwargs: 0)
    monkeypatch.setattr(cli, "SerialPTT", forbidden)
    monkeypatch.setattr(cli, "Playback", forbidden)
    monkeypatch.setattr(cli, "load_model", lambda name: object())
    monkeypatch.setattr(cli, "capture_from_device", lambda *args, **kwargs: fake_utterance())
    monkeypatch.setattr(cli, "transcribe_audio", lambda model, pcm, rate: "charlotte what is rain")
    assert cli.main(["-c", str(config_path), "talk", "--capture", "--once"]) == 0
    output = capsys.readouterr().out
    assert "Receive-only" in output
    assert "Accepted" in output
    assert "Traffic: what is rain" in output
    assert "Simulated reply complete." in output


def test_talk_conversation_wake_only_opens_window(monkeypatch, tmp_path, config_data, capsys):
    config_data["listening"]["mode"] = "conversation"
    config_path = write_config(tmp_path, config_data)
    monkeypatch.setattr(cli, "preflight", lambda config, **kwargs: 0)
    monkeypatch.setattr(cli, "load_model", lambda name: object())
    monkeypatch.setattr(cli, "capture_from_device", lambda *args, **kwargs: fake_utterance())
    monkeypatch.setattr(cli, "transcribe_audio", lambda model, pcm, rate: "charlotte")
    assert cli.main(["-c", str(config_path), "talk", "--capture", "--once"]) == 0
    output = capsys.readouterr().out
    assert "Wake heard; listening for traffic." in output
    assert "Simulated reply complete." not in output
    assert "State: awake" in output


def test_talk_ignores_speech_without_wake(monkeypatch, tmp_path, config_data, capsys):
    config_path = write_config(tmp_path, config_data)
    monkeypatch.setattr(cli, "preflight", lambda config, **kwargs: 0)
    monkeypatch.setattr(cli, "load_model", lambda name: object())
    monkeypatch.setattr(cli, "capture_from_device", lambda *args, **kwargs: fake_utterance())
    monkeypatch.setattr(cli, "transcribe_audio", lambda model, pcm, rate: "hello kids")
    assert cli.main(["-c", str(config_path), "talk", "--capture", "--once"]) == 0
    assert "Ignored" in capsys.readouterr().out


def test_color_off_when_no_color_set(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert style("transcript", "Transcript: hi") == "Transcript: hi"


def test_force_color_wraps_transcript_and_keeps_words(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    painted = style("transcript", "Transcript: hi")
    assert "Transcript: hi" in painted
    assert painted.startswith("\033")
    assert painted.endswith("\033[0m")
