import array
import copy
import os
import selectors
import signal
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path
from unittest.mock import Mock, call

import pytest
import yaml

from walkietalk import cli
from walkietalk.audio import Playback, read_wav
from walkietalk.config import WalkietalkError, load_config
from walkietalk.devices import resolve_device
from walkietalk.ptt import SerialPTT
from walkietalk.session import transmit


@pytest.fixture
def config_data():
    return yaml.safe_load(Path("config.example.yaml").read_text())


@pytest.fixture
def wav_path(tmp_path):
    path = tmp_path / "speech.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b"\x00\x40" * 4800)
    return path


def write_config(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.mark.parametrize("value", [0, -1, 31, True, "10", float("nan"), float("inf")])
def test_invalid_tx_limit_rejected(tmp_path, config_data, value):
    config_data["radio"]["max_tx_seconds"] = value
    with pytest.raises(WalkietalkError):
        load_config(write_config(tmp_path, config_data))


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("audio", "gain", 0),
        ("audio", "input_device", "default"),
        ("ptt", "serial_port", "ttyACM0"),
        ("ptt", "line", "both"),
        ("radio", "settle_seconds", 0),
    ],
)
def test_invalid_config_rejected(tmp_path, config_data, section, field, value):
    config_data[section][field] = value
    with pytest.raises(WalkietalkError):
        load_config(write_config(tmp_path, config_data))


def test_config_unknown_and_missing_fields_rejected(tmp_path, config_data):
    for data in [dict(config_data, unknown={}), copy.deepcopy(config_data)]:
        if "unknown" not in data:
            del data["audio"]["gain"]
        with pytest.raises(WalkietalkError):
            load_config(write_config(tmp_path, data))


def test_valid_config(tmp_path, config_data):
    config = load_config(write_config(tmp_path, config_data))
    assert config.line == "dtr"
    assert config.max_tx_seconds == 10


def test_exact_device_selection_never_falls_back():
    device = {"name": "AIOC (hw:1,0)", "max_input_channels": 1, "max_output_channels": 1}
    assert resolve_device([device], device["name"], "output") == 0
    for devices, name in [
        ([device], "AIOC"),
        ([device, device], device["name"]),
        ([dict(device, name="default")], "default"),
    ]:
        with pytest.raises(WalkietalkError):
            resolve_device(devices, name, "output")
    with pytest.raises(WalkietalkError):
        resolve_device([dict(device, max_output_channels=0)], device["name"], "output")


def test_wav_duration_and_gain(wav_path):
    wav = read_wav(wav_path, 1, gain=0.25)
    assert wav.duration == 0.1
    samples = array.array("h", wav.frames)
    if sys.byteorder != "little":
        samples.byteswap()
    assert set(samples) == {4096}
    with pytest.raises(WalkietalkError, match="no longer"):
        read_wav(wav_path, 0.05)


@pytest.mark.parametrize("gain", [0, -1, True, "2", None, float("nan"), float("inf"), 10**400])
def test_invalid_gain_rejected_in_config_and_playback(tmp_path, config_data, wav_path, gain):
    config_data["audio"]["gain"] = gain
    with pytest.raises(WalkietalkError, match="audio.gain"):
        load_config(write_config(tmp_path, config_data))
    with pytest.raises(WalkietalkError, match="audio.gain"):
        read_wav(wav_path, 1, gain=gain)


@pytest.mark.parametrize(
    ("gain", "expected"),
    [
        (0.5, [-16384, -10000, -500, 0, 500, 10000, 16384]),
        (1, [-32768, -20000, -1000, 0, 1000, 20000, 32767]),
        (1.5, [-32768, -30000, -1500, 0, 1500, 30000, 32767]),
        (2, [-32768, -32768, -2000, 0, 2000, 32767, 32767]),
        (1e308, [-32768, -32768, -32768, 0, 32767, 32767, 32767]),
    ],
)
def test_gain_amplifies_and_clamps_without_wraparound(tmp_path, config_data, gain, expected):
    config_data["audio"]["gain"] = gain
    config = load_config(write_config(tmp_path, config_data))
    path = tmp_path / "peaks.wav"
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(48000)
        writer.writeframes(struct.pack("<7h", -32768, -20000, -1000, 0, 1000, 20000, 32767))
    result = read_wav(path, 1, gain=config.gain)
    assert list(struct.unpack("<7h", result.frames)) == expected
    assert result.rate == 48000
    assert result.duration == 7 / 48000


def test_truncated_wav_rejected(wav_path):
    wav_path.write_bytes(wav_path.read_bytes()[:-2])
    with pytest.raises(WalkietalkError, match="Truncated"):
        read_wav(wav_path, 1)


@pytest.mark.parametrize("channels,width,rate", [(2, 2, 48000), (1, 1, 48000), (1, 2, 44100)])
def test_unsupported_wav_rejected(tmp_path, channels, width, rate):
    path = tmp_path / "bad.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(b"\x00" * 100)
    with pytest.raises(WalkietalkError):
        read_wav(path, 1)


@pytest.mark.parametrize("failure_at", [None, "open", "on", "action", "off"])
def test_cleanup_after_partial_open_assertion_or_action_failure(failure_at):
    ptt = Mock()
    action = Mock()
    if failure_at:
        (action if failure_at == "action" else getattr(ptt, failure_at)).side_effect = OSError
    if failure_at:
        with pytest.raises(OSError):
            transmit(ptt, action, 1)
    else:
        transmit(ptt, action, 1)
    ptt.off.assert_called_once_with()
    ptt.close.assert_called_once_with()
    assert ptt.mock_calls[-2:] == [call.off(), call.close()]


