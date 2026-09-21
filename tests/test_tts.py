import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import yaml

from walkietalk import cli, tts
from walkietalk.audio import Wav, read_wav
from walkietalk.config import Config, WalkietalkError, load_config


@pytest.fixture
def fake_piper(tmp_path, monkeypatch):
    executable = tmp_path / "fake piper"
    model = tmp_path / "amy.onnx"
    model.write_bytes(b"fake model")
    Path(str(model) + ".json").write_text("{}")
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    record = tmp_path / "record.json"
    executable.write_text(f"""#!{sys.executable}
import json, os, sys, time, wave
from pathlib import Path
s = json.loads(Path({str(settings)!r}).read_text())
text = sys.stdin.read()
record = {{'text': text, 'args': sys.argv[1:], 'env': dict(os.environ)}}
Path({str(record)!r}).write_text(json.dumps(record))
if s.get('hang'): time.sleep(30)
if s.get('fail'):
    print('secret-private-diagnostic', file=sys.stderr)
    sys.exit(1)
p = Path(sys.argv[sys.argv.index('--output-file') + 1])
if s.get('missing'): sys.exit(0)
if s.get('invalid'):
    p.write_bytes(b'not a wave')
    sys.exit(0)
if s.get('huge'):
    p.write_bytes(b'x' * 3000001)
    sys.exit(0)
with wave.open(str(p), 'wb') as w:
    w.setnchannels(s.get('channels', 1))
    w.setsampwidth(2)
    w.setframerate(s.get('rate', 22050))
    w.writeframes(b'\\x00\\x20' * s.get('samples', 44100))
if s.get('truncated'): p.write_bytes(p.read_bytes()[:-2])
""")
    executable.chmod(0o755)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-piper")
    config = replace(Config(), piper_executable=str(executable), piper_model=str(model))
    return config, settings, record


def test_real_subprocess_uses_stdin_and_returns_48khz_without_keys(fake_piper):
    config, _, record = fake_piper
    speech = tts.PiperTts(config).synthesize("Hello; $(do-not-run) `anything`.")
    assert speech.rate == 48000
    assert speech.duration == 2
    assert len(speech.frames) == 192000  # Above the agent's 64 KiB file limit.
    assert np.max(np.frombuffer(speech.frames, dtype="<i2")) == 8192
    call = json.loads(record.read_text())
    assert call["text"] == "Hello; $(do-not-run) `anything`.\n"
    assert call["args"][:2] == ["--model", config.piper_model]
    assert "Hello" not in " ".join(call["args"])
    assert "ANTHROPIC_API_KEY" not in call["env"]
    assert not Path(call["args"][-1]).exists()  # Temporary speech is removed.


def test_peak_normalize_fills_the_wav_and_off_keeps_engine_level(fake_piper):
    config, _, _ = fake_piper
    quiet = tts.PiperTts(config).synthesize("Hello.")
    loud = tts.PiperTts(replace(config, tts_normalize="peak")).synthesize("Hello.")
    assert np.max(np.abs(np.frombuffer(quiet.frames, dtype="<i2"))) == 8192
    assert np.max(np.abs(np.frombuffer(loud.frames, dtype="<i2"))) == 32767
    silence = Wav(b"\x00\x00" * 48000, 48000, 1)
    assert tts.level_wav(silence, "peak").frames == silence.frames
    assert tts.level_wav(quiet, "off").frames == quiet.frames


@pytest.mark.parametrize(
    "settings",
    [
        {"fail": True},
        {"missing": True},
        {"invalid": True},
        {"huge": True},
        {"channels": 2},
        {"rate": 44100},
        {"samples": 0},
        {"samples": 22050 * 11},
        {"truncated": True},
    ],
)
def test_invalid_piper_output_is_local_error(fake_piper, settings):
    config, path, _ = fake_piper
    path.write_text(json.dumps(settings))
    with pytest.raises(WalkietalkError) as error:
        tts.PiperTts(config).synthesize("Hello.")
    assert "secret-private-diagnostic" not in str(error.value)
    assert "must-not-reach-piper" not in str(error.value)


