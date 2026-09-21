# Troubleshooting

Start with `walkietalk --version`, then `walkietalk -c /path/to/config.yaml
config-check`. Use the same normal Linux user and virtual environment as your
working installation. Global flags go before the command. Preserve the error
message, selected backend names, and installed CLI versions when reporting a
problem; never post credentials, CLI login files, or an unredacted private config.

## Installation and settings

| Symptom | What to check |
| --- | --- |
| `walkietalk: command not found` | Activate the virtual environment or use its absolute `bin/walkietalk` path. |
| Python installation is externally managed | Create a virtual environment; do not install into the system Python with sudo. |
| PortAudio cannot load | Install Ubuntu's `libportaudio2`; then restart the command. |
| `init` says the directory already exists | This protects existing settings. Edit them, or select a new `--directory`. |
| Missing/unknown config field | Compare with the complete example shipped with this version. All fields remain required even for unselected backends. |
| Credentials file rejected | Use a regular file owned by your user and `chmod 600`. No symlinks, duplicate names, or multiline values. Error messages intentionally withhold contents. |
| New token appears to be ignored | An exported variable wins over the credentials file, even if empty. Unset that variable in the shell or correct it, then restart. |
| No automatic credentials loading | The default file is named exactly `credentials.env`, beside the explicitly selected config. For an old `.env.hermes.local`, use `--env-file` if it contains only literal assignments. See [format and precedence](CONFIGURATION.md#credentials). |

## Devices and reception

Run `walkietalk devices` again after reconnecting hardware. ALSA card numbers may
change; copy the new exact AIOC names into the config. Walkietalk deliberately
does not fall back to the desktop microphone or speakers.

For a serial permission error, check the actual AIOC device and follow
[Linux serial permissions](INSTALL.md#linux-serial-permissions). A temporary ACL
can disappear after reconnecting. Group membership takes effect after logging
out and back in. Close browser flashers and other programs holding the port.
Do not run Walkietalk as root to bypass this check.

`check` only verifies local readiness; a device may still be busy when capture
or playback opens it. Close other audio applications using the AIOC. A capture
failure stops the command; it is different from an STT/provider failure.

If you see “No speech detected within the wait limit,” you ran a one-utterance
check (`listen` or `talk --once`). For unattended waiting, use `talk --capture`
without `--once` or `--timeout`. Continuous mode has no idle limit. For an actual
missed utterance, compare the RMS meter with `vad.energy_threshold`, check radio
receive volume, and confirm the chosen capture device before reducing the threshold.

If transcription is wrong, try `listen --capture` to inspect STT without the wake
gate. Set explicit `wake.aliases` for recurring transcriptions of your chosen
phrase. In `wake_phrase` mode, say phrase and traffic together each time. In
`conversation` mode, a phrase-only utterance opens the follow-up window. The
window expiring requires the name again; it does not end continuous listening.

## Accounts, agents, and voices

Use `agent-check 'a short question'` to isolate the agent from capture/STT, and
`tts-check 'a short sentence' --output new-name.wav` to isolate the voice from
playback. Both need the appropriate `-c` before the command. They do not key PTT.

| Symptom | Action |
| --- | --- |
| Missing CLI executable | Install the official CLI, check `command -v codex` (or `grok`/`claude`), and set the corresponding `*_executable` to its name or absolute path. Do not put shell arguments in that field. |
| Expired/rejected account login | Use that CLI's official login as the same Linux user: `codex login`, `grok login`, or `claude auth login`; repeat `agent-check`. No API-key fallback occurs. |
| Missing/rejected API key | Confirm the explicitly selected `*_api` backend and matching private variable. Subscription login does not supply billed API access. |
| Hermes unavailable | Check the intended profile, service process, URL, and bearer token. Agent and speech use distinct services/ports. Follow [Charlotte setup](https://github.com/bbusenius/charlotte/blob/master/runtime/hermes/README.md#walkietalk) or the [native speech guide](HERMES-TTS.md). |
| Agent timeout | Increase `agent.timeout_seconds` within its 300-second limit or choose supported lower reasoning effort. This does not change the reply cap or radio transmit cap. |
| Oversized reply | The bridge rejects it instead of speaking a long answer. Ask for a shorter answer or deliberately adjust `agent.max_reply_chars` (maximum 2000). |
| Local Whisper model missing | Run `models` with your config to explicitly download the selected `tiny`/`base` model. |
| Piper unavailable/model missing | Install the `[piper]` extra and the selected voice's `.onnx` plus `.onnx.json`; check the configured path. |
| Hermes speech provider mismatch | Correct the provider and its dependencies in Hermes. The companion discards a silent provider fallback. |
| `tts-check` output already exists | Pick another filename; the command does not overwrite recordings. |

Provider text, raw diagnostics, and tokens must not be used as spoken error
messages. In continuous mode, STT/agent/TTS errors are reported locally and the
bridge returns to listening. A local failure check is not proof that a real
expired account has been repaired.

## Transmitted audio

If replies are quiet, raise `audio.gain`; values above 1 are allowed but can clip
and degrade sound. `tts.normalize: peak` first raises synthesized audio toward
full scale; gain still applies afterward. These are bridge settings for every
voice, not Piper-only controls. Input volume is separate.

If the first word is clipped, increase `radio.settle_seconds` a little so PTT
engages before playback. Settle must remain below the total transmit cap. If an
answer is cut off at the end, it may exceed the remaining transmit budget:
ask for shorter replies or deliberately adjust `radio.max_tx_seconds`.

After unkeying, `radio.post_tx_mute_seconds` suppresses immediate recapture.
If station ID is unwanted, select `callsign_mode: off`; if needed, set your own
callsign and mode. The bridge does not infer your station ID.

Ctrl+C or SIGTERM triggers cleanup. If the gateway remains keyed, turn off the
radio and investigate before running again. Power loss, SIGKILL, disconnected
hardware, and a stuck driver are outside the software cleanup guarantee.
