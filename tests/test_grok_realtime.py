"""Grok realtime Phase 1–2: schema, fake transport, offline capture (no live network)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import wave
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
import yaml

from walkietalk import cli, grok_realtime
from walkietalk.audio import Wav
from walkietalk.config import Config, WalkietalkError, load_config
from walkietalk.grok_realtime import (
    APPEND_CHUNK_BYTES,
    FUNCTION_CALL_ARGUMENTS_DONE,
    OUTPUT_AUDIO_DELTA,
    OUTPUT_AUDIO_DONE,
    REALTIME_PCM_RATE,
    RESPONSE_DONE,
    GrokRealtimeClient,
    OfflineVoiceResult,
    RealtimeEvent,
    offline_voice_check,
    open_voice_agent,
    resample_pcm16,
    run_offline_turn,
)
from walkietalk.voice_agent_tx import (
    PTT_ENERGY_THRESHOLD_RMS,
    PttAction,
    RealtimeTalkSession,
    SupervisedRealtimeTx,
    SupervisedTxResult,
    pcm16_rms,
    run_supervised_turn,
    speak_text_via_realtime,
    stream_pcm_frames_for_tests,
    supervised_text_turn,
    supervised_voice_check,
)
from walkietalk.wake import ListeningSession


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


def loud_pcm(samples: int = 16, amplitude: int = 5000) -> bytes:
    """Synthetic audible PCM16 LE mono for energy-gate tests."""
    import struct

    amp = max(-32768, min(32767, amplitude))
    return struct.pack("<" + "h" * samples, *([amp] * samples))


def silent_pcm(samples: int = 16) -> bytes:
    return b"\x00\x00" * samples


def realtime_config(**overrides) -> Config:
    base = replace(
        Config(),
        agent_backend="grok_realtime",
        agent_realtime_model="grok-voice-latest",
        agent_realtime_voice="eve",
        agent_realtime_api_key_env="XAI_API_KEY",
        agent_realtime_websocket_url="wss://api.x.ai/v1/realtime",
        agent_realtime_connect_timeout_seconds=2,
        agent_realtime_idle_timeout_seconds=2,
    )
    return replace(base, **overrides) if overrides else base


def test_example_config_loads_realtime_defaults(tmp_path, config_data):
    config = load_config(write_config(tmp_path, config_data))
    assert config.agent_backend == "stub"
    assert config.agent_realtime_model == "grok-voice-latest"
    assert config.agent_realtime_voice == "eve"
    assert config.agent_realtime_api_key_env == "XAI_API_KEY"
    assert config.agent_realtime_websocket_url == "wss://api.x.ai/v1/realtime"
    assert config.agent_realtime_connect_timeout_seconds == 10
    assert config.agent_realtime_idle_timeout_seconds == 60
    assert open_voice_agent(config) is None


def test_legacy_voice_agent_section_raises_migrate(tmp_path, config_data):
    config_data["voice_agent"] = {
        "backend": "grok_realtime",
        "model": "grok-voice-latest",
        "voice": "eve",
        "api_key_env": "XAI_API_KEY",
        "websocket_url": "wss://api.x.ai/v1/realtime",
        "connect_timeout_seconds": 10,
        "idle_timeout_seconds": 60,
    }
    with pytest.raises(WalkietalkError, match="voice_agent: is no longer supported"):
        load_config(write_config(tmp_path, config_data))


def test_agent_backend_grok_realtime_validates(tmp_path, config_data):
    config_data["agent"]["backend"] = "grok_realtime"
    config_data["agent"]["realtime"]["model"] = "grok-voice-think-fast-2.0"
    config = load_config(write_config(tmp_path, config_data))
    assert config.agent_backend == "grok_realtime"
    assert config.agent_realtime_model == "grok-voice-think-fast-2.0"
    client = open_voice_agent(config, transport=FakeTransport())
    assert isinstance(client, GrokRealtimeClient)
    assert "grok_realtime" in client.label()
    assert "XAI_API_KEY" in client.label()


@pytest.mark.parametrize(
    ("field", "value"),
    [
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
def test_invalid_realtime_fields_rejected(tmp_path, config_data, field, value):
    config_data["agent"]["realtime"][field] = value
    with pytest.raises(WalkietalkError):
        load_config(write_config(tmp_path, config_data))


def test_missing_realtime_section_rejected(tmp_path, config_data):
    del config_data["agent"]["realtime"]
    with pytest.raises(WalkietalkError, match="realtime"):
        load_config(write_config(tmp_path, config_data))


def test_open_voice_agent_only_for_grok_realtime():
    assert open_voice_agent(replace(Config(), agent_backend="stub")) is None
    assert open_voice_agent(replace(Config(), agent_backend="grok")) is None
    client = open_voice_agent(realtime_config(), transport=FakeTransport())
    assert isinstance(client, GrokRealtimeClient)


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
    transport = FakeTransport(incoming=[event("error", error={"message": "unknown event type"})])

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


# --- Phase 2: offline capture path (WAV in → deltas → WAV out; no TX) ---


def pcm_wav(path: Path, *, rate=48000, samples=4800, amplitude=8000):
    # Leading/trailing silence so EnergyVad can start and end (hangover 400ms).
    silence = b"\x00\x00" * (rate // 2)  # 0.5s > default hangover_ms
    speech = (amplitude).to_bytes(2, "little", signed=True) * samples
    frames = silence + speech + silence
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(frames)
    return path, frames


def scripted_reply_transport(pcm_out: bytes, *, with_transcripts=True) -> FakeTransport:
    half = len(pcm_out) // 2
    first = base64.b64encode(pcm_out[:half] or pcm_out).decode("ascii")
    second = base64.b64encode(pcm_out[half:] or b"\x00\x00").decode("ascii")
    incoming = [
        event("session.updated"),
        event("input_audio_buffer.committed"),
    ]
    if with_transcripts:
        incoming.append(
            event(
                "conversation.item.input_audio_transcription.completed",
                transcript=" hello radio ",
            )
        )
    incoming.extend(
        [
            event(OUTPUT_AUDIO_DELTA, delta=first),
            event(OUTPUT_AUDIO_DELTA, delta=second),
        ]
    )
    if with_transcripts:
        incoming.append(event("response.output_audio_transcript.done", transcript=" short reply "))
    incoming.extend([event(OUTPUT_AUDIO_DONE), event(RESPONSE_DONE)])
    return FakeTransport(incoming=incoming)


def test_resample_pcm16_48k_to_24k():
    pcm = b"\x00\x10" * 4800  # 0.1s at 48 kHz
    out = resample_pcm16(pcm, 48000, 24000)
    assert len(out) == 2400 * 2
    assert resample_pcm16(out, 24000, 24000) == out


def test_offline_voice_check_requires_grok_realtime_backend():
    with pytest.raises(WalkietalkError, match="grok_realtime"):
        offline_voice_check(Config(), b"\x00\x00" * 100, 24000, api_key="x")


def test_offline_wav_in_assembles_reply_wav_without_tx(monkeypatch, tmp_path):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    reply_pcm = b"\x01\x00\x02\x00\x03\x00\x04\x00" * 100
    transport = scripted_reply_transport(reply_pcm)
    config = realtime_config()
    input_pcm = b"\x10\x00" * 4800  # 0.2s at 24 kHz

    # Hardware / radio paths must never be touched in Phase 2 offline tests.
    def forbid_tx(*a, **k):
        pytest.fail("TX path must not run during offline voice-agent check")

    for name in ("SerialPTT", "Playback", "transmit", "DryPTT"):
        monkeypatch.setattr(cli, name, forbid_tx)

    result = offline_voice_check(
        config,
        input_pcm,
        24000,
        transport=transport,
        api_key="fake-only-for-this-test",
    )
    assert isinstance(result, OfflineVoiceResult)
    assert result.reply_wav is not None
    assert result.reply_wav.rate == REALTIME_PCM_RATE
    assert result.reply_wav.frames == reply_pcm
    assert result.input_transcript == "hello radio"
    assert result.output_transcript == "short reply"
    assert OUTPUT_AUDIO_DELTA in result.event_types
    assert OUTPUT_AUDIO_DONE in result.event_types
    assert RESPONSE_DONE in result.event_types

    sent = [json.loads(item) for item in transport.sent]
    assert sent[0]["type"] == "session.update"
    appends = [item for item in sent if item["type"] == "input_audio_buffer.append"]
    assert appends
    reconstructed = b"".join(base64.b64decode(item["audio"]) for item in appends)
    assert reconstructed == input_pcm
    assert any(item["type"] == "input_audio_buffer.commit" for item in sent)
    assert any(item["type"] == "response.create" for item in sent)
    assert transport.closed is True


def test_offline_chunks_large_pcm(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    # > one append chunk so the client splits frames.
    input_pcm = b"\x00\x01" * (APPEND_CHUNK_BYTES // 2 + 50)
    reply_pcm = b"\x00\x02" * 32
    transport = scripted_reply_transport(reply_pcm, with_transcripts=False)
    result = offline_voice_check(
        realtime_config(),
        input_pcm,
        REALTIME_PCM_RATE,
        transport=transport,
        api_key="fake-key",
    )
    appends = [
        json.loads(item)
        for item in transport.sent
        if json.loads(item)["type"] == "input_audio_buffer.append"
    ]
    assert len(appends) >= 2
    assert result.reply_wav is not None
    assert result.reply_wav.frames == reply_pcm


@pytest.mark.parametrize("supervised", [False, True], ids=["offline", "supervised"])
@pytest.mark.parametrize("ping_delay", [0.001, 0.1], ids=["frequent-pings", "slow-pings"])
def test_response_start_pings_only_fails_fast(monkeypatch, supervised, ping_delay):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    assert grok_realtime.RESPONSE_START_TIMEOUT_SECONDS == 15.0
    monkeypatch.setattr(grok_realtime, "RESPONSE_START_TIMEOUT_SECONDS", 0.05)

    class PingingTransport(FakeTransport):
        async def recv(self):
            if self.incoming:
                return await super().recv()
            await asyncio.sleep(ping_delay)
            return event("ping")

    transport = PingingTransport(
        incoming=[
            event("session.updated"),
            event("input_audio_buffer.committed"),
            event("ping"),
        ]
    )
    config = realtime_config(agent_realtime_idle_timeout_seconds=60)
    client = GrokRealtimeClient(config, transport=transport, api_key="fake-key")
    ptt = FakePTT()

    async def run():
        if supervised:
            turn = run_supervised_turn(
                client, config, b"\x10\x00" * 2400, 24000, ptt, allow_key=True
            )
        else:
            turn = run_offline_turn(client, b"\x10\x00" * 2400, 24000)
        # A regression to the 60-second idle deadline must fail this test quickly.
        with pytest.raises(WalkietalkError, match="no model response after audio commit") as exc:
            await asyncio.wait_for(turn, timeout=1.0)
        message = str(exc.value)
        assert "input may not be intelligible speech" in message
        assert "PCM16 mono 24 kHz" in message
        assert "no stt/agent/tts fallback" in message

    asyncio.run(run())
    assert [json.loads(item)["type"] for item in transport.sent][-2:] == [
        "input_audio_buffer.commit",
        "response.create",
    ]
    assert transport.closed
    assert "on" not in ptt.actions


@pytest.mark.parametrize("supervised", [False, True], ids=["offline", "supervised"])
def test_response_started_can_finish_after_start_timeout(monkeypatch, supervised):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setattr(grok_realtime, "RESPONSE_START_TIMEOUT_SECONDS", 0.05)

    class DelayedDoneTransport(FakeTransport):
        async def recv(self):
            if self.incoming and json.loads(self.incoming[0])["type"] == RESPONSE_DONE:
                await asyncio.sleep(0.1)
            return await super().recv()

    reply_pcm = b"\x01\x00" * 16
    transport = DelayedDoneTransport(
        incoming=[
            event("session.updated"),
            event("input_audio_buffer.committed"),
            event("ping"),
            event(OUTPUT_AUDIO_DELTA, delta=base64.b64encode(reply_pcm).decode("ascii")),
            event("ping"),
            event(RESPONSE_DONE),
        ]
    )
    config = realtime_config(agent_realtime_idle_timeout_seconds=60)
    kwargs = {"transport": transport, "api_key": "fake-key"}
    if supervised:
        result = supervised_voice_check(
            config, b"\x10\x00" * 2400, 24000, FakePTT(), allow_key=False, **kwargs
        )
    else:
        result = offline_voice_check(config, b"\x10\x00" * 2400, 24000, **kwargs)
    assert result.reply_wav is not None
    assert result.reply_wav.frames == reply_pcm
    assert result.event_types[-4:] == ("ping", OUTPUT_AUDIO_DELTA, "ping", RESPONSE_DONE)
    assert transport.closed


def test_voice_agent_check_cli_writes_wav_and_prints(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    wav_path, frames = pcm_wav(tmp_path / "in.wav", rate=24000, samples=12000)
    out_path = tmp_path / "reply.wav"
    reply_pcm = b"\x05\x00" * 64

    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["agent"]["backend"] = "grok_realtime"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data))

    def fake_offline(config, pcm, rate, **kwargs):
        assert config.agent_backend == "grok_realtime"
        assert rate == 24000
        assert pcm  # VAD may trim edges; must still deliver captured speech
        assert len(pcm) <= len(frames)
        return OfflineVoiceResult(
            reply_wav=Wav(reply_pcm, REALTIME_PCM_RATE, len(reply_pcm) / 2 / REALTIME_PCM_RATE),
            input_transcript="fixture heard",
            output_transcript="fixture reply",
            event_types=(OUTPUT_AUDIO_DELTA, OUTPUT_AUDIO_DONE, RESPONSE_DONE),
        )

    # Replace the sync helper so CLI never opens a network socket.
    monkeypatch.setattr(cli, "offline_voice_check", fake_offline)

    def forbid_hw(*a, **k):
        pytest.fail("TX/hardware path must not run during offline voice-agent check")

    for name in ("SerialPTT", "Playback", "transmit", "DryPTT", "preflight"):
        monkeypatch.setattr(cli, name, forbid_hw)

    assert (
        cli.main(
            [
                "-c",
                str(config_path),
                "--no-env-file",
                "voice-agent-check",
                str(wav_path),
                "--output",
                str(out_path),
            ]
        )
        == 0
    )
    assert out_path.is_file()
    with wave.open(str(out_path), "rb") as reader:
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getframerate() == REALTIME_PCM_RATE
        assert reader.readframes(reader.getnframes()) == reply_pcm
    printed = capsys.readouterr().out
    assert "Heard: fixture heard" in printed
    assert "Reply: fixture reply" in printed
    assert "No hardware TX" in printed
    assert "No PTT" in printed or "no PTT" in printed


def test_voice_agent_check_cli_rejects_backend_off(tmp_path):
    wav_path, _ = pcm_wav(tmp_path / "in.wav")
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    assert data["agent"]["backend"] == "stub"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data))
    assert (
        cli.main(
            [
                "-c",
                str(config_path),
                "--no-env-file",
                "voice-agent-check",
                str(wav_path),
                "--output",
                str(tmp_path / "out.wav"),
            ]
        )
        == 1
    )


def test_voice_agent_check_cli_refuses_overwrite(tmp_path):
    wav_path, _ = pcm_wav(tmp_path / "in.wav")
    out_path = tmp_path / "exists.wav"
    out_path.write_bytes(b"x")
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["agent"]["backend"] = "grok_realtime"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data))
    assert (
        cli.main(
            [
                "-c",
                str(config_path),
                "--no-env-file",
                "voice-agent-check",
                str(wav_path),
                "--output",
                str(out_path),
            ]
        )
        == 1
    )


# --- Phase 3: parent-owned supervised TX (fake PTT; no live radio) ---


@dataclass
class FakePTT:
    actions: list[str] = field(default_factory=list)
    keyed: bool = False
    opened: bool = False
    closed: bool = False

    def open(self) -> None:
        self.opened = True
        self.actions.append("open")

    def on(self) -> None:
        assert self.opened and not self.closed
        self.keyed = True
        self.actions.append("on")

    def off(self) -> None:
        self.keyed = False
        self.actions.append("off")

    def close(self) -> None:
        self.closed = True
        self.actions.append("close")


def _delta(pcm: bytes, *, first: bool) -> RealtimeEvent:
    return RealtimeEvent(
        type=OUTPUT_AUDIO_DELTA,
        data={"type": OUTPUT_AUDIO_DELTA, "delta": base64.b64encode(pcm).decode("ascii")},
        is_first_output_audio_delta=first,
        audio_delta_b64=base64.b64encode(pcm).decode("ascii"),
    )


def test_client_module_has_no_ptt_or_serial_imports():
    source = Path("src/walkietalk/grok_realtime.py").read_text()
    for banned in (
        "from .ptt",
        "import serial",
        "SerialPTT",
        "DryPTT",
        "from .session import transmit",
        "import sounddevice",
        "from .session import",
        "from .audio import Playback",
    ):
        assert banned not in source, banned


def test_supervised_keys_on_audible_energy_unkeys_on_audio_done():
    config = realtime_config(max_tx_seconds=10, settle_seconds=0)
    ptt = FakePTT()
    played: list[bytes] = []

    def play(pcm, rate, deadline):
        played.append(pcm)

    loud_a = loud_pcm(10, 5000)
    loud_b = loud_pcm(10, 6000)
    tx = SupervisedRealtimeTx(config, ptt, allow_key=True, play_segment=play, clock=lambda: 0.0)
    tx.handle(_delta(loud_a, first=True))
    assert ptt.actions == ["open", "on"]
    assert tx.actions[0].reason == "audible_audio_energy"
    tx.handle(_delta(loud_b, first=False))
    assert ptt.keyed is True
    tx.handle(RealtimeEvent(type=OUTPUT_AUDIO_DONE, data={"type": OUTPUT_AUDIO_DONE}))
    assert ptt.actions == ["open", "on", "off"]
    assert played == [loud_a + loud_b]
    tx.close()
    assert ptt.actions[-1] == "close"


def test_supervised_silent_deltas_never_key():
    config = realtime_config(max_tx_seconds=10, settle_seconds=0)
    ptt = FakePTT()
    tx = SupervisedRealtimeTx(
        config, ptt, allow_key=True, play_segment=lambda *a: None, clock=lambda: 0.0
    )
    quiet = silent_pcm(240)  # 10 ms of silence at 24 kHz
    assert pcm16_rms(quiet) < PTT_ENERGY_THRESHOLD_RMS
    tx.handle(_delta(quiet, first=True))
    tx.handle(_delta(quiet, first=False))
    tx.handle(RealtimeEvent(type=OUTPUT_AUDIO_DONE, data={"type": OUTPUT_AUDIO_DONE}))
    assert ptt.actions == []
    assert tx.actions == []
    tx.close()


def test_supervised_pre_roll_then_energy_keys():
    config = realtime_config(max_tx_seconds=10, settle_seconds=0)
    ptt = FakePTT()
    played: list[bytes] = []

    def play(pcm, rate, deadline):
        played.append(pcm)

    quiet = silent_pcm(48)
    loud = loud_pcm(48, 8000)
    tx = SupervisedRealtimeTx(config, ptt, allow_key=True, play_segment=play, clock=lambda: 0.0)
    tx.handle(_delta(quiet, first=True))
    assert ptt.actions == []
    tx.handle(_delta(loud, first=False))
    assert ptt.actions == ["open", "on"]
    assert tx.actions[0].reason == "audible_audio_energy"
    tx.handle(RealtimeEvent(type=OUTPUT_AUDIO_DONE, data={"type": OUTPUT_AUDIO_DONE}))
    assert played == [quiet + loud]
    tx.close()


def test_supervised_unkeys_on_tool_gap_and_rekeys_on_next_energy():
    config = realtime_config(max_tx_seconds=10, settle_seconds=0)
    ptt = FakePTT()
    tx = SupervisedRealtimeTx(
        config, ptt, allow_key=True, play_segment=lambda *a: None, clock=lambda: 0.0
    )
    tx.handle(_delta(loud_pcm(4), first=True))
    tx.handle(
        RealtimeEvent(
            type=FUNCTION_CALL_ARGUMENTS_DONE,
            data={"type": FUNCTION_CALL_ARGUMENTS_DONE, "name": "lookup"},
        )
    )
    assert ptt.actions == ["open", "on", "off"]
    tx.handle(_delta(loud_pcm(4, 7000), first=True))
    assert ptt.actions == ["open", "on", "off", "on"]
    assert tx.actions[-1].reason == "audible_audio_energy"
    tx.handle(RealtimeEvent(type=OUTPUT_AUDIO_DONE, data={"type": OUTPUT_AUDIO_DONE}))
    assert ptt.actions == ["open", "on", "off", "on", "off"]
    tx.close()


def test_supervised_tx_cap_truncates_and_unkeys():
    config = realtime_config(max_tx_seconds=0.2, settle_seconds=0)
    ptt = FakePTT()
    played: list[int] = []

    def play(pcm, rate, deadline):
        played.append(len(pcm))

    # 0.2s * 24000 * 2 = 9600 bytes max. Arm with a short loud burst, then overflow.
    tx = SupervisedRealtimeTx(config, ptt, allow_key=True, play_segment=play, clock=lambda: 0.0)
    tx.handle(_delta(loud_pcm(100, 5000), first=True))
    assert ptt.keyed is True
    tx.handle(_delta(loud_pcm(20000, 5000), first=False))
    assert tx.truncated is True
    assert ptt.actions.count("on") == 1
    assert ptt.actions.count("off") == 1
    assert played and played[0] <= 9600
    tx.close()


def test_supervised_never_keys_without_allow_key():
    config = realtime_config()
    ptt = FakePTT()
    tx = SupervisedRealtimeTx(config, ptt, allow_key=False, play_segment=lambda *a: None)
    tx.handle(_delta(loud_pcm(8), first=True))
    tx.handle(RealtimeEvent(type=OUTPUT_AUDIO_DONE, data={"type": OUTPUT_AUDIO_DONE}))
    tx.close()
    assert ptt.actions == []
    assert tx.actions == []


def test_supervised_unkeys_on_fail():
    config = realtime_config(settle_seconds=0)
    ptt = FakePTT()
    tx = SupervisedRealtimeTx(config, ptt, allow_key=True, play_segment=lambda *a: None)
    tx.handle(_delta(loud_pcm(4), first=True))
    tx.fail("boom")
    assert "off" in ptt.actions
    assert ptt.closed is True


def test_supervised_voice_check_fake_transport_ptt_sequence(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    transport = FakeTransport(
        incoming=[
            event("session.updated"),
            event("input_audio_buffer.committed"),
            event(
                OUTPUT_AUDIO_DELTA,
                delta=base64.b64encode(loud_pcm(16)).decode("ascii"),
            ),
            event(
                OUTPUT_AUDIO_DELTA,
                delta=base64.b64encode(loud_pcm(16, 6000)).decode("ascii"),
            ),
            event(OUTPUT_AUDIO_DONE),
            event(
                FUNCTION_CALL_ARGUMENTS_DONE,
                name="lookup",
                call_id="c1",
                arguments="{}",
            ),
            event(
                OUTPUT_AUDIO_DELTA,
                delta=base64.b64encode(loud_pcm(16, 7000)).decode("ascii"),
            ),
            event(OUTPUT_AUDIO_DONE),
            event("response.done"),
        ]
    )
    ptt = FakePTT()
    result = supervised_voice_check(
        realtime_config(settle_seconds=0),
        b"\x10\x00" * 2400,
        24000,
        ptt,
        allow_key=True,
        transport=transport,
        api_key="fake-key",
        play_segment=lambda *a: None,
    )
    kinds = [item.kind for item in result.ptt_actions]
    assert kinds == ["key", "unkey", "key", "unkey"]
    assert result.ptt_actions[0].reason == "audible_audio_energy"
    assert result.ptt_actions[1].reason == "output_audio.done"
    assert result.ptt_actions[2].reason == "audible_audio_energy"
    assert result.ptt_actions[3].reason in {"output_audio.done", "response.done"}
    assert ptt.closed is True
    assert result.reply_wav is not None


def test_supervised_cli_dry_ptt_without_transmit(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    wav_path, frames = pcm_wav(tmp_path / "in.wav", rate=24000, samples=12000)
    out_path = tmp_path / "reply.wav"
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["agent"]["backend"] = "grok_realtime"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data))

    def fake_supervised(config, pcm, rate, ptt, **kwargs):
        assert kwargs.get("allow_key") is True
        assert type(ptt).__name__ == "DryPTT"
        return SupervisedTxResult(
            reply_wav=Wav(b"\x05\x00" * 32, REALTIME_PCM_RATE, 32 / REALTIME_PCM_RATE),
            output_transcript="dry reply",
            ptt_actions=(
                PttAction("key", "audible_audio_energy"),
                PttAction("unkey", "output_audio.done"),
            ),
        )

    monkeypatch.setattr(cli, "supervised_voice_check", fake_supervised)
    monkeypatch.setattr(
        cli,
        "SerialPTT",
        lambda *a, **k: pytest.fail("SerialPTT must not run without --transmit"),
    )
    assert (
        cli.main(
            [
                "-c",
                str(config_path),
                "--no-env-file",
                "voice-agent-check",
                str(wav_path),
                "--output",
                str(out_path),
                "--supervised",
            ]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "DryPTT" in printed
    assert "PTT actions:" in printed
    assert out_path.is_file()


def test_supervised_cli_transmit_requires_supervised(tmp_path):
    wav_path, _ = pcm_wav(tmp_path / "in.wav", rate=24000, samples=12000)
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["agent"]["backend"] = "grok_realtime"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data))
    assert (
        cli.main(
            [
                "-c",
                str(config_path),
                "--no-env-file",
                "voice-agent-check",
                str(wav_path),
                "--output",
                str(tmp_path / "out.wav"),
                "--transmit",
            ]
        )
        == 1
    )


# --- Talk routing: grok_realtime streams live audio; off keeps agent path ---


def test_talk_routes_realtime_dry_ptt_without_agent(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    wav_path, _ = pcm_wav(tmp_path / "in.wav", rate=24000, samples=12000)
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["agent"]["backend"] = "grok_realtime"
    data["listening"]["mode"] = "wake_phrase"
    data["wake"]["primary"] = "charlotte"
    data["wake"]["aliases"] = []
    config_path = write_config(tmp_path, data)

    listener = type("L", (), {})()
    listener.label = lambda self=None: "fake-stt"
    listener.prepare = lambda self=None: None
    listener.transcribe = lambda pcm, rate: "charlotte what is rain"

    called = {"frames": 0}

    class FakeSession:
        def __init__(self, config, **kwargs):
            called["config_backend"] = config.agent_backend

        def warm(self, *, instructions=None):
            called["warm_instructions"] = instructions

        def on_frame(self, pcm, rate=None):
            called["frames"] += 1
            called["last_rate"] = rate

        def on_reset(self):
            called["reset"] = True

        def clear_input(self):
            called["cleared"] = True

        def commit_and_respond(self, ptt, *, allow_key, play_segment=None):
            called["ptt"] = type(ptt).__name__
            called["allow_key"] = allow_key
            called["committed"] = True
            # Must not be driven by decision.traffic text.
            return SupervisedTxResult(
                reply_wav=Wav(b"\x05\x00" * 32, REALTIME_PCM_RATE, 32 / REALTIME_PCM_RATE),
                input_transcript="charlotte what is rain",
                output_transcript="rain falls from clouds",
                ptt_actions=(
                    PttAction("key", "audible_audio_energy"),
                    PttAction("unkey", "output_audio.done"),
                ),
            )

        def close(self):
            called["closed"] = True

    monkeypatch.setattr(cli, "open_stt", lambda config: listener)
    monkeypatch.setattr(
        cli, "open_agent", lambda *a, **k: pytest.fail("open_agent must not run for realtime talk")
    )
    monkeypatch.setattr(
        cli, "open_tts", lambda *a, **k: pytest.fail("open_tts must not run without --transmit")
    )
    monkeypatch.setattr(cli, "RealtimeTalkSession", FakeSession)
    monkeypatch.setattr(
        cli,
        "supervised_voice_check",
        lambda *a, **k: pytest.fail("talk must not batch-upload utterance PCM for the question"),
    )
    if hasattr(cli, "supervised_text_turn"):
        monkeypatch.setattr(
            cli,
            "supervised_text_turn",
            lambda *a, **k: pytest.fail("talk must not send decision.traffic as input_text"),
        )
    monkeypatch.setattr(
        cli,
        "SerialPTT",
        lambda *a, **k: pytest.fail("SerialPTT must not run without --transmit"),
    )
    monkeypatch.setattr(cli, "preflight", lambda *a, **k: pytest.fail("preflight unexpected"))

    assert (
        cli.main(
            [
                "-c",
                str(config_path),
                "--no-env-file",
                "talk",
                str(wav_path),
                "--once",
            ]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert called["ptt"] == "DryPTT"
    assert called["allow_key"] is True
    assert called["committed"] is True
    assert called["frames"] > 0
    assert called.get("warm_instructions")
    assert "Agent: grok_realtime" in printed
    assert "DryPTT" in printed
    assert "PTT actions:" in printed
    assert "Reply: rain falls from clouds" in printed


def test_talk_backend_off_still_uses_agent(monkeypatch, tmp_path, capsys):
    wav_path, _ = pcm_wav(tmp_path / "in.wav", rate=24000, samples=12000)
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    assert data["agent"]["backend"] == "stub"
    data["listening"]["mode"] = "wake_phrase"
    data["wake"]["primary"] = "charlotte"
    data["wake"]["aliases"] = []
    data["agent"]["backend"] = "stub"
    config_path = write_config(tmp_path, data)

    listener = type("L", (), {})()
    listener.label = lambda self=None: "fake-stt"
    listener.prepare = lambda self=None: None
    listener.transcribe = lambda pcm, rate: "charlotte what is rain"

    monkeypatch.setattr(cli, "open_stt", lambda config: listener)
    monkeypatch.setattr(
        cli,
        "supervised_voice_check",
        lambda *a, **k: pytest.fail("supervised_voice_check must not run when backend off"),
    )
    monkeypatch.setattr(
        cli,
        "RealtimeTalkSession",
        lambda *a, **k: pytest.fail("RealtimeTalkSession must not run when backend off"),
    )

    assert (
        cli.main(
            [
                "-c",
                str(config_path),
                "--no-env-file",
                "talk",
                str(wav_path),
                "--once",
            ]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "Agent:" in printed
    assert "Voice agent: grok_realtime" not in printed
    assert "Reply:" in printed


def test_talk_realtime_transmit_uses_serial_ptt(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    wav_path, _ = pcm_wav(tmp_path / "in.wav", rate=24000, samples=12000)
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["agent"]["backend"] = "grok_realtime"
    data["listening"]["mode"] = "wake_phrase"
    data["wake"]["primary"] = "charlotte"
    data["wake"]["aliases"] = []
    config_path = write_config(tmp_path, data)

    listener = type("L", (), {})()
    listener.label = lambda self=None: "fake-stt"
    listener.prepare = lambda self=None: None
    listener.transcribe = lambda pcm, rate: "charlotte what is rain"

    voice = type("V", (), {})()
    voice.label = lambda self=None: "fake-tts"
    voice.prepare = lambda self=None: None
    voice.synthesize = lambda *a, **k: pytest.fail("TTS must not synthesize realtime replies")

    called = {}

    class FakeSerial:
        def __init__(self, *a, **k):
            called["serial"] = True

    class FakeSession:
        def __init__(self, config, **kwargs):
            pass

        def warm(self, *, instructions=None):
            pass

        def on_frame(self, pcm, rate=None):
            called["frames"] = called.get("frames", 0) + 1

        def on_reset(self):
            pass

        def clear_input(self):
            pass

        def commit_and_respond(self, ptt, *, allow_key, play_segment=None):
            called["ptt"] = type(ptt).__name__
            assert allow_key is True
            return SupervisedTxResult(
                reply_wav=Wav(b"\x05\x00" * 16, REALTIME_PCM_RATE, 16 / REALTIME_PCM_RATE),
                input_transcript="charlotte what is rain",
                output_transcript="ok",
                ptt_actions=(
                    PttAction("key", "audible_audio_energy"),
                    PttAction("unkey", "output_audio.done"),
                ),
            )

        def close(self):
            pass

    monkeypatch.setattr(cli, "open_stt", lambda config: listener)
    monkeypatch.setattr(cli, "open_tts", lambda config: voice)
    monkeypatch.setattr(
        cli, "open_agent", lambda *a, **k: pytest.fail("open_agent must not run for realtime talk")
    )
    monkeypatch.setattr(cli, "RealtimeTalkSession", FakeSession)
    monkeypatch.setattr(
        cli,
        "supervised_voice_check",
        lambda *a, **k: pytest.fail("talk must not batch-upload utterance PCM for the question"),
    )
    monkeypatch.setattr(cli, "SerialPTT", FakeSerial)
    monkeypatch.setattr(cli, "preflight", lambda *a, **k: None)

    assert (
        cli.main(
            [
                "-c",
                str(config_path),
                "--no-env-file",
                "talk",
                str(wav_path),
                "--once",
                "--transmit",
            ]
        )
        == 0
    )
    assert called.get("serial") is True
    assert called["ptt"] == "FakeSerial"
    assert called.get("frames", 0) > 0
    printed = capsys.readouterr().out
    assert "Reply: ok" in printed


def test_live_stream_appends_as_frames_arrive(monkeypatch):
    """Appends happen during capture framing — not only after STT — and order is preserved."""
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    reply = loud_pcm(16)
    transport = FakeTransport(
        incoming=[
            event("session.updated"),
            event("input_audio_buffer.committed"),
            event(OUTPUT_AUDIO_DELTA, delta=base64.b64encode(reply).decode("ascii")),
            event(OUTPUT_AUDIO_DONE),
            event(RESPONSE_DONE),
        ]
    )
    session = RealtimeTalkSession(
        realtime_config(settle_seconds=0), transport=transport, api_key="fake"
    )
    session.warm(instructions="brief")
    pcm = b"\x10\x00" * 2400  # 100 ms at 24 kHz
    counts = stream_pcm_frames_for_tests(session, pcm, REALTIME_PCM_RATE)
    assert counts
    assert counts[-1] > 0
    assert all(counts[i] <= counts[i + 1] for i in range(len(counts) - 1))
    # Mid-stream appends already happened before commit.
    mid_types = [json.loads(item)["type"] for item in transport.sent]
    assert mid_types.count("input_audio_buffer.append") >= 2
    assert "input_audio_buffer.commit" not in mid_types
    ptt = FakePTT()
    result = session.commit_and_respond(ptt, allow_key=True, play_segment=lambda *a: None)
    payloads = [json.loads(item) for item in transport.sent]
    types = [item["type"] for item in payloads]
    assert "input_audio_buffer.commit" in types
    assert "response.create" in types
    assert not any(
        item["type"] == "conversation.item.create"
        and item.get("item", {}).get("content", [{}])[0].get("type") == "input_text"
        for item in payloads
    )
    assert result.ptt_actions[0].reason == "audible_audio_energy"
    session.close()


def test_live_stream_reject_clears_without_commit(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    transport = FakeTransport(
        incoming=[
            event("session.updated"),
            event("input_audio_buffer.cleared"),
        ]
    )
    session = RealtimeTalkSession(
        realtime_config(settle_seconds=0), transport=transport, api_key="fake"
    )
    session.warm()
    stream_pcm_frames_for_tests(session, b"\x10\x00" * 960, REALTIME_PCM_RATE)
    session.clear_input()
    types = [json.loads(item)["type"] for item in transport.sent]
    assert "input_audio_buffer.append" in types
    assert "input_audio_buffer.clear" in types
    assert "input_audio_buffer.commit" not in types
    assert "response.create" not in types
    session.close()


def test_collect_utterance_on_frame_orders_captured_audio():
    from walkietalk.capture import collect_utterance, frames_from_pcm
    from walkietalk.vad import frame_samples

    rate = 16000
    samples = frame_samples(rate)
    silence = b"\x00\x00" * samples
    speech = b"\x00\x40" * samples  # loud-ish
    pcm = silence * 2 + speech * 20 + silence * 30
    seen: list[bytes] = []
    resets = []

    utterance = collect_utterance(
        frames_from_pcm(pcm, rate),
        rate=rate,
        energy_threshold=0.01,
        hangover_ms=200,
        max_utterance_seconds=12,
        wait_deadline=None,
        log=lambda _line: None,
        on_frame=lambda chunk, r: seen.append(chunk),
        on_reset=lambda: resets.append(1),
    )
    assert seen
    assert b"".join(seen) == utterance.pcm
    assert utterance.rate == rate


def test_session_update_includes_web_search_tools_when_enabled(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-key-not-real")
    transport = FakeTransport(incoming=[event("session.updated")])
    client = GrokRealtimeClient(
        realtime_config(agent_web_search=True), transport=transport, api_key="test-key-not-real"
    )

    async def run():
        await client.connect()
        await client.session_update()
        await client.close()

    asyncio.run(run())
    payload = json.loads(transport.sent[0])
    assert payload["type"] == "session.update"
    assert payload["session"]["tools"] == [{"type": "web_search"}]


def test_session_update_omits_tools_when_web_search_false(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-key-not-real")
    transport = FakeTransport(incoming=[event("session.updated")])
    client = GrokRealtimeClient(
        realtime_config(agent_web_search=False), transport=transport, api_key="test-key-not-real"
    )

    async def run():
        await client.connect()
        await client.session_update()
        await client.close()

    asyncio.run(run())
    payload = json.loads(transport.sent[0])
    assert "tools" not in payload["session"]


def test_speak_text_via_realtime_force_message(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    reply = loud_pcm(32)
    transport = FakeTransport(
        incoming=[
            event("session.updated"),
            event(OUTPUT_AUDIO_DELTA, delta=base64.b64encode(reply).decode("ascii")),
            event(OUTPUT_AUDIO_DONE),
            event(RESPONSE_DONE),
        ]
    )
    ptt = FakePTT()
    result = speak_text_via_realtime(
        realtime_config(),
        "Wake phrase received!",
        ptt,
        allow_key=True,
        transport=transport,
        api_key="test-key-not-real",
        play_segment=lambda *a: None,
    )
    payloads = [json.loads(item) for item in transport.sent]
    sent_types = [item["type"] for item in payloads]
    assert "session.update" in sent_types
    assert "conversation.item.create" in sent_types
    assert "response.create" not in sent_types
    force = next(item for item in payloads if item["type"] == "conversation.item.create")
    assert force["item"]["type"] == "force_message"
    assert force["item"]["content"][0]["text"] == "Wake phrase received!"
    session = next(item["session"] for item in payloads if item["type"] == "session.update")
    assert "tools" not in session
    assert result.ptt_actions[0].reason == "audible_audio_energy"
    assert ptt.actions.count("on") == 1


def test_create_user_text_message_payload(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-key-not-real")
    transport = FakeTransport(incoming=[event("session.updated")])
    client = GrokRealtimeClient(realtime_config(), transport=transport, api_key="test-key-not-real")

    async def run():
        await client.connect()
        await client.create_user_text_message("what is rain")
        await client.close()

    asyncio.run(run())
    payloads = [json.loads(item) for item in transport.sent]
    create = next(item for item in payloads if item["type"] == "conversation.item.create")
    assert create["item"]["type"] == "message"
    assert create["item"]["role"] == "user"
    assert create["item"]["content"] == [{"type": "input_text", "text": "what is rain"}]


def test_supervised_text_turn_sends_text_not_audio(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    reply = loud_pcm(32)
    transport = FakeTransport(
        incoming=[
            event("session.updated"),
            event(OUTPUT_AUDIO_DELTA, delta=base64.b64encode(reply).decode("ascii")),
            event(OUTPUT_AUDIO_DONE),
            event(RESPONSE_DONE),
        ]
    )
    ptt = FakePTT()
    result = supervised_text_turn(
        realtime_config(settle_seconds=0),
        "what is rain",
        ptt,
        allow_key=True,
        transport=transport,
        api_key="fake-key",
        play_segment=lambda *a: None,
        instructions="be brief",
    )
    payloads = [json.loads(item) for item in transport.sent]
    sent_types = [item["type"] for item in payloads]
    assert "session.update" in sent_types
    assert "conversation.item.create" in sent_types
    assert "response.create" in sent_types
    assert "input_audio_buffer.append" not in sent_types
    assert "input_audio_buffer.commit" not in sent_types
    create = next(item for item in payloads if item["type"] == "conversation.item.create")
    assert create["item"]["type"] == "message"
    assert create["item"]["role"] == "user"
    assert create["item"]["content"][0]["type"] == "input_text"
    assert create["item"]["content"][0]["text"] == "what is rain"
    assert result.input_transcript == "what is rain"
    assert result.ptt_actions[0].reason == "audible_audio_energy"
    assert result.reply_wav is not None


def test_supervised_voice_check_still_uploads_audio(monkeypatch):
    """voice-agent-check / offline WAV-in path keeps append+commit audio upload."""
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    transport = FakeTransport(
        incoming=[
            event("session.updated"),
            event("input_audio_buffer.committed"),
            event(
                OUTPUT_AUDIO_DELTA,
                delta=base64.b64encode(loud_pcm(16)).decode("ascii"),
            ),
            event(OUTPUT_AUDIO_DONE),
            event(RESPONSE_DONE),
        ]
    )
    ptt = FakePTT()
    result = supervised_voice_check(
        realtime_config(settle_seconds=0),
        b"\x10\x00" * 2400,
        24000,
        ptt,
        allow_key=True,
        transport=transport,
        api_key="fake-key",
        play_segment=lambda *a: None,
    )
    payloads = [json.loads(item) for item in transport.sent]
    sent_types = [item["type"] for item in payloads]
    assert "input_audio_buffer.append" in sent_types
    assert "input_audio_buffer.commit" in sent_types
    assert not any(
        item["type"] == "conversation.item.create"
        and item.get("item", {}).get("content", [{}])[0].get("type") == "input_text"
        for item in payloads
    )
    assert result.reply_wav is not None


# --- In-window: shutdown gate before commit; armed never commits ---


def test_talk_in_window_commits_after_shutdown_gate_stt(monkeypatch, tmp_path, capsys):
    """In-window not armed: STT shutdown gate, then commit; STT text is not traffic."""
    import time as time_mod

    monkeypatch.delenv("XAI_API_KEY", raising=False)
    wav_path, _ = pcm_wav(tmp_path / "in.wav", rate=24000, samples=12000)
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["agent"]["backend"] = "grok_realtime"
    data["listening"]["mode"] = "conversation"
    data["listening"]["conversation_timeout_seconds"] = 60
    data["wake"]["primary"] = "charlotte"
    data["wake"]["aliases"] = []
    data["shutdown"]["enabled"] = True
    data["shutdown"]["phrase"] = "bird"
    data["shutdown"]["code"] = "seven"
    data["shutdown"]["arm_confirmation_phrase"] = "Armed."
    data["shutdown"]["confirmation_phrase"] = "Shutting down."
    config_path = write_config(tmp_path, data)

    order: list[str] = []
    called: dict = {"frames": 0}

    def transcribe(pcm, rate):
        order.append("stt")
        return "what about rain"

    listener = type("L", (), {})()
    listener.label = lambda self=None: "fake-stt"
    listener.prepare = lambda self=None: None
    listener.transcribe = transcribe

    class FakeSession:
        def __init__(self, config, **kwargs):
            pass

        def warm(self, *, instructions=None):
            called["warm"] = True

        def on_frame(self, pcm, rate=None):
            called["frames"] += 1

        def on_reset(self):
            pass

        def clear_input(self):
            order.append("clear")
            called["cleared"] = True

        def commit_and_respond(self, ptt, *, allow_key, play_segment=None):
            order.append("commit")
            called["ptt"] = type(ptt).__name__
            called["committed_after_stt"] = "stt" in order
            return SupervisedTxResult(
                reply_wav=Wav(b"\x05\x00" * 16, REALTIME_PCM_RATE, 16 / REALTIME_PCM_RATE),
                input_transcript="(realtime)",
                output_transcript="rain continues",
                ptt_actions=(
                    PttAction("key", "audible_audio_energy"),
                    PttAction("unkey", "output_audio.done"),
                ),
            )

        def close(self):
            called["closed"] = True

    gate = ListeningSession(load_config(config_path), clock=time_mod.monotonic)
    gate.awake_until = time_mod.monotonic() + 3600

    monkeypatch.setattr(cli, "open_stt", lambda config: listener)
    monkeypatch.setattr(
        cli, "open_agent", lambda *a, **k: pytest.fail("open_agent must not run for realtime talk")
    )
    monkeypatch.setattr(
        cli, "open_tts", lambda *a, **k: pytest.fail("open_tts must not run without --transmit")
    )
    monkeypatch.setattr(cli, "RealtimeTalkSession", FakeSession)
    monkeypatch.setattr(cli, "ListeningSession", lambda config: gate)
    monkeypatch.setattr(
        cli,
        "supervised_voice_check",
        lambda *a, **k: pytest.fail("talk must not batch-upload utterance PCM"),
    )
    if hasattr(cli, "supervised_text_turn"):
        monkeypatch.setattr(
            cli,
            "supervised_text_turn",
            lambda *a, **k: pytest.fail("talk must not send traffic as input_text"),
        )
    monkeypatch.setattr(cli, "preflight", lambda *a, **k: pytest.fail("preflight unexpected"))

    assert (
        cli.main(
            [
                "-c",
                str(config_path),
                "--no-env-file",
                "talk",
                str(wav_path),
                "--once",
            ]
        )
        == 0
    )
    assert order == ["stt", "commit"]
    assert called.get("committed_after_stt") is True
    assert called["ptt"] == "DryPTT"
    assert called["frames"] > 0
    printed = capsys.readouterr().out
    assert "Accepted (follow-up)" in printed
    assert "live-streamed; local STT used for shutdown gate only" in printed
    # STT text may be logged as transcript, but must not be treated as agent traffic input.
    assert "Traffic: what about rain" not in printed


def test_talk_in_window_armed_code_confirms_without_commit(monkeypatch, tmp_path, capsys):
    """Armed + follow-up open + code utterance → confirmed; zero commit_and_respond."""
    import time as time_mod

    monkeypatch.delenv("XAI_API_KEY", raising=False)
    wav_path, _ = pcm_wav(tmp_path / "code.wav", rate=24000, samples=12000)
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["agent"]["backend"] = "grok_realtime"
    data["listening"]["mode"] = "conversation"
    data["listening"]["conversation_timeout_seconds"] = 60
    data["wake"]["primary"] = "charlotte"
    data["wake"]["aliases"] = []
    data["shutdown"]["enabled"] = True
    data["shutdown"]["phrase"] = "bird"
    data["shutdown"]["phrase_aliases"] = []
    data["shutdown"]["code"] = "seven"
    data["shutdown"]["code_aliases"] = ["7"]
    data["shutdown"]["confirmation_seconds"] = 30
    data["shutdown"]["arm_confirmation_phrase"] = "Armed."
    data["shutdown"]["confirmation_phrase"] = "Shutting down."
    config_path = write_config(tmp_path, data)
    cfg = load_config(config_path)

    order: list[str] = []
    called: dict = {"commits": 0, "clears": 0}

    def transcribe(pcm, rate):
        order.append("stt")
        return "seven"

    listener = type("L", (), {})()
    listener.label = lambda self=None: "fake-stt"
    listener.prepare = lambda self=None: None
    listener.transcribe = transcribe

    class FakeSession:
        def __init__(self, config, **kwargs):
            pass

        def warm(self, *, instructions=None):
            pass

        def on_frame(self, pcm, rate=None):
            pass

        def on_reset(self):
            pass

        def clear_input(self):
            order.append("clear")
            called["clears"] += 1

        def commit_and_respond(self, ptt, *, allow_key, play_segment=None):
            order.append("commit")
            called["commits"] += 1
            pytest.fail("commit_and_respond must not run while shutdown is armed")

        def close(self):
            pass

    gate = ListeningSession(cfg, clock=time_mod.monotonic)
    gate.awake_until = time_mod.monotonic() + 3600

    from walkietalk.shutdown import ShutdownSession

    shutdown = ShutdownSession(cfg, clock=time_mod.monotonic)
    assert shutdown.decide("bird", time_mod.monotonic()).kind == "armed"
    assert shutdown.is_armed is True

    monkeypatch.setattr(cli, "open_stt", lambda config: listener)
    monkeypatch.setattr(cli, "RealtimeTalkSession", FakeSession)
    monkeypatch.setattr(cli, "ListeningSession", lambda config: gate)
    monkeypatch.setattr(cli, "ShutdownSession", lambda config, clock=None: shutdown)
    monkeypatch.setattr(cli, "open_agent", lambda *a, **k: pytest.fail("open_agent must not run"))
    monkeypatch.setattr(cli, "preflight", lambda *a, **k: pytest.fail("preflight unexpected"))
    if hasattr(cli, "supervised_text_turn"):
        monkeypatch.setattr(
            cli,
            "supervised_text_turn",
            lambda *a, **k: pytest.fail("must not send code as input_text"),
        )

    assert (
        cli.main(
            [
                "-c",
                str(config_path),
                "--no-env-file",
                "talk",
                str(wav_path),
                "--once",
            ]
        )
        == 0
    )
    assert called["commits"] == 0
    assert "commit" not in order
    assert order == ["stt", "clear"]
    assert called["clears"] >= 1
    assert shutdown.is_armed is False
    printed = capsys.readouterr().out
    assert "Shutdown confirmed" in printed
    assert "Accepted (follow-up)" not in printed


def test_talk_in_window_phrase_arms_without_commit(monkeypatch, tmp_path, capsys):
    """In-window shutdown phrase arms and clears input; never commit_and_respond."""
    import time as time_mod

    monkeypatch.delenv("XAI_API_KEY", raising=False)
    wav_path, _ = pcm_wav(tmp_path / "phrase.wav", rate=24000, samples=12000)
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["agent"]["backend"] = "grok_realtime"
    data["listening"]["mode"] = "conversation"
    data["listening"]["conversation_timeout_seconds"] = 60
    data["wake"]["primary"] = "charlotte"
    data["wake"]["aliases"] = []
    data["shutdown"]["enabled"] = True
    data["shutdown"]["phrase"] = "bird"
    data["shutdown"]["phrase_aliases"] = []
    data["shutdown"]["code"] = "seven"
    data["shutdown"]["code_aliases"] = []
    data["shutdown"]["confirmation_seconds"] = 30
    data["shutdown"]["arm_confirmation_phrase"] = "Armed."
    data["shutdown"]["confirmation_phrase"] = "Shutting down."
    config_path = write_config(tmp_path, data)

    order: list[str] = []
    called: dict = {"commits": 0}

    def transcribe(pcm, rate):
        order.append("stt")
        return "bird"

    listener = type("L", (), {})()
    listener.label = lambda self=None: "fake-stt"
    listener.prepare = lambda self=None: None
    listener.transcribe = transcribe

    class FakeSession:
        def __init__(self, config, **kwargs):
            pass

        def warm(self, *, instructions=None):
            pass

        def on_frame(self, pcm, rate=None):
            pass

        def on_reset(self):
            pass

        def clear_input(self):
            order.append("clear")

        def commit_and_respond(self, ptt, *, allow_key, play_segment=None):
            order.append("commit")
            called["commits"] += 1
            pytest.fail("commit_and_respond must not run for shutdown phrase")

        def close(self):
            pass

    gate = ListeningSession(load_config(config_path), clock=time_mod.monotonic)
    gate.awake_until = time_mod.monotonic() + 3600
    shutdown_holder: dict = {}

    real_shutdown = cli.ShutdownSession

    def capture_shutdown(config, clock=None):
        shutdown_holder["s"] = real_shutdown(config, clock=clock or time_mod.monotonic)
        return shutdown_holder["s"]

    monkeypatch.setattr(cli, "open_stt", lambda config: listener)
    monkeypatch.setattr(cli, "RealtimeTalkSession", FakeSession)
    monkeypatch.setattr(cli, "ListeningSession", lambda config: gate)
    monkeypatch.setattr(cli, "ShutdownSession", capture_shutdown)
    monkeypatch.setattr(cli, "open_agent", lambda *a, **k: pytest.fail("open_agent must not run"))
    monkeypatch.setattr(cli, "preflight", lambda *a, **k: pytest.fail("preflight unexpected"))

    assert (
        cli.main(
            [
                "-c",
                str(config_path),
                "--no-env-file",
                "talk",
                str(wav_path),
                "--once",
            ]
        )
        == 0
    )
    assert called["commits"] == 0
    assert "commit" not in order
    assert order == ["stt", "clear"]
    assert shutdown_holder["s"].is_armed is True
    printed = capsys.readouterr().out
    assert "Shutdown armed" in printed
    assert "Accepted (follow-up)" not in printed


def test_talk_cold_wake_still_gates_on_stt(monkeypatch, tmp_path, capsys):
    """Cold / waiting_for_wake: STT must finish before commit; reject without wake clears input."""
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    wav_path, _ = pcm_wav(tmp_path / "in.wav", rate=24000, samples=12000)
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["agent"]["backend"] = "grok_realtime"
    data["listening"]["mode"] = "conversation"
    data["wake"]["primary"] = "charlotte"
    data["wake"]["aliases"] = []
    data["shutdown"]["enabled"] = False
    config_path = write_config(tmp_path, data)

    order: list[str] = []
    called: dict = {"frames": 0}

    def transcribe(pcm, rate):
        order.append("stt")
        return "hello there"  # no wake phrase

    listener = type("L", (), {})()
    listener.label = lambda self=None: "fake-stt"
    listener.prepare = lambda self=None: None
    listener.transcribe = transcribe

    class FakeSession:
        def __init__(self, config, **kwargs):
            pass

        def warm(self, *, instructions=None):
            pass

        def on_frame(self, pcm, rate=None):
            called["frames"] += 1

        def on_reset(self):
            pass

        def clear_input(self):
            order.append("clear")
            called["cleared"] = True

        def commit_and_respond(self, ptt, *, allow_key, play_segment=None):
            order.append("commit")
            called["committed"] = True
            return SupervisedTxResult(
                reply_wav=Wav(b"\x05\x00" * 8, REALTIME_PCM_RATE, 8 / REALTIME_PCM_RATE),
                input_transcript="x",
                output_transcript="y",
            )

        def close(self):
            pass

    monkeypatch.setattr(cli, "open_stt", lambda config: listener)
    monkeypatch.setattr(cli, "RealtimeTalkSession", FakeSession)
    monkeypatch.setattr(
        cli, "open_agent", lambda *a, **k: pytest.fail("open_agent must not run for realtime talk")
    )
    monkeypatch.setattr(cli, "preflight", lambda *a, **k: pytest.fail("preflight unexpected"))

    assert (
        cli.main(
            [
                "-c",
                str(config_path),
                "--no-env-file",
                "talk",
                str(wav_path),
                "--once",
            ]
        )
        == 0
    )
    assert "stt" in order
    assert "commit" not in order
    assert called.get("cleared") is True
    assert order.index("stt") < order.index("clear")
    printed = capsys.readouterr().out
    assert "Transcribing..." in printed
    assert "Ignored" in printed or "wake" in printed.lower()


def test_talk_cold_wake_accept_commits_after_stt(monkeypatch, tmp_path, capsys):
    """Cold wake with phrase: STT gates accept; then commit once (no second STT wait)."""
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    wav_path, _ = pcm_wav(tmp_path / "in.wav", rate=24000, samples=12000)
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["agent"]["backend"] = "grok_realtime"
    data["listening"]["mode"] = "wake_phrase"
    data["wake"]["primary"] = "charlotte"
    data["wake"]["aliases"] = []
    data["shutdown"]["enabled"] = False
    config_path = write_config(tmp_path, data)

    order: list[str] = []
    stt_calls = {"n": 0}

    def transcribe(pcm, rate):
        stt_calls["n"] += 1
        order.append("stt")
        return "charlotte what is rain"

    listener = type("L", (), {})()
    listener.label = lambda self=None: "fake-stt"
    listener.prepare = lambda self=None: None
    listener.transcribe = transcribe

    class FakeSession:
        def __init__(self, config, **kwargs):
            pass

        def warm(self, *, instructions=None):
            pass

        def on_frame(self, pcm, rate=None):
            pass

        def on_reset(self):
            pass

        def clear_input(self):
            order.append("clear")

        def commit_and_respond(self, ptt, *, allow_key, play_segment=None):
            order.append("commit")
            return SupervisedTxResult(
                reply_wav=Wav(b"\x05\x00" * 8, REALTIME_PCM_RATE, 8 / REALTIME_PCM_RATE),
                input_transcript="charlotte what is rain",
                output_transcript="clouds",
                ptt_actions=(PttAction("key", "audible_audio_energy"),),
            )

        def close(self):
            pass

    monkeypatch.setattr(cli, "open_stt", lambda config: listener)
    monkeypatch.setattr(cli, "RealtimeTalkSession", FakeSession)
    monkeypatch.setattr(cli, "open_agent", lambda *a, **k: pytest.fail("open_agent must not run"))
    monkeypatch.setattr(cli, "preflight", lambda *a, **k: pytest.fail("preflight unexpected"))

    assert (
        cli.main(
            [
                "-c",
                str(config_path),
                "--no-env-file",
                "talk",
                str(wav_path),
                "--once",
            ]
        )
        == 0
    )
    assert stt_calls["n"] == 1
    assert order == ["stt", "commit"]
    assert "Transcribing..." in capsys.readouterr().out
