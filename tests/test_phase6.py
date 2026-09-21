import time
from dataclasses import replace
from unittest.mock import Mock

import pytest

from walkietalk import cli
from walkietalk.agent import Turn
from walkietalk.audio import Wav, read_wav
from walkietalk.capture import Utterance
from walkietalk.config import Config, WalkietalkError
from walkietalk.wake import ListeningSession

SPEECH = Wav(b"\x00\x20" * 4800, 48000, 0.1)
ARGS = ["-c", "unused.yaml", "talk", "--capture", "--transmit"]


@pytest.fixture
def bridge(monkeypatch):
    config = replace(Config(), listening_mode="conversation", settle_seconds=0)
    backend = Mock()
    backend.reply.return_value = "Rain falls from clouds."
    voice = Mock()
    voice.synthesize.return_value = SPEECH
    listener = Mock()
    listener.transcribe.return_value = "charlotte what is rain"
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "open_agent", lambda config: backend)
    monkeypatch.setattr(cli, "open_stt", lambda config: listener)
    monkeypatch.setattr(cli, "open_tts", lambda config: voice)
    monkeypatch.setattr(cli, "preflight", lambda config, **kwargs: None)
    monkeypatch.setattr(cli, "capture_from_device", lambda *a, **k: utterance(100))
    for name in ("SerialPTT", "Playback"):
        monkeypatch.setattr(cli, name, lambda *a, **k: pytest.fail("unexpected hardware access"))
    return config, backend, voice, listener


def utterance(started):
    return Utterance(b"\x00\x40" * 320, 16000, 0.5, 0.5, 1, "silence", started)


def test_transmission_needs_explicit_config():
    assert cli.main(["talk", "file.wav", "--transmit"]) == 1


def test_default_talk_does_not_even_prepare_voice(bridge, monkeypatch):
    monkeypatch.setattr(cli, "open_tts", lambda *a: pytest.fail("opened voice"))
    assert cli.main(["-c", "unused", "talk", "--capture", "--once"]) == 0


@pytest.mark.parametrize("text", ["", "ordinary chatter", "charlotte"])
def test_ignored_and_wake_only_do_not_synthesize(bridge, text):
    _, backend, voice, listener = bridge
    listener.transcribe.return_value = text
    assert cli.main([*ARGS, "--once"]) == 0
    voice.synthesize.assert_not_called()
    backend.reply.assert_not_called()


def test_shutdown_does_not_synthesize_without_confirmation_phrase(bridge, monkeypatch):
    config, backend, voice, listener = bridge
    config = replace(
        config, shutdown_enabled=True, shutdown_phrase="stop bridge", shutdown_code="confirm stop"
    )
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    listener.transcribe.return_value = "stop bridge confirm stop"
    assert cli.main([*ARGS, "--once"]) == 0
    backend.reply.assert_not_called()
    voice.synthesize.assert_not_called()


def test_confirmed_shutdown_speaks_confirmation_then_exits(bridge, monkeypatch, capsys):
    config, backend, voice, listener = bridge
    config = replace(
        config,
        shutdown_enabled=True,
        shutdown_phrase="stop bridge",
        shutdown_code="confirm stop",
        shutdown_arm_confirmation_phrase="Shutdown armed.",
        shutdown_confirmation_phrase="Walkietalk shutting down.",
    )
    played = []
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "transmit_speech", lambda speech, cfg, **kwargs: played.append(speech))
    listener.transcribe.return_value = "stop bridge confirm stop"
    assert cli.main([*ARGS, "--once"]) == 0
    backend.reply.assert_not_called()
    voice.synthesize.assert_called_once_with("Walkietalk shutting down.")
    assert played == [SPEECH]
    output = capsys.readouterr()
    assert "Shutdown confirmed" in output.out
    assert "Reply:" not in output.out


