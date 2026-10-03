# Troubleshooting

Start with `walkietalk --version`, `walkietalk config-check`, and
`walkietalk check`. When asking for help, include the error and the selected
backends; never post credentials, CLI login files, or an unredacted config.

## Installing and settings

| Symptom | What to check |
| --- | --- |
| Build fails in `whisper-rs-sys` | Install `cmake`, `clang`, and `libclang-dev`. |
| Build fails mentioning `alsa` | Install `libasound2-dev` and `pkg-config`. |
| `walkietalk: command not found` | Add `~/.cargo/bin` to `PATH` (rustup does this for new shells). |
| "no settings at ~/.config/walkietalk/config.toml" | Run `walkietalk init`, or pass `-c FILE`. |
| `init` says the directory exists | That protects existing settings. Edit them, or use `--directory`. |
| Unknown or invalid field | `config-check` lists every problem; compare with `crates/walkietalk/templates/config.toml`. |
| Credentials file refused | It must be a regular file you own, `chmod 600`, not a symlink. Messages never show its contents. |
| A new token seems ignored | An exported variable wins over the file, even if empty. Unset it in the shell. |

## Devices and reception

| Symptom | What to check |
| --- | --- |
| "no audio device named ..." | Run `walkietalk devices` again after reconnecting and copy the exact `plughw:CARD=...` name. |
| "cannot open audio device ... may be in use" | Close other audio programs and turn the AIOC's profile off in the desktop sound settings. |
| Serial "No such file" or permission errors | Check `/dev/serial/by-id/` and your `dialout` membership ([serial permissions](INSTALL.md#serial-permissions)). Never run as root. |
| "no speech within the wait limit" | That is `listen` or `talk --once`. Compare the RMS meter with `[vad] threshold`, check the gateway radio's volume, or lower the threshold. Continuous `talk` has no idle limit. |
| Speech cut into pieces | Raise `[vad] hangover_ms`. |
| Wrong words | Inspect with `listen --capture`. Add `[wake] aliases` for recurring mistakes; try a larger Whisper model. |
| "Ignored: say ... first" | In `wake-phrase` mode every request needs the name; in `conversation` mode the follow-up window may have ended. |

## Agents and voices

Use `agent-check` to test the agent alone and `tts-check` to test the voice
alone; neither opens the radio. For `grok-realtime`, use `voice-agent-check`.

| Symptom | Action |
| --- | --- |
| CLI not found | Install the official CLI; set `[agent.<name>] executable` to its name or absolute path, without arguments. |
| CLI login refused | Log in as the same user: `codex login`, `grok login`, or `claude auth login`. No API key is used instead. |
| Grok profile check failed | `grok inspect`: API-key login must be disabled; hooks, MCP servers, plugins, LSP servers, and custom providers must be off. |
| Claude CLI lacks isolation options | Update Claude Code. |
| API key missing or refused | Set the named variable in `credentials.toml` for the explicitly selected `*-api` backend. |
| Hermes unavailable | Check the service, URL, and token. The agent API and the speech companion are different services. |
| Agent timed out | Raise `[agent] timeout_seconds` (up to 300) or lower `reasoning_effort`. |
| Reply too long; discarded | Ask for shorter answers or raise `[agent] max_reply_chars` (up to 2000). |
| Whisper model missing | `walkietalk models`. |
| Piper missing | Install Piper and the voice's `.onnx` and `.onnx.json` side by side. |
| Hermes provider mismatch | Fix `tts.provider` in Hermes; the companion refuses fallback audio. |
| Realtime timeouts | Check the network and `[agent.realtime]` timeouts. |

In continuous `talk`, recognition, agent, and voice errors are reported and
listening continues. A failed or unspoken reply is not kept as context.

## On the air

| Symptom | What to check |
| --- | --- |
| First word clipped | Raise `[radio] settle_seconds` a little. |
| Replies quiet | Raise `[audio] gain` (above 1 can clip) or set `[tts] peak_normalize = true`. |
| Replies cut off at the end | They exceed the speech budget (`max_tx_seconds` minus `settle_seconds` minus 0.35 s); ask for shorter answers or raise `max_tx_seconds`. |
| The bridge hears its own reply | Raise `[radio] post_tx_mute_seconds`. |
| "PTT fault" | A key or release failed. Turn the radio off, check the cable and port, then restart. |
| Station ID error stops `talk` | A due ID could not be prepared or sent. For Morse, use only letters, digits, and spaces. |

Ctrl+C or SIGTERM releases the transmitter; a second Ctrl+C exits at once. If
the gateway radio stays keyed, turn it off: power loss, `kill -9`, USB
disconnects, and stuck drivers are beyond what software can release.
