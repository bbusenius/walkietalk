"""Exercise continuous recovery and two-step shutdown without network or hardware."""

from dataclasses import replace
from unittest.mock import Mock, patch

from walkietalk import cli
from walkietalk.capture import Utterance
from walkietalk.config import Config, WalkietalkError
from walkietalk.shutdown import ShutdownSession
from walkietalk.wake import ListeningSession


def main() -> None:
    config = replace(
        Config(),
        listening_mode="conversation",
        conversation_timeout_seconds=10,
        wake_primary="bridge",
        shutdown_enabled=True,
        shutdown_phrase="stop listening",
        shutdown_code="confirm alpha nine",
    )
    now = [100.0]
    gate = ListeningSession(config, clock=lambda: now[0])
    shutdown = ShutdownSession(config, clock=lambda: now[0])
    traffic = [
        "bridge what is rain",
        "why is that",
        "bridge try again",
        "bridge retry",
        WalkietalkError("simulated transcription failure"),
        "stop listening",
        "incorrect code",
        "confirm alpha nine",
        "stop listening",
        "confirm alpha nine",
        "stop listening",
        "confirm alpha nine",
    ]
    listener = Mock()
    listener.label.return_value = "simulated STT"
    listener.transcribe.side_effect = traffic
    agent = Mock()
    agent.label.return_value = "simulated agent"
    agent.reply.side_effect = [
        "Rain falls from clouds.",
        WalkietalkError("simulated agent timeout"),
        "Ready again.",
    ]
    captures = []

    def capture(device, cfg, wait, **kwargs):
        index = len(captures)
        if index >= len(traffic):
            raise AssertionError("Expected remote shutdown")
        assert wait is None, "Continuous capture acquired an idle deadline"
        captures.append(index)
        if index == 1:
            print("\nSimulating two hours of silence, then an unaddressed follow-up.", flush=True)
            now[0] += 7200
        elif index == 9:
            print("\nSimulating expiry of the shutdown confirmation window.", flush=True)
            now[0] += 31
        else:
            now[0] += 1
        kwargs["on_wait"]()
        return Utterance(b"\x00\x40" * 320, 16000, 0.5, 0.5, 1, "silence", now[0])

    def forbidden(*args, **kwargs):
        raise AssertionError("Simulation attempted hardware access")

    print("Controlled simulation: no account, network, radio, audio, or real waiting.", flush=True)
    with (
        patch.object(cli, "load_config", return_value=config),
        patch.object(cli, "open_stt", return_value=listener),
        patch.object(cli, "open_agent", return_value=agent),
        patch.object(cli, "capture_from_device", capture),
        patch.object(cli, "preflight", return_value=None),
        patch.object(cli, "ListeningSession", return_value=gate),
        patch.object(cli, "ShutdownSession", return_value=shutdown),
        patch.object(cli, "SerialPTT", forbidden),
        patch.object(cli, "DryPTT", forbidden),
        patch.object(cli, "Playback", forbidden),
        patch.object(cli, "transmit", forbidden),
    ):
        result = cli.main(["--no-env-file", "-c", "simulated-config", "talk", "--capture"])
    assert result == 0 and len(captures) == len(traffic)
    assert [call.args[0] for call in agent.reply.call_args_list] == [
        "what is rain",
        "try again",
        "retry",
    ]
    print(
        "PASS: silence and failures recovered; only the armed, timely code stopped the program.",
        flush=True,
    )


if __name__ == "__main__":
    main()
