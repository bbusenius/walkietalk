# Development

```bash
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
```

The workspace has two crates: `crates/walkietalk` (the bridge) and
`crates/walkietalk-hermes-speech` (the Hermes speech companion).

## Layout

| Module | Responsibility |
| --- | --- |
| `cli` | Command-line parsing and dispatch; config path and credentials resolution |
| `config` | The TOML schema, defaults, and all validation, including phrase collisions |
| `credentials` | The private credentials file and the `Secret` type |
| `audio` | `Clip` (mono 16-bit audio), WAV I/O, resampling, VAD, capture and playback (cpal), Morse |
| `radio` | The PTT supervisor, `Radio::transmit`, `TransmitConsent`, station-ID planning |
| `gate` | Wake/sleep and shutdown state machines (pure, driven by timestamps) |
| `phrases` | Word-level phrase matching |
| `stt`, `tts`, `agent` | Backend traits and implementations |
| `realtime` | The xAI realtime client, streaming transmitter, and talk session |
| `messaging` | Queues, text chunking, voice-note conversion, WhatsApp and Signal adapters |
| `operator` | Review queue, control socket server and client |
| `panel` | The terminal operator panel |
| `talk` | The main loop |
| `exec`, `http` | Bounded child processes and HTTP bodies |

## Rules for changes

- Only `radio` touches the serial line, and only the PTT supervisor thread
  owns it. Keying always has a deadline. A live `Radio` requires a
  `TransmitConsent`, created only for `--transmit`.
- Prepare audio before keying. Treat PTT errors as fatal.
- Backends return final text or audio, never transmit, and never fall back to
  another backend or credential.
- Every external call is bounded in time and output size (`exec::Job`,
  `http::body`, `http::within`), and child process groups are killed.
- Errors must not contain secrets or raw provider diagnostics.

## Tests

Tests use fakes for hardware and services: `radio::ptt::fake::FakeLine` and
`radio::tests::FakeOut` for the transmitter, local axum servers for HTTP APIs,
a local WebSocket server for the realtime protocol, shell scripts standing in
for CLIs and ffmpeg, and `talk::tests` for end-to-end runs of the loop with
fake audio frames. No test uses real hardware, network services, or
credentials.

Live checks (`agent-check`, `tts-check`, `voice-agent-check`, on-air tests)
are manual.