def test_real_serial_levels_set_before_open_and_both_released(monkeypatch):
    events = []

    class FakeSerial:
        is_open = False

        def __init__(self, **kwargs):
            assert kwargs["port"] is None
            assert kwargs["exclusive"] is True

        def __setattr__(self, key, value):
            if key in ("dtr", "rts"):
                events.append((key, value))
            object.__setattr__(self, key, value)

        def open(self):
            assert self.dtr is False and self.rts is False
            events.append("open")
            self.is_open = True

        def close(self):
            events.append("close")
            self.is_open = False

    monkeypatch.setattr("serial.Serial", FakeSerial)
    transmit(SerialPTT("/dev/fake"), lambda deadline: None, 1)
    assert events == [
        ("dtr", False),
        ("rts", False),
        "open",
        ("dtr", True),
        ("dtr", False),
        ("rts", False),
        "close",
    ]


def test_release_attempts_other_line_even_when_first_fails():
    events = []

    class BrokenSerial:
        is_open = True

        def __setattr__(self, name, value):
            events.append(name)
            if name == "dtr":
                raise OSError("disconnected")

    ptt = SerialPTT("/dev/fake")
    ptt.serial = BrokenSerial()
    with pytest.raises(WalkietalkError, match="turn the radio off"):
        ptt.off()
    assert events == ["dtr", "rts"]


def test_dry_run_does_not_enumerate_or_open_hardware(monkeypatch, capsys, wav_path):
    def forbidden(*args, **kwargs):
        pytest.fail("dry run touched hardware")

    monkeypatch.setattr(cli, "preflight", forbidden)
    monkeypatch.setattr(cli, "SerialPTT", forbidden)
    monkeypatch.setattr(cli, "Playback", forbidden)
    assert cli.main(["ptt", "--seconds", "0.001"]) == 0
    assert cli.main(["play", str(wav_path)]) == 0
    assert "DRY RUN: PTT OFF" in capsys.readouterr().out


def test_live_access_requires_config(capsys):
    assert cli.main(["ptt", "--transmit"]) == 1
    assert "requires --config" in capsys.readouterr().err


def test_audio_prepare_failure_does_not_open_ptt(monkeypatch, tmp_path, config_data, wav_path):
    config_path = write_config(tmp_path, config_data)
    ptt = Mock()
    playback = Mock()
    playback.prepare.side_effect = WalkietalkError("No device")
    monkeypatch.setattr(cli, "preflight", lambda config: 0)
    monkeypatch.setattr(cli, "SerialPTT", lambda *args: ptt)
    monkeypatch.setattr(cli, "Playback", lambda *args: playback)
    assert cli.main(["-c", str(config_path), "play", str(wav_path), "--transmit"]) == 1
    ptt.open.assert_not_called()
    ptt.on.assert_not_called()
    playback.close.assert_called_once()


def fake_worker(code):
    playback = Playback(Path("unused"), "unused", 1, 1)
    playback.command = [sys.executable, "-c", code]
    return playback


def test_partial_ready_cannot_block_preparation_forever():
    playback = fake_worker(
        "import sys,time; sys.stdout.write('REA'); sys.stdout.flush(); time.sleep(30)"
    )
    try:
        with pytest.raises(WalkietalkError, match="preparation timed out"):
            playback.prepare(timeout=0.2)
    finally:
        playback.close()
    assert playback.process.poll() is not None


def test_hung_audio_worker_unkeys_before_worker_cleanup():
    playback = fake_worker("import time; print('READY',flush=True); input(); time.sleep(30)")
    events = []
    ptt = Mock()
    ptt.off.side_effect = lambda: events.append(("off", playback.process.poll()))
    try:
        playback.prepare()
        with pytest.raises(WalkietalkError, match="Transmit time limit"):
            transmit(ptt, playback.play, 0.1)
        assert events == [("off", None)]  # Child is still hung when PTT is released.
        ptt.close.assert_called_once()
    finally:
        playback.close()
    assert playback.process.poll() is not None


@pytest.mark.parametrize("exit_code", [0, 1])
def test_worker_success_and_failure(exit_code):
    playback = fake_worker(
        "import sys; print('native diagnostic',file=sys.stderr); "
        f"print('READY',flush=True); input(); sys.exit({exit_code})"
    )
    try:
        playback.prepare()
        if exit_code:
            with pytest.raises(WalkietalkError, match="native diagnostic"):
                playback.play(time.monotonic() + 1)
        else:
            playback.play(time.monotonic() + 1)
    finally:
        playback.close()


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_process_signal_releases_simulated_ptt(sig):
    env = dict(os.environ, PYTHONPATH=str(Path("src").resolve()))
    process = subprocess.Popen(
        [sys.executable, "-m", "walkietalk", "ptt", "--seconds", "5"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        bufsize=0,
    )
    output = b""
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 5
            while b"PTT ON" not in output:
                assert selector.select(max(0, deadline - time.monotonic())), "child not ready"
                chunk = os.read(process.stdout.fileno(), 1024)
                assert chunk, output
                output += chunk
        process.send_signal(sig)
        output += process.communicate(timeout=3)[0]
        assert process.returncode == 130
        assert b"PTT OFF" in output
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
