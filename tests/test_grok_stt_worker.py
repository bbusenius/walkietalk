import json
import sys
import time
from pathlib import Path

import pytest

from walkietalk import agent_process, grok_stt_worker, stt
from walkietalk.config import WalkietalkError


@pytest.mark.parametrize("api", [False, True])
@pytest.mark.parametrize("scenario", ["success", "headers", "body", "oversized", "large_allowed"])
def test_worker_bounds_network_work_and_recovers(tmp_path, monkeypatch, api, scenario):
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    monkeypatch.setenv("XAI_API_KEY", "unrelated-key")
    monkeypatch.setenv("SELECTED_STT_KEY", "api-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unrelated-secret")
    (tmp_path / "auth.json").write_text(json.dumps({"user": {"key": "saved-token"}}))
    original_run = grok_stt_worker.run_cli
    original_popen = agent_process.subprocess.Popen
    processes = []
    paths = []
    program = r"""
import io, json, os, signal, sys, time
from walkietalk import grok_stt_worker, stt
assert 'XAI_API_KEY' not in os.environ
assert 'ANTHROPIC_API_KEY' not in os.environ
assert ('SELECTED_STT_KEY' in os.environ) == API_MODE
scenario = SCENARIO
signal.signal(signal.SIGTERM, signal.SIG_IGN)
class Response(io.BytesIO):
    def read(self, size):
        if scenario == 'body': time.sleep(30)
        return super().read(size)
def urlopen(request, timeout):
    if scenario == 'headers': time.sleep(30)
    token = 'api-token' if API_MODE else 'saved-token'
    assert request.get_header('Authorization') == 'Bearer ' + token
    assert b'RIFF' in request.data
    assert b'charlotte' in request.data
    count = 1100000 if scenario == 'large_allowed' else 2000 if scenario == 'oversized' else 5
    return Response(json.dumps({'text': 'x' * count}).encode())
stt.urlopen = urlopen
raise SystemExit(grok_stt_worker.main())
""".replace("API_MODE", repr(api))

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    current = [scenario]

    def run(command, **kwargs):
        paths.append(Path(kwargs["final_path"]))
        assert "saved-token" not in str(command) + kwargs["prompt"].decode()
        assert "api-token" not in str(command) + kwargs["prompt"].decode()
        code = program.replace("SCENARIO", repr(current[0]))
        return original_run([sys.executable, "-c", code, *command[-2:]], **kwargs)

    monkeypatch.setattr(agent_process.subprocess, "Popen", popen)
    monkeypatch.setattr(grok_stt_worker, "run_cli", run)
    options = {
        "timeout": 1.5,
        "max_response_bytes": 2000000 if scenario == "large_allowed" else 1000,
        "keyterms": ("charlotte",),
    }
    listener = (
        stt.GrokApiStt(token_env="SELECTED_STT_KEY", **options)
        if api
        else stt.GrokAccountStt(**options)
    )
    started = time.monotonic()
    if scenario in {"success", "large_allowed"}:
        assert listener.transcribe(b"\x00\x00" * 320, 16000) == "x" * (
            1100000 if scenario == "large_allowed" else 5
        )
    else:
        message = "max_response_bytes" if scenario == "oversized" else "timed out"
        with pytest.raises(WalkietalkError, match=message):
            listener.transcribe(b"\x00\x00" * 320, 16000)
    assert time.monotonic() - started < 3.5
    assert all(process.poll() is not None for process in processes)
    assert all(not path.parent.exists() for path in paths)
    if scenario in {"headers", "body", "oversized"}:
        current[0] = "success"
        assert listener.transcribe(b"\x00\x00" * 320, 16000) == "xxxxx"
        assert all(process.poll() is not None for process in processes)


def test_worker_deadline_includes_blocked_login_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(
        json.dumps(
            {
                "user": {
                    "key": "old",
                    "refresh_token": "refresh",
                    "oidc_client_id": "client",
                    "expires_at": "2000-01-01T00:00:00Z",
                }
            }
        )
    )
    original = grok_stt_worker.run_cli
    program = """
import time
from walkietalk import grok_stt_worker, stt
def urlopen(request, timeout):
    assert request.full_url == stt.GROK_TOKEN_URL
    time.sleep(30)
stt.urlopen = urlopen
raise SystemExit(grok_stt_worker.main())
"""

    def run(command, **kwargs):
        return original([sys.executable, "-c", program, *command[-2:]], **kwargs)

    monkeypatch.setattr(grok_stt_worker, "run_cli", run)
    started = time.monotonic()
    with pytest.raises(WalkietalkError, match="timed out"):
        stt.GrokAccountStt(1.5).transcribe(b"\x00\x00" * 320, 16000)
    assert time.monotonic() - started < 3
