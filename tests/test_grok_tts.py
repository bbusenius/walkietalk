import asyncio
import io
import json
import time
import wave
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import yaml

from walkietalk import cli, grok_tts, tts
from walkietalk.capture import Utterance
from walkietalk.config import Config, WalkietalkError, load_config

TOKEN = "test-subscription-token"
KEY = "test-api-key-must-not-be-fallback"


def wav_bytes(samples=4800, rate=48000, channels=1):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x20" * samples)
    return stream.getvalue()


def audio_response(data=None, **kwargs):
    return httpx.Response(
        200,
        content=wav_bytes() if data is None else data,
        headers={"content-type": "audio/wav"},
        **kwargs,
    )


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    monkeypatch.setenv("XAI_API_KEY", KEY)
    (tmp_path / "auth.json").write_text(
        json.dumps(
            {
                "account": {
                    "key": TOKEN,
                    "refresh_token": "test-refresh",
                    "oidc_client_id": "test-client",
                    "expires_at": "2999-01-01T00:00:00Z",
                }
            }
        )
    )
    return replace(Config(), tts_backend="grok", tts_timeout_seconds=1)


@pytest.fixture(autouse=True)
def forbid_hardware_and_fallback(monkeypatch):
    # HTTP unit tests run the worker body in-process with an injected transport.
    monkeypatch.setattr(grok_tts.GrokTts, "synthesize", grok_tts.GrokTts._synthesize_direct)

    def forbidden(*a, **k):
        pytest.fail("Voice adapter used hardware, agent, or Piper fallback")

    for name in ("SerialPTT", "Playback", "transmit", "open_agent", "open_stt"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(tts, "PiperTts", forbidden)


def transport(monkeypatch, handler):
    original = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert kwargs["timeout"] is None
        return original(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(grok_tts.httpx, "AsyncClient", client)


def test_subscription_request_and_real_wav_export(config, monkeypatch, tmp_path, capsys):
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == grok_tts.GROK_TTS_URL
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert KEY not in request.content.decode()
        assert json.loads(request.content) == {
            "text": "Hello.",
            "voice_id": "ara",
            "language": "en",
            "speed": 1.2,
            "text_normalization": False,
            "output_format": {"codec": "wav", "sample_rate": 48000},
        }
        return audio_response()

    transport(monkeypatch, handler)
    config = replace(config, grok_tts_voice="ara", grok_tts_speed=1.2)
    voice = tts.open_tts(config)
    voice.prepare()
    assert not requests  # Startup reads credentials; no network until synthesis.
    assert "SuperGrok saved login" in voice.label()
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    path = tmp_path / "speech.wav"
    assert cli.main(["-c", "unused", "tts-check", "Hello.", "--output", str(path)]) == 0
    assert path.read_bytes() == wav_bytes()
    output = capsys.readouterr()
    assert "No hardware opened" in output.out
    assert TOKEN not in output.out + output.err


def test_api_mode_never_reads_subscription_credentials(config, monkeypatch):
    monkeypatch.setattr(grok_tts, "load_grok_store", lambda: pytest.fail("read subscription"))

    def handler(request):
        assert request.headers["authorization"] == f"Bearer {KEY}"
        return audio_response()

    transport(monkeypatch, handler)
    voice = tts.open_tts(replace(config, tts_backend="grok_api"))
    assert "billed API" in voice.label()
    assert voice.synthesize("Hello.").duration == 0.1


@pytest.mark.parametrize("mode", ["grok", "grok_api"])
def test_missing_credentials_fail_before_network(config, monkeypatch, tmp_path, mode):
    (tmp_path / "auth.json").unlink()
    if mode == "grok_api":
        monkeypatch.delenv("XAI_API_KEY")
    transport(monkeypatch, lambda request: pytest.fail("HTTP without credentials"))
    with pytest.raises(WalkietalkError, match="login|billed"):
        tts.open_tts(replace(config, tts_backend=mode)).synthesize("Hello.")


@pytest.mark.parametrize("status", [301, 400, 403, 404, 429, 500])
def test_http_failures_discard_diagnostics_and_never_fallback(config, monkeypatch, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status, text=TOKEN + KEY, headers={"Location": "https://example.com/"}
        )

    transport(monkeypatch, handler)
    with pytest.raises(WalkietalkError, match=f"HTTP {status}") as error:
        tts.open_tts(config).synthesize("Hello.")
    assert TOKEN not in str(error.value) and KEY not in str(error.value)
    assert len(requests) == 1


def test_401_refreshes_existing_cli_grant_once(config, monkeypatch, tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(401)
        if len(requests) == 2:
            assert str(request.url) == grok_tts.GROK_TOKEN_URL
            assert "authorization" not in request.headers
            assert b"grant_type=refresh_token" in request.content
            return httpx.Response(
                200,
                json={
                    "access_token": "new-token",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                },
            )
        assert request.headers["authorization"] == "Bearer new-token"
        return audio_response()

    transport(monkeypatch, handler)
    assert tts.open_tts(config).synthesize("Hello.").duration == 0.1
    assert len(requests) == 3
    saved = json.loads((tmp_path / "auth.json").read_text())["account"]
    assert saved["key"] == "new-token" and saved["refresh_token"] == "new-refresh"
    assert (tmp_path / "auth.json").stat().st_mode & 0o777 == 0o600


def test_expired_login_refreshes_before_sending_text_and_retry_is_bounded(
    config, monkeypatch, tmp_path
):
    path = tmp_path / "auth.json"
    data = json.loads(path.read_text())
    data["account"]["expires_at"] = "2000-01-01T00:00:00Z"
    path.write_text(json.dumps(data))
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            assert str(request.url) == grok_tts.GROK_TOKEN_URL
            return httpx.Response(200, json={"access_token": "new-token", "expires_in": 3600})
        return httpx.Response(401, text=TOKEN)

    transport(monkeypatch, handler)
    with pytest.raises(WalkietalkError, match="HTTP 401"):
        tts.open_tts(config).synthesize("Hello.")
    assert len(requests) == 2


@pytest.mark.parametrize(
    "payload",
    [{}, [], {"access_token": "\nsecret"}, {"access_token": "new", "expires_in": 10**400}],
)
def test_invalid_refresh_data_is_not_saved(config, monkeypatch, tmp_path, payload):
    path = tmp_path / "auth.json"
    before = path.read_bytes()

    def handler(request):
        if str(request.url) == grok_tts.GROK_TTS_URL:
            return httpx.Response(401)
        return httpx.Response(200, json=payload)

    transport(monkeypatch, handler)
    with pytest.raises(WalkietalkError, match="invalid data"):
        tts.open_tts(config).synthesize("Hello.")
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b'{"error":"private"}',
        wav_bytes()[:-2],
        wav_bytes(channels=2),
        wav_bytes(rate=44100),
        wav_bytes(samples=48000 * 40),
        b"x" * (tts.MAX_TTS_BYTES + 1),
    ],
)
def test_bad_or_overlong_audio_rejected(config, monkeypatch, data):
    transport(monkeypatch, lambda request: audio_response(data))
    with pytest.raises(WalkietalkError):
        tts.open_tts(config).synthesize("Hello.")


def test_grok_can_reject_overlong_station_id_instead_of_cropping(config, monkeypatch):
    transport(monkeypatch, lambda request: audio_response(wav_bytes(samples=48000)))
    voice = tts.open_tts(replace(config, max_tx_seconds=1, settle_seconds=0.2))
    assert voice.synthesize("Answer.").duration == 0.8
    with pytest.raises(WalkietalkError, match="maximum"):
        voice.synthesize("TEST1ID", truncate=False)


def test_json_content_type_rejected(config, monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, json={"diagnostic": TOKEN}))
    with pytest.raises(WalkietalkError, match="no WAV"):
        tts.open_tts(config).synthesize("Hello.")


