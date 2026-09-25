"""Exercise real pipe streaming with fake audio; never open an audio device."""

import io
import selectors
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest

from walkietalk import worker
from walkietalk.config import Config, WalkietalkError
from walkietalk.streaming_playback import StreamingPlayback


def test_worker_receives_pcm_before_finish_and_preserves_order(tmp_path):
    output = tmp_path / "played.pcm"
    script = tmp_path / "fake_worker.py"
    script.write_text("""import sys
from pathlib import Path
print("READY", flush=True)
with Path(sys.argv[1]).open("wb") as out:
    while chunk := sys.stdin.buffer.read1(4096):
        out.write(chunk)
        out.flush()
        print("CHUNK", flush=True)
""")
    playback = StreamingPlayback(Config())
    playback.command = [sys.executable, "-u", str(script), str(output)]
    try:
        playback.prepare(timeout=2)
        deadline = time.monotonic() + 2
        playback(b"\x01\x00" * 240, 24000, deadline)
        with selectors.DefaultSelector() as selector:
            selector.register(playback.process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=1), "First chunk was buffered until finish"
        assert playback.process.stdout.readline() == b"CHUNK\n"
        assert output.read_bytes() == b"\x01\x00" * 240
        playback(b"\x02\x00" * 240, 24000, deadline)
        playback.finish(deadline)
        assert output.read_bytes() == b"\x01\x00" * 240 + b"\x02\x00" * 240
    finally:
        playback.close()
        playback.close()  # Timer and response cleanup can both close the worker.


def test_stalled_worker_drain_is_bounded(tmp_path):
    script = tmp_path / "stalled.py"
    script.write_text('import time\nprint("READY", flush=True)\ntime.sleep(30)\n')
    playback = StreamingPlayback(Config())
    playback.command = [sys.executable, "-u", str(script)]
    try:
        playback.prepare(timeout=2)
        process = playback.process
        deadline = time.monotonic() + 0.05
        playback(b"\x01\x00" * 240, 24000, deadline)
        with pytest.raises(WalkietalkError, match="time limit"):
            playback.finish(deadline)
    finally:
        playback.close()
    assert process.poll() is not None


def test_stream_worker_applies_gain_and_drains_after_eof(monkeypatch):
    actions = []
    played = []

    class Stream:
        def start(self):
            actions.append("start")

        def write(self, pcm):
            played.append(pcm)
            return True  # An underrun between network chunks is recoverable.

        def stop(self):
            actions.append("drain")

        def close(self):
            actions.append("close")

    fake_sd = SimpleNamespace(
        check_output_settings=lambda **_: None, RawOutputStream=lambda **_: Stream()
    )
    source = np.full(2400, 5000, dtype="<i2").tobytes()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BufferedReader(io.BytesIO(source))))
    monkeypatch.setattr(worker, "audio_devices", lambda: [])
    monkeypatch.setattr(worker, "resolve_device", lambda *_: 0)
    assert worker.stream_audio(fake_sd, "fake", 0.5, 24000) == 0
    assert actions == ["start", "drain", "close"]
    result = np.frombuffer(b"".join(played), dtype="<i2")
    assert len(result) == 4800
    assert (result == 2500).all()


def test_stream_worker_rejects_partial_pcm_sample(monkeypatch):
    actions = []
    fake_sd = SimpleNamespace(
        check_output_settings=lambda **_: None,
        RawOutputStream=lambda **_: SimpleNamespace(close=lambda: actions.append("close")),
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BufferedReader(io.BytesIO(b"x"))))
    monkeypatch.setattr(worker, "audio_devices", lambda: [])
    monkeypatch.setattr(worker, "resolve_device", lambda *_: 0)
    with pytest.raises(ValueError, match="Truncated PCM"):
        worker.stream_audio(fake_sd, "fake", 1, 24000)
    assert actions == ["close"]
