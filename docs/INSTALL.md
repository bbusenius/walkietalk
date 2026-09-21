# Install on Ubuntu

Walkietalk runs on Linux with Python 3.11 or later. Automated CI covers Python
3.11–3.13. The hardware reference is an AIOC connected to the gateway radio;
computer-only checks also work without that hardware. Pi/Omarchy packaging and
automatic start-on-login are deferred.

## 1. Install the application

```bash
sudo apt update
sudo apt install git python3-venv libportaudio2
git clone https://github.com/bbusenius/walkietalk.git
cd walkietalk
python3 -m venv .venv
.venv/bin/python -m pip install .
source .venv/bin/activate
walkietalk --version
walkietalk --help
```

Expect version `0.1.0` and command help. The normal installation does not need
pytest, Ruff, provider SDKs, or agent CLIs. Install a selected agent CLI separately.
Piper is optional; to include it, use `pip install '.[piper]'` in the same checkout
and environment. No speech-model downloads occur during these help checks.

The repository is the distribution source. There is no published PyPI release
or claim that `pip install walkietalk` installs this project. A locally built
wheel is also supported:

```bash
python -m pip install /path/to/walkietalk-0.1.0-py3-none-any.whl
```

Use the Python in your chosen virtual environment. Dependencies are resolved
within that environment; the project does not change the system Python.

## 2. Create your configuration

```bash
walkietalk init
walkietalk -c "$HOME/.config/walkietalk/config.yaml" config-check
walkietalk -c "$HOME/.config/walkietalk/config.yaml" agent-check "Hello."
walkietalk ptt --seconds 0.1
```

Expected results: `Config OK`, a fixed pretend answer, then `DRY RUN` PTT ON/OFF.
No hardware, provider account, or network is needed for these checks. Device
placeholders are valid configuration strings but must be replaced before hardware
use. `config-check` checks the schema; `check` checks actual device availability.

`init` creates `config.yaml` and `credentials.env` with owner-only permissions
inside a new private directory. For another location, use:

```bash
walkietalk init --directory /path/to/new-settings-directory
```

It refuses an existing directory, including an empty one; it never resets your
settings. Existing users can keep `config.local.yaml` and add a private
`credentials.env` in the same directory. See [credentials](CONFIGURATION.md#credentials)
for the format, environment precedence, and alternatives.

The packaged template and repository `config.example.yaml` use the same exact
schema. Every field must be present, including fields for unselected backends.
Start from the complete template rather than combining partial YAML snippets.

## 3. Select the three backends

Edit the config, selecting an agent, speech recognition, and a voice using
[Backend setup](BACKENDS.md). Choose local faster-whisper and Piper for speech
without cloud accounts. The stub agent stays offline; real agents have their
own account requirements. Voice setup can wait while testing printed replies.

For local Whisper, select `stt.backend: faster-whisper` and `stt.model: tiny` or
`base`, then explicitly download that model:

```bash
walkietalk -c "$HOME/.config/walkietalk/config.yaml" models
```

Normal listening does not download missing models. Models are cached under
`~/.cache/walkietalk/faster-whisper/`; download time and disk usage depend on the
selected model. The download requires network access once.

For Piper with the Amy voice, from your activated environment in the checkout:

```bash
python -m pip install '.[piper]'
python -m piper.download_voices --download-dir "$HOME/.cache/walkietalk/piper" en_US-amy-medium
mkdir -p recordings
walkietalk -c "$HOME/.config/walkietalk/config.yaml" tts-check \
  'Hello. This is Amy.' --output recordings/amy.wav
```

Select `tts.backend: piper` first. Keep the `.onnx` and matching `.onnx.json`
files together at the configured location. Expect a mono PCM16 WAV at 48 kHz
and `No hardware opened`. Use a new output filename for a repeat check.
Piper's engine and voice licenses are separate from Walkietalk's MIT license;
see [the Piper setup reference](PHASE6-DEMO.md).

## 4. Configure the AIOC

```bash
walkietalk devices
ls -l /dev/serial/by-id/
```

Copy the exact capture and playback names into `audio.input_device` and
`audio.output_device`, and the AIOC's stable serial path into `ptt.serial_port`.
Do not choose the system default. ALSA card numbers can change after reconnecting;
rediscover the names if matching fails. The AIOC's standard PTT is `line: dtr`:
DTR high means talk, RTS stays low, and both lines low means idle.

Close the browser flasher's serial connection and other applications using the
AIOC. No firmware change is required by Walkietalk's installation.

### Linux serial permissions

Inspect the AIOC's actual device ownership and your groups:

```bash
ls -l /dev/ttyACM0
id -nG
```

Substitute the actual serial device if different. On Ubuntu, if the device
belongs to `dialout`, grant access to your normal user:

```bash
sudo usermod -aG dialout "$USER"
```

Log out of the desktop and back in, then verify `id -nG`. A temporary alternative
for the device above is `sudo setfacl -m "u:$USER:rw" /dev/ttyACM0` (install the
`acl` package if needed). That temporary permission can expire on reconnect.
Run Walkietalk as your normal user, never with `sudo`.

```bash
walkietalk -c "$HOME/.config/walkietalk/config.yaml" check
```

Expect the selected audio names, serial path, and backend labels. This does not
key PTT or open an audio stream. Serial/audio may still be busy when you start
real capture or playback; see [Troubleshooting](TROUBLESHOOTING.md).

## 5. Receive, then enable replies on air

```bash
walkietalk -c "$HOME/.config/walkietalk/config.yaml" listen --capture
walkietalk -c "$HOME/.config/walkietalk/config.yaml" talk --capture
```

`listen` transcribes one utterance without the wake gate. `talk` waits through
indefinite silence, applies your wake settings, and prints agent replies.
Ctrl+C stops it. Neither command transmits as shown.

Once the receive path, agent, voice, and radio setup are verified, spoken replies
require both an explicit config and the transmit flag:

```bash
walkietalk -c "$HOME/.config/walkietalk/config.yaml" talk --capture --transmit
```

Run a supervised test using your appropriate licensed radio setup. Confirm the
first word is audible and the radio unkeys afterward. Follow the existing
[voice](PHASE6-DEMO.md) and [radio protection](PHASE7-DEMO.md) checks when bringing
up new hardware. These checks are separate from a computer-only installation test.

## Run again, upgrade, or remove

On another day, activate the same environment and use the same explicit config.
The adjacent private credentials file loads automatically; no shell sourcing is
needed. Official CLIs still use the login belonging to your Linux user.

```bash
cd /path/to/your/walkietalk
source .venv/bin/activate
walkietalk -c "$HOME/.config/walkietalk/config.yaml" talk --capture
```

To upgrade a clean checkout, stop Walkietalk, record the current commit with
`git rev-parse HEAD`, back up your config and credentials privately, then:

```bash
git pull --ff-only
python -m pip install .
walkietalk -c "$HOME/.config/walkietalk/config.yaml" config-check
```

Include `[piper]` if you use that extra. Compare the new example config with
your existing file and add required fields; never overwrite device names,
credentials, gain, wake settings, or station ID with template values. Version
`0.1.0` covers multiple project checkpoints, so record the Git commit or keep
the wheel when you need a precise rollback. Reinstall the prior wheel/checkpoint
and its matching saved config to roll back.

`python -m pip uninstall walkietalk` removes the installed package from the
active environment. Your settings, recordings, voice/model files, and official
CLI logins remain; delete those separately only when you intend to remove them.
No system service, autostart entry, or hardware permission is installed by the
Python package. An optional external service should use the same Linux user,
absolute executable/config paths, login environment, and audio access as the
validated manual command; it is not included in phase 8.
