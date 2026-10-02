# Hermes speech companion

`[tts] backend = "hermes"` speaks with the TTS provider configured in a
Hermes installation, through `walkietalk-hermes-speech`, a small service that
runs beside Hermes. Hermes keeps its provider, voice, and provider
credentials; walkietalk sends only the final text and the time limit.

The service:

- serves one endpoint, `POST /v1/speech`, protected by a bearer token;
- handles one request at a time (others get HTTP 503);
- limits requests to 16 KiB and 2000 characters, audio to `--max-audio-seconds`,
  and each generation to `--timeout-seconds`;
- runs only the call into Hermes's `text_to_speech_tool` in Hermes's Python, in
  its own process group, which is killed on timeout;
- refuses audio from any provider other than the one explicitly configured
  in Hermes (`tts.provider`, a built-in or `type: command` provider), so a
  silent fallback never reaches the radio;
- converts the result to 48 kHz mono WAV with ffmpeg, cropping replies to the
  requested length and refusing station IDs that would need cropping
  (HTTP 413);
- never logs the text, the token, or provider output.

## Install

On the machine (or in the container) that runs Hermes, with ffmpeg installed:

```bash
cargo install --locked --path crates/walkietalk-hermes-speech
```

For a container whose system libraries differ from the build machine, build
a static binary and copy it in:

```bash
rustup target add x86_64-unknown-linux-musl
cargo build --release --locked --target x86_64-unknown-linux-musl -p walkietalk-hermes-speech
docker cp target/x86_64-unknown-linux-musl/release/walkietalk-hermes-speech YOUR_CONTAINER:/opt/data/walkietalk/
```

## Run

As the user that owns the Hermes profile:

```bash
API_SERVER_KEY="a-token-of-16-or-more-characters" \
  walkietalk-hermes-speech --hermes-root /path/to/hermes
```

- `--python` defaults to `<hermes-root>/.venv/bin/python`.
- `--token-env NAME` reads the token from another variable. If it is not
  exported, the service reads it from the profile's `.env`
  (`$HERMES_HOME/.env`, default `~/.hermes/.env`).
- `--host` (default `127.0.0.1`) and `--port` (default `8643`). In a container,
  use `--host 0.0.0.0` and the container's private address in walkietalk's
  config.
- Stop it with Ctrl+C or SIGTERM.

Docker example:

```bash
docker exec --user hermes -e HOME=/opt/data/home YOUR_CONTAINER \
  /opt/data/walkietalk/walkietalk-hermes-speech --hermes-root /opt/hermes --host 0.0.0.0
```

Do not expose the service on an untrusted network; use an SSH tunnel or a
TLS reverse proxy for remote access.

## Configure walkietalk

```toml
[tts]
backend = "hermes"

[tts.hermes]
url = "http://127.0.0.1:8643"
token_env = "WALKIETALK_HERMES_TOKEN"
```

```toml
# credentials.toml
WALKIETALK_HERMES_TOKEN = "the same token the service uses"
```

`[tts.hermes] url` is the speech service; `[agent.hermes] url` is Hermes's
agent API. They are configured separately and may use different tokens.
`[tts] timeout_seconds`, `peak_normalize`, `[audio] gain`, and every
transmission limit still apply.

```bash
walkietalk tts-check "This voice comes from my Hermes environment." --output hermes.wav
```

A missing token, a rejected token, a provider mismatch, or a timeout fails
without producing any audio.
