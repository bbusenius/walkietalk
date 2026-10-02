# Configuration reference

Walkietalk reads one TOML file: `~/.config/walkietalk/config.toml` unless
you pass `-c FILE`. `walkietalk init` writes a complete, commented starting
point. Only `[audio]`, `[ptt]`, and `[wake]` are required; every other section
and field has a default, and sections for backends you do not use can be left
out. Unknown fields are errors, and `config-check` reports every problem at
once.

```bash
walkietalk config-check
```

Restart `talk` after editing the file.

## Credentials

Tokens and API keys live in `credentials.toml` beside the config, never in
`config.toml`. Fields such as `token_env` and `key_env` hold the **name** of
a variable, not its value.

```toml
WALKIETALK_HERMES_TOKEN = "local-hermes-service-token"
XAI_API_KEY = "billed-xai-api-key"
ANTHROPIC_API_KEY = "billed-anthropic-api-key"
```

- The file must be a regular file you own with no group or other access
  (`chmod 600 credentials.toml`). Symlinks are refused.
- A variable already exported in the environment wins, even if it is empty.
- Errors about the file never show its contents.
- `--credentials FILE` uses another file (it must exist); `--no-credentials`
  uses only the environment.

Official CLI logins (Codex, Grok, Claude) stay in those CLIs' own stores. A key
in this file never selects a backend or enables a fallback.

## `[audio]`

| Field | Default | Meaning |
| --- | --- | --- |
| `input` | required | Capture device, e.g. `plughw:CARD=AllInOneCable,DEV=0`, from `walkietalk devices` |
| `output` | required | Playback device |
| `gain` | `1.0` | Outgoing level multiplier, above 0 up to 16. Above 1 amplifies and can clip. |

Default and sound-server devices (`default`, `pulse`, `pipewire`, `dmix`, ...)
are refused: pick the radio interface itself.

## `[ptt]`

| Field | Default | Meaning |
| --- | --- | --- |
| `port` | required | Serial device, ideally `/dev/serial/by-id/...` |
| `line` | `"dtr"` | Line that keys the radio: `dtr` (AIOC) or `rts`. The other line is held low. |

## `[radio]`

| Field | Default | Meaning |
| --- | --- | --- |
| `max_tx_seconds` | `10` | Hard limit on one transmission, settle included |
| `settle_seconds` | `0.2` | Delay between keying and audio, so the first word is not clipped (up to 2, less than the limit) |
| `post_tx_mute_seconds` | `2` | Listening pause after the transmitter is released (0 to 30) |

Speech must fit in `max_tx_seconds - settle_seconds`. Longer agent replies are
cropped at that point; station IDs are never cropped.

### `[radio.station_id]`

| Field | Default | Meaning |
| --- | --- | --- |
| `callsign` | `""` | Your granted station ID. Empty disables identification. |
| `mode` | `"end-of-reply"` | `off`, `end-of-reply`, or `interval` |
| `method` | `"voice"` | `voice` speaks the call sign; `morse` sends an 800 Hz tone at 20 words per minute |
| `interval_seconds` | `900` | For `interval` mode, up to 1800 |

When an ID is due it is appended to the reply (after a 0.2 s gap) if the whole
reply and ID fit one transmission; otherwise the reply goes out first and the
ID follows in its own transmission, with the transmitter released for 0.2 s
between them. The interval restarts only after the ID was transmitted in full.
If a due ID cannot be prepared or transmitted, `talk` stops with an error
(after recording a reply that already went out). Morse is generated locally
and accepts letters, digits, and spaces; a space separates a unit number.
With the realtime voice, the ID always uses its own transmission.

## `[vad]` (voice detection)

| Field | Default | Meaning |
| --- | --- | --- |
| `threshold` | `0.02` | RMS level (0 to 1) that starts an utterance. Compare with the live meter. |
| `hangover_ms` | `400` | Silence that ends an utterance (1 to 5000) |
| `max_utterance_seconds` | `12` | Longest recording (up to 30) |

Bursts with less than a quarter second of speech are ignored as noise.

## `[stt]` (speech recognition)

| Field | Default | Meaning |
| --- | --- | --- |
| `backend` | `"whisper"` | `whisper` (local), `grok` (saved Grok login), or `grok-api` (billed key) |
| `model` | `"base"` | Whisper model: `tiny`, `base`, or `small` |
| `timeout_seconds` | `30` | Transcription deadline, including any login refresh (up to 120) |
| `max_response_bytes` | `1048576` | Cap on each remote response |
| `api_key_env` | `"XAI_API_KEY"` | Variable holding the key for `grok-api` |

## `[listening]`

| Field | Default | Meaning |
| --- | --- | --- |
| `mode` | `"conversation"` | `conversation`: the wake name opens a follow-up window. `wake-phrase`: every request starts with the name. |
| `follow_up_seconds` | `30` | How long unaddressed follow-ups are accepted after a reply (up to 600) |