def test_two_step_shutdown_speaks_confirmation_after_code(bridge, monkeypatch):
    config, backend, voice, listener = bridge
    config = replace(
        config,
        shutdown_enabled=True,
        shutdown_phrase="stop bridge",
        shutdown_code="confirm stop",
        shutdown_confirmation_phrase="Walkietalk shutting down.",
    )
    played = []
    listener.transcribe.side_effect = ["stop bridge", "confirm stop"]
    started = time.monotonic()
    captures = iter([utterance(started), utterance(started + 1)])

    def capture(*a, **k):
        try:
            return next(captures)
        except StopIteration:
            pytest.fail("continued listening after confirmed shutdown")

    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "capture_from_device", capture)
    monkeypatch.setattr(cli, "transmit_speech", lambda speech, cfg, **kwargs: played.append(speech))
    assert cli.main(["-c", "unused.yaml", "talk", "--capture", "--transmit"]) == 0
    backend.reply.assert_not_called()
    voice.synthesize.assert_called_once_with("Walkietalk shutting down.")
    assert played == [SPEECH]


def test_armed_shutdown_does_not_speak_confirmation(bridge, monkeypatch):
    config, backend, voice, listener = bridge
    config = replace(
        config,
        shutdown_enabled=True,
        shutdown_phrase="stop bridge",
        shutdown_code="confirm stop",
        shutdown_confirmation_phrase="Walkietalk shutting down.",
    )
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(
        cli, "transmit_speech", lambda *a, **k: pytest.fail("spoke confirmation before code")
    )
    listener.transcribe.return_value = "stop bridge"
    assert cli.main([*ARGS, "--once"]) == 0
    voice.synthesize.assert_not_called()
    backend.reply.assert_not_called()


def test_armed_shutdown_speaks_phrase_confirmation_and_stays_available(bridge, monkeypatch):
    config, backend, voice, listener = bridge
    config = replace(
        config,
        shutdown_enabled=True,
        shutdown_phrase="stop bridge",
        shutdown_code="confirm stop",
        shutdown_arm_confirmation_phrase="Shutdown armed.",
        shutdown_confirmation_phrase="Walkietalk shutting down.",
    )
    listener.transcribe.side_effect = ["stop bridge", "confirm stop"]
    started = time.monotonic()
    captures = iter([utterance(started), utterance(started + 1)])

    def capture(*a, **k):
        try:
            return next(captures)
        except StopIteration:
            pytest.fail("continued listening after confirmed shutdown")

    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "capture_from_device", capture)
    monkeypatch.setattr(cli, "transmit_speech", lambda *a, **k: None)
    assert cli.main(["-c", "unused.yaml", "talk", "--capture", "--transmit"]) == 0
    assert [call.args[0] for call in voice.synthesize.call_args_list] == [
        "Shutdown armed.",
        "Walkietalk shutting down.",
    ]
    backend.reply.assert_not_called()


def test_failed_phrase_confirmation_stays_armed_for_the_code(bridge, monkeypatch, capsys):
    config, backend, voice, listener = bridge
    config = replace(
        config,
        shutdown_enabled=True,
        shutdown_phrase="stop bridge",
        shutdown_code="confirm stop",
        shutdown_arm_confirmation_phrase="Shutdown armed.",
        shutdown_confirmation_phrase="Walkietalk shutting down.",
    )
    listener.transcribe.side_effect = ["stop bridge", "confirm stop"]
    started = time.monotonic()
    captures = iter([utterance(started), utterance(started + 1)])
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "capture_from_device", lambda *a, **k: next(captures))
    monkeypatch.setattr(cli, "transmit_speech", lambda *a, **k: None)
    voice.synthesize.side_effect = [WalkietalkError("Piper timed out"), SPEECH]
    assert cli.main(["-c", "unused.yaml", "talk", "--capture", "--transmit"]) == 0
    output = capsys.readouterr()
    assert "Shutdown phrase confirmation failed" in output.err
    assert "Still armed" in output.out
    assert [call.args[0] for call in voice.synthesize.call_args_list] == [
        "Shutdown armed.",
        "Walkietalk shutting down.",
    ]
    backend.reply.assert_not_called()


