import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import yaml

from walkietalk import cli
from walkietalk.agent import AgentSession, open_agent
from walkietalk.config import Config, WalkietalkError, load_config
from walkietalk.hermes import MAX_RESPONSE_BYTES, HermesAgent

FEATURES = {"run_submission": True, "run_status": True, "run_stop": True}
SECRET = "test-bearer-secret"


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("WALKIETALK_HERMES_TOKEN", SECRET)
    return replace(Config(), agent_backend="hermes", agent_timeout_seconds=0.1)


def install_transport(monkeypatch, handler):
    original = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr("walkietalk.hermes.httpx.AsyncClient", client)


def mock_service(monkeypatch, *, status="completed", output="Rain falls from clouds."):
    requests = []

    async def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, json={"features": FEATURES})
        if request.url.path.endswith("/stop"):
            return httpx.Response(200, json={"status": "stopping"})
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "run_test"})
        return httpx.Response(
            200,
            json={
                "run_id": "run_test",
                "status": status,
                "output": output,
                "error": SECRET,
                "tool_output": SECRET,
            },
        )

    install_transport(monkeypatch, handler)
    return requests


def test_final_text_only_and_bounded_history_shared_radio_session(monkeypatch, config):
    requests = mock_service(monkeypatch)
    agent = open_agent(config)
    assert isinstance(agent, HermesAgent)
    session = AgentSession(config, agent)
    assert session.reply("What is rain?") == "Rain falls from clouds."
    assert session.reply("Why does that happen?") == "Rain falls from clouds."
    posts = [json.loads(request.content) for request in requests if request.method == "POST"]
    assert len(posts) == 2
    assert posts[0]["conversation_history"] == []
    assert posts[1]["conversation_history"] == [
        {"role": "user", "content": "What is rain?"},
        {"role": "assistant", "content": "Rain falls from clouds."},
    ]
    assert posts[0]["session_id"] == posts[1]["session_id"]
    assert posts[0]["session_id"].startswith("walkietalk-")
    assert "spoken-style" in posts[0]["instructions"]
    assert "Do not search the web." in posts[0]["instructions"]
    assert "provider" not in posts[0] and "model" not in posts[0]


def test_web_search_instruction_allows_a_lookup_only(monkeypatch, config):
    requests = mock_service(monkeypatch)
    config = replace(config, agent_web_search=True)
    assert AgentSession(config, open_agent(config)).reply("weather") == "Rain falls from clouds."
    posts = [json.loads(request.content) for request in requests if request.method == "POST"]
    instructions = posts[0]["instructions"]
    assert "public web" in instructions
    assert "Do not run commands" in instructions
    assert "Do not search the web." not in instructions


def test_missing_token_never_contacts_service(monkeypatch, config):
    monkeypatch.delenv(config.hermes_token_env)
    monkeypatch.setenv("XAI_API_KEY", "must-not-be-used")
    requests = mock_service(monkeypatch)
    with pytest.raises(WalkietalkError, match="environment variable"):
        AgentSession(config, open_agent(config)).reply("hello")
    assert requests == []


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
def test_http_errors_and_redirects_never_expose_body_or_credentials(monkeypatch, config, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, text=SECRET, headers={"Location": "https://example.org"})

    install_transport(monkeypatch, handler)
    with pytest.raises(WalkietalkError) as error:
        AgentSession(config, open_agent(config)).reply("hello")
    assert SECRET not in str(error.value)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "status", ["failed", "cancelled", "interrupted", "waiting_for_approval", "unknown"]
)
def test_failed_and_approval_runs_never_return_output(monkeypatch, config, status):
    requests = mock_service(monkeypatch, status=status, output=SECRET)
    session = AgentSession(config, open_agent(config))
    with pytest.raises(WalkietalkError) as error:
        session.reply("hello")
    assert SECRET not in str(error.value)
    assert session.history == ()
    assert requests[-1].url.path.endswith("/stop") == (
        status in {"waiting_for_approval", "unknown"}
    )
    assert all(not request.url.path.endswith("/approval") for request in requests)


def test_overall_timeout_stops_known_run_and_discards_late_reply(monkeypatch, config):
    requests = mock_service(monkeypatch, status="running")
    start = time.monotonic()
    session = AgentSession(config, open_agent(config))
    with pytest.raises(WalkietalkError, match="timed out"):
        session.reply("hello")
    assert time.monotonic() - start < 1
    assert requests[-1].url.path.endswith("/stop")
    assert session.history == ()


def test_cancellation_stops_active_run(monkeypatch, config):
    requests = mock_service(monkeypatch, status="running")
    agent = open_agent(replace(config, agent_timeout_seconds=30))

    async def run():
        from walkietalk.agent import SessionContext

        task = asyncio.create_task(
            agent._reply("hello", SessionContext("test", "short", ()), SECRET)
        )
        while not any(request.url.path.endswith("/run_test") for request in requests):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert requests[-1].url.path.endswith("/stop")


class DrippingBody(httpx.AsyncByteStream):
    closed = False

    async def __aiter__(self):
        while True:
            yield b" "
            await asyncio.sleep(0.01)

    async def aclose(self):
        self.closed = True


def test_total_deadline_includes_dripping_response_body(monkeypatch, config):
    stream = DrippingBody()
    install_transport(monkeypatch, lambda request: httpx.Response(200, stream=stream))
    start = time.monotonic()
    with pytest.raises(WalkietalkError, match="timed out"):
        AgentSession(config, open_agent(config)).reply("hello")
    assert time.monotonic() - start < 1
    assert stream.closed


