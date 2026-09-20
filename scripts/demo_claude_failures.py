"""Offline Claude failure demonstration; no real account, network, STT, or radio.

Run: .venv/bin/python scripts/demo_claude_failures.py
"""

import asyncio
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from walkietalk import cli
from walkietalk.claude import REQUIRED_FLAGS
from walkietalk.config import Config

FAKE_CLI = """
import json, sys, time
scenario = SCENARIO
if '--help' in sys.argv:
    print(FLAGS)
    sys.exit(0)
if 'auth' in sys.argv:
    print(json.dumps({'loggedIn': scenario != 'login',
                     'authMethod': 'claude.ai', 'apiProvider': 'firstParty'}))
    sys.exit(0)
sys.stdin.read()
if scenario == 'timeout': time.sleep(30)
session = sys.argv[sys.argv.index('--session-id') + 1]
model = sys.argv[sys.argv.index('--model') + 1]
print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': session,
                 'model': model, 'tools': [], 'mcp_servers': []}))
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                 'session_id': session, 'result': 'A' * 601}))
"""


def forbidden(*args, **kwargs):
    raise AssertionError("Failure demonstration attempted hardware, STT, or fallback")


def show_failure(config, scenario):
    print(f"\n{config.agent_backend}: simulated {scenario}", flush=True)
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


def main():
    print("Controlled fakes: no account, network, STT, or radio access.", flush=True)
    with tempfile.TemporaryDirectory(prefix="walkietalk-demo-claude-") as directory:
        executable = Path(directory) / "fake-claude"
        for scenario in ("oversized", "timeout", "login", "missing CLI"):
            executable.write_text(
                f"#!{sys.executable}\n"
                + FAKE_CLI.replace("SCENARIO", repr(scenario)).replace(
                    "FLAGS", repr(" ".join(REQUIRED_FLAGS))
                )
            )
            executable.chmod(0o700)
            config = replace(
                Config(),
                agent_backend="claude",
                agent_timeout_seconds=0.5,
                claude_executable=str(
                    executable if scenario != "missing CLI" else Path(directory) / "missing"
                ),
            )
            show_failure(config, scenario)

    original_client = httpx.AsyncClient
    for scenario in ("missing key", "oversized", "timeout", "HTTP 401", "HTTP 500"):

        async def handler(request, scenario=scenario):
            if scenario == "timeout":
                await asyncio.sleep(30)
            if scenario.startswith("HTTP"):
                return httpx.Response(int(scenario.split()[1]), text="withheld fake diagnostic")
            return httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "A" * 601}],
                },
            )

        def client(**kwargs):
            return original_client(**kwargs, transport=httpx.MockTransport(handler))

        config = replace(Config(), agent_backend="claude_api", agent_timeout_seconds=0.1)
        with (
            patch.dict(
                "os.environ", {"ANTHROPIC_API_KEY": "" if scenario == "missing key" else "fake"}
            ),
            patch("walkietalk.claude_api.httpx.AsyncClient", client),
        ):
            show_failure(config, scenario)


if __name__ == "__main__":
    main()
