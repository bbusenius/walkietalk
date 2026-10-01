"""Inspect, check, pulse PTT, play speech, and transcribe radio speech."""

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

from . import __version__
from .agent import AgentSession, open_agent, render_guidance
from .audio import Playback, PlaybackPreparationError, Wav, read_wav
from .callsign import (
    IDENT_GAP_SECONDS,
    CallsignSession,
    StationIDError,
    identification_transmissions,
    morse_wav,
    station_id_wav,
)
from .capture import capture_from_device, capture_from_wav
from .config import (
    MESSAGING_SERVICES,
    Config,
    WalkietalkError,
    load_config,
    seconds,
)
from .devices import audio_devices, preflight
from .grok_realtime import RealtimeAuthenticationError, offline_voice_check, open_voice_agent
from .messaging import (
    MAX_TRANSCRIBE_SECONDS,
    PLAYBACK_ATTEMPTS,
    MessagePlayback,
    MessagingReady,
    normalize_message,
    open_messaging,
    spoken_text,
    voice_wav,
)
from .operator_mode import (
    COMMAND_HELP,
    OPERATOR_ACTIONS,
    OperatorReady,
    OperatorServer,
    edited_status,
    normalized_change,
    operator_request,
    operator_snapshot,
)
from .ptt import DryPTT, SerialPTT
from .session import PTTHardwareError, handle_stop_signals, transmit, uninterrupted_cleanup
from .setup import credentials_environment, initialize
from .shutdown import ShutdownSession
from .streaming_playback import StreamingPlayback
from .stt import ensure_model, model_ready, model_size_bytes, open_stt
from .term import capture_log, emit
from .tts import open_tts, radio_wav, write_wav
from .voice_agent_tx import (
    RealtimeCaptureError,
    RealtimeHardwareError,
    RealtimeTalkSession,
    RealtimeTranscriptTimeout,
    play_segment_dry,
    speak_text_via_realtime,
    supervised_voice_check,
)
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
    voice_agent_check = commands.add_parser(
        "voice-agent-check",
        help="Grok realtime network request; saves a WAV, transmits only with explicit flags",
    )
    voice_agent_check.add_argument(
        "wav", nargs="?", type=Path, help="WAV utterance to send (no radio)"
    )
    voice_agent_check.add_argument(
        "--capture",
        action="store_true",
        help="Record from the pinned AIOC input; does not open PTT",
    )
    voice_agent_check.add_argument(
        "--timeout",
        type=float,
        default=60,
        help="Seconds to wait for speech when capturing (default 60)",
    )
    voice_agent_check.add_argument(
        "--output",
        required=True,
        type=Path,
        help="New reply WAV to create from streamed audio deltas",
    )
    voice_agent_check.add_argument(
        "--supervised",
        action="store_true",
        help="Parent-owned PTT state machine (DryPTT unless --transmit)",
    )
    voice_agent_check.add_argument(
        "--transmit",
        action="store_true",
        help="With --supervised, use real SerialPTT and playback; requires --config",
    )
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
    talk.add_argument(
        "--panel", action="store_true", help="Fixed log and review panels; requires operator mode"
    )
    operator = commands.add_parser(
        "operator", help="Review messages and control a running operator-mode talk instance"
    )
    operator.add_argument("action", nargs="?", default="status", choices=OPERATOR_ACTIONS)
    operator.add_argument(
        "--approved", action="store_true", help="Select the oldest approved incoming message"
    )
    operator.add_argument(
        "--text", help="Replacement text for edit; without it, edit prompts with the current text"
    )
    operator.add_argument(
        "--timeout",
        type=float,
        default=120,
        help="Maximum seconds to wait for a command to finish (default 120)",
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


def _handle_shutdown_control(
    control,
    *,
    session: ListeningSession,
    shutdown: ShutdownSession,
    talk_realtime: RealtimeTalkSession | None,
    config: Config,
    voice,
    realtime: bool,
    transmit: bool,
) -> str:
    """Apply a non-none shutdown decision. Returns 'return', 'continue', or 'fallthrough'."""
    session.close()
    if talk_realtime is not None:
        _clear_realtime_input(talk_realtime)
    # Never print the code or send control traffic into agent context.
    emit("status", control.message)
    if control.kind == "confirmed":
        speak_shutdown_confirmation(config, voice, realtime=realtime, transmit=transmit)
        return "return"
    if control.kind == "armed":
        spoken = acknowledge(
            config,
            voice,
            config.shutdown_arm_confirmation_phrase,
            preparing=("Speaking shutdown phrase confirmation; PTT off until speech is ready..."),
            failed="Shutdown phrase confirmation failed",
            finished="Shutdown phrase confirmation finished; PTT released.",
            realtime=realtime,
            transmit=transmit,
        )
        if spoken == "spoken":
            wait_post_tx_mute(config)
        elif spoken == "failed":
            emit(
                "status",
                "Still armed; the phrase confirmation was not transmitted.",
            )
    return "continue"


def _realtime_commit_reply(
    *,
    talk_realtime: RealtimeTalkSession,
    config: Config,
    args: argparse.Namespace,
    session: ListeningSession,
    callsigns: CallsignSession | None = None,
) -> bool:
    """Commit already-streamed audio and emit result lines. False => recoverable fail."""
    try:
        emit(
            "status",
            "Realtime voice turn (live-streamed audio commit); parent owns PTT...",
        )
        if args.transmit:
            ptt = SerialPTT(config.serial_port, config.line)

            play = StreamingPlayback(config)

        else:
            ptt = DryPTT()
            play = play_segment_dry
        # Audio was already streamed during capture. Commit + respond.
        # Never substitute the gate transcript for captured audio.
        result = talk_realtime.commit_and_respond(
            ptt,
            allow_key=True,
            play_segment=play,
        )
    except (RealtimeHardwareError, RealtimeAuthenticationError):
        raise
    except (WalkietalkError, OSError) as exc:
        if args.once or args.wav is not None:
            raise
        emit("error", f"Voice agent failed: {exc}", file=sys.stderr)
        emit("status", "Still listening; say the wake phrase and try again.")
        return False
    keyed = any(action.kind == "key" for action in result.ptt_actions)
    if not keyed:
        session.close()
        if args.once or args.wav is not None:
            raise WalkietalkError("Voice agent returned no audible reply")
        emit("warn", "Voice agent returned no audible reply; follow-up window remains closed.")
        return False
    if result.input_transcript:
        emit("transcript", f"Heard: {result.input_transcript}")
    if result.output_transcript:
        emit("reply", f"Reply: {result.output_transcript}")
    elif result.reply_wav is None:
        emit("warn", "Voice agent returned no spoken audio; no stt/agent/tts fallback.")
    if result.ptt_actions:
        summary = ", ".join(f"{item.kind}:{item.reason}" for item in result.ptt_actions)
        emit("status", f"PTT actions: {summary}")
    if result.truncated_by_tx_cap:
        emit(
            "warn", "TX limit reached; interrupted conversation discarded. Next turn starts fresh."
        )
    if args.transmit:
        if callsigns is not None and callsigns.due():
            emit("status", "Sending station ID in a separate bounded burst; PTT released.")
            time.sleep(IDENT_GAP_SECONDS)
            try:
                if config.callsign_method == "morse":
                    transmit_speech(
                        morse_wav(config.callsign),
                        config,
                        finished="Station ID finished; PTT released.",
                    )
                else:
                    ident = speak_text_via_realtime(
                        config,
                        config.callsign,
                        SerialPTT(config.serial_port, config.line),
                        allow_key=True,
                        play_segment=StreamingPlayback(config),
                    )
                    if ident.truncated_by_tx_cap or not ident.ptt_actions:
                        raise WalkietalkError("Station ID was not transmitted in full")
                callsigns.mark()
            except (RealtimeHardwareError, PTTHardwareError):
                raise
            except (WalkietalkError, OSError) as exc:
                wait_post_tx_mute(config)
                raise StationIDError(str(exc), message_transmitted=True) from exc
        wait_post_tx_mute(config)
    if not result.truncated_by_tx_cap:
        session.complete_turn()
    emit("status", session.status_line())
    return True


def talk_command(args: argparse.Namespace) -> None:
    if args.capture and args.wav is not None:
        raise WalkietalkError("Use either a WAV file or --capture, not both")
    if not args.capture and args.wav is None:
        raise WalkietalkError("Pass a WAV file, or --capture to listen on the AIOC")
    if (args.capture or args.transmit) and args.config is None:
        raise WalkietalkError("Hardware access requires --config with explicit AIOC devices")
    config = load_config(args.config) if args.config else Config()
    if config.messaging_operator_mode:
        if not args.capture or args.once:
            raise WalkietalkError("Operator mode requires continuous talk --capture (no --once)")
    if args.panel:
        if not config.messaging_operator_mode:
            raise WalkietalkError("--panel requires messaging.operator_mode: true")
        from .operator_panel import OperatorPanel, validate_terminal

        validate_terminal()
    realtime = config.agent_backend == "grok_realtime"
    session = ListeningSession(config)
    shutdown = ShutdownSession(config)
    callsigns = CallsignSession(config, time.monotonic)
    spoken_seconds = config.max_tx_seconds - config.settle_seconds if args.transmit else None
    agent = None
    conversation = None
    if not realtime:

        def new_conversation(backend):
            return AgentSession(config, backend, spoken_seconds=spoken_seconds)

        agent = open_agent(config)
        conversation = new_conversation(agent)
    once = args.once or args.wav is not None
    if args.timeout is not None and not (args.capture and args.once):
        raise WalkietalkError(
            "talk --timeout is only for --capture --once; omit it for continuous listening"
        )
    listener = None if realtime else open_stt(config)
    if listener is not None:
        emit("status", f"Listener: {listener.label()}")
    if realtime:
        emit(
            "status",
            f"Agent: grok_realtime ({config.agent_realtime_model}); "
            "native realtime transcripts gate wake/sleep/shutdown; audio replies stream directly",
        )
    else:
        emit("status", f"Agent: {agent.label()}")
    voice = None
    messaging_enabled = any(getattr(config, service).enabled() for service in MESSAGING_SERVICES)
    if args.transmit:
        if realtime:
            emit(
                "status",
                "Radio replies and acks via parent-owned realtime TX; "
                "PTT stays off until audible AI audio energy.",
            )
            emit(
                "status",
                f"Realtime voice: {config.agent_realtime_voice} "
                f"({config.agent_realtime_model}); "
                + ("TTS handles contact messages." if messaging_enabled else "TTS not used."),
            )
        if not realtime or messaging_enabled:
            prepared_voice = open_tts(config)
            # Fail before any agent request or serial open.
            prepared_voice.prepare()
            voice = prepared_voice
            emit("status", f"Voice: {voice.label()}")
            emit(
                "status",
                "Radio replies enabled; PTT stays off until speech and playback are ready.",
            )
        if callsigns.enabled():
            emit(
                "status",
                f"Station ID {config.callsign!r} mode {config.callsign_mode} "
                f"method {config.callsign_method}; supplied in config, not invented.",
            )
        if config.post_tx_mute_seconds:
            emit(
                "status",
                f"Post-transmit mute {config.post_tx_mute_seconds:g}s after unkey.",
            )
    elif realtime:
        emit("status", "Realtime dry path: DryPTT only; no SerialPTT.")
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
        if listener is not None:
            emit("meter", f"Preparing {listener.label()}...")
            listener.prepare()
    message_listener = listener
    if realtime and any(
        getattr(config, service).enabled() and getattr(config, service).transcribe_voice
        for service in MESSAGING_SERVICES
    ):
        message_listener = open_stt(config)
        emit("status", f"Voice-note transcription: {message_listener.label()}")
        message_listener.prepare()
    if config.shutdown_enabled:
        notice = "Remote shutdown enabled; phrase and code together or in two transmissions."
        if args.transmit and config.shutdown_arm_confirmation_phrase:
            notice += " The phrase alone is acknowledged on the radio."
        if args.transmit and config.shutdown_confirmation_phrase:
            notice += " After the code, the confirmation phrase is spoken before exit."
        emit("status", notice)
    talk_realtime: RealtimeTalkSession | None = None
    if realtime:
        talk_realtime = RealtimeTalkSession(config)
    bridge = None
    operator = None
    controls = None
    panel = None

    def operator_log(kind: str, message: str, *, file=None) -> None:
        if operator is not None:
            operator.respond(kind, message)
        emit(kind, message, file=file)

    def speak_notice(phrase: str, *, realtime_voice: bool = False, **messages) -> str:
        result = acknowledge(
            config, voice, phrase, realtime=realtime_voice, transmit=args.transmit, **messages
        )
        if result == "spoken":
            wait_post_tx_mute(config)
        return result

    def enter_sleep(message: str, *, local: bool = False) -> None:
        session.sleep()
        if shutdown.is_armed:
            shutdown.close()
            emit("status", "Pending shutdown cancelled.")
        if operator is not None and operator.cancel_dispatch():
            emit("status", "Operator delivery paused; remaining message stays approved.")
        log = operator_log if local else emit
        log("status", message)
        if talk_realtime is not None:
            _clear_realtime_input(talk_realtime)
        spoken = speak_notice(
            config.sleep_confirmation_phrase,
            preparing="Speaking sleep confirmation; PTT off until speech is ready...",
            failed="Sleep confirmation failed",
            finished="Sleep confirmation finished; PTT released.",
            realtime_voice=realtime,
        )
        if spoken == "failed":
            log("status", "Sleep mode entered; the confirmation was not transmitted.")
        log("status", session.status_line())
        if panel is not None:
            panel.delivery_status("status", "Asleep; queued messages retained.")

    next_message_at = 0.0
    message_progress: dict[tuple[str, str], MessagePlayback] = {}

    def message_service() -> str:
        requested = operator.dispatch_service() if operator is not None else ""
        return requested or session.destination

    def ready_message(service: str) -> bool:
        item = bridge.peek(service)
        if item is None:
            return False
        if operator is None:
            return True
        return (
            not controls.closed.is_set()
            and (panel is None or not panel.closed.is_set())
            and operator.approved(item)
            and (
                operator.dispatched(item)
                or (
                    session.destination == service
                    and (
                        bridge.mode(service).listening_mode == "wake_phrase"
                        or session.follow_up_open_at(time.monotonic())
                    )
                )
            )
        )

    def transcribe_review(audio: Wav) -> str:
        nonlocal message_listener
        if message_listener is None:
            prepared_listener = open_stt(config)
            operator_log("status", f"Preparing voice transcription: {prepared_listener.label()}...")
            prepared_listener.prepare()
            message_listener = prepared_listener
        transcript = normalize_message(message_listener.transcribe(audio.frames, audio.rate))
        if not transcript:
            raise WalkietalkError("Voice message transcription returned no words")
        return transcript

    def transcribe_note(item, progress) -> None:
        if progress.transcript is not None:
            return
        if item.audio_path is None:
            raise WalkietalkError("Voice message has no audio")
        audio = voice_wav(item.audio_path, MAX_TRANSCRIBE_SECONDS, rate=16000, truncate=False)
        transcript = transcribe_review(audio)
        progress.transcript = transcript
        progress.remaining = transcript

    def prepare_voice_message(item, progress) -> Wav:
        """Prepare radio audio separately from the full-note review transcript."""
        if progress.speech is not None:
            return progress.speech
        if item.audio_path is None:
            raise WalkietalkError("Voice message has no audio")
        limit = config.max_tx_seconds - config.settle_seconds
        mode = bridge.mode(item.service)
        alias = mode.sender_alias or mode.wake
        introduction = radio_wav(voice.synthesize(f"{alias} says:", truncate=False), limit)
        if introduction.duration >= limit:
            raise WalkietalkError("Sender introduction exceeds the transmit limit")
        audio = voice_wav(item.audio_path, limit - introduction.duration)
        frames = introduction.frames + audio.frames
        progress.speech = Wav(frames, audio.rate, len(frames) / (audio.rate * 2))
        return progress.speech

    def preview_item(item) -> None:
        if item.edited is not None:
            for kind, message in edited_status(item.edited, item.original):
                operator_log(kind, message)
            return
        transcript = None
        if item.kind == "text":
            operator_log("reply", item.incoming.text if item.incoming else item.text)
        elif item.incoming is None:
            # The capture gate already transcribed the complete outgoing recording.
            transcript = item.transcript or item.text or transcribe_review(item.audio)
            operator_log("reply", f"Voice transcript: {transcript}")
        else:
            incoming = item.incoming
            if incoming.audio_path is None:
                raise WalkietalkError("Voice message has no audio")
            key = (incoming.service, incoming.identity)
            progress = message_progress.setdefault(key, MessagePlayback(incoming.text))
            mode = bridge.mode(item.service)
            if mode.transcribe_voice:
                operator_log("status", "Transcribing voice message for review...")
                transcribe_note(incoming, progress)
                transcript = progress.transcript
            else:
                if item.transcript is None:
                    operator_log("status", "Transcribing voice message for review...")
                    if progress.preview_audio is None:
                        progress.preview_audio = voice_wav(
                            incoming.audio_path, MAX_TRANSCRIBE_SECONDS, rate=16000, truncate=False
                        )
                    transcript = transcribe_review(progress.preview_audio)
                else:
                    transcript = item.transcript
                progress.preview_audio = None
            operator_log("reply", f"Voice transcript: {transcript}")
        operator.mark_previewed(item, transcript)

    def operator_command() -> None:
        command, item, stale = operator.next_command()
        ok = False
        try:
            if command not in ("read", "edit", "approve", "transmit", "deny", "sleep"):
                operator_log("status", COMMAND_HELP)
                return
            if stale:
                operator_log(
                    "warn",
                    "Cancelled operator sleep command ignored."
                    if command == "sleep"
                    else "Stale or cancelled operator command ignored; review the current item.",
                )
                return
            if command == "sleep":
                enter_sleep("Sleep requested by operator; waiting for wake.", local=True)
                ok = True
                return
            if item is None:
                operator_log("status", "No message waiting for operator review.")
                return
            if command == "deny":
                if item.incoming is not None:
                    bridge.discard(item.incoming)
                    message_progress.pop((item.service, item.incoming.identity), None)
                else:
                    operator.finish(item)
                operator_log("status", "Message denied.")
                ok = True
                return
            if command == "edit":
                blocked = operator.edit_block(item)
                if blocked:
                    operator_log("warn", blocked)
                    return
                text = normalized_change(operator.command_text() or "", item.content)
                if text is None:
                    operator_log("status", "Edit cancelled; message unchanged.")
                    ok = True
                    return
                original = operator.edit(item, text)
                if item.incoming is not None:
                    # Delivery restarts from the edited words; cached speech is discarded.
                    message_progress[(item.service, item.incoming.identity)] = MessagePlayback(
                        text, transcript=text
                    )
                mode = bridge.mode(item.service)
                direction = "incoming" if item.incoming else "outgoing"
                operator_log(
                    "status",
                    f"Operator edited item {item.sequence} ({direction} {item.service}, "
                    f"{mode.sender_alias or mode.wake}, {item.kind}).",
                )
                operator_log("status", f"Before: {original}")
                operator_log("reply", f"After: {text}")
                ok = True
                return
            if command == "approve" and not operator.waiting(item):
                operator_log(
                    "warn", "This message is already approved; use transmit to release it."
                )
                return
            if command == "transmit":
                if item.incoming is None:
                    operator_log(
                        "warn", "Transmit applies to incoming messages; approve to send outgoing."
                    )
                    return
                if shutdown.is_armed:
                    operator_log(
                        "warn", "Transmission held while shutdown awaits its confirmation code."
                    )
                    return
                if operator.dispatch_service():
                    operator_log(
                        "warn", "An operator delivery is already pending; wait for it to finish."
                    )
                    return
                if bridge.peek(item.service) is not item.incoming:
                    operator_log(
                        "warn",
                        "An earlier message for this contact is waiting; "
                        "review it or use --approved.",
                    )
                    return
            if command == "read":
                operator_log("status", f"Preparing {item.kind} preview; transmitter unkeyed.")
                preview_item(item)
                operator_log(
                    "status",
                    "Preview complete; message still waiting for approval or denial."
                    if operator.waiting(item)
                    else "Preview complete; message remains approved for delivery.",
                )
            elif item.kind == "voice" and not item.previewed:
                operator_log("warn", "Read the voice message before approval or transmission.")
                return
            elif command == "transmit":
                if operator.waiting(item):
                    operator.finish(item)
                operator.dispatch(item)
                action = "transmission" if args.transmit else "display (receive-only)"
                operator_log(
                    "status",
                    f"One message scheduled for operator {action}; conversation unchanged.",
                )
            elif item.incoming is not None:
                operator.finish(item)
                operator_log("status", "Message approved; waiting for radio delivery.")
            else:
                operator_log("status", "Sending approved outgoing message...")
                if item.audio is not None:
                    bridge.send_voice(item.service, item.audio)
                else:
                    bridge.send_text(item.service, item.content)
                operator.finish(item)
                operator_log("status", "Approved outgoing message sent.")
            ok = True
        except (WalkietalkError, OSError) as exc:
            if command == "sleep":
                # Confirmation transmission faults stop talk, just as voice sleep does.
                raise
            operator_log("error", f"Operator {command} failed: {exc}", file=sys.stderr)
            if command == "approve":
                operator.failed_send()
                operator_log(
                    "warn",
                    "Message retained. Delivery may be uncertain after a send failure; "
                    "review before approving another attempt.",
                )
        finally:
            operator.complete_command(ok)

    def play_next(service: str) -> bool:
        nonlocal next_message_at
        if not ready_message(service):
            return False
        item = bridge.peek(service)
        if item is None:
            return False
        requested = operator is not None and operator.dispatched(item)
        key = (item.service, item.identity)
        words = operator.playback_words(item) if operator is not None else None
        progress = message_progress.setdefault(
            key,
            MessagePlayback(item.text.strip())
            if words is None
            else MessagePlayback(words, transcript=words),
        )
        remaining = ""
        mode = bridge.mode(service)
        alias = mode.sender_alias or mode.wake
        as_text = item.kind == "text" or mode.transcribe_voice
        if panel is not None:
            panel.delivery_status(
                "status",
                "Preparing message for radio delivery..."
                if args.transmit
                else "Displaying message (receive-only)...",
            )
        try:
            if item.kind == "voice" and mode.transcribe_voice and progress.transcript is None:
                transcribe_note(item, progress)
            text = progress.transcript if progress.transcript is not None else item.text
            label = f"{alias} says: {spoken_text(text)}" if as_text else f"{alias}: voice message"
            emit("reply", f"Reply: {label}")
            if args.transmit:
                if as_text:
                    speech, remaining = progress.prepare_text(voice, alias, config)
                else:
                    speech = prepare_voice_message(item, progress)
        except (WalkietalkError, OSError) as exc:
            emit("error", f"Message playback preparation failed: {exc}", file=sys.stderr)
            progress.failures += 1
            if operator is not None:
                operator.hold(item)
                emit("warn", "Message retained for a new operator approval.")
                if panel is not None:
                    panel.delivery_status(
                        "error", "Delivery failed; retained for review. See radio log."
                    )
            elif progress.failures >= PLAYBACK_ATTEMPTS:
                emit(
                    "error",
                    "Skipping unplayable message after three attempts.",
                    file=sys.stderr,
                )
                bridge.acknowledge(item)
                message_progress.pop(key)
            next_message_at = time.monotonic() + 5
            return False
        # Preparation can outlast a receive window or the operator control server.
        if operator is not None and not ready_message(service):
            if panel is not None:
                panel.delivery_status(
                    "warn", "Delivery paused; waiting for an eligible receive window."
                )
            return False
        identification_error = None
        if args.transmit:
            try:
                transmit_with_callsign(speech, config, voice, callsigns)
            except StationIDError as exc:
                if not exc.message_transmitted:
                    raise
                identification_error = exc
            except PTTHardwareError:
                raise
            except (WalkietalkError, OSError) as exc:
                if operator is None:
                    raise
                operator.hold(item)
                emit("error", f"Message transmission failed: {exc}", file=sys.stderr)
                emit(
                    "warn",
                    "Message retained; PTT was not asserted and nothing was transmitted."
                    if isinstance(exc, PlaybackPreparationError)
                    else "Message retained; a partial transmission may have been heard.",
                )
                if panel is not None:
                    panel.delivery_status(
                        "error", "Delivery failed; retained for review. See radio log."
                    )
                next_message_at = time.monotonic() + 5
                return False
        progress.remaining = remaining
        progress.failures = 0
        if not remaining:
            bridge.acknowledge(item)
            message_progress.pop(key)
            if requested:
                emit("status", "Operator delivery complete; conversation unchanged.")
            if panel is not None:
                panel.delivery_status(
                    "status",
                    "Message transmitted; conversation unchanged."
                    if args.transmit and requested
                    else "Message transmitted."
                    if args.transmit
                    else "Message displayed (receive-only).",
                )
        elif panel is not None:
            panel.delivery_status("status", "Part transmitted; remaining chunks pending.")
        if identification_error is not None:
            raise identification_error
        if not requested:
            session.complete_turn()
        # Reopen capture and leave a chance to speak (including sleep) between replies.
        next_message_at = time.monotonic() + 1
        return True

    try:
        if config.messaging_operator_mode:
            controls = OperatorServer(None, args.config)
            controls.acquire()
        bridge = open_messaging(config)
        if config.messaging_operator_mode:
            operator = bridge.operator
            operator.status_provider = session.conversation_status
            controls.review = operator
            emit("status", "Operator mode: messages require local approval. " + COMMAND_HELP)
            operator.announce()
            controls.start()
            if args.panel:
                panel = OperatorPanel(operator, args.config)
                panel.start()
        while True:
            if panel is not None:
                panel.check()
            if operator is not None:
                operator.announce()
                if controls.closed.is_set():
                    raise WalkietalkError("Operator controls closed unexpectedly; stopping talk.")
                if operator.has_commands():
                    operator_command()
                    continue
            emit("status", session.status_line())
            if talk_realtime is not None and not talk_realtime.warm_connected:
                # Contact playback does not depend on the realtime provider.
                service = message_service()
                if (
                    service in MESSAGING_SERVICES
                    and not shutdown.is_armed
                    and time.monotonic() >= next_message_at
                    and ready_message(service)
                ):
                    play_next(service)
                try:
                    emit("status", "Connecting voice session before the next listen.")
                    talk_realtime.warm(instructions=render_guidance(config, spoken=True))
                except RealtimeAuthenticationError:
                    raise
                except (WalkietalkError, OSError) as exc:
                    if once:
                        raise
                    session.close()
                    shutdown.close()
                    emit("error", f"Voice connection failed: {exc}", file=sys.stderr)
                    emit("status", "Still listening; retrying voice connection in 1s.")
                    time.sleep(1)
                    continue

            def on_wait() -> None:
                if panel is not None:
                    panel.check()
                if operator is not None:
                    operator.announce()
                    if controls.closed.is_set() or operator.has_commands():
                        raise OperatorReady()
                if talk_realtime is not None and not talk_realtime.warm_connected:
                    # Close the idle capture before reconnecting; do not discover
                    # an expired socket only after somebody starts their wake phrase.
                    raise RealtimeCaptureError("Voice connection closed while waiting for speech")
                shutdown_message = shutdown.expire_if_needed()
                if shutdown_message:
                    emit("warn", shutdown_message)
                message = session.expire_if_needed()
                if message:
                    emit("warn", message)
                    emit("status", session.status_line())
                service = message_service()
                if (
                    service in MESSAGING_SERVICES
                    and not shutdown.is_armed
                    and time.monotonic() >= next_message_at
                    and ready_message(service)
                ):
                    raise MessagingReady(service)

            stream_frame = talk_realtime.on_frame if talk_realtime is not None else None
            stream_reset = talk_realtime.on_reset if talk_realtime is not None else None
            try:
                if args.capture:
                    utterance = capture_from_device(
                        config.input_device,
                        config,
                        wait,
                        log=capture_log,
                        on_wait=on_wait,
                        on_frame=stream_frame,
                        on_reset=stream_reset,
                    )
                else:
                    utterance = capture_from_wav(
                        args.wav,
                        config,
                        log=capture_log,
                        on_frame=stream_frame,
                        on_reset=stream_reset,
                    )
                    if listener is not None:
                        emit("meter", f"Preparing {listener.label()}...")
                        listener.prepare()
            except OperatorReady:
                if talk_realtime is not None:
                    _clear_realtime_input(talk_realtime)
                continue
            except MessagingReady as ready:
                if talk_realtime is not None:
                    _clear_realtime_input(talk_realtime)
                play_next(ready.service)
                emit("status", session.status_line())
                if once:
                    return
                continue
            except RealtimeCaptureError as exc:
                if once:
                    raise
                session.close()
                shutdown.close()
                emit("error", f"Realtime capture discarded: {exc}", file=sys.stderr)
                emit("status", "Reconnecting on the next utterance; say the wake phrase again.")
                continue
            if controls is not None and controls.closed.is_set():
                raise WalkietalkError("Operator controls closed unexpectedly; stopping talk.")
            if panel is not None:
                panel.check()
            started = utterance.started_at if utterance.started_at is not None else time.monotonic()
            # Every turn uses the same native wake/control/empty-input gate,
            # including follow-ups. Captured audio remains the model input.
            emit("meter", "Transcribing...")
            try:
                text = (
                    talk_realtime.gate_transcript()
                    if talk_realtime is not None
                    else listener.transcribe(utterance.pcm, utterance.rate)
                )
            except RealtimeTranscriptTimeout:
                shutdown.close()
                emit(
                    "warn",
                    "Realtime transcription timed out; no transcript received. Window unchanged.",
                )
                if once:
                    return
                continue
            except RealtimeAuthenticationError:
                raise
            except (WalkietalkError, OSError) as exc:
                if once:
                    raise
                session.close()
                shutdown.close()
                if talk_realtime is not None:
                    _clear_realtime_input(talk_realtime)
                emit("error", f"Transcription failed: {exc}", file=sys.stderr)
                emit(
                    "status",
                    "Still listening; shutdown cancelled. Say the wake phrase and try again.",
                )
                continue
            if panel is not None:
                panel.check()
            control = shutdown.decide(text, started)
            if control.kind != "none":
                if operator is not None:
                    operator.cancel_dispatch()
                action = _handle_shutdown_control(
                    control,
                    session=session,
                    shutdown=shutdown,
                    talk_realtime=talk_realtime,
                    config=config,
                    voice=voice,
                    realtime=realtime,
                    transmit=args.transmit,
                )
                if action == "return":
                    return
                if once:
                    return
                continue
            if not text:
                _ignore_empty_realtime(session, talk_realtime, started)
                if once:
                    return
                continue
            emit("transcript", f"Transcript: {text}")
            decision = session.decide(text, started)
            if operator is not None and (
                decision.destination and decision.destination != operator.dispatch_service()
            ):
                if operator.cancel_dispatch():
                    emit("status", "Operator delivery paused; remaining message stays approved.")
                    if panel is not None:
                        panel.delivery_status(
                            "warn", "Delivery paused; remaining message stays approved."
                        )
            if decision.kind == "wake_only" and decision.destination in MESSAGING_SERVICES:
                emit("status", decision.message)
                if talk_realtime is not None:
                    _clear_realtime_input(talk_realtime)
                # Check the wake's queue before opening the follow-up window.
                incoming = bridge.peek(decision.destination)
                if incoming is not None and (operator is None or operator.approved(incoming)):
                    session.complete_turn()
                    play_next(decision.destination)
                else:
                    mode = bridge.mode(decision.destination)
                    spoken = speak_notice(
                        mode.empty_queue_phrase,
                        preparing="Speaking empty queue; PTT off until speech is ready...",
                        failed="Empty queue confirmation failed",
                        finished="Empty queue confirmation finished; PTT released.",
                    )
                    if spoken == "failed":
                        emit(
                            "status",
                            "The wake phrase was received; the queue notice was not transmitted.",
                        )
                    session.complete_turn()
                emit("status", session.status_line())
            elif decision.kind == "sleep":
                enter_sleep(decision.message)
            elif decision.kind == "wake_only":
                emit("status", decision.message)
                if talk_realtime is not None:
                    _clear_realtime_input(talk_realtime)
                spoken = speak_notice(
                    config.wake_confirmation_phrase,
                    preparing="Speaking wake confirmation; PTT off until speech is ready...",
                    failed="Wake confirmation failed",
                    finished="Wake confirmation finished; PTT released.",
                    realtime_voice=realtime,
                )
                if spoken == "failed":
                    emit("status", "Wake was received; the confirmation was not transmitted.")
                session.complete_turn()
                emit("status", session.status_line())
            elif decision.accepted and decision.destination in MESSAGING_SERVICES:
                emit("accepted", decision.message)
                emit("accepted", f"Traffic: {decision.traffic}")
                session.close()
                if talk_realtime is not None:
                    _clear_realtime_input(talk_realtime)
                if operator is not None:
                    audio = (
                        Wav(utterance.pcm, utterance.rate, utterance.duration)
                        if bridge.mode(decision.destination).send_as_voice
                        else None
                    )
                    operator.add(
                        decision.destination,
                        text=normalize_message(text if audio is not None else decision.traffic),
                        audio=audio,
                    )
                    emit("status", "Outgoing message held for operator approval.")
                    session.complete_turn()
                    continue
                try:
                    if bridge.mode(decision.destination).send_as_voice:
                        bridge.send_voice(
                            decision.destination,
                            Wav(utterance.pcm, utterance.rate, utterance.duration),
                        )
                    else:
                        bridge.send_text(decision.destination, decision.traffic)
                except WalkietalkError as exc:
                    emit("error", str(exc), file=sys.stderr)
                    notice = str(exc)
                    speak_notice(
                        notice,
                        preparing="Speaking send failure; PTT off until speech is ready...",
                        failed="Send failure notice failed",
                        finished="Send failure notice finished; PTT released.",
                    )
                    if once:
                        raise
                    continue
                if not play_next(decision.destination):
                    session.complete_turn()
                emit("status", session.status_line())
            elif decision.accepted:
                emit("accepted", decision.message)
                emit("accepted", f"Traffic: {decision.traffic}")
                # Eligibility is already decided using speech start time. Close the
                # old window while working; failure/interruption must not reopen it.
                session.close()
                if realtime:
                    assert talk_realtime is not None
                    if not _realtime_commit_reply(
                        talk_realtime=talk_realtime,
                        config=config,
                        args=args,
                        session=session,
                        callsigns=callsigns,
                    ):
                        continue
                else:
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
                    if args.transmit and voice is not None:
                        try:
                            emit("status", "Generating speech; PTT off...")
                            speech = radio_wav(
                                voice.synthesize(answer), spoken_seconds, truncate=True
                            )
                        except (WalkietalkError, OSError) as exc:
                            # A completed model reply that was never spoken is not radio history.
                            conversation = new_conversation(open_agent(config))
                            conversation.history = previous_history
                            if once:
                                raise
                            emit("error", f"Speech failed: {exc}", file=sys.stderr)
                            emit("status", "Still listening; say the wake phrase and try again.")
                            continue
                        # Capture has closed before STT. It stays closed throughout synthesis/TX.
                        # Hardware errors stop the loop after cleanup instead of retrying hardware.
                        transmit_with_callsign(speech, config, voice, callsigns)
                    emit("reply", f"Reply: {answer}")
                    session.complete_turn()
                    emit("status", session.status_line())
            else:
                emit("ignored", decision.message)
                if talk_realtime is not None:
                    _clear_realtime_input(talk_realtime)
            if once:
                return

    finally:
        with uninterrupted_cleanup():
            try:
                if bridge is not None:
                    bridge.close()
            finally:
                try:
                    if controls is not None:
                        controls.close()
                finally:
                    try:
                        if talk_realtime is not None:
                            talk_realtime.close()
                    finally:
                        if panel is not None:
                            panel.close()


def _ignore_empty_realtime(session: ListeningSession, talk_realtime, started: float) -> None:
    """Print the empty-transcript line and drop a realtime socket that returned it."""
    emit("ignored", session.decide("", started).message)
    if talk_realtime is not None:
        try:
            talk_realtime.discard_turn()
            emit("warn", "Realtime transcription returned no text; voice session reset.")
        except (WalkietalkError, OSError) as exc:
            emit("error", f"Realtime discard failed; session reset: {exc}", file=sys.stderr)


def _clear_realtime_input(talk_realtime) -> None:
    try:
        talk_realtime.clear_input()
    except (WalkietalkError, OSError) as exc:
        # clear_input resets the failed socket before raising. There is no
        # accepted reply to lose, and the next listen can reconnect safely.
        emit("error", f"Realtime input cleanup failed; session reset: {exc}", file=sys.stderr)


def wait_post_tx_mute(config: Config) -> None:
    mute = config.post_tx_mute_seconds
    if mute <= 0:
        return
    emit("status", f"Post-transmit mute {mute:g}s; PTT released, capture closed.")
    time.sleep(mute)
    emit("status", "Mute ended; listening.")


def acknowledge(
    config: Config,
    voice,
    text: str,
    *,
    preparing: str,
    failed: str,
    finished: str,
    realtime: bool = False,
    transmit: bool = False,
) -> str:
    """Return spoken, silent, or failed for speech generation; transmission errors raise."""
    if not text:
        return "silent"
    if not transmit:
        return "silent"
    if realtime:
        try:
            emit("status", preparing)
            ptt = SerialPTT(config.serial_port, config.line)

            play = StreamingPlayback(config)

            result = speak_text_via_realtime(config, text, ptt, allow_key=True, play_segment=play)
            if result.truncated_by_tx_cap or not result.ptt_actions:
                raise WalkietalkError("Acknowledgement was not transmitted in full")
        except RealtimeHardwareError:
            raise
        except (WalkietalkError, OSError) as exc:
            emit("error", f"{failed}: {exc}", file=sys.stderr)
            return "failed"
        if result.ptt_actions:
            summary = ", ".join(f"{item.kind}:{item.reason}" for item in result.ptt_actions)
            emit("status", f"PTT actions: {summary}")
        emit("status", finished)
        return "spoken"
    if voice is None:
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


def speak_shutdown_confirmation(
    config: Config, voice, *, realtime: bool = False, transmit: bool = False
) -> None:
    """Speak the code confirmation, then stop; transmission errors exit through main."""
    if (
        acknowledge(
            config,
            voice,
            config.shutdown_confirmation_phrase,
            preparing="Speaking shutdown confirmation; PTT off until speech is ready...",
            failed="Shutdown confirmation failed",
            finished="Shutdown confirmation finished; PTT released.",
            realtime=realtime,
            transmit=transmit,
        )
        == "failed"
    ):
        emit("status", "Stopping walkietalk without an on-air confirmation.")


def transmit_with_callsign(speech: Wav, config: Config, voice, callsigns: CallsignSession) -> None:
    """Transmit speech, appending the station ID when it is due. PTT stays in the parent."""
    spoken_seconds = config.max_tx_seconds - config.settle_seconds
    transmissions = (speech,)
    ident = None
    if callsigns.due():
        try:
            emit("status", "Generating station ID; PTT off...")
            ident = radio_wav(station_id_wav(config, voice), spoken_seconds)
            transmissions = identification_transmissions(speech, ident, spoken_seconds)
        except (WalkietalkError, OSError) as exc:
            raise StationIDError(str(exc)) from exc
    mute = True
    try:
        try:
            transmit_speech(transmissions[0], config)
        except PlaybackPreparationError:
            mute = False
            raise
        if len(transmissions) == 2:
            emit("status", "Station ID needs a separate burst; PTT released between bursts.")
            time.sleep(IDENT_GAP_SECONDS)
            try:
                transmit_speech(
                    transmissions[1], config, finished="Station ID finished; PTT released."
                )
            except PTTHardwareError:
                raise
            except (WalkietalkError, OSError) as exc:
                raise StationIDError(str(exc), message_transmitted=True) from exc
        if ident is not None:
            callsigns.mark()
    except PTTHardwareError:
        # Hardware faults stop immediately after PTT cleanup; never resume capture.
        mute = False
        raise
    finally:
        if mute:
            wait_post_tx_mute(config)


def transmit_speech(
    speech: Wav, config: Config, finished: str = "Spoken reply finished; PTT released."
) -> None:
    """Prepare the isolated worker first, then key/play/unkey in the parent."""
    transmission_started = False
    try:
        speech = radio_wav(speech, config.max_tx_seconds - config.settle_seconds)
        with tempfile.TemporaryDirectory(prefix="walkietalk-reply-") as directory:
            path = Path(directory) / "reply.wav"
            write_wav(path, speech)
            playback = Playback(
                path,
                config.output_device,
                config.gain,
                config.max_tx_seconds - config.settle_seconds,
            )
            ptt_failed = False
            transmission_completed = False
            try:
                playback.prepare()
                ptt = SerialPTT(config.serial_port, config.line)

                def action(deadline: float) -> None:
                    time.sleep(min(config.settle_seconds, max(0, deadline - time.monotonic())))
                    playback.play(deadline)

                transmission_started = True
                transmit(ptt, action, config.max_tx_seconds)
                transmission_completed = True
            except PTTHardwareError:
                ptt_failed = True
                raise
            finally:
                with uninterrupted_cleanup():
                    # Preserve a fatal PTT fault even when audio teardown also fails.
                    try:
                        playback.close()  # transmit has already attempted release.
                    except (WalkietalkError, OSError) as exc:
                        if not ptt_failed and not transmission_completed:
                            raise
                        context = "PTT fault" if ptt_failed else "transmission"
                        emit(
                            "error",
                            f"Audio cleanup failed after {context}: {exc}",
                            file=sys.stderr,
                        )
    except PTTHardwareError:
        raise
    except (WalkietalkError, OSError) as exc:
        if not transmission_started:
            raise PlaybackPreparationError(str(exc)) from exc
        raise
    emit("status", finished)


def models_command(args: argparse.Namespace) -> None:
    config = load_config(args.config) if args.config else Config()
    emit("status", f"Speech model: {config.stt_model}")
    path = ensure_model(config.stt_model, download=True)
    size = model_size_bytes(config.stt_model)
    emit("status", f"Ready at {path} ({size / 1_000_000:.0f} MB).")


def voice_agent_check_command(args: argparse.Namespace) -> None:
    """Realtime turn: WAV/capture in, reply WAV out; supervised TX only with flags."""
    if args.wav is not None and args.capture:
        raise WalkietalkError("Use either a WAV file or --capture, not both")
    if args.wav is None and not args.capture:
        raise WalkietalkError(
            "Pass a WAV file to voice-agent-check, or --capture to listen on the AIOC"
        )
    if args.transmit and not args.supervised:
        raise WalkietalkError("voice-agent-check --transmit requires --supervised")
    if (args.capture or args.transmit) and args.config is None:
        raise WalkietalkError("Hardware access requires --config with explicit AIOC devices")
    if args.output.exists():
        raise WalkietalkError("Output file already exists; choose a new --output path")
    config = load_config(args.config) if args.config else Config()
    if config.agent_backend != "grok_realtime":
        raise WalkietalkError(
            "voice-agent-check requires agent.backend: grok_realtime; "
            "it never falls back to stt/agent/tts"
        )
    wait = seconds(args.timeout, "timeout", maximum=600)
    if args.capture:
        emit("meter", "Capturing one utterance for grok_realtime...")
        utterance = capture_from_device(config.input_device, config, wait, log=capture_log)
    else:
        utterance = capture_from_wav(args.wav, config, log=capture_log)
    emit("status", f"Agent: grok_realtime ({config.agent_realtime_model})")
    if not args.supervised:
        emit("status", "Voice-agent check: network request; no PTT or transmission.")
        result = offline_voice_check(config, utterance.pcm, utterance.rate)
        ptt_actions = ()
        truncated = False
    else:
        allow_key = True
        if args.transmit:
            emit("status", "Supervised voice-agent TX: real SerialPTT with --transmit.")
            ptt = SerialPTT(config.serial_port, config.line)

            play = StreamingPlayback(config)

        else:
            emit("status", "Supervised voice-agent dry run: DryPTT; no SerialPTT.")
            ptt = DryPTT()
            play = play_segment_dry
        supervised = supervised_voice_check(
            config,
            utterance.pcm,
            utterance.rate,
            ptt,
            allow_key=allow_key,
            play_segment=play,
        )
        result = supervised
        ptt_actions = supervised.ptt_actions
        truncated = supervised.truncated_by_tx_cap
    if result.input_transcript:
        emit("transcript", f"Heard: {result.input_transcript}")
    if result.output_transcript:
        emit("reply", f"Reply: {result.output_transcript}")
    if result.reply_wav is None:
        raise WalkietalkError(
            "Voice agent returned no spoken audio; nothing written; no stt/agent/tts fallback"
        )
    write_wav(args.output, result.reply_wav)
    if ptt_actions:
        summary = ", ".join(f"{item.kind}:{item.reason}" for item in ptt_actions)
        emit("status", f"PTT actions: {summary}")
    if truncated:
        emit(
            "warn", "TX limit reached; interrupted conversation discarded. Next turn starts fresh."
        )
    if args.supervised and args.transmit:
        tx_note = "SerialPTT used."
    elif args.supervised:
        tx_note = "DryPTT only."
    else:
        tx_note = "No hardware TX."
    emit(
        "status",
        f"Reply WAV: {args.output}; {result.reply_wav.rate} Hz, mono PCM16, "
        f"{result.reply_wav.duration:.3f}s. {tx_note}",
    )


def prompt_edit(current: str) -> str | None:
    """Edit in place at a prompt prefilled with the current words."""
    import readline

    readline.set_startup_hook(lambda: readline.insert_text(current))
    try:
        return input("Edit> ")
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    finally:
        readline.set_startup_hook(None)


def operator_edit(config_path, text: str | None, timeout: float) -> None:
    submitted: str | None = None
    try:
        if text is not None:
            submitted = text
            ok = operator_request(config_path, "edit", timeout=timeout, text=text)
        else:
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                raise WalkietalkError("operator edit needs a terminal; use --text in scripts")
            snapshot = operator_snapshot(config_path)
            item = snapshot["item"]
            if item is None:
                raise WalkietalkError("No message waiting for operator review.")
            blocked = item.get("edit_block")
            if blocked:
                raise WalkietalkError(blocked)
            emit(
                "status",
                f"Item {item['number']}: {item['direction']} {item['service']}, "
                f"{item['alias']}, {item['kind']}.",
            )
            current = item["text"] if item["kind"] == "text" else item["transcript"]
            edited = prompt_edit(current)
            if edited is None or normalized_change(edited, current) is None:
                emit("status", "Edit cancelled; message unchanged.")
                return
            submitted = edited
            ok = operator_request(
                config_path,
                "edit",
                timeout=timeout,
                revision=snapshot["revision"],
                text=edited,
                show_snapshot=False,
            )
    except WalkietalkError:
        if submitted is not None:
            print(f"Unsent edit: {submitted}", file=sys.stderr)
        raise
    if not ok:
        if submitted is not None:
            print(f"Unsent edit: {submitted}", file=sys.stderr)
        raise WalkietalkError("Operator edit did not complete; see above")


def run(args: argparse.Namespace) -> None:
    if args.command == "operator":
        timeout = seconds(args.timeout, "--timeout", maximum=600)
        if args.text is not None and args.action != "edit":
            raise WalkietalkError("--text applies only to operator edit")
        if args.action == "edit":
            if args.approved:
                raise WalkietalkError("Approved messages can't be edited; deny to drop it")
            operator_edit(args.config, args.text, timeout)
            return
        options = {"approved": True} if args.approved else {}
        if not operator_request(args.config, args.action, timeout=timeout, **options):
            raise WalkietalkError(f"Operator {args.action} did not complete; see above")
        return
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
            f"Agent: {config.agent_backend}; STT: {config.stt_backend}; TTS: {config.tts_backend}"
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
    if args.command == "voice-agent-check":
        voice_agent_check_command(args)
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
        if config.agent_backend == "grok_realtime":
            voice_agent = open_voice_agent(config)
            assert voice_agent is not None
            key_status = "set" if os.environ.get(config.agent_realtime_api_key_env) else "missing"
            print(f"Voice agent: {voice_agent.label()}; API key {key_status}", flush=True)
            print(
                "talk uses native audio and transcripts; separate STT/TTS are unused.", flush=True
            )
        else:
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
        if config.sleep_primary:
            print(
                f"Sleep: {' / '.join((config.sleep_primary, *config.sleep_aliases))}; "
                f"confirmation {config.sleep_confirmation_phrase!r} with talk --transmit",
                flush=True,
            )
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
                f"Station ID: {config.callsign!r}; mode {config.callsign_mode}; "
                f"method {config.callsign_method}",
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
        elif args.command in {
            "models",
            "agent-check",
            "tts-check",
            "voice-agent-check",
            "operator",
        }:
            emit("warn", "Stopped.", file=sys.stderr)
        else:
            emit("warn", "Stopped; PTT cleanup attempted.", file=sys.stderr)
        return 130
    except (WalkietalkError, OSError) as exc:
        emit("error", f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