def test_wake_only_speaks_confirmation_without_calling_the_agent(bridge, monkeypatch):
    config, backend, voice, listener = bridge
    config = replace(config, wake_confirmation_phrase="Go ahead.")
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "transmit_speech", lambda *a, **k: None)
    listener.transcribe.return_value = "charlotte"
    assert cli.main([*ARGS, "--once"]) == 0
    voice.synthesize.assert_called_once_with("Go ahead.")
    backend.reply.assert_not_called()


def test_wake_plus_traffic_does_not_speak_the_wake_confirmation(bridge, monkeypatch):
    config, backend, voice, listener = bridge
    config = replace(config, wake_confirmation_phrase="Go ahead.")
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "transmit_speech", lambda *a, **k: None)
    listener.transcribe.return_value = "charlotte what is rain"
    assert cli.main([*ARGS, "--once"]) == 0
    voice.synthesize.assert_called_once_with("Rain falls from clouds.")
    backend.reply.assert_called_once()


def test_acknowledgements_stay_silent_without_transmit(bridge, monkeypatch):
    config, backend, voice, listener = bridge
    config = replace(
        config,
        wake_confirmation_phrase="Go ahead.",
        shutdown_enabled=True,
        shutdown_phrase="stop bridge",
        shutdown_code="confirm stop",
        shutdown_arm_confirmation_phrase="Shutdown armed.",
        shutdown_confirmation_phrase="Walkietalk shutting down.",
    )
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "open_tts", lambda *a: pytest.fail("opened voice"))
    listener.transcribe.return_value = "charlotte"
    assert cli.main(["-c", "unused.yaml", "talk", "--capture", "--once"]) == 0
    backend.reply.assert_not_called()


def test_failed_shutdown_confirmation_still_exits(bridge, monkeypatch, capsys):
    config, backend, voice, listener = bridge
    config = replace(
        config,
        shutdown_enabled=True,
        shutdown_phrase="stop bridge",
        shutdown_code="confirm stop",
        shutdown_confirmation_phrase="Walkietalk shutting down.",
    )
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(
        cli, "transmit_speech", lambda *a, **k: pytest.fail("PTT after failed synth")
    )
    listener.transcribe.return_value = "stop bridge confirm stop"
    voice.synthesize.side_effect = WalkietalkError("Piper timed out")
    assert cli.main([*ARGS, "--once"]) == 0
    backend.reply.assert_not_called()
    output = capsys.readouterr()
    assert "Shutdown confirmation failed" in output.err
    assert "without an on-air confirmation" in output.out


@pytest.mark.parametrize("failure", ["agent", "prepare", "synthesis"])
def test_failures_never_open_ptt_or_print_answer(bridge, failure, capsys):
    _, backend, voice, _ = bridge
    if failure == "agent":
        backend.reply.side_effect = WalkietalkError("Login expired")
    elif failure == "prepare":
        voice.prepare.side_effect = WalkietalkError("Missing model")
    else:
        voice.synthesize.side_effect = WalkietalkError("Piper timed out")
    assert cli.main([*ARGS, "--once"]) == 1
    assert "Reply:" not in capsys.readouterr().out


def test_overlong_speech_is_cropped_then_transmitted(bridge, monkeypatch):
    _, backend, voice, _ = bridge
    voice.synthesize.return_value = Wav(b"\x00\x20" * 48000 * 11, 48000, 11)
    played = []
    monkeypatch.setattr(cli, "transmit_speech", lambda speech, cfg, **k: played.append(speech))
    assert cli.main([*ARGS, "--once"]) == 0
    backend.reply.assert_called_once()
    assert played[0].duration == pytest.approx(10)


