# walkietalk

A Linux radio bridge with interchangeable AI agents and voices. Speak into a
walkie-talkie, address the configured wake phrase, and get an answer from your
chosen agent. The bridge can print replies or speak them over the radio.

Built as a family learning project, with automated checks and a live
demonstration for each phase. The [phase history](docs/PHASES.md) records those
checkpoints; the guides below are for installing and using the application.

## Install and try it

Ubuntu with Python 3.11 or later:

```bash
sudo apt update
sudo apt install git python3-venv libportaudio2
git clone https://github.com/bbusenius/walkietalk.git
cd walkietalk
python3 -m venv .venv
.venv/bin/python -m pip install .
source .venv/bin/activate
walkietalk init
walkietalk -c "$HOME/.config/walkietalk/config.yaml" config-check
walkietalk -c "$HOME/.config/walkietalk/config.yaml" agent-check "Hello."
```

Expect `Config OK` and a fixed pretend reply from the offline stub. These checks
do not use a radio, download a model, or require an account. `init` creates a
private configuration directory and refuses to overwrite an existing one.

Follow [Install on Ubuntu](docs/INSTALL.md) to configure devices, select real
backends, download any local speech models, and try the receive-only path.
Walkietalk has not been published to PyPI; install from this repository or a
wheel built from it. For development, use `pip install -e '.[dev]'` instead.

## Choose your backends

| Job | Implemented choices |
| --- | --- |
| Recognize incoming speech (STT) | Local faster-whisper; Grok Voice Transcribe with saved login or explicit billed API key |
| Answer (agent) | Offline stub; Hermes environment; Codex CLI; Grok Build CLI; Claude CLI or explicit Messages API |
| Speak the answer (TTS) | Local Piper; Grok speech with saved login or explicit billed API key; the Hermes environment's speech provider |

These choices are independent. A Hermes agent retains its own context, skills,
and memory, even if another backend speaks its answer. Grok Build is an agent;
Grok Voice Transcribe is STT. No backend silently substitutes another backend or
switches from subscription login to billed API access.

[Backend setup and verification](docs/BACKENDS.md) lists exact configuration
names, login commands, connection checks, and the limits of previous testing.
Official CLI logins stay CLI-owned. Local connection tokens and API keys can
live in the private `credentials.env` beside your config; Walkietalk loads it
when you pass `-c`, with existing environment variables taking precedence.

## Use the radio

Configure exact audio names and a stable serial path first:

```bash
walkietalk devices
# Edit ~/.config/walkietalk/config.yaml with your devices and backend choices.
walkietalk -c "$HOME/.config/walkietalk/config.yaml" check
walkietalk -c "$HOME/.config/walkietalk/config.yaml" talk --capture
```

`talk --capture` listens continuously and prints replies. It does not transmit.
To enable spoken radio answers, add `--transmit` with an explicit config after
setting up your voice and confirming the hardware. AIOC normally keys with DTR
high while RTS stays low; both low is idle. Run as your normal user.

The parent owns PTT and limits transmission time even if playback blocks.
See [radio behavior and limits](docs/CONFIGURATION.md#radio-and-playback) for what
software cleanup can and cannot guarantee. Use the appropriate licensed radio
setup and supervise live transmission.

## Guides

- [Installation, upgrades, and removal](docs/INSTALL.md)
- [Configuration, credentials, and command reference](docs/CONFIGURATION.md)
- [Backend setup and supported integrations](docs/BACKENDS.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Development and adding an adapter](docs/DEVELOPMENT.md)
- [Phase 8 installation demonstration](docs/PHASE8-DEMO.md)

MIT licensed. Python package version: `0.1.0`.
