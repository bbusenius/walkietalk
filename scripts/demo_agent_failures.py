"""Controlled Hermes failures for the family demo; no network or hardware.

Run from the project root: .venv/bin/python scripts/demo_agent_failures.py
The real CLI and Hermes adapter run against an in-process HTTP transport fake.
Real login, config, and service state are never changed.
"""

from dataclasses import replace
from unittest.mock import patch

import httpx

from walkietalk import cli
from walkietalk.config import Config


def demonstrate(scenario: str) -> None:
    stop_requested = []

    async def service(request):
        path = request.url.path
        if scenario == "login":
            return httpx.Response(401, text="Private diagnostic: never print this")
        if path.endswith("/capabilities"):
            return httpx.Response(
                200,
                json={
                    "features": {
                        "run_submission": True,
                        "run_status": True,
                        "run_stop": True,
                    }
                },
            )
        if path.endswith("/stop"):
            stop_requested.append(True)
            return httpx.Response(200, json={"status": "stopping"})
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "run_demo"})
        return httpx.Response(
            200,
            json={
                "run_id": "run_demo",
                "status": "running" if scenario == "timeout" else "completed",
                "output": "A" * 601,
            },
        )

    original_client = httpx.AsyncClient

    def client(**kwargs):
        return original_client(**kwargs, transport=httpx.MockTransport(service))

    def forbidden(*args, **kwargs):
        raise AssertionError("Failure demonstration attempted hardware access")

    config = replace(Config(), agent_backend="hermes", agent_timeout_seconds=0.2)
    print(f"\nSimulated {scenario} failure:", flush=True)
    with (
        patch("walkietalk.hermes.httpx.AsyncClient", client),
        patch.dict("os.environ", {config.hermes_token_env: "fake-local-token"}),
        patch.object(cli, "load_config", return_value=config),
        patch.object(cli, "SerialPTT", forbidden),
        patch.object(cli, "Playback", forbidden),
        patch.object(cli, "preflight", forbidden),
        patch.object(cli, "open_stt", forbidden),
    ):
        result = cli.main(["-c", "simulated-config", "agent-check", "What is rain?"])
    if result != 1 or (scenario == "timeout" and not stop_requested):
        raise AssertionError("Expected a local error and stop request on timeout")
    print("Expected error received; no answer printed and no PTT opened.", flush=True)
    if stop_requested:
        print("The simulated service received the stop request.", flush=True)


def main() -> None:
    print("Controlled simulation: no network, radio capture, or transmission.", flush=True)
    for scenario in ("oversized", "timeout", "login"):
        demonstrate(scenario)


if __name__ == "__main__":
    main()
