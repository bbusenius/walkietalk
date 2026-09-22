import asyncio
import json
import time
from dataclasses import replace

import httpx
import pytest

from walkietalk import cli
from walkietalk.agent import AgentSession, open_agent
from walkietalk.claude_api import MAX_RESPONSE_BYTES, MESSAGES_URL, WEB_SEARCH_TOOL, ClaudeApiAgent
from walkietalk.config import Config, WalkietalkError

SECRET = "fake-test-key-never-log"


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    return replace(Config(), agent_backend="claude_api", agent_timeout_seconds=0.05)


@pytest.fixture(autouse=True)
def no_other_backend_or_hardware(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Claude API touched another backend or radio hardware")

    for name in ("SerialPTT", "DryPTT", "Playback", "transmit", "preflight", "open_stt"):
        monkeypatch.setattr(cli, name, forbidden)
    for module, name in (
        ("agent", "StubAgent"),
        ("hermes", "HermesAgent"),
        ("codex", "CodexAgent"),
        ("grok_agent", "GrokAgent"),
        ("claude", "ClaudeCodeAgent"),
    ):
        monkeypatch.setattr(f"walkietalk.{module}.{name}", forbidden)


def install_transport(monkeypatch, handler):
    original = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert kwargs["timeout"] is None
        return original(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr("walkietalk.claude_api.httpx.AsyncClient", client)


def answer(text="Rain falls from clouds.", **changes):
    return {
        "type": "message",
        "role": "assistant",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
        **changes,
    }


def test_messages_system_final_only_bounded_history_and_cli(monkeypatch, config, capsys):
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == MESSAGES_URL and request.method == "POST"
        assert request.headers["x-api-key"] == SECRET
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert request.headers["content-type"] == "application/json"
        return httpx.Response(
            200,
            json=answer(
                content=[
                    {"type": "thinking", "thinking": "private reasoning"},
                    {"type": "redacted_thinking", "data": "hidden"},
                    {"type": "text", "text": "Rain falls from clouds."},
                ]
            ),
        )

    install_transport(monkeypatch, handler)
    config = replace(config, agent_history_turns=1)
    agent = open_agent(config)
    assert isinstance(agent, ClaudeApiAgent)
    assert "ANTHROPIC_API_KEY" in agent.label() and "billed API" in agent.label()
    session = AgentSession(config, agent)
    for traffic in ("What is rain?", "Why?", "Again?"):
        assert session.reply(traffic) == "Rain falls from clouds."
    payloads = [json.loads(r.content) for r in requests]
    assert payloads[0]["messages"] == [{"role": "user", "content": "What is rain?"}]
    assert payloads[2]["messages"] == [
        {"role": "user", "content": "Why?"},
        {"role": "assistant", "content": "Rain falls from clouds."},
        {"role": "user", "content": "Again?"},
    ]
    assert "spoken-style" in payloads[0]["system"]
    assert "600 characters" in payloads[0]["system"]
    assert payloads[0]["model"] == config.claude_api_model
    assert payloads[0]["output_config"] == {"effort": "low"}
    assert 600 <= payloads[0]["max_tokens"] <= 4000
    assert "tools" not in payloads[0]
    assert SECRET not in json.dumps(payloads)
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    assert cli.main(["-c", "unused", "agent-check", "Hello"]) == 0
    assert "Reply: Rain falls from clouds." in capsys.readouterr().out


def test_web_search_tool_is_sent_only_when_enabled(monkeypatch, config):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json=answer(
                content=[
                    {"type": "server_tool_use", "name": "web_search", "input": {"query": "rain"}},
                    {
                        "type": "web_search_tool_result",
                        "content": [{"type": "web_search_result", "url": "https://secret.example"}],
                    },
                    {"type": "text", "text": "Rain falls from clouds."},
                ]
            ),
        )

    install_transport(monkeypatch, handler)
    config = replace(config, agent_web_search=True)
    assert AgentSession(config, open_agent(config)).reply("weather") == "Rain falls from clouds."
    payload = json.loads(requests[0].content)
    assert payload["tools"] == [WEB_SEARCH_TOOL]
    assert "https://secret.example" not in payload["system"]


def test_web_search_rejects_other_api_tools(monkeypatch, config):
    def handler(request):
        return httpx.Response(
            200,
            json=answer(content=[{"type": "tool_use", "name": "bash", "input": {}}]),
        )

    install_transport(monkeypatch, handler)
    config = replace(config, agent_web_search=True)
    with pytest.raises(WalkietalkError, match="unexpected content"):
        AgentSession(config, open_agent(config)).reply("weather")


@pytest.mark.parametrize("key", [None, "", " ", "secret\nheader", "sëcret"])
def test_missing_or_invalid_key_never_contacts_api(monkeypatch, config, key):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    if key is not None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    monkeypatch.setenv("XAI_API_KEY", "unrelated")
    install_transport(monkeypatch, lambda request: pytest.fail("HTTP called without key"))
    with pytest.raises(WalkietalkError, match="billed Anthropic API"):
        AgentSession(config, open_agent(config)).reply("hello")


def test_custom_key_name_and_default_effort(monkeypatch, config):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("RADIO_CLAUDE_KEY", SECRET)

    def handler(request):
        assert request.headers["x-api-key"] == SECRET
        assert "output_config" not in json.loads(request.content)
        return httpx.Response(200, json=answer())

    install_transport(monkeypatch, handler)
    config = replace(
        config, claude_api_key_env="RADIO_CLAUDE_KEY", claude_api_reasoning_effort="default"
    )
    assert AgentSession(config, open_agent(config)).reply("hello")


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 429, 500, 529])
def test_http_errors_no_body_disclosure_retries_redirects_or_history(monkeypatch, config, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, text=SECRET, headers={"Location": "https://example.org"})

    install_transport(monkeypatch, handler)
    session = AgentSession(config, open_agent(config))
    with pytest.raises(WalkietalkError, match=f"HTTP {status}") as error:
        session.reply("hello")
    assert SECRET not in str(error.value) and session.history == () and len(requests) == 1
    if status in (401, 403):
        assert "billed Anthropic API" in str(error.value)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        answer(""),
        answer("x" * 601),
        answer("\x1b[31mred"),
        answer(content=[]),
        answer(content=[{}]),
        answer(content=["oops"]),
        answer(content=[{"type": "tool_use", "input": SECRET}]),
        answer(content=[{"type": "text", "text": 42}]),
        answer(content=[{"type": "thinking", "thinking": SECRET}]),
        answer(stop_reason="max_tokens"),
        answer(stop_reason="tool_use"),
        answer(role="user"),
    ],
)
def test_invalid_answers_never_retained(monkeypatch, config, payload):
    install_transport(monkeypatch, lambda request: httpx.Response(200, json=payload))
    session = AgentSession(config, open_agent(config))
    with pytest.raises(WalkietalkError) as error:
        session.reply("hello")
    assert session.history == () and SECRET not in str(error.value)


