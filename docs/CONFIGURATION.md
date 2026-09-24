# Configuration and daily use

Create the complete example with `walkietalk init`, then edit
`~/.config/walkietalk/config.yaml`. Pass that path with `-c` on every configured
command. Walkietalk does not silently select a config for hardware access.
`--transmit` and an explicit config are both required to transmit.

All sections and fields in [config.example.yaml](../config.example.yaml) are
required. Unknown or missing fields are errors, including fields for backends
you are not using. The snippets in other guides show changes to an existing
complete config, not replacement files. Restart the command after editing it.

## Credentials

`init` creates a private `credentials.env` beside `config.yaml`. Add only the
credentials your selected backends need. Example format (replace placeholders):

```dotenv
WALKIETALK_HERMES_TOKEN="your-local-Hermes-service-token"
# XAI_API_KEY="your-explicit-billed-API-key"
# ANTHROPIC_API_KEY="your-explicit-billed-API-key"
```

These names hold values in the private file. YAML fields such as
`agent.hermes_token_env` contain the **variable name**, never the token value.
If you configure a custom variable name, use that same name in this file.

When the selected config file exists, Walkietalk reads `credentials.env` in
that file's directory if present. A missing config does not select a credentials
file. The directory comes from the config path, not the current working directory. There is no search through
parent directories or automatic loading of a repository `.env`. An already
exported environment variable wins, even if empty. Missing optional files are
fine when your chosen backends need no credentials or use exported variables.

```bash
# Explicit alternative file; a missing file is an error:
walkietalk --env-file /path/to/private.env -c /path/to/config.yaml agent-check 'Hello.'
# Ignore all credentials files, using only the process environment:
walkietalk --no-env-file -c /path/to/config.yaml agent-check 'Hello.'
```

Files must be regular files owned by the current user, with no group/other
permissions (`chmod 600 /path/to/credentials.env`). Symlinks are refused.
The parser accepts one literal `KEY=value` per line, optionally prefixed with
`export`, with comments and single/double quotes. Quote values containing spaces
or `#`. Empty values remain empty. There is no variable expansion, command
substitution, multiline value, or shell execution. Malformed/duplicate entries
fail locally without displaying their values. The file is limited to 64 KiB.

Official Codex, Grok, and Claude login stores remain owned by their CLIs. Do not
copy OAuth tokens into this file. Keeping an API key here does not select its
backend or permit automatic fallback. Keep credentials and real configuration
out of version control; the repository ignores `credentials.env`, `config.local.yaml`,
`.env`, and `.env.*.local`.

## Commands

Global options (`-c`, `--env-file`, `--no-env-file`) go **before** the command.

| Command | What it does | Hardware/network behavior |
| --- | --- | --- |
| `--help`, `--version` | Show commands/version | No hardware, credentials file, or network |
| `init [--directory NEW_PATH]` | Create private config and credential templates | No hardware or network; never overwrites |
| `config-check` | Validate the exact YAML schema and show backend selections | Requires `-c`; no devices, logins, or network checked |
| `devices` | List exact audio names and stable serial paths | Enumerates devices; no PTT |
| `check` | Check selected devices, permissions, and local readiness | Requires `-c`; no PTT or capture/playback stream; not a remote connectivity test |
| `models` | Download/verify configured local Whisper weights | Explicit download; network if missing |
| `agent-check 'text'` | Ask the selected agent without a wake phrase | No STT, audio, or PTT; remote request for a real agent |
| `tts-check 'text' --output NEW.wav` | Synthesize and save a WAV | No radio/playback; selected voice may use network; refuses overwrite |
| `ptt --seconds 1` | Simulate key/unkey | Add `--transmit` with `-c` for actual PTT |
| `play speech.wav` | Validate/simulate a mono PCM16 WAV | Add `--transmit` with `-c` for actual playback and PTT |
| `listen speech.wav` | Transcribe one utterance from a file | No radio; selected STT may use network |
| `listen --capture` | Capture/transcribe one radio utterance | Requires `-c`; receive-only, no wake gate |
| `talk speech.wav` | Transcribe, wake-gate, and answer file traffic | Printed reply; transmit only with explicit `-c` and `--transmit` |
| `talk --capture` | Keep listening and print accepted replies | Requires `-c`; receive-only until `--transmit` is added |

`talk --capture --once --timeout 60` is a one-utterance diagnostic. Its timeout
is a wait-for-speech limit, not how long someone may speak. Continuous `talk`
has no idle wait limit; `--timeout` requires `--once`. `listen --capture` waits
up to 60 seconds by default and accepts its own `--timeout`. `NO_COLOR` disables
colored logs.

## Audio, capture, and speech recognition

