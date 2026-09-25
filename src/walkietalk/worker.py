"""Private audio worker. Only the parent process controls the transmitter."""

import sys
from pathlib import Path

from .audio import read_wav
from .devices import audio_devices, resolve_device


def stream_audio(sd, name: str, gain: float, rate: int) -> int:
    """Read raw PCM until EOF; drain before exiting. Never opens serial/PTT."""
    import numpy as np

    from .config import validate_gain
    from .grok_realtime import resample_pcm16

    gain = validate_gain(gain)
    device = resolve_device(audio_devices(), name, "output")
    output_rate = 48000
    sd.check_output_settings(device=device, channels=1, dtype="int16", samplerate=output_rate)
    stream = sd.RawOutputStream(
        device=device, channels=1, dtype="int16", samplerate=output_rate, latency="low"
    )
    try:
        print("READY", flush=True)
        pending = b""
        started = False
        while True:
            chunk = sys.stdin.buffer.read1(4096)
            if not chunk:
                break
            pending += chunk
            count = len(pending) // 2 * 2
            if not count:
                continue
            pcm, pending = pending[:count], pending[count:]
            pcm = resample_pcm16(pcm, rate, output_rate)
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
            pcm = np.rint(np.clip(samples * gain, -32768, 32767)).astype("<i2").tobytes()
            if not started:
                stream.start()
                started = True
            # Temporary starvation can occur between network chunks. PortAudio
            # inserts silence; keep the stream alive for subsequent speech.
            stream.write(pcm)
        if pending:
            raise ValueError("Truncated PCM sample")
        if started:
            stream.stop()
    finally:
        stream.close()
    return 0


def main() -> int:
    import sounddevice as sd

    try:
        if sys.argv[1:2] == ["--stream"]:
            _, name, gain, rate = sys.argv[1:]
            return stream_audio(sd, name, float(gain), int(rate))
        path, name, gain, maximum = sys.argv[1:]
        wav = read_wav(Path(path), float(maximum), float(gain))
        device = resolve_device(audio_devices(), name, "output")
        sd.check_output_settings(device=device, channels=1, dtype="int16", samplerate=wav.rate)
        stream = sd.RawOutputStream(
            device=device,
            samplerate=wav.rate,
            channels=1,
            dtype="int16",
        )
        try:
            print("READY", flush=True)
            if sys.stdin.readline() != "GO\n":
                return 1
            stream.start()
            if stream.write(wav.frames):
                raise RuntimeError("Playback underrun")
            stream.stop()  # Drain queued audio before notifying the parent.
        finally:
            stream.close()
        return 0
    except Exception as exc:
        print(f"Audio error: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