@pytest.mark.parametrize("content", [b"not JSON", b"[]", b"x" * (MAX_RESPONSE_BYTES + 1)])
def test_bad_transport_response_rejected(monkeypatch, config, content):
    install_transport(monkeypatch, lambda request: httpx.Response(200, content=content))
    with pytest.raises(WalkietalkError):
        AgentSession(config, open_agent(config)).reply("hello")


@pytest.mark.parametrize("answer", ["", None, {}, "x" * 601])
def test_invalid_final_answer_not_saved(monkeypatch, config, answer):
    mock_service(monkeypatch, output=answer)
    session = AgentSession(config, open_agent(config))
    with pytest.raises(WalkietalkError):
        session.reply("hello")
    assert session.history == ()


def test_capability_check_prevents_unsupported_submission(monkeypatch, config):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"features": {}})

    install_transport(monkeypatch, handler)
    with pytest.raises(WalkietalkError, match="must support"):
        AgentSession(config, open_agent(config)).reply("hello")
    assert len(requests) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("timeout_seconds", 0),
        ("timeout_seconds", 301),
        ("timeout_seconds", float("inf")),
        ("hermes_url", "http://user:secret@example.org"),
        ("hermes_url", "file:///tmp/api"),
        ("hermes_url", "http://localhost:bad"),
        ("hermes_url", "http://localhost/?token=secret"),
        ("hermes_token_env", "a secret"),
        ("hermes_token_env", ""),
    ],
)
def test_invalid_hermes_configuration_rejected(tmp_path, field, value):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["agent"][field] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WalkietalkError):
        load_config(path)


def test_hermes_check_never_opens_radio_or_stt(monkeypatch, config, capsys):
    mock_service(monkeypatch)

    def forbidden(*args, **kwargs):
        pytest.fail("Hermes agent check touched radio/STT")

    for name in ("SerialPTT", "Playback", "preflight", "open_stt", "transmit"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    assert cli.main(["-c", "unused", "agent-check", "What is rain?"]) == 0
    assert "Reply: Rain falls from clouds." in capsys.readouterr().out


def test_connection_error_does_not_expose_details(monkeypatch, config):
    def handler(request):
        raise httpx.ConnectError(SECRET)

    install_transport(monkeypatch, handler)
    with pytest.raises(WalkietalkError, match="connection failed") as error:
        AgentSession(config, open_agent(config)).reply("hello")
    assert SECRET not in str(error.value)


def test_unacknowledged_submission_warns_instead_of_claiming_cancellation(
    monkeypatch, config, capsys
):
    def handler(request):
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, json={"features": FEATURES})
        raise httpx.ReadError(SECRET)

    install_transport(monkeypatch, handler)
    with pytest.raises(WalkietalkError, match="connection failed"):
        AgentSession(config, open_agent(config)).reply("hello")
    output = capsys.readouterr().err
    assert "server work may still be running" in output
    assert SECRET not in output


def test_stop_failure_is_reported_safely(monkeypatch, config, capsys):
    async def handler(request):
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, json={"features": FEATURES})
        if request.url.path.endswith("/stop"):
            return httpx.Response(500, text=SECRET)
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "run_test"})
        return httpx.Response(200, json={"run_id": "run_test", "status": "running"})

    install_transport(monkeypatch, handler)
    with pytest.raises(WalkietalkError, match="timed out"):
        AgentSession(config, open_agent(config)).reply("hello")
    output = capsys.readouterr().err
    assert "stop could not be confirmed" in output
    assert SECRET not in output


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_process_shutdown_stops_hermes_run(sig):
    code = """
import asyncio, os, signal, sys
from dataclasses import replace
from unittest.mock import patch
import httpx
from walkietalk import cli
from walkietalk.config import Config
fired = False
async def service(request):
    global fired
    if request.url.path.endswith('/capabilities'):
        features = dict(run_submission=True, run_status=True, run_stop=True)
        return httpx.Response(200, json={'features': features})
    if request.url.path.endswith('/stop'):
        print('STOP REQUESTED', flush=True)
        return httpx.Response(200, json={'status':'stopping'})
    if request.method == 'POST':
        return httpx.Response(202, json={'run_id':'run_test'})
    if not fired:
        fired = True
        asyncio.get_running_loop().call_later(0.01, os.kill, os.getpid(), int(sys.argv[1]))
    return httpx.Response(200, json={'run_id':'run_test','status':'running'})
original = httpx.AsyncClient
def client(**kwargs):
    return original(**kwargs, transport=httpx.MockTransport(service))
config = replace(Config(), agent_backend='hermes')
with (
    patch('walkietalk.hermes.httpx.AsyncClient', client),
    patch.object(cli, 'load_config', return_value=config),
):
    sys.exit(cli.main(['-c','unused','agent-check','hello']))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(sig)],
        env=dict(os.environ, WALKIETALK_HERMES_TOKEN=SECRET),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 130, result.stderr
    assert "STOP REQUESTED" in result.stdout
    assert "Reply:" not in result.stdout
    assert SECRET not in result.stderr


def test_failure_demo_script():
    result = subprocess.run(
        [sys.executable, "scripts/demo_agent_failures.py"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("Expected error received") == 3
    assert "Reply:" not in result.stdout
    assert "Private diagnostic" not in result.stdout + result.stderr
    assert "stop request" in result.stdout