| Fields | Meaning |
| --- | --- |
| `audio.input_device`, `audio.output_device` | Exact names from `devices`; no default-device fallback |
| `audio.gain` | Outgoing amplitude multiplier, any positive finite value; above 1 can clip and degrade sound |
| `vad.energy_threshold` | RMS level that begins speech; compare with the live meter |
| `vad.hangover_ms` | Silence interval that ends the utterance |
| `vad.max_utterance_seconds` | Cap on one incoming recording |
| `stt.backend` | `faster-whisper`, `grok`, or `grok_api` |
| `stt.model` | Local `tiny` or `base`; remains required for remote backends |
| `stt.timeout_seconds` | Transcription deadline (greater than zero, up to 120 seconds), independent of wait-for-speech and agent time. Grok includes worker startup, login refresh, upload, response reading, and authentication retry in one budget. |
| `stt.max_response_bytes` | Optional positive integer; defaults to 1048576 (1 MiB). Caps each Grok transcript, login-refresh, or error response body. Oversized responses are discarded; local Whisper ignores this setting. |

See [backend setup](BACKENDS.md) for model download and login instructions.
Capture gain comes from the radio/interface; `audio.gain` only changes output.

## Combined voice agent (optional realtime)

| Fields | Meaning |
| --- | --- |
| `voice_agent.backend` | `off` (default) or `grok_realtime`. Explicit combined Speech to Speech path; never a silent substitute for `stt` / `agent` / `tts` |
| `voice_agent.model` | `grok-voice-latest` (default) or a versioned ID such as `grok-voice-think-fast-2.0` |
| `voice_agent.voice` | Built-in or custom xAI voice ID (example `eve`) |
| `voice_agent.api_key_env` | Environment variable **name** for the billed console key; default `XAI_API_KEY` (same as `grok_api`). SuperGrok login is never used |
| `voice_agent.websocket_url` | Default `wss://api.x.ai/v1/realtime` |
| `voice_agent.connect_timeout_seconds` | WebSocket connect deadline |
| `voice_agent.idle_timeout_seconds` | Wait for server events after connect/commit |

Phase 1 validates this schema and ships a fake-transport client. Selecting
`grok_realtime` does not change the live `talk` radio loop yet. See
[GROK-REALTIME-PLAN.md](GROK-REALTIME-PLAN.md).

## Wake and conversations

`wake.primary` is your chosen phrase; `wake.aliases` contains explicit alternate
transcriptions. Matching ignores case and accepts punctuation after the phrase.
Choose your own names; no agent identity is hardcoded.

- `listening.mode: wake_phrase`: address every request with phrase plus traffic
  in the same utterance. A phrase-only utterance does not open a follow-up window.
- `listening.mode: conversation`: the wake phrase opens a follow-up window.
  Phrase-only also opens it; unaddressed follow-ups within the window are accepted.
- `wake.confirmation_phrase`: spoken when the wake phrase arrives with no traffic.
  Empty leaves that step silent. With `talk --transmit`, it is spoken before the
  follow-up window starts. Receive-only mode prints the wake status and does not key.
- `listening.conversation_timeout_seconds`: determines when addressing is needed
  again. It does not end the program. After a successful reply, the window is
  refreshed after printing in receive-only mode, or after playback/unkey and the
  post-transmit mute in transmit mode. Restarting clears agent conversation history.

Only the traffic after the wake phrase goes to the agent. A follow-up uses the
same bounded radio context; this is not a resumed desktop CLI conversation.
Continuous mode returns to listening after STT, agent, or speech errors. Failed
or unheard agent turns are not retained as completed radio answers. Hardware
capture failures stop with a local error.

## Agent and voice

`agent.backend` selects `stub`, `hermes`, `codex`, `grok`, `claude`, or `claude_api`.
`agent.max_reply_chars` caps final text (1–2000); `agent.history_turns` bounds
completed context pairs (1–32); `agent.timeout_seconds` caps the overall request.
Traffic is also bounded to 4000 characters.

`agent.web_search` defaults to `false`. Set it to `true` to let the selected
agent look up public web information before answering. Commands, local file
changes, and messages to other people stay unavailable. The lookup uses the
same `agent.timeout_seconds` deadline, so raise that (up to 300 seconds) when a
search needs longer than the default minute. The radio stays quiet until the
answer is ready, and that answer still has to fit `max_reply_chars` and the
transmit window. Hermes receives this as an instruction; the Hermes profile
still decides which tools exist. Pages found on the web are untrusted text.

`agent.instructions` replaces the guidance paragraph sent to every agent.
Leave it empty for the built-in default: short, plain, spoken-style sentences
suitable for a family, including the character cap. When the answer will be
spoken, that default also names the radio and the word budget. These
placeholders are filled from the other settings, including on a receive-only
session:

- `{max_reply_chars}`
- `{spoken_seconds}` from `radio.max_tx_seconds` minus `radio.settle_seconds`
- `{max_words}`, twice that window

