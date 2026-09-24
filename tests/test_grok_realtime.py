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
    PttAction,
    SupervisedRealtimeTx,
    SupervisedTxResult,
    run_supervised_turn,
    supervised_voice_check,
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
    config = realtime_config(voice_agent_idle_timeout_seconds=60)
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
    config = realtime_config(voice_agent_idle_timeout_seconds=60)
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
    data["voice_agent"]["backend"] = "grok_realtime"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data))

    def fake_offline(config, pcm, rate, **kwargs):
        assert config.voice_agent_backend == "grok_realtime"
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
    assert data["voice_agent"]["backend"] == "off"
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
    data["voice_agent"]["backend"] = "grok_realtime"
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


def test_supervised_keys_on_first_delta_unkeys_on_audio_done():
    config = realtime_config(max_tx_seconds=10, settle_seconds=0)
    ptt = FakePTT()
    played: list[bytes] = []

    def play(pcm, rate, deadline):
        played.append(pcm)

    tx = SupervisedRealtimeTx(config, ptt, allow_key=True, play_segment=play, clock=lambda: 0.0)
    tx.handle(_delta(b"\x01\x00" * 10, first=True))
    assert ptt.actions == ["open", "on"]
    tx.handle(_delta(b"\x02\x00" * 10, first=False))
    assert ptt.keyed is True
    tx.handle(RealtimeEvent(type=OUTPUT_AUDIO_DONE, data={"type": OUTPUT_AUDIO_DONE}))
    assert ptt.actions == ["open", "on", "off"]
    assert played == [b"\x01\x00" * 10 + b"\x02\x00" * 10]
    tx.close()
    assert ptt.actions[-1] == "close"


def test_supervised_unkeys_on_tool_gap_and_rekeys_on_next_first_delta():
    config = realtime_config(max_tx_seconds=10, settle_seconds=0)
    ptt = FakePTT()
    tx = SupervisedRealtimeTx(
        config, ptt, allow_key=True, play_segment=lambda *a: None, clock=lambda: 0.0
    )
    tx.handle(_delta(b"\x01\x00" * 4, first=True))
    tx.handle(
        RealtimeEvent(
            type=FUNCTION_CALL_ARGUMENTS_DONE,
            data={"type": FUNCTION_CALL_ARGUMENTS_DONE, "name": "lookup"},
        )
    )
    assert ptt.actions == ["open", "on", "off"]
    tx.handle(_delta(b"\x03\x00" * 4, first=True))
    assert ptt.actions == ["open", "on", "off", "on"]
    tx.handle(RealtimeEvent(type=OUTPUT_AUDIO_DONE, data={"type": OUTPUT_AUDIO_DONE}))
    assert ptt.actions == ["open", "on", "off", "on", "off"]
    tx.close()


def test_supervised_tx_cap_truncates_and_unkeys():
    config = realtime_config(max_tx_seconds=0.2, settle_seconds=0)
    ptt = FakePTT()
    played: list[int] = []

    def play(pcm, rate, deadline):
        played.append(len(pcm))

    # 0.2s * 24000 * 2 = 9600 bytes max
    tx = SupervisedRealtimeTx(config, ptt, allow_key=True, play_segment=play, clock=lambda: 0.0)
    huge = b"\x01\x00" * 20000
    tx.handle(_delta(huge, first=True))
    assert tx.truncated is True
    assert ptt.actions.count("on") == 1
    assert ptt.actions.count("off") == 1
    assert played and played[0] <= 9600
    tx.close()


def test_supervised_never_keys_without_allow_key():
    config = realtime_config()
    ptt = FakePTT()
    tx = SupervisedRealtimeTx(config, ptt, allow_key=False, play_segment=lambda *a: None)
    tx.handle(_delta(b"\x01\x00" * 8, first=True))
    tx.handle(RealtimeEvent(type=OUTPUT_AUDIO_DONE, data={"type": OUTPUT_AUDIO_DONE}))
    tx.close()
    assert ptt.actions == []
    assert tx.actions == []


def test_supervised_unkeys_on_fail():
    config = realtime_config(settle_seconds=0)
    ptt = FakePTT()
    tx = SupervisedRealtimeTx(config, ptt, allow_key=True, play_segment=lambda *a: None)
    tx.handle(_delta(b"\x01\x00" * 4, first=True))
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
                delta=base64.b64encode(b"\x01\x00" * 16).decode("ascii"),
            ),
            event(
                OUTPUT_AUDIO_DELTA,
                delta=base64.b64encode(b"\x02\x00" * 16).decode("ascii"),
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
                delta=base64.b64encode(b"\x03\x00" * 16).decode("ascii"),
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
    assert result.ptt_actions[0].reason == "first_output_audio_delta"
    assert result.ptt_actions[1].reason == "output_audio.done"
    assert result.ptt_actions[2].reason == "first_output_audio_delta"
    assert result.ptt_actions[3].reason in {"output_audio.done", "response.done"}
    assert ptt.closed is True
    assert result.reply_wav is not None


def test_supervised_cli_dry_ptt_without_transmit(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    wav_path, frames = pcm_wav(tmp_path / "in.wav", rate=24000, samples=12000)
    out_path = tmp_path / "reply.wav"
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["voice_agent"]["backend"] = "grok_realtime"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data))

    def fake_supervised(config, pcm, rate, ptt, **kwargs):
        assert kwargs.get("allow_key") is True
        assert type(ptt).__name__ == "DryPTT"
        return SupervisedTxResult(
            reply_wav=Wav(b"\x05\x00" * 32, REALTIME_PCM_RATE, 32 / REALTIME_PCM_RATE),
            output_transcript="dry reply",
            ptt_actions=(
                PttAction("key", "first_output_audio_delta"),
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
    data["voice_agent"]["backend"] = "grok_realtime"
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
