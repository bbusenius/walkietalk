# walkietalk

A Linux radio bridge, built with kids one verified phase at a time.

**Phase 1:** control an AIOC's push-to-talk (PTT) line and play a short speech
WAV through its audio interface. Speech recognition, wake names, AI backends,
and generated voices are future phases. See [the phase checkpoints](docs/PHASES.md).

The family has verified the Python PTT pulse: the gateway transmit light turned
on and off, and the girls understand what the test demonstrated. Live speech
playback and interruption demonstrations are still pending.

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
```

`main` contains accepted checkpoints. To review work before it is merged,
check out its PR branch before installing.

## Try it without transmitting

```sh
.venv/bin/walkietalk ptt --seconds 1
```

This logs simulated PTT ON/OFF without opening serial or audio hardware.
`play speech.wav` also defaults to simulation, validating the file but making
no sound. Real transmission requires both a configuration file and `--transmit`.

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

`check` verifies device enumeration and serial file permissions. The subsequent
live command checks whether the serial port and audio device can actually open;
an audio device may still be busy. If it is busy, close recording/playback apps
or deselect the AIOC in desktop sound settings, then retry.

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

Phase 1 passes only when the live light/audio checks and automated checks pass.
Logs alone cannot prove the physical radio released PTT. Brad runs the code and explains it to the girls before we commit or push any
phase implementation. Then record the live result in its PR before accepting
the checkpoint and beginning phase 2.

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

Tests use simulated serial/audio or isolated fake playback workers. They never
transmit. Keep recordings, credentials, and machine-specific configuration out
of commits (`config.local.yaml`, `.env`, and `recordings/` are ignored).

MIT licensed. Each phase has a separate pull request and a documented demo.
