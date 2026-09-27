import time
from dataclasses import replace
from unittest.mock import Mock

import pytest

from walkietalk import cli
from walkietalk.audio import Wav, read_wav
from walkietalk.callsign import IDENT_GAP_SECONDS, CallsignSession, identification_transmissions
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


def test_identification_transmissions_keeps_callsign_inside_the_cap():
    answer = Wav(b"\x00\x20" * 48000 * 5, 48000, 5)
    ident = radio_wav(IDENT, 9.8)
    (joined,) = identification_transmissions(answer, ident, 1.0)
    assert joined.duration == pytest.approx(1.0)
    assert joined.frames.endswith(ident.frames)


@pytest.mark.parametrize("samples,bursts", [(38400, 2), (38399, 1), (48000, 2)])
def test_identification_fit_boundary_preserves_full_id(samples, bursts):
    ident = Wav(b"\x00\x30" * samples, 48000, samples / 48000)
    transmissions = identification_transmissions(SPEECH, ident, 1)
    assert len(transmissions) == bursts
    assert all(audio.duration <= 1 for audio in transmissions)
    assert transmissions[-1].frames.endswith(ident.frames)
    if bursts == 2:
        assert transmissions == (SPEECH, ident)


@pytest.mark.parametrize("frames", [b"", b"\x00\x30" * 48001])
def test_invalid_or_overlong_id_is_not_silently_omitted_or_cropped(frames):
    ident = Wav(frames, 48000, len(frames) / 96000)
    with pytest.raises(WalkietalkError):
        identification_transmissions(SPEECH, ident, 1)


@pytest.mark.parametrize(
    "separate,failed_burst", [(False, None), (False, 0), (True, None), (True, 0), (True, 1)]
)
def test_id_transmissions_release_ptt_and_mark_only_after_success(
    bridge, monkeypatch, capsys, separate, failed_burst
):
    config, _, voice, listener = bridge
    config = replace(
        config,
        callsign="TEST1ID",
        callsign_mode="interval",
        max_tx_seconds=1,
        settle_seconds=0.1,
        post_tx_mute_seconds=0.7,
    )
    ident = Wav(b"\x00\x30" * 43200, 48000, 0.9) if separate else IDENT
    voice.synthesize.side_effect = [SPEECH, ident]
    now = [100.0]
    tracker = CallsignSession(config, lambda: now[0])
    events = []
    mark = tracker.mark

    def mark_sent():
        events.append("mark")
        mark()

    monkeypatch.setattr(tracker, "mark", mark_sent)
    monkeypatch.setattr(cli, "CallsignSession", lambda *a: tracker)
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    capture = Mock(side_effect=[utterance(time.monotonic()), KeyboardInterrupt()])
    monkeypatch.setattr(cli, "capture_from_device", capture)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: events.append(("sleep", seconds)))
    played = []
    radios = []

    def playback(path, *args):
        audio = read_wav(path, 1)
        player = Mock()
        player.play.side_effect = lambda deadline: played.append(audio)
        return player

    def ptt(*args):
        index = len(radios)
        radio = Mock()
        radios.append(radio)

        def on():
            assert tracker.due()
            events.append(("on", index))

        def off():
            events.append(("off", index))
            now[0] += 5
            if failed_burst == index:
                raise WalkietalkError("PTT release could not be confirmed; turn the radio off")

        radio.on.side_effect = on
        radio.off.side_effect = off
        radio.close.side_effect = lambda: events.append(("close", index))
        return radio

    monkeypatch.setattr(cli, "Playback", playback)
    monkeypatch.setattr(cli, "SerialPTT", ptt)
    args = ARGS if failed_burst is None else ARGS[:-1]
    assert cli.main(args) == (0 if failed_burst is None else 1)
    capture.assert_called_once()
    listener.transcribe.assert_called_once()
    assert voice.synthesize.call_args_list[-1].kwargs == {"truncate": False}
    expected_bursts = (2 if separate else 1) if failed_burst is None else failed_burst + 1
    assert len(radios) == len(played) == expected_bursts
    for radio in radios:
        radio.on.assert_called_once()
        radio.off.assert_called_once()
        radio.close.assert_called_once()
    assert all(audio.duration <= 0.9 for audio in played)
    if separate:
        assert played[0] == SPEECH
        if expected_bursts == 2:
            assert played[1] == ident
            assert events.index(("close", 0)) < events.index(("sleep", IDENT_GAP_SECONDS))
            assert events.index(("sleep", IDENT_GAP_SECONDS)) < events.index(("on", 1))
    else:
        assert played[0].frames.endswith(ident.frames)
    if failed_burst is None:
        assert not tracker.due()
        assert tracker.last_id_at == 100 + 5 * expected_bursts
        assert events[-2:] == ["mark", ("sleep", 0.7)]
    else:
        assert tracker.due()
        assert tracker.last_id_at is None
        assert "mark" not in events
        assert ("sleep", 0.7) not in events
        assert "turn the radio off" in capsys.readouterr().err


@pytest.mark.parametrize(
    "failure", [WalkietalkError("Speech failed"), Wav(b"\x00\x30" * 96000, 48000, 2)]
)
def test_unavailable_id_sends_answer_and_does_not_mark(bridge, monkeypatch, capsys, failure):
    config, _, voice, _ = bridge
    config = replace(config, callsign="TEST1ID", callsign_mode="interval", max_tx_seconds=1)
    tracker = CallsignSession(config, lambda: 100)
    monkeypatch.setattr(cli, "CallsignSession", lambda *a: tracker)
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    voice.synthesize.side_effect = [SPEECH, failure]
    transmission = Mock()
    monkeypatch.setattr(cli, "transmit_speech", transmission)
    assert cli.main(ARGS) == 0
    transmission.assert_called_once_with(SPEECH, config)
    assert tracker.due()
    assert tracker.last_id_at is None
    assert "Station ID failed" in capsys.readouterr().err


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

    def synth(text, **kwargs):
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
