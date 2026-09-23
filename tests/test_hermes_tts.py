import asyncio
import io
import json
import sys
import threading
import time
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import yaml

from walkietalk import cli, hermes_tts, tts
from walkietalk import hermes_speech_service as service
from walkietalk.config import Config, WalkietalkError, load_config


def audio_bytes(frames=4800):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        audio.writeframes(b"\x00\x20" * frames)
    return stream.getvalue()


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("WALKIETALK_HERMES_TOKEN", "local-service-token")
    for name in ("SerialPTT", "Playback", "transmit", "open_agent", "open_stt"):
        monkeypatch.setattr(cli, name, Mock(side_effect=AssertionError("Hardware or agent used")))
    for name in ("PiperTts",):
        monkeypatch.setattr(tts, name, Mock(side_effect=AssertionError("Fallback used")))
    return replace(Config(), tts_backend="hermes", tts_timeout_seconds=1)


def fake_http(monkeypatch, handler):
    original = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs == dict(trust_env=False, follow_redirects=False, timeout=None)
        return original(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(hermes_tts.httpx, "AsyncClient", client)


def test_speech_sends_final_text_only_to_independent_environment(config, monkeypatch):
    def handler(request):
        assert str(request.url) == "http://voice.test/profile/v1/audio/speech"
        assert request.headers["authorization"] == "Bearer local-service-token"
        assert json.loads(request.content) == dict(
            text="Charlotte remembers our lesson.", timeout_seconds=1, max_audio_seconds=9.8
        )
        return httpx.Response(200, content=audio_bytes(), headers={"content-type": "audio/wav"})

    fake_http(monkeypatch, handler)
    voice = tts.open_tts(replace(config, hermes_tts_url="http://voice.test/profile"))
    result = voice._synthesize_direct("Charlotte remembers our lesson.")
    assert result.rate == 48000 and result.duration == 0.1


@pytest.mark.parametrize("status", [301, 401, 403, 404, 429, 500, 502, 503, 504])
def test_errors_never_become_audio_or_leak_diagnostics(config, monkeypatch, status):
    fake_http(
        monkeypatch, lambda req: httpx.Response(status, text="secret-token private diagnostic")
    )
    with pytest.raises(WalkietalkError) as error:
        hermes_tts.HermesTts(config)._synthesize_direct("Hello.")
    assert "secret-token" not in str(error.value)


@pytest.mark.parametrize(
    "body,kind",
    [
        (b"", "audio/wav"),
        (b"not audio", "audio/wav"),
        (b"{}", "application/json"),
        (b"x" * 1000000, "audio/wav"),
    ],
)
def test_invalid_or_oversized_audio(config, monkeypatch, body, kind):
    fake_http(
        monkeypatch, lambda req: httpx.Response(200, content=body, headers={"content-type": kind})
    )
    with pytest.raises(WalkietalkError):
        hermes_tts.HermesTts(config)._synthesize_direct("Hello.")


def test_http_deadline(config, monkeypatch):
    async def blocked(request):
        await asyncio.sleep(10)

    fake_http(monkeypatch, blocked)
    with pytest.raises(WalkietalkError, match="timed out"):
        hermes_tts.HermesTts(replace(config, tts_timeout_seconds=0.02))._synthesize_direct("Hello.")


def test_missing_token_fails_before_network(config, monkeypatch):
    monkeypatch.delenv("WALKIETALK_HERMES_TOKEN")
    monkeypatch.setattr(hermes_tts, "run_cli", Mock(side_effect=AssertionError("Worker started")))
    with pytest.raises(WalkietalkError, match="bearer token"):
        hermes_tts.HermesTts(config).synthesize("Hello.")


@pytest.mark.parametrize("text", ["", "x" * 601, "Hello\x00"])
def test_bad_text_fails_before_worker(config, monkeypatch, text):
    monkeypatch.setattr(hermes_tts, "run_cli", Mock(side_effect=AssertionError("Worker started")))
    with pytest.raises(WalkietalkError):
        hermes_tts.HermesTts(config).synthesize(text)


@pytest.mark.parametrize("blocked", [False, True])
@pytest.mark.parametrize("truncate", [False, True])
def test_parent_deadline_and_environment_isolation(config, monkeypatch, blocked, truncate):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-inherit")
    original = hermes_tts.run_cli
    program = f"""
import os,time
from walkietalk import hermes_tts
from walkietalk.audio import Wav
assert 'ANTHROPIC_API_KEY' not in os.environ
assert os.environ['WALKIETALK_HERMES_TOKEN'] == 'local-service-token'
def synthesize(self,text, *, truncate=True):
    assert truncate == {truncate}
    if {blocked}: time.sleep(30)
    return Wav(b'\\x00\\x20'*4800,48000,.1)
hermes_tts.HermesTts._synthesize_direct=synthesize
raise SystemExit(hermes_tts.main())
"""

    def run(command, **kwargs):
        assert "local-service-token" not in str(command) + kwargs["prompt"].decode()
        return original([sys.executable, "-c", program, command[-1]], **kwargs)

    monkeypatch.setattr(hermes_tts, "run_cli", run)
    start = time.monotonic()
    if blocked:
        with pytest.raises(WalkietalkError, match="timed out"):
            hermes_tts.HermesTts(config).synthesize("Hello.", truncate=truncate)
    else:
        assert hermes_tts.HermesTts(config).synthesize("Hello.", truncate=truncate).duration == 0.1
    assert time.monotonic() - start < 2.5


@pytest.mark.parametrize("url", ["https://voice.test/p/one", "http://127.0.0.1:8643"])
def test_config_keeps_agent_and_voice_targets_independent(tmp_path, url):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["tts"].update(backend="hermes", hermes_url=url, hermes_token_env="VOICE_TOKEN")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    config = load_config(path)
    assert config.hermes_tts_url == url and config.hermes_tts_token_env == "VOICE_TOKEN"
    assert config.hermes_url == data["agent"]["hermes_url"]
    assert config.hermes_token_env == data["agent"]["hermes_token_env"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("hermes_url", "http://user:secret@host"),
        ("hermes_url", "file:///tmp"),
        ("hermes_url", "http://x:bad"),
        ("hermes_token_env", "actual secret"),
    ],
)
def test_bad_config(tmp_path, field, value):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["tts"][field] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WalkietalkError, match="tts.hermes"):
        load_config(path)


