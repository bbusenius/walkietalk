# Phase 1–4 demonstrations

These are the original teaching checkpoints. For a new installation, start
with [the current installation guide](INSTALL.md).

## Phase 1 family demonstration

Use your licensed, supervised radio setup on a clear channel. Match the gateway
and receiving walkie's channel/settings. Keep the gateway's transmit light
visible to the adult and the radio's power switch within reach.

1. **Press talk from code.** Run:

   ```sh
   .venv/bin/walkietalk -c config.local.yaml ptt --seconds 1 --transmit
   ```

   The gateway's TX light should come on for about one second, then go off.
   A PTT-only pulse still transmits, even though it carries no speech.

2. **Send a short spoken recording.** Prepare `speech.wav`: mono, uncompressed
   16-bit PCM, preferably 48000 Hz, shorter than 9.8 seconds with the defaults.
   Say something like “This is our computer talking.” Do not use music for this
   demo. Preview it in a normal audio player, then run:

   ```sh
   .venv/bin/walkietalk play speech.wav
   .venv/bin/walkietalk -c config.local.yaml play speech.wav --transmit
   ```

   Check the printed AIOC names, hear the words on the receiving walkie, and
   confirm the gateway TX light goes off. Reduce gain if distorted; adjust
   `settle_seconds` if the beginning is clipped. Audio preparation happens before
   PTT is asserted. Playback finishes draining before PTT is released.

3. **Show that stopping releases talk.** Run the following, then press Ctrl+C
   as soon as the light comes on:

   ```sh
   .venv/bin/walkietalk -c config.local.yaml ptt --seconds 5 --transmit
   ```

   Confirm the TX light goes off immediately. The automated tests below also
   exercise failures, SIGTERM, and hung playback without transmitting.

**Explain to the girls:** “We taught the computer to press talk, send our
recording, and let go. It checks which cable to use first, and it tries to let
go even if the sound fails or we stop the program. Now we can test speaking
before adding the AI.”

Phase 1 is complete. Brad runs each new phase and explains it to the girls
before we commit or push. Record live results on that phase's PR before
accepting the checkpoint.

## Phase 2 family demonstration

The computer stays receive-only. Do not pass `--transmit`. Watch the gateway TX
light: it must stay off. Speak on a handheld walkie on the shared channel.

1. **Download the speech model once** (needs network; skip if `check` already
   says the model is ready):

   ```sh
   .venv/bin/walkietalk -c config.local.yaml models
   .venv/bin/walkietalk -c config.local.yaml check
   ```

   Expect `Ready at ~/.cache/walkietalk/faster-whisper/base (... MB)` and
   `STT: base; ready`. `check` still opens neither the serial port nor PTT.

2. **Optional file rehearsal, no radio.** Use any short mono 16-bit PCM WAV of
   spoken words (not music), then:

   ```sh
   .venv/bin/walkietalk listen speech.wav
   ```

   Expect RMS / speech-started lines, then `Transcript:` followed by roughly
   those words. Small mistakes are acceptable.

3. **Live radio sentence.** After the program prints `Waiting for someone to talk`, say a
   clear sentence on a handheld walkie without long pauses, then stop talking:

   ```sh
   .venv/bin/walkietalk -c config.local.yaml listen --capture
   ```

   Capture should end about 0.4 seconds after you stop. The TX light stays off.
   The screen shows roughly what you said, for example:

   ```text
   Receive-only: PTT will not be opened.
   Waiting for someone to talk (give up after 60s; threshold 0.020)...
   Speech started (RMS 0.091)
   Speech ended after 2.4s (silence; peak RMS 0.140)
   Transcript: the computer writes down what we said
   ```

   Short cable clicks are ignored; keep talking for at least a quarter second.
   If it never starts, use the logged RMS: raise gateway volume, speak closer,
   or lower `vad.energy_threshold`. If it never stops, close squelch a little,
   lower volume, or shorten `vad.hangover_ms`. If the AIOC name in `devices`
   changed, update `config.local.yaml` rather than using a default device.

4. **Show that stopping is safe.** Run the live command again, wait until
   `Waiting for someone to talk`, then press Ctrl+C before talking. Expect `Stopped;
   capture closed.`, exit status 130, and no TX light.

