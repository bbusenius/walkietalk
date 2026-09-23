import io
import json
import threading
import time
from pathlib import Path
from urllib.error import HTTPError

import pytest
import yaml

from walkietalk import cli
from walkietalk.config import WalkietalkError, load_config
from walkietalk.stt import GROK_STT_URL, GROK_TOKEN_URL, FasterWhisperStt, GrokAccountStt, open_stt


@pytest.fixture
def config_data():
    return yaml.safe_load(Path("config.example.yaml").read_text())


def write_config(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def write_grok_auth(
    tmp_path, monkeypatch, *, key="session-token", expires_at="2099-01-01T00:00:00Z"
):
    home = tmp_path / "grok-home"
    home.mkdir()
    (home / "auth.json").write_text(
        json.dumps(
            {
                "https://auth.x.ai::test": {
                    "key": key,
                    "refresh_token": "refresh-token",
                    "oidc_client_id": "client-id",
                    "expires_at": expires_at,
                }
            }
        )
    )
    monkeypatch.setenv("GROK_HOME", str(home))
    return home


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_open_stt_selects_faster_whisper(tmp_path, config_data):
    config = load_config(write_config(tmp_path, config_data))
    listener = open_stt(config)
    assert listener.label().startswith("faster-whisper")


def test_open_stt_selects_grok_session_backend(tmp_path, config_data, monkeypatch):
    write_grok_auth(tmp_path, monkeypatch)
    config_data["stt"]["backend"] = "grok"
    config = load_config(write_config(tmp_path, config_data))
    listener = open_stt(config)
    assert isinstance(listener, GrokAccountStt)
    listener.prepare()


def test_grok_requires_login_not_api_key(monkeypatch, tmp_path, config_data):
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "empty-grok"))
    monkeypatch.setenv("XAI_API_KEY", "must-not-be-used")
    config_data["stt"]["backend"] = "grok"
    config = load_config(write_config(tmp_path, config_data))
    with pytest.raises(WalkietalkError, match="grok login"):
        open_stt(config).prepare()


def test_grok_posts_wav_with_session_token(monkeypatch, tmp_path, config_data):
    write_grok_auth(tmp_path, monkeypatch, key="session-token")
    config_data["stt"]["backend"] = "grok"
    config = load_config(write_config(tmp_path, config_data))
    seen = {}

    def fake_urlopen(request, timeout=0):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return FakeResponse({"text": "code nine hello"})

    monkeypatch.setattr("walkietalk.stt.urlopen", fake_urlopen)
    monkeypatch.setattr(
        "walkietalk.stt.load_model", lambda name: pytest.fail("fell back to whisper")
    )
    text = open_stt(config).transcribe(b"\x00\x40" * 320, 16000)
    assert text == "code nine hello"
    assert seen["url"] == GROK_STT_URL
    assert seen["auth"] == "Bearer session-token"


def test_grok_refresh_writes_auth_json(monkeypatch, tmp_path, config_data):
    home = write_grok_auth(tmp_path, monkeypatch, expires_at="2000-01-01T00:00:00Z")
    config_data["stt"]["backend"] = "grok"
    config = load_config(write_config(tmp_path, config_data))
    seen = []

    def fake_urlopen(request, timeout=0):
        seen.append((request.full_url, request.get_header("Authorization"), timeout))
        if request.full_url == GROK_TOKEN_URL:
            return FakeResponse(
                {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                }
            )
        return FakeResponse({"text": "refreshed hello"})

    monkeypatch.setattr("walkietalk.stt.urlopen", fake_urlopen)
    text = open_stt(config).transcribe(b"\x00\x40" * 320, 16000)
    assert text == "refreshed hello"
    assert seen[0][0] == GROK_TOKEN_URL
    assert seen[1][1] == "Bearer new-access"
    saved = json.loads((home / "auth.json").read_text())
    session = saved["https://auth.x.ai::test"]
    assert session["key"] == "new-access"
    assert session["refresh_token"] == "new-refresh"
    assert "expires_at" in session


