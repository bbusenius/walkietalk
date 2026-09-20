import array
import copy
import sys
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
import yaml

from walkietalk import cli
from walkietalk.capture import Utterance, capture_from_device, collect_utterance, frames_from_pcm
from walkietalk.config import Config, WalkietalkError, load_config
from walkietalk.stt import ensure_model, to_whisper_audio, transcribe_audio
from walkietalk.vad import EnergyVad, frame_samples, rms


@pytest.fixture
def config_data():
    return yaml.safe_load(Path("config.example.yaml").read_text())


def write_config(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def loud_frame(rate=16000, amplitude=8000):
    return array.array("h", [amplitude] * frame_samples(rate)).tobytes()


def silent_frame(rate=16000):
    return b"\x00\x00" * frame_samples(rate)


def speech_wav(path, *, rate=48000, silence=0.3, speech=0.5, tail=0.5, amplitude=8000):
    pcm = (
        array.array("h", [0] * int(rate * silence))
        + array.array("h", [amplitude] * int(rate * speech))
        + array.array("h", [0] * int(rate * tail))
    )
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())
    return path


def fake_utterance():
    return Utterance(loud_frame(), 16000, 0.24, 0.24, 0.5, "silence")


class FakeListener:
    def __init__(self, text=""):
        self.text = text

    def label(self):
        return "fake"

    def prepare(self):
        return None

    def transcribe(self, pcm, rate):
        return self.text


def test_valid_vad_and_stt_config(tmp_path, config_data):
    config = load_config(write_config(tmp_path, config_data))
    assert config.energy_threshold == 0.02
    assert config.hangover_ms == 400
    assert config.max_utterance_seconds == 12
    assert config.stt_model == "base"
    assert config.stt_backend == "faster-whisper"
    assert config.stt_timeout_seconds == 30


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("vad", "energy_threshold", 0),
        ("vad", "energy_threshold", 1.1),
        ("vad", "hangover_ms", 0),
        ("vad", "hangover_ms", 400.0),
        ("vad", "max_utterance_seconds", 0),
        ("stt", "model", "large"),
        ("stt", "model", "tiny.en"),
        ("stt", "backend", "whisper"),
        ("stt", "timeout_seconds", 0),
    ],
)
def test_invalid_vad_stt_rejected(tmp_path, config_data, section, field, value):
    config_data[section][field] = value
    with pytest.raises(WalkietalkError):
        load_config(write_config(tmp_path, config_data))


def test_phase1_config_without_vad_or_stt_rejected(tmp_path, config_data):
    for section in ("vad", "stt"):
        data = copy.deepcopy(config_data)
        del data[section]
        with pytest.raises(WalkietalkError, match="exactly"):
            load_config(write_config(tmp_path, data))


def test_rms_of_constant_samples():
    frame = array.array("h", [8192] * 320).tobytes()
    assert rms(frame) == pytest.approx(8192 / 32768)
    assert rms(b"\x00\x00" * 320) == 0


def test_vad_silence_never_starts():
    vad = EnergyVad(0.02, 400, 12, 16000)
    for _ in range(50):
        state, _ = vad.push(silent_frame())
        assert state == "waiting"
    assert not vad.speaking
    assert vad.captured == b""


def test_vad_speech_then_silence_hangover():
    vad = EnergyVad(0.02, 400, 12, 16000)
    for _ in range(15):
        assert vad.push(loud_frame())[0] == "speaking"
    quiet = [vad.push(silent_frame())[0] for _ in range(vad.hangover_frames)]
    assert quiet[-1] == "finished"
    assert vad.end_reason == "silence"
    assert vad.duration() > 0


def test_vad_short_click_is_ignored():
    vad = EnergyVad(0.02, 400, 12, 16000)
    assert vad.push(loud_frame())[0] == "speaking"
    states = [vad.push(silent_frame())[0] for _ in range(vad.hangover_frames)]
    assert states[-1] == "waiting"
    assert not vad.speaking
    assert vad.captured == b""
    for _ in range(15):
        assert vad.push(loud_frame())[0] == "speaking"


def test_vad_includes_preroll_before_speech():
    vad = EnergyVad(0.02, 400, 12, 16000)
    marker = array.array("h", [1] * frame_samples(16000)).tobytes()
    vad.push(marker)
    vad.push(loud_frame())
    assert marker in bytes(vad.captured)
    assert len(vad.captured) == 2 * len(loud_frame())


def test_vad_max_utterance_ends_capture():
    vad = EnergyVad(0.02, 400, 0.1, 16000)
    states = [vad.push(loud_frame())[0] for _ in range(vad.max_frames)]
    assert states[-1] == "finished"
    assert vad.end_reason == "max"


def test_collect_utterance_logs_rms_and_end_reason():
    logs = []
    rate = 16000
    frames = [(silent_frame(rate), False)] * 3 + [(loud_frame(rate), False)] * 15
    frames += [(silent_frame(rate), False)] * 25
    utterance = collect_utterance(
        frames,
        rate=rate,
        energy_threshold=0.02,
        hangover_ms=400,
        max_utterance_seconds=12,
        wait_deadline=None,
        log=logs.append,
    )
    assert utterance.end_reason == "silence"
    assert any("Speech started" in line for line in logs)
    assert any("silence" in line for line in logs)
    assert utterance.peak_rms > 0.02


