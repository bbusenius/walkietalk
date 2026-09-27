# Hermes speech

`tts.backend: hermes` speaks the finished answer through the selected Hermes
environment's speech configuration. It does not select Grok, replace the agent
with its underlying model, or ask a second agent to rewrite the answer.

With `agent.backend: hermes`, answers still come from the configured agent
environment, including its instructions, skills, memory, and radio conversation
history. With another agent selected, Hermes can supply just the voice. The
agent model and speech provider are independent choices.

## Companion service

Walkietalk supplies
[`hermes_speech_service.py`](../src/walkietalk/hermes_speech_service.py), a small
standalone companion that runs with **Hermes's Python**, profile, credentials,
and `ffmpeg`. This is Walkietalk's service, not an advertised upstream Hermes
API. It calls Hermes's `text_to_speech_tool(text, output_path)` and
converts the result to mono PCM16 WAV at 48 kHz.

Sources: [Hermes API server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server)
and [Hermes speech configuration](https://hermes-agent.nousresearch.com/docs/user-guide/features/tts).

The environment must explicitly set `tts.provider`. Installed Hermes built-in
providers and configured `type: command` providers are accepted. There is no
Walkietalk list of model vendors or fixed voice. Plugin-only
speech providers are not supported by this companion yet. Required engines,
voices, and packages must be installed in Hermes. If Hermes reports a different
provider (including its Edge-to-NeuTTS fallback), the audio is discarded.

Provider credentials, subscription/API choices, voice, language, speed, and
provider-specific style settings belong in **Hermes's** profile. The bridge
passes only final text and limits. `tts.grok_*` fields do not control Hermes.
An explicitly configured paid Hermes provider uses that provider's billing;
Walkietalk neither supplies a substitute key nor switches providers on failure.

## Start the speech service

Station-ID synthesis requests `truncate: false` so an overlong callsign is
rejected instead of shortened. When upgrading a setup that uses Hermes speech
and callsigns, update the companion `hermes_speech_service.py` and restart the
speech service too. Older services reject this request; Walkietalk reports an
ID synthesis failure and does not count the ID as sent. Ordinary reply requests
remain compatible with older services.

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
using your container name, account, and paths. The example below assumes Hermes
is installed at `/opt/hermes` and its profile is under `/opt/data/home/.hermes`:

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
container recreation. For managed startup, add the companion to your container
configuration with persistent profile storage and an appropriate restart policy.
For a remote host, use HTTPS through a reverse proxy or an SSH tunnel. Do not
send a bearer token over an untrusted network using plain HTTP.

## Walkietalk configuration

Edit these fields in the complete config from `walkietalk init`. Keep the other
`tts` fields (see [config.example.yaml](../src/walkietalk/data/config.example.yaml)):

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
the selected Walkietalk config; Walkietalk loads it automatically. Existing
exported variables take precedence. See [credentials](CONFIGURATION.md#credentials).
`tts.timeout_seconds` bounds the whole client process, including network and
conversion. The service also kills the provider process group at its deadline.
`tts.normalize`, `audio.gain`, settle time, station ID, post-transmit mute, and
PTT watchdogs continue to apply. No speech failure can reach playback as an error
message. Continuous talk discards the unheard agent turn and returns to listening.

## Check the connection

With the companion running and `tts.backend: hermes` selected:

```bash
walkietalk -c "$HOME/.config/walkietalk/config.yaml" tts-check \
  'Hello. This voice comes from my Hermes environment.' --output hermes-check.wav
```

Expect a 48 kHz mono PCM16 WAV and `No hardware opened.` The output path must
be new. A missing token, rejected login, provider mismatch, or timeout fails
without creating playable error audio. Check the companion's process, profile,
URL, and token if synthesis fails.

If the agent also uses Hermes, check it separately with `agent-check`. Follow
[the installation guide](INSTALL.md#5-receive-then-enable-replies-on-air) for
receive and radio playback checks after both connections work.