def test_timeout_in_headers(config, monkeypatch):
    async def handler(request):
        await asyncio.sleep(1)
        return audio_response()

    transport(monkeypatch, handler)
    started = time.monotonic()
    with pytest.raises(WalkietalkError, match="timed out"):
        tts.open_tts(replace(config, tts_timeout_seconds=0.03)).synthesize("Hello.")
    assert time.monotonic() - started < 0.5


def test_whole_body_deadline_cancels_slow_drip(config, monkeypatch):
    closed = []

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(0.01)
                yield b"a"

        async def aclose(self):
            closed.append(True)

    transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, stream=SlowStream(), headers={"content-type": "audio/wav"}
        ),
    )
    with pytest.raises(WalkietalkError, match="timed out"):
        tts.open_tts(replace(config, tts_timeout_seconds=0.03)).synthesize("Hello.")
    assert closed == [True]


def test_refresh_and_synthesis_share_deadline(config, monkeypatch):
    async def handler(request):
        await asyncio.sleep(0.02)
        if str(request.url) == grok_tts.GROK_TOKEN_URL:
            return httpx.Response(200, json={"access_token": "new-token"})
        return httpx.Response(401)

    transport(monkeypatch, handler)
    with pytest.raises(WalkietalkError, match="timed out"):
        tts.open_tts(replace(config, tts_timeout_seconds=0.03)).synthesize("Hello.")


