"""Private audio worker. Only the parent process controls the transmitter."""

import sys
from pathlib import Path

from .audio import read_wav
from .devices import audio_devices, resolve_device


def main() -> int:
    import sounddevice as sd

    try:
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