def test_collect_utterance_timeout_without_speech():
    with pytest.raises(WalkietalkError, match="peak RMS"):
        collect_utterance(
            [(silent_frame(), False)] * 5,
            rate=16000,
            energy_threshold=0.02,
            hangover_ms=400,
            max_utterance_seconds=12,
            wait_deadline=time.monotonic() - 1,
            log=lambda _line: None,
        )


def test_whisper_resampler_changes_48k_to_16k():
    pcm = array.array("h", [1000] * 48000).tobytes()
    audio = to_whisper_audio(pcm, 48000)
    assert audio.dtype == np.float32
    assert len(audio) == 16000
    assert audio.max() == pytest.approx(1000 / 32768, rel=0.05)


def test_transcribe_joins_segment_text():
    class Segment:
        def __init__(self, text):
            self.text = text

    class Model:
        def transcribe(self, audio, **kwargs):
            assert kwargs["language"] == "en"
            assert kwargs["vad_filter"] is False
            return iter([Segment(" hello"), Segment("kids ")]), None

    text = transcribe_audio(Model(), loud_frame() * 50, 16000)
    assert text == "hello kids"


def test_missing_model_explains_download(monkeypatch):
    monkeypatch.setattr("walkietalk.stt.model_ready", lambda name: False)
    with pytest.raises(WalkietalkError, match="walkietalk models"):
        ensure_model("base", download=False)


def test_listen_wav_does_not_open_hardware(monkeypatch, tmp_path, capsys):
    path = speech_wav(tmp_path / "speech.wav")

    def forbidden(*args, **kwargs):
        pytest.fail("file listen touched hardware")

    monkeypatch.setattr(cli, "preflight", forbidden)
    monkeypatch.setattr(cli, "SerialPTT", forbidden)
    monkeypatch.setattr(cli, "Playback", forbidden)
    monkeypatch.setattr(cli, "capture_from_device", forbidden)
    monkeypatch.setattr(cli, "open_stt", lambda config: FakeListener("hello kids"))
    assert cli.main(["listen", str(path)]) == 0
    assert "Transcript: hello kids" in capsys.readouterr().out


def test_listen_requires_wav_or_capture(capsys):
    assert cli.main(["listen"]) == 1
    assert "WAV file" in capsys.readouterr().err


def test_listen_capture_requires_config(capsys):
    assert cli.main(["listen", "--capture"]) == 1
    assert "requires --config" in capsys.readouterr().err


def test_listen_rejects_wav_and_capture_together(tmp_path):
    path = speech_wav(tmp_path / "speech.wav")
    assert cli.main(["listen", str(path), "--capture"]) == 1


def test_listen_capture_never_opens_ptt(monkeypatch, tmp_path, config_data, capsys):
    config_path = write_config(tmp_path, config_data)

    def forbidden(*args, **kwargs):
        pytest.fail("listen --capture opened PTT or playback")

    monkeypatch.setattr(cli, "preflight", lambda config, **kwargs: 0)
    monkeypatch.setattr(cli, "SerialPTT", forbidden)
    monkeypatch.setattr(cli, "Playback", forbidden)
    monkeypatch.setattr(cli, "open_stt", lambda config: FakeListener("the computer writes"))
    monkeypatch.setattr(cli, "capture_from_device", lambda *args, **kwargs: fake_utterance())
    assert cli.main(["-c", str(config_path), "listen", "--capture"]) == 0
    output = capsys.readouterr().out
    assert "Receive-only" in output
    assert "Transcript: the computer writes" in output


def test_listen_empty_transcript_is_an_error(monkeypatch, tmp_path):
    path = speech_wav(tmp_path / "speech.wav")
    monkeypatch.setattr(cli, "open_stt", lambda config: FakeListener(""))
    assert cli.main(["listen", str(path)]) == 1


def test_models_downloads_configured_name(monkeypatch, tmp_path, config_data, capsys):
    config_data["stt"]["model"] = "tiny"
    config_path = write_config(tmp_path, config_data)
    dest = tmp_path / "tiny"
    dest.mkdir()
    monkeypatch.setattr(cli, "ensure_model", lambda name, download: dest)
    monkeypatch.setattr(cli, "model_size_bytes", lambda name: 75_000_000)
    assert cli.main(["-c", str(config_path), "models"]) == 0
    assert "Speech model: tiny" in capsys.readouterr().out


def test_capture_interrupt_closes_stream(monkeypatch):
    closed = []
    stream = Mock()
    stream.read.side_effect = KeyboardInterrupt
    stream.close.side_effect = lambda: closed.append("close")
    fake_sd = SimpleNamespace(
        PortAudioError=type("PortAudioError", (Exception,), {}),
        check_input_settings=lambda **kwargs: None,
        RawInputStream=lambda **kwargs: stream,
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    monkeypatch.setattr(
        "walkietalk.capture.audio_devices",
        lambda: [
            {
                "name": "AIOC (hw:1,0)",
                "max_input_channels": 1,
                "max_output_channels": 1,
                "default_samplerate": 48000,
            }
        ],
    )
    with pytest.raises(KeyboardInterrupt):
        capture_from_device("AIOC (hw:1,0)", Config(), 5, log=lambda _line: None)
    assert closed == ["close"]


def test_frames_from_pcm_covers_remainder():
    frames = list(frames_from_pcm(b"\x01\x00" * 10, 16000))
    assert frames
    assert all(len(frame) == frame_samples(16000) * 2 for frame, overflowed in frames)
    assert all(overflowed is False for _frame, overflowed in frames)
