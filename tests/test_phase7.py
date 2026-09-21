import time
from dataclasses import replace
from unittest.mock import Mock

import pytest

from walkietalk import cli
from walkietalk.audio import Wav
from walkietalk.callsign import CallsignSession, join_identification
from walkietalk.capture import Utterance
from walkietalk.config import Config, WalkietalkError
from walkietalk.ptt import SerialPTT
from walkietalk.tts import radio_wav

SPEECH = Wav(b"\x00\x20" * 4800, 48000, 0.1)
IDENT = Wav(b"\x00\x30" * 2400, 48000, 0.05)
ARGS = ["-c", "unused.yaml", "talk", "--capture", "--transmit", "--once"]


def utterance(started):
    return Utterance(b"\x00\x40" * 320, 16000, 0.5, 0.5, 1, "silence", started)


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
    monkeypatch.setattr(cli, "capture_from_device", lambda *a, **k: utterance(time.monotonic()))
    for name in ("SerialPTT", "Playback"):
        monkeypatch.setattr(cli, name, lambda *a, **k: pytest.fail("unexpected hardware access"))
    return config, backend, voice, listener


def test_empty_callsign_never_invents_an_id():
    session = CallsignSession(Config(), time.monotonic)
    assert not session.enabled()
    assert not session.due()


def test_end_of_reply_callsign_is_due_until_marked():
    config = replace(Config(), callsign="TEST1ID", callsign_mode="end_of_reply")
    session = CallsignSession(config, time.monotonic)
    assert session.due()
    session.mark()
    assert session.due()


def test_interval_callsign_waits_after_first_id():
    now = [100.0]
    config = replace(
        Config(), callsign="TEST1ID", callsign_mode="interval", callsign_interval_seconds=30
    )
    session = CallsignSession(config, lambda: now[0])
    assert session.due()
    session.mark()
    now[0] = 129
    assert not session.due()
    now[0] = 130
    assert session.due()


def test_join_identification_keeps_callsign_inside_the_cap():
    answer = Wav(b"\x00\x20" * 48000 * 5, 48000, 5)
    ident = radio_wav(IDENT, 9.8)
    joined = join_identification(answer, ident, 1.0)
    assert joined.duration == pytest.approx(1.0)
    assert joined.frames.endswith(ident.frames)


def test_stuck_key_after_release_is_an_error():
    class Stuck:
        is_open = True
        dtr = True
        rts = False

        def __setattr__(self, name, value):
            if name in {"dtr", "rts"}:
                return
            object.__setattr__(self, name, value)

    ptt = SerialPTT("/dev/fake")
    ptt.serial = Stuck()
    with pytest.raises(WalkietalkError, match="still asserted"):
        ptt.off()


def test_post_tx_mute_waits_after_unkey(bridge, monkeypatch):
    config, backend, voice, _ = bridge
    config = replace(config, post_tx_mute_seconds=1.5)
    slept = []
    played = []
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "transmit_speech", lambda speech, cfg, **k: played.append(speech))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: slept.append(seconds))
    assert cli.main(ARGS) == 0
    backend.reply.assert_called_once()
    assert played == [SPEECH]
    assert slept == [1.5]


def test_callsign_appended_on_spoken_reply(bridge, monkeypatch):
    config, _, voice, _ = bridge
    config = replace(config, callsign="TEST1ID", callsign_mode="end_of_reply")
    played = []

    def synth(text):
        return IDENT if text == "TEST1ID" else SPEECH

    voice.synthesize.side_effect = synth
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "transmit_speech", lambda speech, cfg, **k: played.append(speech))
    assert cli.main(ARGS) == 0
    assert voice.synthesize.call_count == 2
    assert played[0].frames.endswith(IDENT.frames)


def test_no_callsign_when_mode_off(bridge, monkeypatch):
    config, _, voice, _ = bridge
    config = replace(config, callsign="TEST1ID", callsign_mode="off")
    played = []
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "transmit_speech", lambda speech, cfg, **k: played.append(speech))
    assert cli.main(ARGS) == 0
    voice.synthesize.assert_called_once()
    assert played == [SPEECH]


def test_receive_only_does_not_mute_or_identify(bridge, monkeypatch):
    config, _, voice, _ = bridge
    config = replace(
        config, post_tx_mute_seconds=2, callsign="TEST1ID", callsign_mode="end_of_reply"
    )
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: pytest.fail("muted without TX"))
    monkeypatch.setattr(cli, "transmit_speech", lambda *a, **k: pytest.fail("transmit"))
    assert cli.main(["-c", "unused.yaml", "talk", "--capture", "--once"]) == 0
    voice.synthesize.assert_not_called()
