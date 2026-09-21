# Phase 6: Amy speaks on the radio

Phase 5 is merged in [PR #5](https://github.com/bbusenius/walkietalk/pull/5).
Phase 6 family demonstrations of Piper (Amy) and Grok speech are accepted.
Brad showed the girls and authorized publication. Phase 6 is merged in
[PR #6](https://github.com/bbusenius/walkietalk/pull/6), merge commit `ab37520`. Automated checks and local
synthesis remain separate from those live radio observations.

The bridge now has a `TtsBackend`: agent text goes to local Piper, which creates
speech. Walkietalk checks the whole WAV, converts it to 48 kHz mono PCM16, prepares
the existing isolated playback worker, keys DTR, plays the answer, and releases
PTT. Amy is the first voice, chosen by Brad. Agent, STT, and voice choices remain
independent. Piper receives answer text; it does not receive provider keys.

`talk --capture` remains text-only. Real replies require an explicit config and
`talk --capture --transmit`. `tts-check` creates a WAV without opening hardware.
No automatic voice download or fallback occurs while listening.

## Adult setup

Run from the project directory as your normal user:

```sh
cd /home/brad/Documents/Code/walkietalk
```

The development machine already has Piper 1.8.0 and Amy downloaded. For another
installation, add the optional extra and explicitly download the model:

```sh
uv pip install --python .venv/bin/python -e '.[dev,piper]'
.venv/bin/python -m piper.download_voices --download-dir "$HOME/.cache/walkietalk/piper" en_US-amy-medium
```

For a pip-managed environment, use `.venv/bin/python -m pip install -e '.[dev,piper]'`
instead of the uv command. No Piper install is needed for text-only operation.
Walkietalk also finds Piper alongside its active Python interpreter, so activating
the virtual environment is optional when using `.venv/bin/walkietalk`.

Phase 6 adds this required section to the exact-field config. It has already been
appended to Brad's `config.local.yaml`; all his other settings were preserved:

```yaml
tts:
  backend: "piper"
  piper_executable: "piper"
  piper_model: "~/.cache/walkietalk/piper/en_US-amy-medium.onnx"
  timeout_seconds: 30
  grok_voice: "eve"
  grok_language: "en"
  grok_speed: 1.0
  grok_api_key_env: "XAI_API_KEY"
  normalize: "off"
```

Keep both the `.onnx` model and its `.onnx.json` sidecar. Relative model paths
resolve from the config file's directory; `~` expands to your home. Other Piper
voices can be selected by downloading their files and changing `piper_model`.
Piper, `grok` (saved SuperGrok login), and `grok_api` (explicit billed API key)
are implemented. See [the Grok checkpoint](GROK-TTS.md) for setup and all added
demonstrations. An unknown backend fails explicitly.

Piper's engine is a separate GPL-3.0 dependency; model terms are separate. See the
[maintained Piper CLI instructions](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/CLI.md)
and [Amy's model card](https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/amy/medium/MODEL_CARD)
for voice and dataset terms. Neither engine nor model weights are bundled in this repository.

## Limits and timing

Playback volume is controlled by `audio.gain`, for both Piper replies and `play`.
Any positive finite gain is accepted: `1.0` preserves the original amplitude;
try `1.5` or `2.0` for amplification. Above `1.0`, peaks may clip and sound quality
may degrade. Samples are clamped to the PCM range instead of overflowing; this
does not remove clipping distortion.

`tts.normalize` is `off` or `peak`. `off` keeps each engine's own level. `peak`
scales a synthesized reply so its loudest sample fills the WAV, which can make
a quieter Grok file as hot as Piper before `audio.gain`. With `peak`, start gain
at `1.0`. `play` of an existing file is unchanged. Restart after editing config.
Capture and STT sensitivity are unaffected.

Brad confirms that spoken replies work well. Matching Amy's volume to regular
radio speech is still being adjusted; amplified audio needs a listening check.

These are independent settings:

| Setting | Meaning |
| --- | --- |
| `agent.timeout_seconds` | How long the helper may take to answer. |
| `agent.max_reply_chars` | Maximum completed answer text; oversized answers are discarded. |
| `tts.timeout_seconds` | Whole synthesis deadline, including engine startup; allowed up to 120 seconds. |
| `radio.max_tx_seconds` | Existing hard limit while keyed, including settle time. |
| `radio.settle_seconds` | Delay after keying before audio starts. |
| `listening.conversation_timeout_seconds` | Follow-up window after the spoken answer finishes and PTT is released. |

For example, with a 10-second TX limit and 0.2-second settle, playback is cropped
to 9.8 seconds so the transmitter unkeys on time. The existing character cap is
unchanged. See [phase 7](PHASE7-DEMO.md) for mute and optional station ID.

Capture closes before transcription and stays closed through agent work,
synthesis, and playback. After successful playback/unkey, the reply is printed,
the conversation window opens, and capture resumes. There is no idle program
limit in continuous mode. Speech synthesis failures return to listening with a
fresh wake required; the unheard answer is removed from conversation history.
Audio/PTT hardware failures stop the program after cleanup, so inspect the setup
before restarting. Post-transmit mute and optional station ID are phase 7.

## Complete family demonstration checklist

Brad confirms Piper and Grok work with the girls. Individual itemized TX,
follow-up, and shutdown notes were not separately reported. Use the current
configured wake phrase and shutdown phrases; the examples below reflect Brad's
current settings. If those settings change, use the new phrases.

1. **Check the exact devices, then synthesize Amy without transmitting.**

   ```sh
   .venv/bin/walkietalk devices
   .venv/bin/walkietalk -c config.local.yaml check
   mkdir -p recordings
   .venv/bin/walkietalk -c config.local.yaml tts-check "Hello. This is Amy speaking through Walkietalk." --output recordings/phase6-amy.wav
   .venv/bin/walkietalk -c config.local.yaml play recordings/phase6-amy.wav
   ```

   Expect the configured AIOC and serial path, then `Voice: piper
   (en_US-amy-medium.onnx; local voice)`, a 48 kHz mono PCM16 WAV of about 3.6 seconds,
   and `No hardware opened.` The `play` command above reports a **dry run**; no
   sound is transmitted and the TX light stays off. If the output file already
   exists, choose a new filename in subsequent commands; it is never overwritten.
   Rediscover changed ALSA names rather than choosing the system default. Retain
   DTR as PTT with RTS low. If the serial ACL expired after reconnecting, restore
   permission on the currently discovered serial device before live commands.

2. **Hear Amy on the Midland and see clean PTT release.**

   ```sh
   .venv/bin/walkietalk -c config.local.yaml play recordings/phase6-amy.wav --transmit
   ```

   Expect the gateway TX light on during playback, Amy's entire sentence heard
   on the Midland, then TX off and `Finished; talk released.` This is the first
   on-air step. Confirm the beginning and end are audible and volume is clear.

3. **Confirm the default stays text-only, then hear a real agent answer.**

   ```sh
   .venv/bin/walkietalk -c config.local.yaml talk --capture --once
   ```

   Say “Picard delta five, why is the sky blue?” Expect a text `Reply:` and TX
   light off. This one-shot command waits up to 60 seconds for speech; it is only
   a short test. For the real continuous spoken loop:

   ```sh
   .venv/bin/walkietalk -c config.local.yaml talk --capture --transmit
   ```

   Say the same named question. Expect `Generating speech; PTT off...`, then
   Amy's short answer on the Midland, `Spoken reply finished; PTT released.`, and
   `Reply:` on screen. TX stays off while the agent and Piper are working and
   turns off again after the answer. Let the response finish before speaking.

4. **Follow-up, quiet time, and no self-trigger.**

   Keep the same invocation running in `listening.mode: conversation`. Immediately
   after the reply finishes, ask “Why does that happen?” without the name.
   Expect `Accepted (follow-up).` and an answer using the previous discussion.
   Brad's configured 10-second window begins after the spoken answer finishes,
   not when the first question was received. After another answer, remain quiet:
   the bridge must not respond to its own voice. Wait more than 60 seconds;
   the program must remain listening with TX off. An unaddressed question after
   the follow-up window expires is ignored; a named question works again.

5. **Keep Amy while changing the helper.**

   Stop with Ctrl+C while idle. In `config.local.yaml`, change only
   `agent.backend` from `grok` to another already demonstrated helper, such as
   `codex` (with its saved login). Restart:

   ```sh
   .venv/bin/walkietalk -c config.local.yaml talk --capture --transmit
   ```

   Ask the same named question. Expect the selected helper's short answer in
   Amy's voice. No radio, wake, STT, or TTS edits are needed. This is a new session.
   Stop and restore your preferred agent afterward.

6. **Controlled speech failures stay off-air.**

   ```sh
   .venv/bin/python scripts/demo_voice_failures.py
   ```

   This uses a fake Piper executable and fake incoming speech, with hardware
   access forbidden. Expect five `PASS: local error; no PTT or playback opened.`
   lines: missing CLI, missing voice, timeout, synthesis error, oversized audio.
   Each produces an expected local `Error:`; private diagnostics never become
   an answer. TX stays off. These are controlled software demonstrations, not
   intentional failures of the real radio or account. Existing phase 5 account
   failures remain covered by their adapter tests and demo scripts.

7. **Interruption and remote shutdown still release the radio.**

   Repeat the real WAV transmission from step 2 and press Ctrl+C while Amy is
   speaking. Expect TX off immediately and a stopped/cleanup message. Then start
   continuous spoken mode again, wait until listening, and say “Picard epsilon
   five, initiate self-destruct” in one transmission. Expect the configured
   confirmation phrase on the walkie, then TX off, no agent answer, and
   program exit. Restart and verify the two-part form too: “Picard epsilon
   five”, then “initiate self-destruct” within 30 seconds; the confirmation
   phrase plays only after the code. Capture is paused while the bridge is
   answering, so radio shutdown is accepted during listening; Ctrl+C also works
   during agent work, synthesis, playback, or the shutdown confirmation.

8. **Explain it together.**

   “The helper gives us words. Amy turns the words into a voice. The bridge presses
   the radio's talk button, plays the voice, and lets go so we can answer.”

Report any cut-off speech, distortion, repeated self-replies, unexpected TX light,
or missed follow-up separately. Brad confirmed the demonstration and authorized
committing and publishing phase 6. Phase 7 does not start automatically.

## Automated and local verification

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
.venv/bin/python scripts/demo_voice_failures.py
```

Tests use fake voice processes and fake hardware; CI never downloads a voice or
keys a radio. They cover subprocess deadlines, malformed/oversized output,
credential isolation, offline WAV export, default text-only behavior, rejected
traffic, failed synthesis recovery/history, reply timing, and release on playback
failure or interruption. Local real Amy synthesis produced a 3.599-second WAV;
that verifies file generation, not audible quality or RF behavior.

Current result: **549 tests passed**, Ruff lint and formatting passed, and all
five controlled voice failure demonstrations passed. The isolated source/wheel
build also passed (`.venv/bin/python -m build --installer uv`), with version
unchanged at `0.1.0`. Read-only host checks found
the configured AIOC at index 4 and confirmed serial permissions. No live
transmission was performed by the coding assistant.
