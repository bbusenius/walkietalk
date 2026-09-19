"""Phase 1 commands: inspect, check, pulse PTT, and play speech."""

import argparse
import sys
import time
from pathlib import Path

from .audio import Playback, read_wav
from .config import Config, WalkietalkError, load_config, seconds
from .devices import audio_devices, preflight
from .ptt import DryPTT, SerialPTT
from .session import handle_stop_signals, transmit, uninterrupted_cleanup


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="walkietalk phase 1: computer-controlled radio PTT"
    )
    result.add_argument(
        "-c", "--config", type=Path, help="YAML config (required for hardware access)"
    )
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("devices", help="List exact audio names and stable serial paths; no TX")
    commands.add_parser("check", help="Validate device selection and permissions; no TX")
    ptt = commands.add_parser("ptt", help="Brief talk-button test; dry run unless --transmit")
    ptt.add_argument(
        "--seconds", type=float, default=1, help="Pulse length, at most max_tx_seconds"
    )
    play = commands.add_parser(
        "play", help="Play a mono 16-bit speech WAV; dry run unless --transmit"
    )
    play.add_argument("wav", type=Path)
    for command in (ptt, play):
        command.add_argument(
            "--transmit", action="store_true", help="Use real hardware and transmit"
        )
    return result


def run(args: argparse.Namespace) -> None:
    if args.command == "devices":
        for i, device in enumerate(audio_devices()):
            print(
                f"[{i}] {device['name']} | input={device['max_input_channels']} "
                f"output={device['max_output_channels']}"
            )
        ports = sorted(Path("/dev/serial/by-id").glob("*"))
        for port in ports:
            print(f"Serial: {port}")
        if not ports:
            print("No stable serial paths found; connect the AIOC.")
        return
    live = args.command == "check" or args.transmit
    if live and args.config is None:
        raise WalkietalkError("Hardware access requires --config with explicit AIOC devices")
    config = load_config(args.config) if args.config else Config()
    if args.command == "check":
        preflight(config)
        print("Device names and serial permissions OK. No port opened; no transmission.")
        return
    if args.command == "ptt":
        duration = seconds(args.seconds, "--seconds", maximum=config.max_tx_seconds)
    else:
        wav = read_wav(args.wav, config.max_tx_seconds - config.settle_seconds, config.gain)
        duration = wav.duration + config.settle_seconds
        print(f"WAV: {wav.rate} Hz, mono PCM16, {wav.duration:.3f}s, gain={config.gain:g}")
    if live:
        preflight(config)
    else:
        print(f"Configured capture: {config.input_device}; playback: {config.output_device}")
    ptt = SerialPTT(config.serial_port, config.line) if live else DryPTT()
    playback = None
    try:
        if args.command == "play" and live:
            playback = Playback(
                args.wav,
                config.output_device,
                config.gain,
                config.max_tx_seconds - config.settle_seconds,
            )
            playback.prepare()  # No assertion if WAV/output preparation fails.

        def action(deadline: float) -> None:
            if playback is not None:
                time.sleep(min(config.settle_seconds, max(0, deadline - time.monotonic())))
                playback.play(deadline)
            else:
                if args.command == "play":
                    print("DRY RUN: simulated playback", flush=True)
                time.sleep(min(duration, max(0, deadline - time.monotonic())))

        transmit(ptt, action, config.max_tx_seconds)
    finally:
        # transmit() has already released PTT, even if worker teardown stalls.
        with uninterrupted_cleanup():
            if playback is not None:
                playback.close()
    print("Finished; talk released.")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        with handle_stop_signals():
            run(args)
        return 0
    except KeyboardInterrupt:
        print("Stopped; PTT cleanup attempted.", file=sys.stderr)
        return 130
    except (WalkietalkError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
