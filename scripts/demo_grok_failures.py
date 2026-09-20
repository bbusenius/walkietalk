"""Controlled Grok Build failures; no account, network, STT, or hardware.

Run: .venv/bin/python scripts/demo_grok_failures.py
The actual CLI adapter and subprocess supervisor run against a temporary fake CLI.
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
if sys.argv[1:3] == ['inspect', '--json']:
    print(json.dumps({'loginPolicy': {'apiKeyAuthDisabled': True}, 'hooks': [],
        'mcpServers': [], 'plugins': [], 'lspServers': [], 'configSources': {'layers': []}}))
    sys.exit(0)
sys.stdin.read()
if scenario == 'timeout': time.sleep(30)
if scenario == 'login':
    print(json.dumps({'type': 'error', 'message': 'simulated expired login'}))
    sys.exit(1)
thread = sys.argv[sys.argv.index('--session-id') + 1]
print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': thread,
    'apiKeySource': 'oauth', 'permissionMode': 'dontAsk', 'mcp_servers': []}))
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
    'stop_reason': 'end_turn', 'session_id': thread, 'result': 'A' * 601}))
"""


def forbidden(*args, **kwargs):
    raise AssertionError("Failure demonstration attempted hardware access")


def main() -> None:
    print(
        "Controlled fake CLI: no account, network, STT, radio capture, or transmission.", flush=True
    )
    with tempfile.TemporaryDirectory(prefix="walkietalk-demo-") as directory:
        home = Path(directory) / "grok-home"
        home.mkdir()
        (home / "auth.json").write_text("Fake CLI-owned login, never parsed by the adapter")
        executable = Path(directory) / "fake-grok"
        for scenario in ("oversized", "timeout", "login", "missing CLI"):
            executable.write_text(
                f"#!{sys.executable}\n" + FAKE_CLI.replace("SCENARIO", repr(scenario))
            )
            executable.chmod(0o700)
            config = replace(
                Config(),
                agent_backend="grok",
                agent_timeout_seconds=0.5,
                grok_executable=str(
                    executable if scenario != "missing CLI" else Path(directory) / "missing"
                ),
            )
            print(f"\nSimulated {scenario} failure:", flush=True)
            with (
                patch.dict("os.environ", {"GROK_HOME": str(home)}),
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
