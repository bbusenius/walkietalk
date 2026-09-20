"""Controlled Codex failures with a fake CLI; no account, network, or hardware.

Run: .venv/bin/python scripts/demo_codex_failures.py
Uses the real bridge CLI, Codex adapter, and subprocess supervisor.
"""

import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from walkietalk import cli
from walkietalk.config import Config

FAKE_CLI = """
import json, sys, time
from pathlib import Path
scenario = SCENARIO
if sys.argv[1:3] == ['login', 'status']:
    print('Not logged in' if scenario == 'login' else 'Logged in using ChatGPT')
    sys.exit(1 if scenario == 'login' else 0)
sys.stdin.read()
if scenario == 'timeout':
    time.sleep(30)
Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('A' * 601)
print(json.dumps({'type': 'thread.started',
                  'thread_id': '11111111-1111-4111-8111-111111111111'}))
print(json.dumps({'type': 'turn.completed'}))
"""


def forbidden(*args, **kwargs):
    raise AssertionError("Failure demonstration attempted hardware access")


def main() -> None:
    print("Controlled fake CLI: no account, network, radio capture, or transmission.", flush=True)
    with tempfile.TemporaryDirectory(prefix="walkietalk-demo-") as directory:
        executable = Path(directory) / "fake-codex"
        for scenario in ("oversized", "timeout", "login", "missing CLI"):
            executable.write_text(
                f"#!{sys.executable}\n" + FAKE_CLI.replace("SCENARIO", repr(scenario))
            )
            executable.chmod(0o700)
            config = replace(
                Config(),
                agent_backend="codex",
                codex_executable=str(
                    executable if scenario != "missing CLI" else executable / "missing"
                ),
                agent_timeout_seconds=0.5,
            )
            print(f"\nSimulated {scenario} failure:", flush=True)
            with (
                patch.object(cli, "load_config", return_value=config),
                patch.object(cli, "SerialPTT", forbidden),
                patch.object(cli, "DryPTT", forbidden),
                patch.object(cli, "Playback", forbidden),
                patch.object(cli, "transmit", forbidden),
                patch.object(cli, "preflight", forbidden),
                patch.object(cli, "open_stt", forbidden),
            ):
                result = cli.main(["-c", "simulated-config", "agent-check", "What is rain?"])
            if result != 1:
                raise AssertionError("Expected a local error")
            print("Expected error received; no answer printed and no PTT opened.", flush=True)


if __name__ == "__main__":
    main()