def request_data(**kwargs):
    return dict(text="Finished agent answer.", timeout_seconds=1, max_audio_seconds=2, **kwargs)


def call_service(monkeypatch, payload=None, token="Bearer local-service-token", generate=None):
    handler = object.__new__(service.SpeechHandler)
    handler.server = SimpleNamespace(
        token="local-service-token",
        max_seconds=120,
        hermes_root="/hermes",
        busy=threading.Lock(),
        stopping=threading.Event(),
    )
    from email.message import Message

    handler.headers = Message()
    body = json.dumps(request_data() if payload is None else payload).encode()
    for k, v in {
        "Authorization": token,
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }.items():
        handler.headers[k] = v
    handler.path = "/v1/audio/speech"
    handler.rfile = io.BytesIO(body)
    handler.respond = Mock()
    monkeypatch.setattr(service, "generate", generate or Mock(return_value=audio_bytes()))
    handler.do_POST()
    return handler.respond.call_args.args


def test_service_auth_before_provider(monkeypatch):
    generate = Mock(side_effect=AssertionError("Unauthenticated generation"))
    assert call_service(monkeypatch, token="wrong", generate=generate)[0] == 401


def test_service_audio_and_sanitized_failure(monkeypatch):
    assert call_service(monkeypatch) == (200, audio_bytes(), "audio/wav")
    assert (
        call_service(monkeypatch, generate=Mock(side_effect=ValueError("provider-secret")))[0]
        == 502
    )
    assert call_service(monkeypatch, generate=Mock(side_effect=TimeoutError()))[0] == 504


@pytest.mark.parametrize(
    "change",
    [
        dict(text="a\x00b"),
        dict(text="x" * 2001),
        dict(timeout_seconds=True),
        dict(max_audio_seconds=float("nan")),
        dict(provider="xai"),
        dict(timeout_seconds=121),
        dict(truncate="false"),
    ],
)
def test_service_rejects_bad_limits_and_provider_override(monkeypatch, change):
    payload = request_data()
    payload.update(change)
    assert call_service(monkeypatch, payload=payload)[0] == 400


@pytest.mark.parametrize("provider", ["xai", "openai", "piper", "my-local-voice"])
@pytest.mark.parametrize("truncate", [True, False])
def test_service_honors_environment_provider_without_agent_call(
    tmp_path, monkeypatch, provider, truncate
):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"fake provider audio")
    tool = Mock(
        return_value=json.dumps(dict(success=True, provider=provider, file_path=str(source)))
    )
    config = {"tts": {"provider": provider, "providers": {"my-local-voice": {"type": "command"}}}}
    monkeypatch.setitem(
        sys.modules, "hermes_cli.config", SimpleNamespace(load_config=lambda: config)
    )
    monkeypatch.setitem(
        sys.modules,
        "tools.tts_tool",
        SimpleNamespace(
            BUILTIN_TTS_PROVIDERS={"xai", "openai", "piper", "edge"}, text_to_speech_tool=tool
        ),
    )
    conversion = Mock()
    monkeypatch.setattr(service.subprocess, "run", conversion)
    service.synthesize_in_environment(request_data(truncate=truncate), tmp_path)
    tool.assert_called_once_with("Finished agent answer.", str(source))
    assert conversion.call_args.args[0][0] == "ffmpeg"
    assert "-t" in conversion.call_args.args[0]
    args = conversion.call_args.args[0]
    assert float(args[args.index("-t") + 1]) == (2 if truncate else 96001 / 48000)
    tool.return_value = json.dumps(
        dict(success=True, provider="substituted", file_path=str(source))
    )
    with pytest.raises(service.SpeechFailure, match="no fallback"):
        service.synthesize_in_environment(request_data(), tmp_path)