def test_failed_speech_is_not_retained_and_continuous_listening_recovers(bridge, monkeypatch):
    _, backend, voice, listener = bridge
    listener.transcribe.side_effect = ["charlotte first", "unaddressed", "charlotte second"]
    voice.synthesize.side_effect = [WalkietalkError("Piper timed out"), SPEECH]
    captures = iter([utterance(100), utterance(101), utterance(102)])

    def capture(*a, **k):
        try:
            return next(captures)
        except StopIteration:
            raise KeyboardInterrupt from None

    monkeypatch.setattr(cli, "capture_from_device", capture)
    playback = Mock()
    monkeypatch.setattr(cli, "transmit_speech", playback)
    assert cli.main(ARGS) == 130
    assert [call.args[0] for call in backend.reply.call_args_list] == ["first", "second"]
    contexts = [call.args[1] for call in backend.reply.call_args_list]
    assert contexts[0].history == contexts[1].history == ()
    assert contexts[0].session_id != contexts[1].session_id
    playback.assert_called_once()


def test_followup_window_starts_after_speech_and_unkey_in_same_session(bridge, monkeypatch):
    config, backend, voice, listener = bridge
    now = [100]
    gate = ListeningSession(config, clock=lambda: now[0])
    monkeypatch.setattr(cli, "ListeningSession", lambda config: gate)
    listener.transcribe.side_effect = ["charlotte what is rain", "why does that happen"]
    count = [0]
    capturing = [False]

    def capture(*a, **k):
        count[0] += 1
        assert not capturing[0]
        if count[0] > 2:
            raise KeyboardInterrupt
        if count[0] == 2:
            assert gate.awake_until == 120 + config.conversation_timeout_seconds
        capturing[0] = True
        result = utterance(now[0])
        capturing[0] = False
        return result

    def synthesize(text):
        assert not capturing[0]
        assert gate.awake_until is None
        now[0] += 15
        return SPEECH

    def transmit(speech, config):
        assert not capturing[0]
        assert gate.awake_until is None
        now[0] += 5  # Includes playback, unkey, and worker cleanup.

    voice.synthesize.side_effect = synthesize
    monkeypatch.setattr(cli, "capture_from_device", capture)
    monkeypatch.setattr(cli, "transmit_speech", transmit)
    assert cli.main(ARGS) == 130
    first, second = [call.args[1] for call in backend.reply.call_args_list]
    assert first.session_id == second.session_id
    assert second.history == (Turn("what is rain", "Rain falls from clouds."),)
    assert "at most 20 words" in first.instructions
    assert gate.awake_until == 140 + config.conversation_timeout_seconds


@pytest.mark.parametrize("failure", [None, "prepare", "play", "interrupt", "on"])
def test_supervised_playback_prepares_before_key_and_always_releases(monkeypatch, failure):
    events = []
    config = replace(Config(), settle_seconds=0)

    class Playback:
        def __init__(self, path, device, gain, maximum):
            assert read_wav(path, maximum) == SPEECH
            assert device == config.output_device
            assert gain == config.gain

        def prepare(self):
            events.append("ready")
            if failure == "prepare":
                raise WalkietalkError("Cannot open audio")

        def play(self, deadline):
            events.append("play")
            if failure == "play":
                raise WalkietalkError("Playback timed out")
            if failure == "interrupt":
                raise KeyboardInterrupt

        def close(self):
            events.append("worker closed")

    class PTT:
        def __init__(self, port, line):
            events.append("serial constructed")
            assert line == "dtr"

        def open(self):
            events.append("open")

        def on(self):
            events.append("on")
            if failure == "on":
                raise WalkietalkError("PTT assertion failed")

        def off(self):
            events.append("off")

        def close(self):
            events.append("serial closed")

    monkeypatch.setattr(cli, "Playback", Playback)
    monkeypatch.setattr(cli, "SerialPTT", PTT)
    if failure:
        with pytest.raises(KeyboardInterrupt if failure == "interrupt" else WalkietalkError):
            cli.transmit_speech(SPEECH, config)
    else:
        cli.transmit_speech(SPEECH, config)
    if failure == "prepare":
        assert events == ["ready", "worker closed"]
    else:
        assert events[:4] == ["ready", "serial constructed", "open", "on"]
        assert events[-3:] == ["off", "serial closed", "worker closed"]
