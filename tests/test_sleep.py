"""Sleep controls close conversation eligibility without losing completed history."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from walkietalk import cli
from walkietalk.agent import Turn
from walkietalk.config import Config, WalkietalkError, load_config
from walkietalk.stt import open_stt
from walkietalk.wake import ListeningSession


@pytest.fixture
def config():
    return replace(
        Config(),
        listening_mode="conversation",
        sleep_primary="go to sleep",
        sleep_aliases=("stop listening",),
        sleep_confirmation_phrase="Standing by.",
    )


@pytest.mark.parametrize("mode", ["conversation", "wake_phrase"])
@pytest.mark.parametrize("awake", [False, True])
@pytest.mark.parametrize(
    "phrase",
    ["go to sleep", "STOP LISTENING!", "Charlotte, go to sleep.", "go, to-sleep!"],
)
def test_sleep_closes_window_and_requires_wake(config, mode, awake, phrase):
    gate = ListeningSession(replace(config, listening_mode=mode), clock=lambda: 100)
    if awake:
        gate.complete_turn()
    decision = gate.decide(phrase, 100)
    assert decision.kind == "sleep"
    assert not decision.accepted
    assert decision.traffic == ""
    assert decision.state == gate.state() == "waiting_for_wake"
    assert gate.awake_until is None
    assert not gate.decide("talking to someone else", 101).accepted
    assert gate.decide("charlotte next question", 102).accepted
    gate.complete_turn()
    assert gate.follow_up_open_at(103) == (mode == "conversation")
    assert not gate.follow_up_open_at(100 + config.conversation_timeout_seconds)


@pytest.mark.parametrize(
    "text",
    ["go to sleeps", "please go to sleep", "go to sleep now", "explain stop listening", ""],
)
def test_sleep_requires_complete_phrase(config, text):
    gate = ListeningSession(config, clock=lambda: 100)
    gate.complete_turn()
    assert gate.decide(text, 100).kind != "sleep"
    assert gate.state() == "awake"


def test_sleep_disabled_by_default():
    gate = ListeningSession(replace(Config(), listening_mode="conversation"), clock=lambda: 100)
    gate.complete_turn()
    assert gate.decide("go to sleep", 100).kind == "follow_up"


def write_config(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_sleep_configuration_and_legacy_compatibility(tmp_path):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    config = load_config(write_config(tmp_path, data))
    assert config.sleep_primary == "go to sleep"
    assert config.sleep_aliases == ("stop listening",)
    assert config.sleep_confirmation_phrase == "Standing by."
    del data["sleep"]
    legacy = load_config(write_config(tmp_path, data))
    assert legacy.sleep_primary == ""
    assert legacy.sleep_aliases == ()
    assert legacy.sleep_confirmation_phrase == ""


@pytest.mark.parametrize(
    "field,value",
    [
        ("primary", None),
        ("primary", "!!!"),
        ("primary", "x" * 201),
        ("primary", "sleep\nnow"),
        ("primary", ""),
        ("aliases", "stop"),
        ("aliases", [""]),
        ("aliases", [None]),
        ("aliases", ["!!!"]),
        ("aliases", ["sleep\nnow"]),
        ("confirmation_phrase", False),
        ("confirmation_phrase", "!!!"),
        ("confirmation_phrase", "x" * 201),
        ("primary", "charlotte"),
        ("aliases", ["charlot"]),
        ("confirmation_phrase", "Charlotte, go to sleep!"),
    ],
)
def test_invalid_sleep_config_rejected(tmp_path, field, value):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["sleep"][field] = value
    with pytest.raises(WalkietalkError, match="[Ss]leep"):
        load_config(write_config(tmp_path, data))


@pytest.mark.parametrize("value", [None, [], {}, {"primary": "sleep"}])
def test_malformed_sleep_section_rejected(tmp_path, value):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["sleep"] = value
    with pytest.raises(WalkietalkError, match="sleep"):
        load_config(write_config(tmp_path, data))


def test_sleep_cannot_collide_with_enabled_shutdown(tmp_path):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["shutdown"].update(
        enabled=True,
        phrase="go to sleep",
        code="confirm nine",
        arm_confirmation_phrase="Ready to shut down.",
        confirmation_phrase="Shutting down.",
    )
    with pytest.raises(WalkietalkError, match="Sleep phrases must differ"):
        load_config(write_config(tmp_path, data))


@pytest.mark.parametrize("backend", ["grok", "grok_api"])
def test_sleep_phrases_are_transcription_keyterms(config, backend):
    stt = open_stt(replace(config, stt_backend=backend))
    assert "go to sleep" in stt.keyterms
    assert "stop listening" in stt.keyterms


@pytest.mark.parametrize("ack_result", ["spoken", "silent", "failed"])
def test_cli_sleep_ack_stays_closed_and_wake_preserves_history(config, monkeypatch, ack_result):
    gate = ListeningSession(config, clock=lambda: 100)
    texts = iter(
        [
            "charlotte first question",
            "stop listening",
            "nearby chatter",
            "charlotte second question",
        ]
    )
    listener = Mock()
    listener.transcribe.side_effect = lambda *_: next(texts)
    agent = Mock()
    agent.reply.return_value = "An answer."
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "ListeningSession", lambda _: gate)
    monkeypatch.setattr(cli, "open_stt", lambda _: listener)
    monkeypatch.setattr(cli, "open_agent", lambda _: agent)
    monkeypatch.setattr(cli, "preflight", lambda *_, **__: None)
    monkeypatch.setattr(cli, "open_tts", lambda _: Mock())
    voice_wav = Mock(duration=0.1)
    monkeypatch.setattr(cli, "radio_wav", lambda *_, **__: voice_wav)
    monkeypatch.setattr(cli, "transmit_speech", Mock())
    mute = Mock()
    monkeypatch.setattr(cli, "wait_post_tx_mute", mute)
    captures = 0

    def capture(*_, **__):
        nonlocal captures
        captures += 1
        if captures > 4:
            raise KeyboardInterrupt
        if captures in (3, 4):
            assert gate.awake_until is None
        return SimpleNamespace(pcm=b"", rate=16000, started_at=100)

    def acknowledge(cfg, voice, text, **kwargs):
        assert gate.awake_until is None  # Closed even before synthesis or transmission.
        assert text == "Standing by."
        assert kwargs["transmit"]
        return ack_result

    ack = Mock(side_effect=acknowledge)
    monkeypatch.setattr(cli, "acknowledge", ack)
    monkeypatch.setattr(cli, "capture_from_device", capture)
    assert cli.main(["-c", "unused", "--no-env-file", "talk", "--capture", "--transmit"]) == 130
    ack.assert_called_once()
    assert mute.call_count == 2 + (ack_result == "spoken")
    assert [call.args[0] for call in agent.reply.call_args_list] == [
        "first question",
        "second question",
    ]
    first, second = [call.args[1] for call in agent.reply.call_args_list]
    assert first.session_id == second.session_id
    assert second.history == (Turn("first question", "An answer."),)
