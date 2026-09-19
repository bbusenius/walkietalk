# walkietalk

A Linux radio bridge, built with kids one verified phase at a time.

**Phase 1:** control an AIOC's push-to-talk (PTT) line and play a short speech
WAV through its audio interface. **Phase 2:** capture radio speech on the pinned
AIOC input, detect an utterance by energy, and transcribe it locally with
faster-whisper. Wake names, pluggable STT, AI backends, and generated voices
are future phases. See [the phase checkpoints](docs/PHASES.md).

Phase 1 verification is complete: the family observed the Python PTT pulse,
heard the spoken WAV on the receiving walkie with PTT released afterward, and
confirmed that Ctrl+C releases PTT immediately. The girls understand the talk
control demonstration. Phase 1's 36 automated tests and the CI checks passed.
Phase 2 family radio transcription also passed; it is implemented on
`phase-2-stt-listen` and is not committed until Brad authorizes publishing.

The browser flasher is not part of this application. Python talks directly to
the local AIOC through USB serial and PortAudio. No firmware update is required
by these instructions.

## Install on Ubuntu

Requires Python 3.11 or later, an AIOC, and its connected radio for live tests.

```sh
sudo apt install python3-venv libportaudio2
git clone https://github.com/bbusenius/walkietalk.git
cd walkietalk
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/walkietalk --help
.venv/bin/walkietalk models
```

`models` downloads the configured faster-whisper weights (tiny or base) into
`~/.cache/walkietalk/faster-whisper/`. That is a one-time network step of about
75 MB (tiny) or 145 MB (base). Listening fails clearly if the model is missing
instead of starting a surprise download during a family demo.

`main` contains accepted checkpoints. To review work before it is merged,
check out its PR branch before installing.

## Try it without transmitting

```sh
.venv/bin/walkietalk ptt --seconds 1
.venv/bin/walkietalk listen speech.wav
```

`ptt` logs simulated PTT ON/OFF without opening serial or audio hardware.
`play speech.wav` also defaults to simulation, validating the file but making
no sound. `listen speech.wav` runs energy detection and local transcription on
that file; it does not open the AIOC or PTT. Real transmission still requires
both a configuration file and `--transmit`. Live radio transcription uses
`listen --capture` and is receive-only.

## Configure the AIOC

```sh
.venv/bin/walkietalk devices
cp config.example.yaml config.local.yaml
```

Edit `config.local.yaml` using the exact AIOC input/output names from `devices`
and its `/dev/serial/by-id/...` path. Capture and playback may have the same
name. Do not select `default`, `pulse`, or `pipewire`: the sound must go to the
radio interface. The program reports current indices as well as names; it never
silently substitutes another device. If an ALSA card number in the name changes
after reconnecting, run `devices` again and update the config.

Keep `ptt.line: dtr` for the AIOC's standard firmware: DTR high means talk,
RTS stays low, and both lines are low when idle. `rts` is available only for
hardware whose documented/tested wiring requires it. No programming bytes are
sent to the radio. See the [AIOC firmware documentation](https://github.com/skuep/AIOC).

`audio.gain` scales WAV samples from 0 (excluded) to 1; begin with 0.25.
`radio.max_tx_seconds` defaults to 10 and cannot exceed 30.
`radio.settle_seconds` defaults to 0.2, allowing PTT to settle before playback.
The WAV plus settle time must fit the transmit limit.

`vad.energy_threshold` is RMS from 0 to 1; begin with 0.02 and tune from the
logged values. `vad.hangover_ms` is how long silence may last before an
utterance ends (400 ms). `vad.max_utterance_seconds` caps a single capture
(12 seconds, at most 30). `stt.model` is `tiny` or `base`. Phase 3 wake fields
and the `SttBackend` plug are still rejected.

### Linux serial permissions

Close the webpage's serial connection and other programs using the AIOC.
Check the device path and permissions:

```sh
ls -l /dev/serial/by-id/
ls -l /dev/ttyACM0
id -nG
```

On Ubuntu, serial devices commonly belong to `dialout`. If yours does, permanent
access for your normal user is:

```sh
sudo usermod -aG dialout "$USER"
```

Log out of the desktop and log back in, then check `id -nG` again. For a temporary
test in the current session, if the AIOC resolves to `/dev/ttyACM0`:

```sh
sudo setfacl -m "u:$USER:rw" /dev/ttyACM0
```

Install Ubuntu's `acl` package if `setfacl` is missing. Substitute the actual
AIOC device path if different. Temporary access expires on unplug/replug or
reboot. Run walkietalk as your normal user; do not run the application with sudo.

Now verify names and permissions without opening PTT or transmitting:

```sh
.venv/bin/walkietalk -c config.local.yaml check
```

`check` verifies device enumeration, serial file permissions, and whether the
speech model is already downloaded. The subsequent live command checks whether
the serial port and audio device can actually open; an audio device may still
be busy. If it is busy, close recording/playback apps or deselect the AIOC in
desktop sound settings, then retry. `listen --capture` does not open PTT and
does not require serial permission.

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

3. **Live radio sentence.** After the program prints `Waiting for speech`, say a
   clear sentence on a handheld walkie without long pauses, then stop talking:

   ```sh
   .venv/bin/walkietalk -c config.local.yaml listen --capture
   ```

   Capture should end about 0.4 seconds after you stop. The TX light stays off.
   The screen shows roughly what you said, for example:

   ```text
   Receive-only: PTT will not be opened.
   Waiting for speech (timeout 60s, threshold 0.020)...
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
   `Waiting for speech`, then press Ctrl+C before talking. Expect `Stopped;
   capture closed.`, exit status 130, and no TX light.

**Explain to the girls:** “The computer writes down what we said so the AI can
understand the question.”

Phase 2 is not complete until this live radio transcript is observed, the
family explanation is done, and the automated checks pass. Mocked tests and a
file transcript are not a substitute for hearing a walkie and seeing the words.

## What the cleanup can guarantee

The parent process owns PTT. A separate child handles potentially blocking audio
I/O; the parent enforces the playback deadline and releases PTT before terminating
a stalled child. Normal completion, handled errors, Ctrl+C, and SIGTERM run the
release/close path. Both serial lines are attempted even if releasing one fails.

This is software cleanup, not a hardware watchdog. SIGKILL, power loss, a hung
kernel/serial driver, or a disconnected USB interface can prevent confirmed
release. Turn the radio off if its TX light remains on. Serial levels are set
low before opening, but drivers can briefly change them on open;
[pySerial documents this limitation](https://pyserial.readthedocs.io/en/latest/pyserial_api.html#serial.Serial.open).

## Development checks

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
.venv/bin/python -m build
```

Tests use simulated serial/audio, isolated fake playback workers, or mocked
speech models. They never transmit and they do not download Whisper weights.
Keep recordings, credentials, and machine-specific configuration out of
commits (`config.local.yaml`, `.env`, and `recordings/` are ignored).

MIT licensed. Each phase has a separate pull request and a documented demo.
