"""Inspect, check, pulse PTT, play speech, and transcribe radio speech."""

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

from . import __version__
from .agent import AgentSession, open_agent
from .audio import Playback, Wav, read_wav
from .callsign import CallsignSession, join_identification
from .capture import capture_from_device, capture_from_wav
from .config import Config, WalkietalkError, load_config, seconds
from .devices import audio_devices, preflight
from .ptt import DryPTT, SerialPTT
from .session import handle_stop_signals, transmit, uninterrupted_cleanup
from .setup import credentials_environment, initialize
from .shutdown import ShutdownSession
from .stt import ensure_model, model_ready, model_size_bytes, open_stt
from .term import capture_log, emit
from .tts import open_tts, radio_wav, write_wav
from .wake import ListeningSession


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="walkietalk: radio PTT, speech-to-text, wake gate, and optional spoken replies"
    )
    result.add_argument(
        "-c", "--config", type=Path, help="YAML config (required for hardware access)"
    )
    result.add_argument("--version", action="version", version=f"walkietalk {__version__}")
    env = result.add_mutually_exclusive_group()
    env.add_argument(
        "--env-file",
        type=Path,
        help="Private credentials file; defaults to credentials.env beside --config",
    )
    env.add_argument(
        "--no-env-file", action="store_true", help="Use only the existing process environment"
    )
    commands = result.add_subparsers(dest="command", required=True)
    setup = commands.add_parser(
        "init", help="Create a new private config and credentials directory; no hardware"
    )
    setup.add_argument(
        "--directory",
        type=Path,
        default=Path.home() / ".config" / "walkietalk",
        help="New directory (default: ~/.config/walkietalk); refuses to overwrite",
    )
    commands.add_parser("config-check", help="Validate YAML without devices, logins, or network")
    commands.add_parser("devices", help="List exact audio names and stable serial paths; no TX")
    commands.add_parser("check", help="Validate device selection and permissions; no TX")
    agent_check = commands.add_parser(
        "agent-check", help="Ask the selected agent for text; no STT, audio, or PTT"
    )
    agent_check.add_argument(
        "text", nargs="?", default="Hello.", help="Traffic without a wake name"
    )
    tts_check = commands.add_parser(
        "tts-check", help="Create a speech WAV with selected TTS; no hardware"
    )
    tts_check.add_argument("text", help="Short text to turn into speech")
    tts_check.add_argument("--output", required=True, type=Path, help="New WAV file to create")
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
        help="Capture speech and print an agent reply; spoken radio reply only with --transmit",
    )
    talk.add_argument(
        "--transmit", action="store_true", help="Synthesize and transmit replies; requires --config"
    )
    talk.add_argument("wav", nargs="?", type=Path, help="WAV to gate; no radio unless --transmit")
    talk.add_argument(
        "--capture",
        action="store_true",
        help="Record from the pinned AIOC input; replies transmit only with --transmit",
    )
    talk.add_argument(
        "--timeout",
        type=float,
        help="Wait-for-speech limit for --capture --once only (default 60); "
        "continuous mode has no idle limit",
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
    listener = open_stt(config)
    emit("status", f"Listener: {listener.label()}")
    if args.capture:
        wait = seconds(args.timeout, "--timeout", maximum=300)
        preflight(config, require_serial=False)
        emit("status", "Receive-only: PTT will not be opened.")
        emit("meter", f"Preparing {listener.label()}...")
        listener.prepare()
        utterance = capture_from_device(config.input_device, config, wait, log=capture_log)
    else:
        utterance = capture_from_wav(args.wav, config, log=capture_log)
        emit("meter", f"Preparing {listener.label()}...")
        listener.prepare()
    emit("meter", "Transcribing...")
    text = listener.transcribe(utterance.pcm, utterance.rate)
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
    if (args.capture or args.transmit) and args.config is None:
        raise WalkietalkError("Hardware access requires --config with explicit AIOC devices")
    config = load_config(args.config) if args.config else Config()
    session = ListeningSession(config)
    shutdown = ShutdownSession(config)
    callsigns = CallsignSession(config, time.monotonic)
    agent = open_agent(config)
    spoken_seconds = config.max_tx_seconds - config.settle_seconds if args.transmit else None

    def new_conversation(backend):
        return AgentSession(config, backend, spoken_seconds=spoken_seconds)

    conversation = new_conversation(agent)
    once = args.once or args.wav is not None
    if args.timeout is not None and not (args.capture and args.once):
        raise WalkietalkError(
            "talk --timeout is only for --capture --once; omit it for continuous listening"
        )
    listener = open_stt(config)
    emit("status", f"Listener: {listener.label()}")
    emit("status", f"Agent: {agent.label()}")
    voice = None
    if args.transmit:
        voice = open_tts(config)
        voice.prepare()  # Missing engine/model fails before any agent request or serial open.
        emit("status", f"Voice: {voice.label()}")
        emit("status", "Radio replies enabled; PTT stays off until speech and playback are ready.")
        if callsigns.enabled():
            emit(
                "status",
                f"Station ID {config.callsign!r} mode {config.callsign_mode}; "
                "supplied in config, not invented.",
            )
        if config.post_tx_mute_seconds:
            emit(
                "status",
                f"Post-transmit mute {config.post_tx_mute_seconds:g}s after unkey.",
            )
    if args.transmit and not args.capture:
        preflight(config)
    if args.capture:
        wait = (
            seconds(args.timeout if args.timeout is not None else 60, "--timeout", maximum=300)
            if once
            else None
        )
        preflight(config, require_serial=args.transmit)
        if not args.transmit:
            emit("status", "Receive-only: PTT will not be opened.")
        emit("meter", f"Preparing {listener.label()}...")
        listener.prepare()
    if config.shutdown_enabled:
        notice = "Remote shutdown enabled; phrase and code together or in two transmissions."
        if args.transmit and config.shutdown_arm_confirmation_phrase:
            notice += " The phrase alone is acknowledged on the radio."
        if args.transmit and config.shutdown_confirmation_phrase:
            notice += " After the code, the confirmation phrase is spoken before exit."
        emit("status", notice)
    while True:
        emit("status", session.status_line())

        def on_wait() -> None:
            shutdown_message = shutdown.expire_if_needed()
            if shutdown_message:
                emit("warn", shutdown_message)
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
            emit("meter", f"Preparing {listener.label()}...")
            listener.prepare()
        emit("meter", "Transcribing...")
        try:
            text = listener.transcribe(utterance.pcm, utterance.rate)
        except (WalkietalkError, OSError) as exc:
            if once:
                raise
            session.close()
            shutdown.close()
            emit("error", f"Transcription failed: {exc}", file=sys.stderr)
            emit(
                "status", "Still listening; shutdown cancelled. Say the wake phrase and try again."
            )
            continue
        started = utterance.started_at if utterance.started_at is not None else time.monotonic()
        control = shutdown.decide(text, started)
        if control.kind != "none":
            session.close()
            # Never print the code or send control traffic into agent context.
            emit("status", control.message)
            if control.kind == "confirmed":
                speak_shutdown_confirmation(config, voice)
                return
            if control.kind == "armed":
                spoken = acknowledge(
                    config,
                    voice,
                    config.shutdown_arm_confirmation_phrase,
                    preparing=(
                        "Speaking shutdown phrase confirmation; PTT off until speech is ready..."
                    ),
                    failed="Shutdown phrase confirmation failed",
                    finished="Shutdown phrase confirmation finished; PTT released.",
                )
                if spoken == "spoken":
                    wait_post_tx_mute(config)
                elif spoken == "failed":
                    emit(
                        "status",
                        "Still armed; the phrase confirmation was not transmitted.",
                    )
            if once:
                return
            continue
        if not text:
            emit("ignored", "Ignored (empty transcript). Window unchanged.")
            if once:
                return
            continue
        emit("transcript", f"Transcript: {text}")
        decision = session.decide(text, started)
        if decision.kind == "wake_only":
            emit("status", decision.message)
            spoken = acknowledge(
                config,
                voice,
                config.wake_confirmation_phrase,
                preparing="Speaking wake confirmation; PTT off until speech is ready...",
                failed="Wake confirmation failed",
                finished="Wake confirmation finished; PTT released.",
            )
            if spoken == "spoken":
                wait_post_tx_mute(config)
            elif spoken == "failed":
                emit("status", "Wake was received; the confirmation was not transmitted.")
            session.complete_turn()
            emit("status", session.status_line())
        elif decision.accepted:
            emit("accepted", decision.message)
            emit("accepted", f"Traffic: {decision.traffic}")
            # Eligibility is already decided using speech start time. Close the
            # old window while working; failure/interruption must not reopen it.
            session.close()
            previous_history = conversation.history
            try:
                answer = conversation.reply(decision.traffic)
            except (WalkietalkError, OSError) as exc:
                if once:
                    raise
                emit("error", f"Agent failed: {exc}", file=sys.stderr)
                # Keep completed pairs, but never resume a failed remote turn.
                history = conversation.history
                conversation = new_conversation(open_agent(config))
                conversation.history = history
                emit("status", "Still listening; say the wake phrase and try again.")
                continue
            if voice is not None:
                try:
                    emit("status", "Generating speech; PTT off...")
                    speech = radio_wav(voice.synthesize(answer), spoken_seconds, truncate=True)
                except (WalkietalkError, OSError) as exc:
                    # A completed model reply that was never spoken is not radio history.
                    conversation = new_conversation(open_agent(config))
                    conversation.history = previous_history
                    if once:
                        raise
                    emit("error", f"Speech failed: {exc}", file=sys.stderr)
                    emit("status", "Still listening; say the wake phrase and try again.")
                    continue
                if callsigns.due():
                    try:
                        emit("status", "Generating station ID; PTT off...")
                        ident = radio_wav(
                            voice.synthesize(config.callsign), spoken_seconds, truncate=True
                        )
                        speech = join_identification(speech, ident, spoken_seconds)
                        callsigns.mark()
                    except (WalkietalkError, OSError) as exc:
                        emit("error", f"Station ID failed: {exc}", file=sys.stderr)
                        emit("status", "Sending the answer without a station ID.")
                # Capture has closed before STT. It stays closed throughout synthesis/TX.
                # Hardware errors stop the loop after cleanup instead of retrying hardware.
                transmit_speech(speech, config)
                wait_post_tx_mute(config)
            emit("reply", f"Reply: {answer}")
            session.complete_turn()
            emit("status", session.status_line())
        else:
            emit("ignored", decision.message)
        if once:
            return


def wait_post_tx_mute(config: Config) -> None:
    mute = config.post_tx_mute_seconds
    if mute <= 0:
        return
    emit("status", f"Post-transmit mute {mute:g}s; PTT released, capture closed.")
    time.sleep(mute)
    emit("status", "Mute ended; listening.")


def acknowledge(
    config: Config, voice, text: str, *, preparing: str, failed: str, finished: str
) -> str:
    """Return spoken, silent, or failed for speech generation; transmission errors raise."""
    if voice is None or not text:
        return "silent"
    try:
        emit("status", preparing)
        speech = radio_wav(voice.synthesize(text), config.max_tx_seconds - config.settle_seconds)
    except (WalkietalkError, OSError) as exc:
        emit("error", f"{failed}: {exc}", file=sys.stderr)
        return "failed"
    try:
        transmit_speech(speech, config, finished=finished)
    except (WalkietalkError, OSError) as exc:
        raise WalkietalkError(
            f"{failed}: {exc}. Stopping walkietalk; check the radio before restarting."
        ) from exc
    return "spoken"


def speak_shutdown_confirmation(config: Config, voice) -> None:
    """Speak the code confirmation, then stop; transmission errors exit through main."""
    if (
        acknowledge(
            config,
            voice,
            config.shutdown_confirmation_phrase,
            preparing="Speaking shutdown confirmation; PTT off until speech is ready...",
            failed="Shutdown confirmation failed",
            finished="Shutdown confirmation finished; PTT released.",
        )
        == "failed"
    ):
        emit("status", "Stopping walkietalk without an on-air confirmation.")


def transmit_speech(
    speech: Wav, config: Config, finished: str = "Spoken reply finished; PTT released."
) -> None:
    """Prepare the isolated worker first, then key/play/unkey in the parent."""
    speech = radio_wav(speech, config.max_tx_seconds - config.settle_seconds)
    with tempfile.TemporaryDirectory(prefix="walkietalk-reply-") as directory:
        path = Path(directory) / "reply.wav"
        write_wav(path, speech)
        playback = Playback(
            path, config.output_device, config.gain, config.max_tx_seconds - config.settle_seconds
        )
        try:
            playback.prepare()
            ptt = SerialPTT(config.serial_port, config.line)

            def action(deadline: float) -> None:
                time.sleep(min(config.settle_seconds, max(0, deadline - time.monotonic())))
                playback.play(deadline)

            transmit(ptt, action, config.max_tx_seconds)
        finally:
            with uninterrupted_cleanup():
                playback.close()  # transmit has already attempted release before worker cleanup.
    emit("status", finished)


def models_command(args: argparse.Namespace) -> None:
    config = load_config(args.config) if args.config else Config()
    emit("status", f"Speech model: {config.stt_model}")
    path = ensure_model(config.stt_model, download=True)
    size = model_size_bytes(config.stt_model)
    emit("status", f"Ready at {path} ({size / 1_000_000:.0f} MB).")


def run(args: argparse.Namespace) -> None:
    if args.command == "init":
        initialize(args.directory)
        directory = args.directory.expanduser().absolute()
        print(f"Created config: {directory / 'config.yaml'}")
        print(f"Created private credentials file: {directory / 'credentials.env'}")
        print("Edit device names and backend settings before using hardware. No hardware opened.")
        return
    if args.command == "config-check":
        if args.config is None:
            raise WalkietalkError("config-check requires --config")
        config = load_config(args.config)
        print("Config OK. No hardware, network, or login checks performed.")
        print(
            f"Agent: {config.agent_backend}; STT: {config.stt_backend}; voice: {config.tts_backend}"
        )
        print(
            f"Listening: {config.listening_mode}; "
            f"follow-up window {config.conversation_timeout_seconds:g}s"
        )
        return
    if args.command == "tts-check":
        config = load_config(args.config) if args.config else Config()
        if args.output.exists():
            raise WalkietalkError("Output file already exists; choose a new --output path")
        voice = open_tts(config)
        emit("status", f"Voice: {voice.label()}")
        speech = radio_wav(
            voice.synthesize(args.text), config.max_tx_seconds - config.settle_seconds
        )
        write_wav(args.output, speech)
        emit(
            "status",
            f"Speech WAV: {args.output}; {speech.rate} Hz, mono PCM16, {speech.duration:.3f}s. "
            "No hardware opened.",
        )
        return
    if args.command == "agent-check":
        config = load_config(args.config) if args.config else Config()
        agent = open_agent(config)
        emit("status", "Text-only agent check: no STT, audio, or PTT.")
        emit("status", f"Agent: {agent.label()}")
        answer = AgentSession(config, agent).reply(args.text)
        emit("reply", f"Reply: {answer}")
        return
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
        listener = open_stt(config)
        if config.stt_backend == "faster-whisper":
            status = (
                f"ready ({model_size_bytes(config.stt_model) / 1_000_000:.0f} MB)"
                if model_ready(config.stt_model)
                else "not downloaded (run walkietalk models)"
            )
        elif config.stt_backend == "grok":
            try:
                listener.prepare()
                status = "SuperGrok Plus login ready"
            except WalkietalkError as exc:
                status = str(exc)
        else:
            key = "set" if os.environ.get("XAI_API_KEY") else "missing"
            status = f"XAI_API_KEY {key}; billed API, not SuperGrok Plus"
        print(f"STT: {listener.label()}; {status}", flush=True)
        print(f"Agent: {open_agent(config).label()}; text replies only", flush=True)
        print(
            f"Voice: {open_tts(config).label()}; used only by tts-check or talk --transmit",
            flush=True,
        )
        print(ListeningSession(config).status_line(), flush=True)
        if config.shutdown_enabled:
            print(
                "Shutdown: enabled; phrase confirmation "
                f"{config.shutdown_arm_confirmation_phrase!r}; code confirmation "
                f"{config.shutdown_confirmation_phrase!r} with talk --transmit",
                flush=True,
            )
        if config.wake_confirmation_phrase:
            print(
                "Wake confirmation: "
                f"{config.wake_confirmation_phrase!r} when the wake phrase arrives alone; "
                "spoken with talk --transmit",
                flush=True,
            )
        print(f"Post-TX mute: {config.post_tx_mute_seconds:g}s after unkey", flush=True)
        if config.callsign and config.callsign_mode != "off":
            print(
                f"Station ID: {config.callsign!r}; mode {config.callsign_mode}",
                flush=True,
            )
        else:
            print(
                "Station ID: off; set radio.callsign to your granted ID to speak it",
                flush=True,
            )
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
        with (
            handle_stop_signals(),
            credentials_environment(args.config, args.env_file, disabled=args.no_env_file),
        ):
            run(args)
        return 0
    except KeyboardInterrupt:
        if args.command in {"listen", "talk"}:
            emit("warn", "Stopped; capture closed.", file=sys.stderr)
        elif args.command in {"models", "agent-check", "tts-check"}:
            emit("warn", "Stopped.", file=sys.stderr)
        else:
            emit("warn", "Stopped; PTT cleanup attempted.", file=sys.stderr)
        return 130
    except (WalkietalkError, OSError) as exc:
        emit("error", f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
