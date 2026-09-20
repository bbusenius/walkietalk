"""Inspect, check, pulse PTT, play speech, and transcribe radio speech."""

import argparse
import sys
import time
from pathlib import Path

from .audio import Playback, read_wav
from .capture import capture_from_device, capture_from_wav
from .config import Config, WalkietalkError, load_config, seconds
from .devices import audio_devices, preflight
from .ptt import DryPTT, SerialPTT
from .session import handle_stop_signals, transmit, uninterrupted_cleanup
from .stt import ensure_model, load_model, model_ready, model_size_bytes, transcribe_audio
from .term import capture_log, emit
from .wake import ListeningSession


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="walkietalk: radio PTT, local speech-to-text, and a wake gate"
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
    listen = commands.add_parser(
        "listen",
        help="Turn one utterance into text; WAV file or --capture, never transmits",
    )
    listen.add_argument("wav", nargs="?", type=Path, help="WAV to transcribe (no radio)")
    listen.add_argument(
        "--capture",
        action="store_true",
        help="Record from the pinned AIOC input; does not open PTT",
    )
    listen.add_argument(
        "--timeout",
        type=float,
        default=60,
        help="Seconds to wait for someone to start talking when capturing (default 60)",
    )
    commands.add_parser(
        "models", help="Download or verify the local faster-whisper speech model; no TX"
    )
    talk = commands.add_parser(
        "talk",
        help="Capture speech, apply the wake gate, and print a simulated reply; never transmits",
    )
    talk.add_argument("wav", nargs="?", type=Path, help="WAV to gate (no radio)")
    talk.add_argument(
        "--capture",
        action="store_true",
        help="Record from the pinned AIOC input; does not open PTT",
    )
    talk.add_argument(
        "--timeout",
        type=float,
        default=60,
        help="Seconds to wait for someone to start talking when capturing (default 60)",
    )
    talk.add_argument(
        "--once",
        action="store_true",
        help="Handle one utterance and exit",
    )
    return result


def listen_command(args: argparse.Namespace) -> None:
    if args.capture and args.wav is not None:
        raise WalkietalkError("Use either a WAV file or --capture, not both")
    if not args.capture and args.wav is None:
        raise WalkietalkError("Pass a WAV file to transcribe, or --capture to listen on the AIOC")
    if args.capture and args.config is None:
        raise WalkietalkError("Hardware access requires --config with explicit AIOC devices")
    config = load_config(args.config) if args.config else Config()
    if args.capture:
        wait = seconds(args.timeout, "--timeout", maximum=300)
        preflight(config, require_serial=False)
        emit("status", "Receive-only: PTT will not be opened.")
        emit("meter", f"Loading speech model {config.stt_model}...")
        model = load_model(config.stt_model)
        utterance = capture_from_device(config.input_device, config, wait, log=capture_log)
    else:
        utterance = capture_from_wav(args.wav, config, log=capture_log)
        emit("meter", f"Loading speech model {config.stt_model}...")
        model = load_model(config.stt_model)
    emit("meter", "Transcribing...")
    text = transcribe_audio(model, utterance.pcm, utterance.rate)
    if not text:
        raise WalkietalkError(
            "Speech was captured but produced no words. Try a clearer sentence, "
            "or set stt.model to base and rerun walkietalk models."
        )
    emit("transcript", f"Transcript: {text}")


def talk_command(args: argparse.Namespace) -> None:
    if args.capture and args.wav is not None:
        raise WalkietalkError("Use either a WAV file or --capture, not both")
    if not args.capture and args.wav is None:
        raise WalkietalkError("Pass a WAV file, or --capture to listen on the AIOC")
    if args.capture and args.config is None:
        raise WalkietalkError("Hardware access requires --config with explicit AIOC devices")
    config = load_config(args.config) if args.config else Config()
    session = ListeningSession(config)
    once = args.once or args.wav is not None
    model = None
    if args.capture:
        wait = seconds(args.timeout, "--timeout", maximum=300)
        preflight(config, require_serial=False)
        emit("status", "Receive-only: PTT will not be opened.")
        emit("meter", f"Loading speech model {config.stt_model}...")
        model = load_model(config.stt_model)
    while True:
        emit("status", session.status_line())

        def on_wait() -> None:
            message = session.expire_if_needed()
            if message:
                emit("warn", message)
                emit("status", session.status_line())

        if args.capture:
            utterance = capture_from_device(
                config.input_device, config, wait, log=capture_log, on_wait=on_wait
            )
        else:
            utterance = capture_from_wav(args.wav, config, log=capture_log)
            emit("meter", f"Loading speech model {config.stt_model}...")
            model = load_model(config.stt_model)
        emit("meter", "Transcribing...")
        text = transcribe_audio(model, utterance.pcm, utterance.rate)
        if not text:
            emit("ignored", "Ignored (empty transcript). Window unchanged.")
            if once:
                return
            continue
        emit("transcript", f"Transcript: {text}")
        started = utterance.started_at if utterance.started_at is not None else time.monotonic()
        decision = session.decide(text, started)
        if decision.kind == "wake_only":
            emit("status", decision.message)
            session.complete_turn()
            emit("status", session.status_line())
        elif decision.accepted:
            emit("accepted", decision.message)
            emit("accepted", f"Traffic: {decision.traffic}")
            emit("reply", "Simulated reply complete.")
            session.complete_turn()
            emit("status", session.status_line())
        else:
            emit("ignored", decision.message)
        if once:
            return


def models_command(args: argparse.Namespace) -> None:
    config = load_config(args.config) if args.config else Config()
    emit("status", f"Speech model: {config.stt_model}")
    path = ensure_model(config.stt_model, download=True)
    size = model_size_bytes(config.stt_model)
    emit("status", f"Ready at {path} ({size / 1_000_000:.0f} MB).")


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
    if args.command == "models":
        models_command(args)
        return
    if args.command == "listen":
        listen_command(args)
        return
    if args.command == "talk":
        talk_command(args)
        return
    live = args.command == "check" or args.transmit
    if live and args.config is None:
        raise WalkietalkError("Hardware access requires --config with explicit AIOC devices")
    config = load_config(args.config) if args.config else Config()
    if args.command == "check":
        preflight(config)
        status = (
            f"ready ({model_size_bytes(config.stt_model) / 1_000_000:.0f} MB)"
            if model_ready(config.stt_model)
            else "not downloaded (run walkietalk models)"
        )
        print(f"STT: {config.stt_model}; {status}", flush=True)
        print(ListeningSession(config).status_line(), flush=True)
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
        if args.command in {"listen", "talk"}:
            emit("warn", "Stopped; capture closed.", file=sys.stderr)
        elif args.command == "models":
            emit("warn", "Stopped.", file=sys.stderr)
        else:
            emit("warn", "Stopped; PTT cleanup attempted.", file=sys.stderr)
        return 130
    except (WalkietalkError, OSError) as exc:
        emit("error", f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