def test_late_audio_after_decode_is_discarded(config, monkeypatch):
    transport(monkeypatch, lambda request: audio_response())
    original = grok_tts.radio_wav

    def late(wav, maximum, **kwargs):
        result = original(wav, maximum, **kwargs)
        monkeypatch.setattr(grok_tts.time, "monotonic", lambda: float("inf"))
        return result

    monkeypatch.setattr(grok_tts, "radio_wav", late)
    with pytest.raises(WalkietalkError, match="timed out"):
        tts.open_tts(config).synthesize("Hello.")


@pytest.mark.parametrize("text", ["", "x" * 601, "\x1b[31mred"])
def test_invalid_text_never_leaves_machine(config, monkeypatch, text):
    transport(monkeypatch, lambda request: pytest.fail("sent invalid text"))
    with pytest.raises(WalkietalkError):
        tts.open_tts(config).synthesize(text)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("grok_voice", ""),
        ("grok_voice", "bad\nvoice"),
        ("grok_language", ""),
        ("grok_speed", 0.69),
        ("grok_speed", 1.51),
        ("grok_speed", True),
        ("grok_api_key_env", "key with spaces"),
    ],
)
def test_config_fields_validated(tmp_path, field, value):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["tts"][field] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WalkietalkError, match="tts"):
        load_config(path)


def test_grok_failure_in_talk_never_opens_hardware_or_prints_reply(config, monkeypatch, capsys):
    transport(monkeypatch, lambda request: httpx.Response(403, text=TOKEN))
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    listener = Mock()
    listener.transcribe.return_value = "charlotte hello"
    agent = Mock()
    agent.reply.return_value = "Hello."
    monkeypatch.setattr(cli, "open_agent", lambda config: agent)
    monkeypatch.setattr(cli, "open_stt", lambda config: listener)
    monkeypatch.setattr(cli, "preflight", lambda *a, **k: None)
    monkeypatch.setattr(
        cli,
        "capture_from_device",
        lambda *a, **k: Utterance(b"\x00\x40" * 320, 16000, 0.5, 0.5, 1, "silence", 100),
    )
    assert cli.main(["-c", "unused", "talk", "--capture", "--transmit", "--once"]) == 1
    output = capsys.readouterr()
    assert "Reply:" not in output.out
    assert TOKEN not in output.err


def test_xai_streamed_wav_header_uses_actual_body_length(config, monkeypatch):
    import struct

    audio = bytearray(wav_bytes())
    struct.pack_into("<I", audio, 4, 0x80000023)
    struct.pack_into("<I", audio, 40, 0x7FFFFFFF)
    transport(monkeypatch, lambda request: audio_response(bytes(audio)))
    speech = tts.open_tts(config).synthesize("Hello.")
    assert speech.duration == 0.1 and speech.rate == 48000
    assert speech.frames == b"\x00\x20" * 4800
    with pytest.raises(WalkietalkError, match="truncated PCM"):
        grok_tts.complete_wav_header(bytes(audio[:-1]))


def test_stt_reloads_login_rotated_by_tts(config, monkeypatch, tmp_path):
    from walkietalk.stt import GrokAccountStt

    listener = GrokAccountStt(1)
    listener.prepare()
    assert listener._session["key"] == TOKEN
    path = tmp_path / "auth.json"
    state = json.loads(path.read_text())
    state["account"].update(key="tts-rotated-access", refresh_token="tts-rotated-refresh")
    path.write_text(json.dumps(state))
    seen = []

    def transcribe(wav, token, timeout, keyterms, max_response_bytes):
        seen.append(token)
        return "Hello."

    monkeypatch.setattr("walkietalk.stt.post_grok_stt", transcribe)
    monkeypatch.setattr(GrokAccountStt, "transcribe", GrokAccountStt._transcribe_direct)
    assert listener.transcribe(b"\x00\x20" * 320, 16000) == "Hello."
    assert seen == ["tts-rotated-access"]
