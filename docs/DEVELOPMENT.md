# Development and packaging

Use Python 3.11–3.13 on Ubuntu. From the checkout:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
.venv/bin/python -m build
```

For a uv-managed environment without pip, use `uv pip install -e '.[dev]'` and
`.venv/bin/python -m build --installer uv`. Piper is an optional `[piper]` extra.
Do not bump the project version for each teaching phase; it remains `0.1.0`.

## Verify the distributable

A successful editable install does not establish that a wheel contains its
runtime resources. Build, then install in a separate virtual environment:

```bash
INSTALL_CHECK_DIR=$(mktemp -d /tmp/walkietalk-install.XXXXXX)
python3 -m venv "$INSTALL_CHECK_DIR/venv"
"$INSTALL_CHECK_DIR/venv/bin/python" -m pip install dist/walkietalk-0.1.0-py3-none-any.whl
"$INSTALL_CHECK_DIR/venv/bin/python" scripts/check_installation.py
```

Expect `PASS: installed wheel...`. The checker launches the installed package
from a temporary working directory, with a temporary home and no inherited
provider credentials. It exercises help/version, packaged setup files, no-overwrite
behavior, owner-only file permissions, config validation, the offline stub,
simulated PTT, and an expired-login error using a fake executable. It performs no provider request or hardware access.
CI runs this on each supported Python version after lint, formatting, tests, and
the source/wheel build. Installing dependencies itself requires network access
or a populated package cache; the smoke check is offline.

The wheel contains both templates in `walkietalk/data/`. The root
`config.example.yaml` remains the human-readable reference; update both copies
together. A test checks equality and validates the packaged schema. The source
archive also includes docs and demonstration scripts. Neither artifact should
contain real config, credential files, recordings, models, or CLI login data.

## Architecture and extension points

The CLI coordinates capture, STT, wake/shutdown gates, the agent, speech synthesis,
and supervised playback. `agent.py`, `stt.py`, and `tts.py` define independent
backend interfaces and selection. `config.py` owns the exact YAML schema and
validation. `setup.py` owns packaged templates and private credential-file loading.

- An `AgentBackend` returns `reply(user_text, session_context) -> str`.
  `AgentSession` supplies instructions, bounded history, and reply validation.
  New backends must return final answer text only and fail without substitution.
- An STT backend transcribes captured/file audio; it does not decide wake matching
  or invoke an answering agent to guess speech.
- A TTS backend synthesizes final text to the bridge's validated mono PCM16 WAV.
  It does not open playback, serial, or PTT. Provider settings belong to the
  selected backend or remote environment.
- The parent owns PTT; isolated playback prevents blocked audio from stopping
  timeout/unkey handling. Preserve these boundaries when adding integrations.
- A combined voice-agent uses `voice_agent.backend: grok_realtime` (default
  `off`), not a silent fill-in for `stt`/`agent`/`tts`; see
  [GROK-REALTIME-PLAN.md](GROK-REALTIME-PLAN.md).

Add explicit config fields/defaults to the schema and both examples, document
auth/billing and capability limits, and test the adapter's actual failure contract:
bounded deadlines, malformed/oversized results, missing/rejected login, no secret
or diagnostic leakage, no fallback, and no hardware access. Use fake transports
and processes in automated tests; CI must not spend credits or use saved logins.
Record live checks separately before claiming an integration is verified.

## Teaching workflow

Implement one phase per branch and PR. Run appropriate automated checks, then
provide the entire demonstration checklist with commands and expected behavior.
The family explains and observes the result before authorizing commit/push/PR/merge.
Passing one adapter check is not completion of a phase. See [phase records](PHASES.md).

Keep credentials out of Git. Preserve an existing user's device, wake, voice,
gain, and station settings. Use ordinary user permissions, and never turn an
automated installation check into an on-air transmission.
