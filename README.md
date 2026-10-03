# walkietalk

A Linux radio bridge with interchangeable AI agents and voices, including
optional WhatsApp and Signal messaging. Speak into a walkie-talkie and use
a configured wake phrase to ask your chosen AI agent a question or send a
transcribed message to a contact. Hear agent answers and contact messages
over the radio. Optional [operator mode](docs/MESSAGING.md#operator-mode)
holds messages for local review and approval. Supported agents include Grok,
Codex, Hermes, and Claude.

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

The reference setup is an AIOC USB interface on a Baofeng UV-5G Plus gateway
radio. Other hardware may need different audio selection or PTT wiring.

| Item | Role | Reference |
| --- | --- | --- |
| **Baofeng UV-5G Plus** | Gateway radio with the K1 jack for the AIOC | [Single](https://www.baofengradio.com/products/uv-5g-plus-5w-gmrs-radio) or [Pair](https://www.baofengradio.com/products/uv-5g-plus-5w-gmrs-radio-1-pair) |
| **NA6D AIOC** | USB audio in and out, and PTT on the serial DTR line | [Interface](https://na6d.com/products/aioc-ham-radio-all-in-one-cable) |
| **USB-C data cable** | Data and power for the AIOC; a charge-only cable will not work | [Cable](https://na6d.com/products/na6d-usb) |

Connect the gateway radio to the computer through the AIOC. Talk to the bridge
from a second handheld on the same channel and settings. A Raspberry Pi makes
a good dedicated host; see the [installation guide](docs/INSTALL.md#platforms).

| Optional | Role | Reference |
| --- | --- | --- |
| **Raspberry Pi** | Dedicated Linux host | [Raspberry Pi](https://www.raspberrypi.com/) |
| **Midland T51VP3 X-Talker, 2-pack** | More handheld radios | [Midland](https://midlandusa.com/products/t51vp3-x-talker-frs-walkie-talkie-2-pack) |

## Install

On Ubuntu or Debian, with [Rust](https://rustup.rs) installed:

```bash
sudo apt install build-essential pkg-config cmake clang libclang-dev libasound2-dev ffmpeg
git clone https://github.com/bbusenius/walkietalk.git
cd walkietalk
cargo install --locked --path crates/walkietalk
walkietalk --version
```

The [installation guide](docs/INSTALL.md) covers Piper, the Whisper model,
serial permissions, messaging tools, and upgrades.

## Set up

```bash
walkietalk init          # creates ~/.config/walkietalk/config.toml and credentials.toml
walkietalk devices       # lists the AIOC's audio names and serial path
```

Edit `~/.config/walkietalk/config.toml`:

- `[audio] input` and `output`: the AIOC's `plughw:CARD=...` names from `devices`.
- `[ptt] port`: the AIOC's `/dev/serial/by-id/...` path. It keys with DTR.
- `[wake] name`: the name you will say to address the agent, plus aliases for
  common mistranscriptions.
- `[agent] backend`, `[stt] backend`, `[tts] backend`: see [backends](docs/BACKENDS.md).

Tokens and API keys go in `credentials.toml` beside the config, never in the
config itself. See the [configuration reference](docs/CONFIGURATION.md).

```bash
walkietalk models        # download the local Whisper model
walkietalk config-check  # validate without touching anything
walkietalk check         # confirm devices and backends are ready; never transmits
```

## Run

Listen and print replies (receive only, the radio is never keyed):

```bash
walkietalk talk --capture
```

Hold the handheld's talk button, say "Charlotte, why is the sky blue?", and
release. Expect the transcript and the agent's reply in the terminal.

Speak replies on the radio. Transmitting always needs the config named
explicitly:

```bash
walkietalk -c ~/.config/walkietalk/config.toml talk --capture --transmit
```

Check that the first word is audible and that the gateway radio stops
transmitting afterwards. Ctrl+C stops the bridge and releases the transmitter.

## Guides

- [Installation](docs/INSTALL.md)
- [Configuration reference and commands](docs/CONFIGURATION.md)
- [Backend setup](docs/BACKENDS.md)
- [WhatsApp, Signal, and operator mode](docs/MESSAGING.md)
- [Hermes speech companion](docs/HERMES-SPEECH.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Development](docs/DEVELOPMENT.md)

MIT licensed.
