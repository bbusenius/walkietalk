import io
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest
import yaml

from walkietalk import stt
from walkietalk.config import WalkietalkError, load_config


@pytest.fixture
def login(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            {
                "account": {
                    "key": "saved-access",
                    "refresh_token": "saved-refresh",
                    "oidc_client_id": "client",
                }
            }
        )
    )
    return path


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1024", None])
def test_response_limit_rejects_invalid_config(tmp_path, value):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["stt"]["max_response_bytes"] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WalkietalkError, match="stt.max_response_bytes"):
        load_config(path)


@pytest.mark.parametrize("backend", ["grok", "grok_api"])
@pytest.mark.parametrize("limit", [None, 2048, 2 * 1024 * 1024])
def test_response_limit_defaults_and_reaches_both_backends(tmp_path, backend, limit):
    data = yaml.safe_load(Path("config.example.yaml").read_text())
    data["stt"]["backend"] = backend
    if limit is None:
        del data["stt"]["max_response_bytes"]
    else:
        data["stt"]["max_response_bytes"] = limit
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    assert stt.open_stt(load_config(path)).max_response_bytes == (limit or 1048576)


def test_prepare_does_not_refresh_expired_login(login, monkeypatch):
    data = json.loads(login.read_text())
    data["account"]["expires_at"] = "2000-01-01T00:00:00Z"
    login.write_text(json.dumps(data))
    monkeypatch.setattr(stt, "urlopen", lambda *a, **k: pytest.fail("Network at startup"))
    stt.GrokAccountStt(1).prepare()


@pytest.mark.parametrize("status", [200, 500])
def test_slow_response_hits_total_deadline_and_closes(monkeypatch, status):
    now = [100.0]
    monkeypatch.setattr(stt.time, "monotonic", lambda: now[0])

    class SlowResponse(io.BytesIO):
        def read(self, size):
            now[0] += 0.02
            return super().read(min(1, size))

    body = SlowResponse(b'{"text":"late transcript"}')

    def urlopen(request, timeout):
        if status != 200:
            raise HTTPError(request.full_url, status, "Error", hdrs=None, fp=body)
        return body

    monkeypatch.setattr(stt, "urlopen", urlopen)
    with pytest.raises(WalkietalkError, match="timed out"):
        stt.post_grok_stt(b"wav", "token", 0.05)
    assert body.closed
    assert now[0] < 100.1


@pytest.mark.parametrize("request_kind", ["transcript", "http_error", "refresh", "refresh_error"])
def test_all_response_bodies_are_bounded_and_closed(monkeypatch, request_kind):
    body = io.BytesIO(b"x" * 10000)
    sizes = []
    original_read = body.read

    def read(size):
        sizes.append(size)
        return original_read(size)

    body.read = read

    def urlopen(request, timeout):
        if request_kind.endswith("error"):
            raise HTTPError(request.full_url, 500, "Error", hdrs=None, fp=body)
        return body

    monkeypatch.setattr(stt, "urlopen", urlopen)
    with pytest.raises(WalkietalkError, match="stt.max_response_bytes"):
        if request_kind.startswith("refresh"):
            stt.refresh_grok_access_token(
                {"refresh_token": "refresh", "oidc_client_id": "client"}, 1, 100
            )
        else:
            stt.post_grok_stt(b"wav", "token", 1, max_response_bytes=100)
    assert sizes == [101]
    assert body.closed


def test_limit_counts_bytes_and_accepts_exact_boundary(monkeypatch):
    payload = json.dumps({"text": "héllo"}, ensure_ascii=False).encode()
    monkeypatch.setattr(stt, "urlopen", lambda *a, **k: io.BytesIO(payload))
    assert stt.post_grok_stt(b"wav", "token", 1, max_response_bytes=len(payload)) == "héllo"
    with pytest.raises(WalkietalkError, match="max_response_bytes"):
        stt.post_grok_stt(b"wav", "token", 1, max_response_bytes=len(payload) - 1)


@pytest.mark.parametrize("expired", [False, True])
def test_refresh_and_retry_share_deadline_and_persist_rotation(login, monkeypatch, expired):
    now = [100.0]
    monkeypatch.setattr(stt.time, "monotonic", lambda: now[0])
    if expired:
        state = json.loads(login.read_text())
        state["account"]["expires_at"] = "2000-01-01T00:00:00Z"
        login.write_text(json.dumps(state))
    requests = []

    def urlopen(request, timeout):
        requests.append((request.full_url, timeout, request.get_header("Authorization")))
        now[0] += 4
        if request.full_url == stt.GROK_TOKEN_URL:
            return io.BytesIO(b'{"access_token":"new-access","refresh_token":"new-refresh"}')
        if not expired and len(requests) == 1:
            raise HTTPError(request.full_url, 401, "Unauthorized", hdrs=None, fp=io.BytesIO())
        return io.BytesIO(b'{"text":"late"}')

    monkeypatch.setattr(stt, "urlopen", urlopen)
    listener = stt.GrokAccountStt(6 if expired else 10)
    with pytest.raises(WalkietalkError, match="timed out"):
        listener._transcribe_direct(b"\x00\x00" * 320, 16000)
    assert [round(call[1]) for call in requests] == ([6, 2] if expired else [10, 6, 2])
    assert requests[-1][2] == "Bearer new-access"
    saved = json.loads(login.read_text())["account"]
    assert saved["key"] == "new-access" and saved["refresh_token"] == "new-refresh"
    assert login.stat().st_mode & 0o777 == 0o600


def test_account_retries_401_only_once(login, monkeypatch):
    requests = []

    def urlopen(request, timeout):
        requests.append(request.full_url)
        if request.full_url == stt.GROK_TOKEN_URL:
            return io.BytesIO(b'{"access_token":"new-access"}')
        raise HTTPError(request.full_url, 401, "Unauthorized", hdrs=None, fp=io.BytesIO())

    monkeypatch.setattr(stt, "urlopen", urlopen)
    with pytest.raises(WalkietalkError, match="401"):
        stt.GrokAccountStt(1)._transcribe_direct(b"\x00\x00" * 320, 16000)
    assert requests == [stt.GROK_STT_URL, stt.GROK_TOKEN_URL, stt.GROK_STT_URL]


def test_timeout_leaves_backend_usable_for_fresh_audio(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    listener = stt.GrokApiStt(1)
    outcomes = iter([TimeoutError(), io.BytesIO(b'{"text":"fresh"}')])

    def urlopen(request, timeout):
        result = next(outcomes)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(stt, "urlopen", urlopen)
    with pytest.raises(WalkietalkError, match="timed out"):
        listener._transcribe_direct(b"\x00\x00" * 320, 16000)
    assert listener._transcribe_direct(b"\x00\x00" * 320, 16000) == "fresh"