@pytest.mark.parametrize(
    "scenario", ["success", "blocked", "failed", "oversized", "empty", "one_extra_sample"]
)
def test_service_bounds_actual_provider_worker(tmp_path, monkeypatch, scenario):
    original = service.subprocess.Popen
    processes = []
    code = """
import sys,time,wave
from pathlib import Path
request = Path(sys.argv[1])
if SCENARIO == 'blocked': time.sleep(30)
if SCENARIO == 'failed': raise SystemExit(1)
with wave.open(str(request.parent/'speech.wav'),'wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
    frames = 0 if SCENARIO == 'empty' else 48000 * (3 if SCENARIO == 'oversized' else 1)
    if SCENARIO == 'one_extra_sample': frames = 96001
    w.writeframes(b'\\x00\\x20' * frames)
""".replace("SCENARIO", repr(scenario))

    def start(command, **kwargs):
        process = original([sys.executable, "-c", code, command[-1]], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(service.subprocess, "Popen", start)
    request = request_data()
    request["timeout_seconds"] = 0.2
    if scenario == "success":
        result = service.generate(request, "/unused")
        assert result == audio_bytes(48000)
    else:
        error = TimeoutError if scenario == "blocked" else service.SpeechFailure
        with pytest.raises(error):
            service.generate(request, "/unused")
    assert all(p.poll() is not None for p in processes)


def test_service_rejects_implicit_provider_and_file_escape(tmp_path, monkeypatch):
    tool = Mock()
    config = {"tts": {}}
    monkeypatch.setitem(
        sys.modules, "hermes_cli.config", SimpleNamespace(load_config=lambda: config)
    )
    monkeypatch.setitem(
        sys.modules,
        "tools.tts_tool",
        SimpleNamespace(BUILTIN_TTS_PROVIDERS={"edge"}, text_to_speech_tool=tool),
    )
    with pytest.raises(service.SpeechFailure, match="explicit"):
        service.synthesize_in_environment(request_data(), tmp_path)
    tool.assert_not_called()
    config["tts"]["provider"] = "edge"
    tool.return_value = json.dumps(dict(success=True, provider="edge", file_path="/etc/passwd"))
    with pytest.raises(service.SpeechFailure, match="invalid speech file"):
        service.synthesize_in_environment(request_data(), tmp_path)


def test_service_shutdown_cancels_active_worker(monkeypatch):
    original = service.subprocess.Popen
    processes = []

    def start(command, **kwargs):
        p = original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        processes.append(p)
        return p

    monkeypatch.setattr(service.subprocess, "Popen", start)
    stop = threading.Event()
    stop.set()
    with pytest.raises(service.SpeechFailure, match="stopping"):
        service.generate(request_data(), "/unused", stop)
    assert processes[0].poll() is not None


def test_client_applies_radio_duration_cap_and_normalization(config, monkeypatch):
    import numpy as np

    fake_http(
        monkeypatch,
        lambda req: httpx.Response(
            200, content=audio_bytes(48000), headers={"content-type": "audio/wav"}
        ),
    )
    config = replace(config, max_tx_seconds=1, settle_seconds=0.2, tts_normalize="peak")
    result = hermes_tts.HermesTts(config)._synthesize_direct("Hello.")
    assert result.duration == 0.8
    assert np.frombuffer(result.frames, dtype="<i2").max() == 32767
    with pytest.raises(WalkietalkError, match="maximum"):
        hermes_tts.HermesTts(config)._synthesize_direct("TEST1ID", truncate=False)


def test_station_id_requests_strict_audio_from_hermes_service(config, monkeypatch):
    def response(request):
        assert json.loads(request.content)["truncate"] is False
        return httpx.Response(200, content=audio_bytes(), headers={"content-type": "audio/wav"})

    fake_http(monkeypatch, response)
    assert (
        hermes_tts.HermesTts(config)._synthesize_direct("TEST1ID", truncate=False).duration == 0.1
    )
    assert call_service(monkeypatch, payload=request_data(truncate=False))[0] == 200
