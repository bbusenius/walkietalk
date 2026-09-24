"""Phase 1: Grok realtime schema + fake-transport coverage (no live network)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
import yaml

from walkietalk.config import Config, WalkietalkError, load_config
from walkietalk.grok_realtime import (
    FUNCTION_CALL_ARGUMENTS_DONE,
    OUTPUT_AUDIO_DELTA,
    OUTPUT_AUDIO_DONE,
    GrokRealtimeClient,
    open_voice_agent,
)


@dataclass
class FakeTransport:
    """Injectable WebSocket stand-in; records outbound JSON and yields scripted replies."""

    incoming: list[str | bytes | BaseException] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)
    closed: bool = False
    headers: dict[str, str] | None = None
    url: str | None = None

    async def send(self, data: str) -> None:
        if self.closed:
            raise RuntimeError("transport closed")
        self.sent.append(data)

    async def recv(self) -> str | bytes:
        if self.closed:
            raise RuntimeError("transport closed")
        if not self.incoming:
            raise ConnectionError("fake transport closed")
        item = self.incoming.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


def event(type_: str, **extra) -> str:
    return json.dumps({"type": type_, **extra})


@pytest.fixture
def config_data():
    return yaml.safe_load(Path("config.example.yaml").read_text())


def write_config(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def realtime_config(**overrides) -> Config:
    base = replace(
        Config(),
        voice_agent_backend="grok_realtime",
        voice_agent_model="grok-voice-latest",
        voice_agent_voice="eve",
        voice_agent_api_key_env="XAI_API_KEY",
        voice_agent_websocket_url="wss://api.x.ai/v1/realtime",
        voice_agent_connect_timeout_seconds=2,
        voice_agent_idle_timeout_seconds=2,
    )
    return replace(base, **overrides) if overrides else base


def test_example_config_loads_voice_agent_off(tmp_path, config_data):
    config = load_config(write_config(tmp_path, config_data))
    assert config.voice_agent_backend == "off"
    assert config.voice_agent_model == "grok-voice-latest"
    assert config.voice_agent_voice == "eve"
    assert config.voice_agent_api_key_env == "XAI_API_KEY"
    assert config.voice_agent_websocket_url == "wss://api.x.ai/v1/realtime"
    assert config.voice_agent_connect_timeout_seconds == 10
    assert config.voice_agent_idle_timeout_seconds == 60
    assert open_voice_agent(config) is None


def test_voice_agent_grok_realtime_validates(tmp_path, config_data):
    config_data["voice_agent"]["backend"] = "grok_realtime"
    config_data["voice_agent"]["model"] = "grok-voice-think-fast-2.0"
    config = load_config(write_config(tmp_path, config_data))
    assert config.voice_agent_backend == "grok_realtime"
    assert config.voice_agent_model == "grok-voice-think-fast-2.0"
    client = open_voice_agent(config, transport=FakeTransport())
    assert isinstance(client, GrokRealtimeClient)
    assert "grok_realtime" in client.label()
    assert "XAI_API_KEY" in client.label()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("backend", "grok"),
        ("backend", "grok_api"),
        ("backend", "stt"),
        ("model", "grok-4.6"),
        ("model", ""),
        ("voice", ""),
        ("voice", "bad voice"),
        ("api_key_env", "not-a-name"),
        ("api_key_env", ""),
        ("websocket_url", "https://api.x.ai/v1/realtime"),
        ("websocket_url", "ws://api.x.ai/v1/realtime"),
        ("websocket_url", "wss://user:pass@api.x.ai/v1/realtime"),
        ("connect_timeout_seconds", 0),
        ("idle_timeout_seconds", 0),
    ],
)
def test_invalid_voice_agent_fields_rejected(tmp_path, config_data, field, value):
    config_data["voice_agent"][field] = value
    with pytest.raises(WalkietalkError):
        load_config(write_config(tmp_path, config_data))


def test_missing_voice_agent_section_rejected(tmp_path, config_data):
    del config_data["voice_agent"]
    with pytest.raises(WalkietalkError, match="voice_agent"):
        load_config(write_config(tmp_path, config_data))


def test_open_voice_agent_never_aliases_stt_agent_tts():
    for backend in ("grok", "grok_api", "faster-whisper", "stub", "piper"):
        with pytest.raises(WalkietalkError, match="off or grok_realtime"):
            open_voice_agent(replace(Config(), voice_agent_backend=backend))


def test_happy_path_fake_transport_event_sequence(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    pcm = b"\x00\x01" * 8
    audio_b64 = base64.b64encode(pcm).decode("ascii")
    transport = FakeTransport(
        incoming=[
            event("session.updated"),
            event(OUTPUT_AUDIO_DELTA, delta=audio_b64),
            event(OUTPUT_AUDIO_DELTA, delta=audio_b64),
            event(OUTPUT_AUDIO_DONE),
            event(
                FUNCTION_CALL_ARGUMENTS_DONE,
                name="lookup",
                call_id="call_1",
                arguments='{"q":"x"}',
            ),
            event(OUTPUT_AUDIO_DELTA, delta=audio_b64),
            event(OUTPUT_AUDIO_DONE),
        ]
    )
    captured: dict = {}

    async def factory(url: str, headers: dict[str, str]):
        captured["url"] = url
        captured["headers"] = headers
        transport.url = url
        transport.headers = headers
        return transport

    async def run():
        client = GrokRealtimeClient(
            realtime_config(),
            transport_factory=factory,
            api_key="test-billed-key-not-from-env",
        )
        await client.connect()
        assert captured["headers"]["Authorization"] == "Bearer test-billed-key-not-from-env"
        assert "model=grok-voice-latest" in captured["url"]
        assert captured["url"].startswith("wss://api.x.ai/v1/realtime")

        await client.session_update(instructions="Be brief.")
        await client.append_audio(pcm)
        await client.commit_audio()
        await client.create_response()

        sent = [json.loads(item) for item in transport.sent]
        assert sent[0]["type"] == "session.update"
        assert sent[0]["session"]["voice"] == "eve"
        assert sent[0]["session"]["instructions"] == "Be brief."
        assert sent[0]["session"]["turn_detection"] is None
        assert sent[1]["type"] == "input_audio_buffer.append"
        assert sent[1]["audio"] == audio_b64
        assert sent[2] == {"type": "input_audio_buffer.commit"}
        assert sent[3] == {"type": "response.create"}

        events = []
        async for item in client.events():
            events.append(item)
            if len(events) == 7:
                break

        assert events[0].type == "session.updated"
        assert events[1].type == OUTPUT_AUDIO_DELTA
        assert events[1].is_first_output_audio_delta is True
        assert events[1].audio_delta_b64 == audio_b64
        assert events[2].type == OUTPUT_AUDIO_DELTA
        assert events[2].is_first_output_audio_delta is False
        assert events[3].type == OUTPUT_AUDIO_DONE
        assert events[4].type == FUNCTION_CALL_ARGUMENTS_DONE
        assert events[4].function_name == "lookup"
        assert events[4].function_call_id == "call_1"
        assert events[4].function_arguments == '{"q":"x"}'
        # After a tool-call gap, the next spoken delta is first again (PTT re-key hint).
        assert events[5].is_first_output_audio_delta is True
        assert events[6].type == OUTPUT_AUDIO_DONE
        await client.close()
        assert transport.closed is True

    asyncio.run(run())


def test_missing_api_key_fails_without_network(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)

    async def factory(url, headers):
        pytest.fail("must not open transport without a key")

    async def run():
        client = GrokRealtimeClient(realtime_config(), transport_factory=factory)
        with pytest.raises(WalkietalkError, match="XAI_API_KEY"):
            await client.connect()

    asyncio.run(run())


def test_invalid_api_key_rejected(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "has spaces")

    async def run():
        client = GrokRealtimeClient(realtime_config(), transport=FakeTransport())
        with pytest.raises(WalkietalkError, match="XAI_API_KEY"):
            await client.connect()

    asyncio.run(run())


def test_auth_rejection_from_transport_factory():
    async def factory(url, headers):
        raise RuntimeError("HTTP 401 Unauthorized")

    async def run():
        client = GrokRealtimeClient(
            realtime_config(),
            transport_factory=factory,
            api_key="bad-key",
        )
        with pytest.raises(WalkietalkError, match="authentication failed"):
            await client.connect()

    asyncio.run(run())


def test_error_event_auth_raises_walkietalk_error():
    transport = FakeTransport(
        incoming=[
            event(
                "error",
                error={"message": "Invalid API key", "code": "invalid_api_key"},
            )
        ]
    )

    async def run():
        client = GrokRealtimeClient(
            realtime_config(),
            transport=transport,
            api_key="test-key",
        )
        await client.connect()
        with pytest.raises(WalkietalkError, match="authentication failed"):
            async for _ in client.events():
                pass

    asyncio.run(run())


def test_protocol_error_event_raises_without_fallback():
    transport = FakeTransport(
        incoming=[event("error", error={"message": "unknown event type"})]
    )

    async def run():
        client = GrokRealtimeClient(
            realtime_config(),
            transport=transport,
            api_key="test-key",
        )
        await client.connect()
        with pytest.raises(WalkietalkError, match="protocol error"):
            async for _ in client.events():
                pass

    asyncio.run(run())


def test_connect_does_not_use_live_network_when_transport_injected(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "injected-presence-only")

    def forbid_websockets(*a, **k):
        pytest.fail("live websockets.connect must not run in Phase 1 tests")

    import walkietalk.grok_realtime as mod

    monkeypatch.setattr(mod, "_connect_websockets", forbid_websockets)
    transport = FakeTransport(incoming=[event("session.updated")])

    async def run():
        client = GrokRealtimeClient(realtime_config(), transport=transport)
        await client.connect()
        await client.session_update()
        assert json.loads(transport.sent[0])["type"] == "session.update"
        await client.close()

    asyncio.run(run())


def test_happy_path_runs_without_xai_api_key_in_environment(monkeypatch):
    """Prove CI can run the fake path with no billed key exported."""
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    assert "XAI_API_KEY" not in os.environ

    async def run():
        transport = FakeTransport(
            incoming=[
                event(OUTPUT_AUDIO_DELTA, delta="AA=="),
                event(OUTPUT_AUDIO_DONE),
            ]
        )
        client = GrokRealtimeClient(
            realtime_config(),
            transport=transport,
            api_key="fake-only-for-this-test",
        )
        await client.connect()
        events = [item async for item in client.events()]
        assert events[0].is_first_output_audio_delta
        assert events[1].type == OUTPUT_AUDIO_DONE
        await client.close()

    asyncio.run(run())