def test_synthesis_deadline_kills_process(fake_piper):
    config, path, _ = fake_piper
    path.write_text('{"hang": true}')
    started = time.monotonic()
    with pytest.raises(WalkietalkError, match="timed out"):
        tts.PiperTts(replace(config, tts_timeout_seconds=0.15)).synthesize("Hello.")
    assert time.monotonic() - started < 2


def test_late_audio_discarded_after_processing(fake_piper, monkeypatch):
    config, _, _ = fake_piper
    original = tts.radio_wav

    def delayed(wav, maximum):
        result = original(wav, maximum)
        monkeypatch.setattr(tts.time, "monotonic", lambda: float("inf"))
        return result

    monkeypatch.setattr(tts, "radio_wav", delayed)
    with pytest.raises(WalkietalkError, match="audio discarded"):
        tts.PiperTts(config).synthesize("Hello.")


@pytest.mark.parametrize("missing", ["executable", "model", "sidecar"])
def test_missing_installation_never_starts_process(fake_piper, monkeypatch, missing):
    config, _, _ = fake_piper
    if missing == "executable":
        config = replace(config, piper_executable="/nonexistent/piper")
    else:
        Path(config.piper_model + (".json" if missing == "sidecar" else "")).unlink()
    monkeypatch.setattr(tts, "run_cli", lambda *a, **k: pytest.fail("started process"))
    with pytest.raises(WalkietalkError, match="not found|missing"):
        tts.PiperTts(config).synthesize("Hello.")


@pytest.mark.parametrize("text", ["", "\x1b[31mhello", "x" * 601])
def test_invalid_text_never_starts_piper(fake_piper, monkeypatch, text):
    config, _, _ = fake_piper
    monkeypatch.setattr(tts, "run_cli", lambda *a, **k: pytest.fail("started process"))
    with pytest.raises(WalkietalkError):
        tts.PiperTts(config).synthesize(text)


@pytest.mark.parametrize(
    "wav",
    [
        None,
        Wav(b"", 48000, 1),
        Wav(b"x", 48000, 1),
        Wav(b"xx", 44100, 1),
        Wav(b"xx", 48000.0, 1),
        Wav(b"xx" * 480000, 48000, 0.1),
    ],
)
def test_bridge_revalidates_audio_including_actual_duration(wav):
    with pytest.raises(WalkietalkError):
        tts.radio_wav(wav, 9.8)


def test_duration_and_amplitude_preserved_when_resampling():
    wav = tts.radio_wav(Wav(b"\xff\x7f" * 22050, 22050, 999), 1)
    assert wav.duration == 1
    assert wav.rate == 48000
    assert wav.frames == b"\xff\x7f" * 48000


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("backend", "cloud"),
        ("timeout_seconds", 0),
        ("timeout_seconds", 121),
        ("timeout_seconds", True),
        ("piper_model", None),
        ("piper_model", "amy"),
        ("piper_executable", "piper --bad"),
        ("piper_executable", "./piper"),
        ("normalize", "rms"),
        ("normalize", True),
    ],
)
def test_invalid_tts_configuration(tmp_path, key, value):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["tts"][key] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WalkietalkError, match="tts"):
        load_config(path)


def test_required_tts_fields_and_config_relative_model(tmp_path):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["tts"]["piper_model"] = "voices/amy.onnx"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    assert load_config(path).piper_model == str(tmp_path / "voices/amy.onnx")
    del data["tts"]["timeout_seconds"]
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WalkietalkError, match="tts"):
        load_config(path)
    with pytest.raises(WalkietalkError):
        tts.open_tts(replace(Config(), tts_backend="unknown"))


def test_tts_check_exports_without_hardware_and_refuses_overwrite(
    fake_piper, monkeypatch, tmp_path, capsys
):
    config, _, _ = fake_piper
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    for name in ("SerialPTT", "Playback", "preflight", "open_stt", "open_agent"):
        monkeypatch.setattr(cli, name, lambda *a, **k: pytest.fail("opened hardware or agent"))
    path = tmp_path / "speech.wav"
    args = ["-c", "unused", "tts-check", "Hello.", "--output", str(path)]
    assert cli.main(args) == 0
    assert read_wav(path, 10).rate == 48000
    assert "No hardware opened" in capsys.readouterr().out
    before = path.read_bytes()
    assert cli.main(args) == 1
    assert path.read_bytes() == before
