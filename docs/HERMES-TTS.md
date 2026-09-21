# Hermes speech

`tts.backend: hermes` speaks the finished answer through the selected Hermes
environment's speech configuration. It does not select Grok, replace the agent
with its underlying model, or ask a second agent to rewrite the answer.

With `agent.backend: hermes`, answers still come from the configured agent
environment, including its instructions, skills, memory, and radio conversation
history. With another agent selected, Hermes can supply just the voice. The
agent model and speech provider are independent choices.

## Status

Implemented after phase 7. Brad confirmed Hermes spoken replies over the walkies
and authorized publication. Merged in [PR #9](https://github.com/bbusenius/walkietalk/pull/9)
as `976b592`. Phase 8 installation work is tracked [separately](PHASE8-DEMO.md).

Automated verification: all 625 tests pass (47 focused Hermes speech tests),
Ruff lint and formatting pass, and the source/wheel build passes. Missing-token
and rejected-token checks also passed against the running service without
creating audio.

The real Hermes agent also retained the imaginary boat name “Acorn” across
two turns; its final answer was synthesized into a 3-second WAV through the
same environment. This tested the complete agent-context-to-speech connection
without radio hardware.

A real WAV was generated through Charlotte's configured Hermes speech provider.
The helper runs as the normal Hermes account, alongside the existing agent;
Charlotte did not need a restart. No radio transmission was performed by the
coding assistant. Automated checks cover other provider selections with fakes;
those checks do not claim live verification of every Hermes provider.

## Why there is a companion service

The inspected Hermes Runs API reports `audio_api: false`; it has no speech
endpoint. Walkietalk therefore supplies
[`hermes_speech_service.py`](../src/walkietalk/hermes_speech_service.py), a small
standalone companion that runs with **Hermes's Python**, profile, credentials,
and `ffmpeg`. This is Walkietalk's service, not an advertised upstream Hermes
API. It calls Hermes's public `text_to_speech_tool(text, output_path)` and
converts the result to mono PCM16 WAV at 48 kHz.

Sources: [Hermes API server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server)
and [Hermes speech configuration](https://hermes-agent.nousresearch.com/docs/user-guide/features/tts).
The installed source was also inspected, and the connection was tested locally.

The environment must explicitly set `tts.provider`. Installed Hermes built-in
providers and configured `type: command` providers are accepted. There is no
Walkietalk list of model vendors and no hardcoded Charlotte voice. Plugin-only
speech providers are not supported by this companion yet. Required engines,
voices, and packages must be installed in Hermes. If Hermes reports a different
provider (including its Edge-to-NeuTTS fallback), the audio is discarded.

Provider credentials, subscription/API choices, voice, language, speed, and
provider-specific style settings belong in **Hermes's** profile. The bridge
passes only final text and limits. `tts.grok_*` fields do not control Hermes.
An explicitly configured paid Hermes provider uses that provider's billing;
Walkietalk neither supplies a substitute key nor switches providers on failure.

## Start the speech service

For Charlotte Docker installations, follow the maintained
[Charlotte Walkietalk setup guide](https://github.com/bbusenius/charlotte/blob/master/runtime/hermes/README.md#walkietalk).
It covers the agent connection and a separate speech container with loopback
publishing and a Docker restart policy. The standalone/manual options below
are also available for other Hermes deployments.

On a native Hermes installation, use its Python interpreter and source path:

```sh
HERMES_HOME="$HOME/.hermes" /path/to/hermes/.venv/bin/python \
  /path/to/walkietalk/src/walkietalk/hermes_speech_service.py \
  --hermes-root /path/to/hermes
```

Run this as the account owning the Hermes profile. The helper loads the profile's
`.env` without overriding existing environment variables. By default it requires
`API_SERVER_KEY`, a printable bearer token of at least 16 characters. You may
reuse the agent API's local token or select a separate service token with
`--token-env NAME`. Neither token is a model-provider credential.

Defaults: loopback port `8643`, one active synthesis request, at most 120 seconds
of generation and 120 seconds of returned audio. `--max-audio-seconds` changes
the service's audio ceiling; the request must also fit that ceiling. Walkietalk
requests its configured transmit budget minus PTT settle time. `ffmpeg` crops
long speech to that budget. Excessive source files, invalid audio, missing
credentials, or failed providers return local errors. Raw upstream diagnostics
are withheld. There is no agent invocation, microphone, playback, or serial/PTT
operation in the service.

For Docker, copy the helper into the profile and run it with the profile owner,
using your container name and paths. This example matches Charlotte's layout:

```sh
docker cp src/walkietalk/hermes_speech_service.py \
  YOUR_CONTAINER:/opt/data/walkietalk/hermes_speech_service.py
docker exec YOUR_CONTAINER chown hermes:hermes \
  /opt/data/walkietalk/hermes_speech_service.py
docker exec --user hermes -e HOME=/opt/data/home YOUR_CONTAINER \
  /opt/hermes/.venv/bin/python /opt/data/walkietalk/hermes_speech_service.py \
  --hermes-root /opt/hermes --host 0.0.0.0
```

Create `/opt/data/walkietalk` as the profile owner first if it does not exist.
This manual command runs in the foreground. Keep it running alongside the agent;
use the container's reachable private address for the client URL. After recreating
a container, rediscover its address and restart the companion. Persistence of
the copied helper depends on the profile volume; its process does not survive
container recreation. For managed startup, prefer Charlotte's separate speech
container instructions linked above.
For a remote host, use HTTPS through a reverse proxy or an SSH tunnel. Do not
send a bearer token over an untrusted network using plain HTTP.

## Walkietalk configuration

Add these two required fields to the existing `tts` section in every exact-field
config. Keep all its other fields (see [config.example.yaml](../config.example.yaml)):

```yaml
tts:
  backend: "hermes"
  hermes_url: "http://127.0.0.1:8643"
  hermes_token_env: "WALKIETALK_HERMES_TOKEN"
  # Keep timeout_seconds, normalize, Piper, and Grok fields as well.
```

`tts.hermes_url` targets the companion, while `agent.hermes_url` targets the
Hermes agent API (normally port 8642). Each has its own token-variable setting,
so the agent and voice can use the same environment or different ones. Tokens
stay outside YAML. Put the named variable in a private `credentials.env` beside
the selected Walkietalk config; phase 8 loads it automatically. Existing exported
variables take precedence. See [credentials](CONFIGURATION.md#credentials).
An existing ignored `.env.hermes.local` can alternatively supply a terminal session:

```sh
set -a
. ./.env.hermes.local
set +a
```

`tts.timeout_seconds` bounds the whole client process, including network and
conversion. The service also kills the provider process group at its deadline.
`tts.normalize`, `audio.gain`, settle time, station ID, post-transmit mute, and
PTT watchdogs continue to apply. No speech failure can reach playback as an error
message. Continuous talk discards the unheard agent turn and returns to listening.

## Complete family demonstration

The prepared ignored `recordings/hermes-demo.yaml` copies Brad's current device,
wake, STT, gain, and phase 7 settings, selecting Hermes for **both** agent and
voice. His regular `config.local.yaml` still selects Grok. If hardware names
have changed, run `walkietalk devices` and update the demo config before starting.
Run from the Walkietalk project directory and load the bearer token as above.

1. **Speech without hardware.**

   ```sh
   .venv/bin/walkietalk -c recordings/hermes-demo.yaml tts-check \
     'Hello. This is the voice chosen in my Hermes environment.' \
     --output recordings/hermes-family.wav
   ```

   Expect `Voice: hermes (environment-configured speech provider and voice)` and
   `Speech WAV: ... 48000 Hz, mono PCM16 ... No hardware opened.` The TX light
   stays off. Use a new output filename if this one already exists.

2. **Real agent and spoken answer.**

   ```sh
   .venv/bin/walkietalk -c recordings/hermes-demo.yaml talk --capture --transmit
   ```

   Say the configured wake phrase and a question relevant to Charlotte. Expect
   Hermes to be listed as both agent and voice, the answer to reflect Charlotte's
   environment, and speech on the receiving walkie. Confirm the first word is
   audible, volume is suitable, and TX releases afterward.

3. **Context and phase 7 protections.** In the same running conversation, ask
   an unaddressed follow-up within the configured window. It should use the prior
   exchange. Confirm post-transmit mute prevents self-triggering and any enabled
   station ID is spoken. Ctrl+C must stop cleanly with TX off. The configured
   shutdown phrase/code should also stop; its optional confirmation uses Hermes.

4. **Independence.** Stop the program, change only `tts.backend` in the demo
   config to `grok` or `piper` (with that voice installed), and run the same talk
   command. The agent remains Hermes/Charlotte while the voice changes. Restore
   `tts.backend: hermes` afterward. Other users' Hermes profiles may choose a
   completely different speech provider without changing Walkietalk's agent.

5. **Local failure, no transmission.** After stopping talk, run:

   ```sh
   env -u WALKIETALK_HERMES_TOKEN .venv/bin/walkietalk \
     -c recordings/hermes-demo.yaml tts-check 'Hello.' \
     --output recordings/hermes-missing-token.wav
   WALKIETALK_HERMES_TOKEN=deliberately-invalid .venv/bin/walkietalk \
     -c recordings/hermes-demo.yaml tts-check 'Hello.' \
     --output recordings/hermes-rejected-token.wav
   .venv/bin/pytest tests/test_hermes_tts.py -q
   ```

   Expect a missing service-token error, then HTTP 401, no WAV files, and TX off.
   The focused automated tests additionally exercise deadlines, oversized/bad
   audio, provider failures, credential isolation, and worker termination. Those
   simulations are automated evidence, not a claim of an observed radio failure.

Explain: “Charlotte still answers using what she knows. Hermes chooses how to
speak her answer, and Walkietalk controls when the radio can transmit.”

Brad has confirmed that the Hermes spoken replies work over the walkies and
authorized publication. The commands above remain available for repeat checks;
automated failure results are not a claim that every failure was induced on air.