**Explain to the girls:** “The computer writes down what we said so the AI can
understand the question.”

Phase 2 is complete.

## Phase 3 family demonstration

The computer stays receive-only. Do not pass `--transmit`. Watch the gateway TX
light: it must stay off. Use `talk`, not `listen`, so the wake gate runs.

On a color terminal, **Transcript** is cyan, **Accepted** / **Traffic** are
green, **Ignored** is yellow, and RMS waiting lines are dim. The same words
appear without color. Set `NO_COLOR=1` to turn color off.

1. **Default mode: the name is required every time.** Keep
   `listening.mode: wake_phrase` in `config.local.yaml`.

   ```sh
   .venv/bin/walkietalk -c config.local.yaml check
   .venv/bin/walkietalk -c config.local.yaml talk --capture
   ```

   Expect `Mode: wake_phrase` and `waiting for wake "charlotte"`. Then:
   1. Speak a sentence **without** the name → `Ignored (say "charlotte" first).`
   2. Say **Charlotte** plus traffic → `Accepted`, `Traffic:` with the name
      removed, `Reply: This is a pretend answer. The radio bridge brought me your words.`,
      still waiting for the name.
   3. Speak again **without** the name → ignored again.
   4. Ctrl+C. Expect `Stopped; capture closed.`, exit 130, TX light off.

2. **Conversation mode: follow-ups until a quiet pause.** Edit
   `config.local.yaml` to:

   ```yaml
   listening:
     mode: "conversation"
     conversation_timeout_seconds: 10
   ```

   Restart:

   ```sh
   .venv/bin/walkietalk -c config.local.yaml talk --capture
   ```

   1. Name + traffic → accepted, then `State: awake (10s left).` Saying only
      the name is enough to open the window (`Wake heard; listening for traffic.`).
   2. Before those 10 seconds end, more traffic **without** the name →
      `Accepted (follow-up).` The 10 seconds restart after that text reply prints.
   3. Wait; when the window ends you should see `Follow-up window ended` even
      if nobody is talking. Then speak without the name → ignored.
   4. Say the name again → accepted.

3. **The timeout is configurable.** Change `conversation_timeout_seconds` to
   `5`, restart, and show that the awake window is shorter. Silence does not
   keep it awake.

**Explain to the girls:** “Say the helper’s name, then your traffic — a question,
a command, or anything you need. After a quiet pause, say the name again.”

Phase 3 family demonstration passed and is merged in
[PR #3](https://github.com/bbusenius/walkietalk/pull/3).

## Phase 4 family demonstration

Switch the listener in config. Radio, capture, and wake code stay the same. Do
not pass `--transmit`. TX light stays off.

**Explain:** “If one listener has trouble with a voice, we can plug in a different
one.”

1. **Default faster-whisper still works.** Keep `stt.backend: faster-whisper`.

   ```sh
   .venv/bin/walkietalk -c config.local.yaml check
   .venv/bin/walkietalk -c config.local.yaml talk --capture --once
   ```

   Expect `Listener: faster-whisper (base)` and a normal transcript / traffic
   line.

2. **Grok Voice Transcribe via SuperGrok Plus.** Sign in once with `grok login`
   if needed, then set:

   ```yaml
   stt:
     backend: "grok"
     model: "base"
     timeout_seconds: 30
   ```

   ```sh
   .venv/bin/walkietalk -c config.local.yaml talk --capture --once
   ```

   Expect `Listener: grok (grok-voice-transcribe-2.0; SuperGrok Plus login)` and
   a transcript. Same child sentence as step 1. This is not Grok Build and does
   not use `XAI_API_KEY`. If login is missing, the error tells you to run
   `grok login`.

3. **Optional billed API only if you choose it.** `stt.backend: grok_api` needs
   `XAI_API_KEY`. That is API credits, not SuperGrok Plus. Missing key must
   error; it must not fall back to faster-whisper or SuperGrok login.

Phase 4 family demonstration passed for faster-whisper and SuperGrok `grok`.
The billed `grok_api` path was skipped. Phase 4 is merged in
[PR #4](https://github.com/bbusenius/walkietalk/pull/4).

