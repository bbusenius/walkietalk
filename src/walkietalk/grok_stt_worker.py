"""Isolate Grok STT network work so the parent can enforce a total deadline."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

from .agent_process import run_cli
from .config import WalkietalkError
from .stt import GrokAccountStt, GrokApiStt, _grok_remaining, _read_grok_body


def transcribe_in_worker(listener: GrokAccountStt | GrokApiStt, pcm: bytes, rate: int) -> str:
    deadline = time.monotonic() + listener.timeout
    api = isinstance(listener, GrokApiStt)
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"HOME", "PATH", "LANG", "LC_ALL", "GROK_HOME"}
    }
    if api and listener.token_env in os.environ:
        env[listener.token_env] = os.environ[listener.token_env]
    request = {
        "api": api,
        "token_env": listener.token_env if api else None,
        "timeout": listener.timeout,
        "deadline": deadline,
        "max_response_bytes": listener.max_response_bytes,
        "keyterms": listener.keyterms,
        "rate": rate,
    }
    with tempfile.TemporaryDirectory(prefix="walkietalk-grok-stt-") as directory:
        audio = Path(directory) / "audio.pcm"
        transcript = Path(directory) / "transcript.txt"
        audio.write_bytes(pcm)
        code, stdout, _ = run_cli(
            [sys.executable, "-m", "walkietalk.grok_stt_worker", str(audio), str(transcript)],
            prompt=json.dumps(request).encode(),
            cwd=directory,
            env=env,
            deadline=deadline,
            final_path=transcript,
            max_final_bytes=listener.max_response_bytes,
            name="Grok speech-to-text",
        )
        _grok_remaining(deadline)
        if code:
            try:
                error = json.loads(stdout)["error"]
                if not isinstance(error, str) or len(error) > 2000 or not error.isprintable():
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                error = "Grok speech-to-text worker failed; transcript discarded"
            raise WalkietalkError(error)
        try:
            with transcript.open("rb") as stream:
                text = _read_grok_body(stream, listener.max_response_bytes, deadline).decode(
                    "utf-8"
                )
        except (OSError, UnicodeError):
            raise WalkietalkError("Grok speech-to-text returned no valid transcript") from None
        _grok_remaining(deadline)
        return text


def main() -> int:
    """Private worker; no credentials in arguments and no access to PTT."""
    try:
        request = json.loads(sys.stdin.buffer.read(65536))
        options = {
            "timeout": request["timeout"],
            "keyterms": tuple(request["keyterms"]),
            "max_response_bytes": request["max_response_bytes"],
        }
        listener = (
            GrokApiStt(token_env=request["token_env"], **options)
            if request["api"]
            else GrokAccountStt(**options)
        )
        _grok_remaining(request["deadline"])
        pcm = Path(sys.argv[1]).read_bytes()
        text = listener._transcribe_direct(pcm, request["rate"], deadline=request["deadline"])
        _grok_remaining(request["deadline"])
        Path(sys.argv[2]).write_text(text, encoding="utf-8")
        return 0
    except (WalkietalkError, OSError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    except (ValueError, KeyError, TypeError, OverflowError, RecursionError):
        print(json.dumps({"error": "Grok speech-to-text worker returned invalid data"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
