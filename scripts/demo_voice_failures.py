"""Offline phase 6 failure demonstration; fake Piper, real synthesis supervisor.

Run: .venv/bin/python scripts/demo_voice_failures.py
"""

import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from walkietalk import cli
from walkietalk.capture import Utterance
from walkietalk.config import Config

FAKE_PIPER = """
import sys, time, wave
scenario = SCENARIO
sys.stdin.read()
if scenario == 'timeout': time.sleep(30)
if scenario == 'synthesis error':
    print('Private diagnostics that must never become speech', file=sys.stderr)
    sys.exit(1)
p = sys.argv[sys.argv.index('--output-file') + 1]
with wave.open(p, 'wb') as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(22050)
    w.writeframes(b'\\x00\\x20' * 22050 * 11)
"""


def forbidden(*args, **kwargs):
    raise AssertionError("Failure demonstration attempted hardware access")


def main():
    print(
        "Offline fake Piper: no accounts, network, or hardware. Expected errors follow.", flush=True
    )
    with tempfile.TemporaryDirectory(prefix="walkietalk-voice-demo-") as directory:
        root = Path(directory)
        model = root / "fake-amy.onnx"
        model.write_bytes(b"fake voice")
        Path(str(model) + ".json").write_text("{}")
        executable = root / "fake-piper"
        for scenario in ("missing CLI", "missing voice", "timeout", "synthesis error", "oversized"):
            executable.write_text(
                f"#!{sys.executable}\n" + FAKE_PIPER.replace("SCENARIO", repr(scenario))
            )
            executable.chmod(0o700)
            config = replace(
                Config(),
                piper_executable=str(root / "absent" if scenario == "missing CLI" else executable),
                piper_model=str(root / "absent.onnx" if scenario == "missing voice" else model),
                tts_timeout_seconds=0.5,
            )
            listener = Mock()
            listener.label.return_value = "fake STT"
            listener.transcribe.return_value = "charlotte hello"
            print(f"\nControlled {scenario}:", flush=True)
            with (
                patch.object(cli, "load_config", return_value=config),
                patch.object(cli, "open_stt", return_value=listener),
                patch.object(cli, "preflight"),
                patch.object(
                    cli,
                    "capture_from_device",
                    return_value=Utterance(b"\x00\x40" * 320, 16000, 0.5, 0.5, 1, "silence", 100),
                ),
                patch.object(cli, "SerialPTT", side_effect=forbidden),
                patch.object(cli, "Playback", side_effect=forbidden),
                patch.object(cli, "transmit", side_effect=forbidden),
            ):
                result = cli.main(["-c", "fake.yaml", "talk", "--capture", "--once", "--transmit"])
            if result != 1:
                raise AssertionError(f"{scenario}: expected local failure, got {result}")
            print("PASS: local error; no PTT or playback opened.", flush=True)
    print("\nAll five controlled voice failures passed. Live radio observations are separate.")


if __name__ == "__main__":
    main()