@pytest.mark.parametrize("body", [b"not JSON", b"\xff", b" " * (MAX_RESPONSE_BYTES + 1)])
def test_bad_transport_data(monkeypatch, config, body):
    install_transport(monkeypatch, lambda request: httpx.Response(200, content=body))
    with pytest.raises(WalkietalkError):
        AgentSession(config, open_agent(config)).reply("hello")


@pytest.mark.parametrize("connection_failure", [True, False])
def test_connection_and_header_timeout(monkeypatch, config, connection_failure):
    async def handler(request):
        if connection_failure:
            raise httpx.ConnectError(SECRET)
        await asyncio.sleep(10)
        return httpx.Response(200, json=answer("late answer"))

    install_transport(monkeypatch, handler)
    session = AgentSession(config, open_agent(config))
    start = time.monotonic()
    with pytest.raises(WalkietalkError) as error:
        session.reply("hello")
    assert time.monotonic() - start < 1
    assert session.history == () and SECRET not in str(error.value)


def test_whole_body_deadline_closes_dripping_response(monkeypatch, config):
    class DrippingBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            while True:
                yield b" "
                await asyncio.sleep(0.01)

        async def aclose(self):
            self.closed = True

    stream = DrippingBody()
    install_transport(monkeypatch, lambda request: httpx.Response(200, stream=stream))
    session = AgentSession(config, open_agent(config))
    with pytest.raises(WalkietalkError, match="timed out"):
        session.reply("hello")
    assert stream.closed and session.history == ()
