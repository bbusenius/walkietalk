# walkietalk

A Linux radio bridge with interchangeable AI agents and voices, including
optional WhatsApp and Signal messaging. Speak into a walkie-talkie and use
a configured wake phrase to ask your chosen agent a question or send a
transcribed message to a contact. Hear agent answers and contact messages
over the radio. Optional [operator mode](docs/MESSAGING.md#operator-mode) holds
messages for local review and approval. Supported agents include Grok, Codex,
Hermes, and Claude.

> [!IMPORTANT]
> You are responsible for the rules of your radio service. Walkietalk does not
> decide whether a transmission is permitted. Optional
> [operator mode](docs/MESSAGING.md#operator-mode) requires local review and
> approval of each incoming and outgoing WhatsApp or Signal message. On US GMRS,
> review licensing, station identification, and the requirement that an operator
> monitor the station while it transmits. A GMRS telephone connection is
> prohibited. Repeater, base, and fixed stations may connect to the telephone
> network or another network only for remote control. Operator approval is not
> an exception. The FCC's GMRS page addresses interconnection with other
> networks to carry communications, including internet-linked repeaters, and
> does not address a stored WhatsApp or Signal message. See the
> [FCC's GMRS page](https://www.fcc.gov/wireless/bureau-divisions/mobility-division/general-mobile-radio-service-gmrs)
> and [47 CFR Part 95, Subpart E](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-D/part-95/subpart-E).
> We make no claims as to the legality of using this product in any way.

## Hardware

The reference setup uses an AIOC USB interface and a Baofeng UV-5G Plus gateway
radio. Other hardware may require changes to audio selection or PTT wiring.

| Item | Role | Reference |
| --- | --- | --- |
| **Baofeng UV-5G Plus** | Gateway radio with K1 jack for AIOC | [Single](https://www.baofengradio.com/products/uv-5g-plus-5w-gmrs-radio) or [Pair](https://www.baofengradio.com/products/uv-5g-plus-5w-gmrs-radio-1-pair)|
| **NA6D AIOC** | USB radio audio input/output and PTT control | [Interface](https://na6d.com/products/aioc-ham-radio-all-in-one-cable) |
| **USB-C data cable** | Data and power for AIOC; a charge-only cable will not work | [Cable](https://na6d.com/products/na6d-usb) |

Connect the gateway radio to the computer through the AIOC. Use a second,
compatible handheld to talk to the bridge, with matching channel and radio
settings.

### Optional

| Item | Role | Reference |
| --- | --- | --- |
| **Raspberry Pi** | Dedicated Linux host | [Raspberry Pi](https://www.raspberrypi.com/) |
| **Midland T51VP3 X-Talker, 2-pack** | Additional handheld radios | [Midland](https://midlandusa.com/products/t51vp3-x-talker-frs-walkie-talkie-2-pack) |

A Raspberry Pi is a good affordable option if you don't have a Linux computer.
See the [platform notes](docs/INSTALL.md#platforms) before choosing hardware.
Windows/WSL and macOS are untested.

## Install

Ubuntu or Debian with Python 3.11 or later:

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

Walkietalk has not been published to PyPI; install from this repository or a
wheel built from it. For development, use `pip install -e '.[dev]'` instead.
See the [installation guide](docs/INSTALL.md) for a software-only trial,
upgrades, and removal.

## Configure

### Select your configuration file

For a new setup, run:

```bash
walkietalk init
```

This creates `~/.config/walkietalk/config.yaml` and a private `credentials.env`
beside it. It refuses to overwrite an existing directory. Open `config.yaml`
in your editor for the device and backend settings below.

If you prefer to use a `config.local.yaml` in the project directory, copy
[src/walkietalk/data/config.example.yaml](src/walkietalk/data/config.example.yaml)
to a new `config.local.yaml` and skip `init`. Use `-c config.local.yaml` in place
of the config path in the examples. Walkietalk loads the file you pass with `-c`;
it does not automatically search either location.

### Connect and select your devices

Connect the AIOC to the computer with the USB data cable and to the gateway radio,
then list the devices:

```bash
walkietalk devices
```

Copy the exact AIOC capture and playback names into `audio.input_device` and
`audio.output_device`, and its stable serial path into `ptt.serial_port`. Select
the physical AIOC rather than the system default audio device. AIOC normally
uses `ptt.line: dtr`: DTR high keys the radio while RTS stays low; both low is
idle. Run Walkietalk as your normal user; see
[serial permissions](docs/INSTALL.md#linux-serial-permissions) if access is denied.

Set `wake.primary` to the wake phrase you'll use to activate your agent over the
radio, and update `wake.aliases` for possible mistranscriptions or variations of
that phrase. The example uses `charlotte`. Keep the complete configuration file
and edit its values; see [required and optional fields](docs/CONFIGURATION.md).

### Choose your backends

In the same config file, choose speech recognition (`stt.backend`), an answering
agent (`agent.backend`), and a speaking voice (`tts.backend`). Alternatively,
select `agent.backend: grok_realtime` to handle all three in one live speech to
speech session.

| Job | Implemented choices |
| --- | --- |
| Recognize incoming speech (STT) | Local faster-whisper; Grok Voice Transcribe with saved login or explicit billed API key |
| Answer (agent) | Offline stub; Hermes environment; Codex CLI; Grok Build CLI; Claude CLI or explicit Messages API |
| Live speech-to-speech | Grok realtime (`agent.backend: grok_realtime`); native audio input/output with billed xAI API access |
| Speak the answer (TTS) | Local Piper; Grok speech with saved login or explicit billed API key; the Hermes environment's speech provider |

The default config selects local faster-whisper, the offline stub agent, and
Piper. Choose a real agent for AI answers; the stub only returns a fixed pretend
reply. For local speech, complete the
[Whisper model download and Piper installation](docs/INSTALL.md#3-select-the-three-backends)
before running the radio.

The STT, text-agent, and TTS choices are independent. A Hermes agent retains its
own context, skills, and memory, even if another backend speaks its answer.
Grok realtime uses its own audio and transcripts instead of the separate STT/TTS
settings. See
[realtime configuration](docs/CONFIGURATION.md#combined-voice-agent-optional-realtime).

[Backend setup and verification](docs/BACKENDS.md) lists exact configuration
names, login commands, and connection checks. Complete the setup and checks for
your selected backends before continuing. Official CLI logins stay CLI-owned.
Local connection tokens and API keys can live in a private `credentials.env`
beside your config; see [credentials](docs/CONFIGURATION.md#credentials).
Walkietalk loads it when you pass `-c`, with existing environment variables
taking precedence. No backend silently substitutes another backend or switches
from subscription login to billed API access.

## Run

### Check reception and test printed replies

With the radios on the same channel, validate your configuration and devices,
then start listening:

```bash
walkietalk -c "$HOME/.config/walkietalk/config.yaml" config-check
walkietalk -c "$HOME/.config/walkietalk/config.yaml" check
walkietalk -c "$HOME/.config/walkietalk/config.yaml" talk --capture
```

Hold the talk button on your handheld, say "Charlotte, why is the sky blue?",
then release the button. Substitute your configured wake name if you changed
it. Expect a transcript and an agent reply in the terminal. `talk --capture`
listens continuously and prints replies without transmitting.

With [operator mode](docs/MESSAGING.md#operator-mode) enabled, add `--panel` to
keep live logs above fixed message review controls in the same terminal.

### Enable spoken radio replies

After checking reception, the agent connection, and voice synthesis, enable
radio replies:

```bash
walkietalk -c "$HOME/.config/walkietalk/config.yaml" talk --capture --transmit
```

Ask a question and release the handheld's talk button to hear the answer.
Confirm the first word is audible and the gateway radio stops transmitting
afterward. This is the command for ongoing radio conversations; Ctrl+C stops
it. In a new terminal, activate the virtual environment again and use the same
config path.

The parent owns PTT and limits transmission time even if playback blocks.
See [radio behavior and limits](docs/CONFIGURATION.md#radio-and-playback) for what
software cleanup can and cannot guarantee, and
[troubleshooting](docs/TROUBLESHOOTING.md) if you do not hear a reply.

## Guides

- [Installation, upgrades, and removal](docs/INSTALL.md)
- [WhatsApp and Signal messaging](docs/MESSAGING.md), including [optional dependencies](docs/INSTALL.md#optional-messaging-dependencies)
- [Configuration, credentials, and command reference](docs/CONFIGURATION.md)
- [Backend setup and supported integrations](docs/BACKENDS.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Development and adding an adapter](docs/DEVELOPMENT.md)

MIT licensed. Python package version: `0.1.0`.