@pytest.mark.parametrize("late_error", [False, True])
def test_whisper_timeout_bounds_work_discards_late_outcome_and_recovers(monkeypatch, late_error):
    listener = FasterWhisperStt("base", timeout=0.05)
    monkeypatch.setattr("walkietalk.stt.load_model", lambda name: object())
    release = threading.Event()
    workers = []
    inputs = []

    def inference(model, pcm, rate):
        workers.append(threading.current_thread())
        inputs.append(pcm)
        if pcm == b"first":
            release.wait()
            if late_error:
                raise WalkietalkError("Late failure")
            return "Late transcript"
        return "Fresh transcript"

    monkeypatch.setattr("walkietalk.stt.transcribe_audio", inference)
    started = time.monotonic()
    try:
        with pytest.raises(WalkietalkError, match="timed out"):
            listener.transcribe(b"first", 16000)
        assert time.monotonic() - started < 1.5
        for _ in range(3):
            with pytest.raises(WalkietalkError, match="still processing.*utterance was skipped"):
                listener.transcribe(b"skipped", 16000)
        assert inputs == [b"first"]
        assert len(workers) == 1
        assert workers[0].is_alive()
    finally:
        release.set()
        # Also covers a worker that was slow to start during a failed test.
        if listener._worker is not None:
            listener._worker.join(timeout=2)

    assert not workers[0].is_alive()
    listener.timeout = 2
    assert listener.transcribe(b"fresh", 16000) == "Fresh transcript"
    assert inputs == [b"first", b"fresh"]


def test_whisper_inference_error_does_not_block_the_next_call(monkeypatch):
    listener = FasterWhisperStt("base", timeout=2)
    monkeypatch.setattr("walkietalk.stt.load_model", lambda name: object())

    def inference(model, pcm, rate):
        if pcm == b"bad":
            raise WalkietalkError("Inference failed")
        return "Fresh transcript"

    monkeypatch.setattr("walkietalk.stt.transcribe_audio", inference)
    with pytest.raises(WalkietalkError, match="Inference failed"):
        listener.transcribe(b"bad", 16000)
    assert listener.transcribe(b"fresh", 16000) == "Fresh transcript"


def test_grok_does_not_use_whisper(monkeypatch, tmp_path, config_data):
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "empty-grok"))
    config_data["stt"]["backend"] = "grok"
    config = load_config(write_config(tmp_path, config_data))
    monkeypatch.setattr(
        "walkietalk.stt.load_model", lambda name: pytest.fail("grok backend loaded faster-whisper")
    )
    with pytest.raises(WalkietalkError, match="grok login"):
        open_stt(config).transcribe(b"\x00\x00", 16000)


def test_grok_api_requires_explicit_key(monkeypatch, tmp_path, config_data):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    config_data["stt"]["backend"] = "grok_api"
    config = load_config(write_config(tmp_path, config_data))
    with pytest.raises(WalkietalkError, match="XAI_API_KEY"):
        open_stt(config).prepare()


def test_grok_api_posts_wav_and_returns_text(monkeypatch, tmp_path, config_data):
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    config_data["stt"]["backend"] = "grok_api"
    config = load_config(write_config(tmp_path, config_data))
    seen = {}

    def fake_urlopen(request, timeout=0):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["timeout"] = timeout
        return FakeResponse({"text": "hello from grok"})

    monkeypatch.setattr("walkietalk.stt.urlopen", fake_urlopen)
    text = open_stt(config).transcribe(b"\x00\x40" * 320, 16000)
    assert text == "hello from grok"
    assert seen["url"] == GROK_STT_URL
    assert seen["auth"] == "Bearer test-key"


def test_grok_api_http_error_does_not_fall_back(monkeypatch, tmp_path, config_data):
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    config_data["stt"]["backend"] = "grok_api"
    config = load_config(write_config(tmp_path, config_data))

    def fail_open(request, timeout=0):
        raise HTTPError(request.full_url, 401, "Unauthorized", hdrs=None, fp=io.BytesIO(b"nope"))

    monkeypatch.setattr("walkietalk.stt.urlopen", fail_open)
    monkeypatch.setattr(
        "walkietalk.stt.load_model", lambda name: pytest.fail("fell back to whisper")
    )
    with pytest.raises(WalkietalkError, match="401"):
        open_stt(config).transcribe(b"\x00\x00", 16000)


def test_listen_grok_without_login_never_opens_ptt(monkeypatch, tmp_path, config_data, capsys):
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "empty-grok"))
    config_data["stt"]["backend"] = "grok"
    config_path = write_config(tmp_path, config_data)

    def forbidden(*args, **kwargs):
        pytest.fail("grok listen opened PTT")

    monkeypatch.setattr(cli, "SerialPTT", forbidden)
    monkeypatch.setattr(cli, "Playback", forbidden)
    monkeypatch.setattr(cli, "preflight", lambda config, **kwargs: 0)
    monkeypatch.setattr(cli, "capture_from_device", forbidden)
    assert cli.main(["-c", str(config_path), "listen", "--capture"]) == 1
    assert "grok login" in capsys.readouterr().err
