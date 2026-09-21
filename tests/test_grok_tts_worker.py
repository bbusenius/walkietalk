import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from walkietalk import grok_tts
from walkietalk.config import Config, WalkietalkError


@pytest.mark.parametrize("scenario", ["success", "blocked", "error", "oversized"])
def test_parent_bounds_whole_worker_and_isolates_secrets(tmp_path, monkeypatch, scenario):
    home = tmp_path / "grok"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"user": {"key": "saved-token"}}))
    monkeypatch.setenv("GROK_HOME", str(home))
    monkeypatch.setenv("XAI_API_KEY", "must-not-leak-into-subscription-worker")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak-either")
    original = grok_tts.run_cli
    calls = []
    program = """
import os,sys,time
from walkietalk import grok_tts
from walkietalk.audio import Wav
from walkietalk.config import WalkietalkError
assert 'XAI_API_KEY' not in os.environ
assert 'ANTHROPIC_API_KEY' not in os.environ
assert os.environ['GROK_HOME']
def synthesize(self, text):
    assert text == 'Hello.'
    if SCENARIO == 'blocked': time.sleep(30)
    if SCENARIO == 'error': raise WalkietalkError('Grok TTS HTTP 403; no fallback')
    count = 48000 * (11 if SCENARIO == 'oversized' else 1)
    return Wav(b'\\x00\\x20' * count, 48000, count / 48000)
grok_tts.GrokTts._synthesize_direct = synthesize
raise SystemExit(grok_tts.main())
""".replace("SCENARIO", repr(scenario))

    def run(command, **kwargs):
        calls.append(kwargs)
        assert "saved-token" not in str(command) + kwargs["prompt"].decode()
        return original([sys.executable, "-c", program, command[-1]], **kwargs)

    monkeypatch.setattr(grok_tts, "run_cli", run)
    config = replace(Config(), tts_backend="grok", tts_timeout_seconds=1)
    started = time.monotonic()
    if scenario == "success":
        speech = grok_tts.GrokTts(config).synthesize("Hello.")
        assert speech.duration == 1 and speech.rate == 48000
    elif scenario == "oversized":
        speech = grok_tts.GrokTts(config).synthesize("Hello.")
        assert speech.duration == pytest.approx(config.max_tx_seconds - config.settle_seconds)
    else:
        message = {"blocked": "timed out", "error": "HTTP 403"}[scenario]
        with pytest.raises(WalkietalkError, match=message):
            grok_tts.GrokTts(config).synthesize("Hello.")
    assert time.monotonic() - started < 2.5
    assert not Path(calls[0]["final_path"]).exists()