Write `{{` and `}}` for a literal brace. Any other placeholder is a config
error. The text must be printable and at most 2000 characters. After the
guidance, the bridge still adds the web-search rule and tells the agent to
return only the final answer. A custom paragraph that never mentions
`{max_reply_chars}` is allowed; a longer reply is still discarded. Spoken
audio is still cut at the transmit window.

The `hermes_url` and `hermes_token_env` fields point to the selected environment.
The Codex/Grok/Claude `*_executable`, `*_model`, and `*_reasoning_effort` fields
configure those adapters. Executables are names on PATH or absolute paths, not
shell command strings. `claude_api_key_env` and `claude_api_model` are the direct
Messages API settings. See [backend setup](BACKENDS.md) for precise auth routes.

CLI reasoning defaults to `low` for quick radio answers. `default` omits the
explicit override and uses the adapter's documented model/profile default.
Supported values depend on both adapter and model; an unsupported selection
fails without substitution. Reasoning effort, answer length, and timeout are
separate controls. Hermes uses its environment's reasoning configuration.

`tts.backend` selects `piper`, `grok`, `grok_api`, or `hermes`.
`tts.timeout_seconds` limits synthesis, independent of the agent timeout.
`tts.normalize: off` preserves the engine's level; `peak` fills the PCM headroom
before applying `audio.gain` during playback. With peak normalization, gain above
1 will clip peaks. Normalization does not change `play` of an existing WAV.

- Piper uses `piper_executable` and `piper_model`. A relative model path is relative
  to the config file; `~` expands to the user's home.
- Grok uses `grok_voice`, `grok_language`, and `grok_speed` (0.7–1.5).
  `grok_api_key_env` is used only for the explicit billed API backend.
- Hermes uses its own speech companion's `hermes_url` and `hermes_token_env`.
  Its provider, voice, and provider credentials stay in the Hermes environment.
  These settings are independent of the agent's Hermes URL/token and Grok fields.

## Radio and playback

`ptt.serial_port` is the stable `/dev/serial/by-id/...` path. Standard AIOC uses
`ptt.line: dtr`: DTR high to transmit, RTS low, both low when idle. `rts` exists
for other hardware with documented/tested wiring, not as an AIOC alternative.

`radio.max_tx_seconds` is the total transmit cap. `radio.settle_seconds` reserves
an initial key-up delay, so speech fits in `max_tx_seconds - settle_seconds`.
Overlong speech is cropped. Increase settle if the first word is clipped;
increase `audio.gain` if outgoing audio is quiet, accepting possible clipping.
`radio.post_tx_mute_seconds` delays listening after unkey (0 disables it).

`radio.callsign` is your station ID, or empty for no spoken ID. Walkietalk does
not invent one. `callsign_mode` is `off`, `end_of_reply`, or `interval`;
`callsign_interval_seconds` sets the interval. If enabled, ID uses the selected
voice and normally shares the reply's transmit budget, shortening the answer
audio to reserve space for the complete ID. If the ID and its gap leave no room
for answer audio, Walkietalk sends the answer first and then the ID in a separate
burst, with PTT released for 0.2 seconds between them. Each burst has its own
transmit cap. Listening stays paused through both bursts, and post-transmit mute
starts after the last one. The ID interval starts only after the transmission
containing the ID succeeds. Any hardware failure stops the sequence.

A failed ID synthesis or an ID too long for its own burst is reported locally;
the answer is sent without the ID, and identification remains due. The CLI does
not crop station-ID audio to make it fit.

The parent owns PTT, supervises playback in a child process, and releases PTT
on completion, handled failure, Ctrl+C, or SIGTERM. It cannot guarantee release
through power loss, SIGKILL, USB disconnect, or a stuck kernel/serial driver.
Turn the radio off if TX remains on. Serial drivers may also briefly change
line levels when opening a port; see [pySerial's documented behavior](https://pyserial.readthedocs.io/en/latest/pyserial_api.html#serial.Serial.open).

## Remote program shutdown

Optional `shutdown.enabled` enables your `phrase`, separate `code`, aliases for
each, and `confirmation_seconds`. Say phrase + code in one transmission, or the
phrase then the code within the confirmation window. The ordinary wake phrase
is optional. The code alone does not stop an unarmed bridge; a wrong next
utterance or expiry cancels arming. STT mistakes require explicit aliases.

`arm_confirmation_phrase` is spoken when the phrase arrives alone and arms
shutdown. `confirmation_phrase` is spoken after the code is accepted, including
when the phrase and code arrive in one transmission. Both are required when
shutdown is enabled, and they must differ from each other, from the phrase and
code, and from the wake names. They are spoken only with `--transmit`.
Receive-only mode prints the status and does not key. A failed phrase
acknowledgement leaves shutdown armed. A failed code confirmation still stops
the program with a local error. Shutdown closes Walkietalk; it does
not delete files or shut down the computer. Anyone monitoring the radio can
hear the phrase/code, so this is a convenience control rather than a security
boundary. Send controls while the bridge is listening; capture pauses during
agent processing and playback.