The window refreshes after each completed reply (after the transmitter is
released and the post-transmit mute, when transmitting). Whether a follow-up
counts is judged from when the speaker started talking. The window expiring
never stops the program.

## `[wake]`

| Field | Default | Meaning |
| --- | --- | --- |
| `name` | required | The name that addresses the agent |
| `aliases` | `[]` | Common mistranscriptions of it |
| `confirmation` | `""` | Spoken when the name arrives alone; empty stays silent |

Matching compares words, ignoring case and punctuation: "Charlotte, what
time is it?" matches `charlotte` and sends "what time is it?". Matching is
exact on words; add aliases for recurring recognition mistakes. When several
wake phrases start an utterance (the agent's and a contact's), the longest
wins. In `conversation` mode the name alone opens the follow-up window.

## `[sleep]` (optional)

| Field | Default | Meaning |
| --- | --- | --- |
| `phrase` | required in the section | Closes the open conversation |
| `aliases` | `[]` | Alternatives |
| `confirmation` | `""` | Spoken after sleeping; empty stays silent |

Sleep matches the whole utterance, optionally after a wake name ("Charlotte,
go to sleep"). Mentions inside a longer request are ordinary traffic. Sleep
never reaches the agent, keeps the conversation history, and requires a wake
name again. Omit the section to disable it.

## `[shutdown]` (remote program shutdown)

| Field | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Turn the feature on |
| `phrase`, `phrase_aliases` | | Arms shutdown |
| `code`, `code_aliases` | | Confirms it |
| `confirm_window_seconds` | `30` | Time allowed between phrase and code (up to 300) |
| `armed_reply` | | Spoken when the phrase alone arms shutdown |
| `confirmed_reply` | | Spoken after the code, before exiting |

Say the phrase and code together, or the phrase and then the code within the
window (the wake name is optional). The code alone, a wrong code, or a late one
does nothing and cancels arming. Shutdown phrases never reach the agent or a
contact. Shutdown only exits walkietalk; anyone listening on the channel can
hear the phrase, so treat it as a convenience, not security. Replies are
spoken only with `--transmit`.

### Phrase rules

Wake names, aliases, sleep phrases, shutdown phrases, and codes must all be
distinct, and spoken confirmations must differ from all of them. A longer
wake phrase may not swallow words of a control said after a shorter one
(wake `charlotte go` with sleep `go to sleep` is refused). Contacts' wake
phrases follow the same rules.

## `[agent]`

| Field | Default | Meaning |
| --- | --- | --- |
| `backend` | `"stub"` | `stub`, `hermes`, `codex`, `grok`, `claude`, `claude-api`, or `grok-realtime` |
| `max_reply_chars` | `600` | Longer text replies are discarded (1 to 2000) |
| `history_turns` | `8` | Completed request/reply pairs kept as context (1 to 32) |
| `timeout_seconds` | `60` | Deadline for one text reply, including login checks and web lookups (up to 300) |
| `web_search` | `true` | Let the agent look up public web information |
| `instructions` | `""` | Replaces the built-in guidance |

History lives only for one run. A failed reply, or one that could not be
spoken, is not kept. The selected backend never falls back to another one or
to billed API access.

`instructions` may use `{max_reply_chars}`, `{spoken_seconds}` (the speech
budget), and `{max_words}` (two words per second of budget). Write `{{` and
`}}` for literal braces; other placeholders are config errors. Empty uses
short, family-friendly, spoken-style guidance that mentions the radio when
replies are transmitted.

| Section | Fields |
| --- | --- |
| `[agent.hermes]` | `url` (default `http://127.0.0.1:8642`), `token_env` (default `WALKIETALK_HERMES_TOKEN`) |
| `[agent.codex]` | `executable` (default `codex`), `model` (empty = CLI default), `reasoning_effort` |
| `[agent.grok]` | `executable` (default `grok`), `model` (required when selected), `reasoning_effort` |
| `[agent.claude]` | `executable` (default `claude`), `model` (required when selected), `reasoning_effort` |
| `[agent.claude_api]` | `key_env` (default `ANTHROPIC_API_KEY`), `model` (required when selected), `reasoning_effort` |
| `[agent.realtime]` | `model` (`grok-voice-latest`), `voice` (`eve`), `key_env` (`XAI_API_KEY`), `url` (`wss://api.x.ai/v1/realtime`), `connect_timeout_seconds` (10, up to 120), `idle_timeout_seconds` (60, up to 600) |

`reasoning_effort` is `default` (the model's own) or an explicit level the
adapter supports: Codex `minimal` to `xhigh`; Grok `none` to `max`; Claude and
the Claude API `low` to `max`. The default is `low` for quick answers.
Executables are names on `PATH` or absolute paths, never command lines.

## `[tts]` (voice)

| Field | Default | Meaning |
| --- | --- | --- |
| `backend` | `"piper"` | `piper`, `grok` (saved login), `grok-api` (billed key), or `hermes` |
| `timeout_seconds` | `30` | Synthesis deadline (up to 120) |
| `peak_normalize` | `false` | Scale speech so its loudest peak reaches full scale (`gain` still applies) |

| Section | Fields |
| --- | --- |
| `[tts.piper]` | `executable` (default `piper`), `model` (`.onnx` path; `~` and paths relative to the config work) |
| `[tts.grok]` | `voice` (`eve`), `language` (`en`, or `auto`), `speed` (0.7 to 1.5), `key_env` (for `grok-api`) |
| `[tts.hermes]` | `url` (default `http://127.0.0.1:8643`), `token_env` |

## `[messaging]`

See [messaging](MESSAGING.md) for behavior.

| Field | Default | Meaning |
| --- | --- | --- |
| `operator_mode` | `false` | Hold every message for local review |

`[messaging.whatsapp]` and `[messaging.signal]` (each optional):

| Field | Default | Meaning |
| --- | --- | --- |
| `wake`, `aliases` | required | Opens this contact's conversation |
| `to` | required | The contact's number or ID |
| `sender_alias` | `""` | Spoken label for their messages; empty uses the wake phrase |
| `empty_queue_reply` | `""` | Spoken when the wake phrase arrives alone and nothing is waiting |
| `send_as_voice` | `false` | Send the original recording as a voice note instead of its transcript |
| `transcribe_voice` | `false` | Read incoming voice notes as text with the voice, instead of playing them |
| `listening` | `{ mode = "conversation", follow_up_seconds = 60 }` | This contact's own listening rules |
| `account` | `""` | Signal only: the local account number (`+15551234567`) when several are registered |
| `attachments_dir` | XDG data dir | Signal only: where signal-cli stores attachments |

## Commands

Options `-c`, `--credentials`, and `--no-credentials` may go before or after
the command.

| Command | What it does | Touches |
| --- | --- | --- |
| `init [--directory DIR]` | Create a private settings directory | Never overwrites |
| `config-check` | Validate the config and summarize the backends | No hardware, network, or logins |
| `devices [--all]` | List sound cards and serial ports | Enumerates only |
| `check` | Confirm devices exist, the serial port is accessible, and backends are ready | No streams, no keying, no requests |
| `agent-check [TEXT]` | Ask the text agent one question | The agent only |
| `tts-check TEXT --output FILE` | Synthesize speech to a new WAV | The voice only |
| `voice-agent-check (WAV \| --capture) --output FILE [--supervised [--transmit]]` | One realtime turn saved to a new WAV; `--supervised` runs the transmit logic (simulated unless `--transmit`) | Billed realtime API |
| `models` | Download the Whisper model | Network, once |
| `listen (WAV \| --capture) [--timeout N]` | Transcribe one utterance | Never transmits |
| `talk (WAV \| --capture) [--transmit] [--once] [--timeout N] [--panel]` | The bridge | Transmits only with `--transmit` |
| `operator [ACTION] [--approved] [--text T] [--timeout N]` | Control a running operator-mode `talk` | See [operator mode](MESSAGING.md#operator-mode) |
| `ptt [--seconds N] [--transmit]` | Key briefly (simulated unless `--transmit`) | Serial with `--transmit` |
| `play WAV [--transmit]` | Play a WAV over the radio (simulated unless `--transmit`) | Audio and serial with `--transmit` |

`--transmit` always requires `-c` naming the config explicitly. Continuous
`talk --capture` has no idle limit; `--timeout` applies to `--once` captures.
`NO_COLOR` turns off colored output.

## How transmission works

- The transmitter is owned by one supervisor thread. Every keying has a hard
  deadline of `max_tx_seconds`, enforced by that thread even if audio playback
  hangs.
- Speech is synthesized and the playback device is opened and loaded before
  the radio is keyed. A failure before keying transmits nothing.
- The transmitter is released on success, on any error, on Ctrl+C or SIGTERM,
  and on a crash. After releasing, the serial lines are read back to confirm
  both are low.
- A failure to key or release is a hardware fault: walkietalk stops instead of
  retrying. Check the radio before restarting.
- The realtime voice keys only when audible speech arrives and keeps the whole
  reply within `max_tx_seconds`.
- Software cannot release the transmitter through power loss, `kill -9`, a USB
  disconnect, or a stuck driver. Turn the radio off if it stays keyed. Opening
  the serial port can briefly raise its lines on some drivers; walkietalk opens
  it once at startup and lowers both lines immediately.
