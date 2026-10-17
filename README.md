# walkietalk

A Linux radio bridge with interchangeable AI agents and voices, including
optional WhatsApp and Signal messaging. Speak into a walkie-talkie using
a configured wake phrase to ask your chosen AI agent a question or send a
transcribed message to a contact. Hear agent answers and contact messages
over the radio. Optional [operator mode](docs/MESSAGING.md#operator-mode)
holds messages for local review and approval. Supported agents include Grok,
Codex, Hermes, and Claude.

Want a hand? [Relay](https://x.ai/bot/HVbym4T7Gof6G7C9CFS-V) is a Grok Bot
companion for walkietalk. It can help you choose gear, get a GMRS license,
install and configure walkietalk, run it for you, and handle changes and
upgrades later. It also answers GMRS rule questions, teaches radio lingo like
"roger," "over," and 10-codes, and makes a printable SARNEG code booklet kids
can fill in and use with walkietalk's SARNEG mode.

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
| **NA6D AIOC Case** | protective covering for the AIOC | [Case](https://na6d.com/products/aioc-case) |
| **Raspberry Pi** | Dedicated Linux host | [Raspberry Pi](https://www.raspberrypi.com/) |
| **Midland T51VP3 X-Talker, 2-pack** | More handheld radios | [Midland](https://midlandusa.com/products/t51vp3-x-talker-frs-walkie-talkie-2-pack) |

## Install

Prefer a guided setup? Import [Relay](https://x.ai/bot/HVbym4T7Gof6G7C9CFS-V)
into Grok Bot and it will help you install and configure walkietalk on your
computer.

On Omarchy or Arch, install the build tools from the Arch repositories, then
install Rust with the `rustup` package:

```bash
omarchy pkg add base-devel pkgconf cmake clang alsa-lib ffmpeg git rustup
rustup default stable
export PATH="$HOME/.cargo/bin:$PATH"
git clone https://github.com/bbusenius/walkietalk.git
cd walkietalk
cargo install --locked --path crates/walkietalk
walkietalk --version
```

On plain Arch, `sudo pacman -S --needed` installs those packages. The `export`
above applies to the current shell. Put that same line in `~/.bashrc` so later
shells can find `walkietalk`. The [installation guide](docs/INSTALL.md) covers
that line, Piper, the Whisper model, ALSA device names, serial permissions,
messaging tools, and upgrades.

On Ubuntu or Debian, with [Rust](https://rustup.rs) installed:

```bash
sudo apt install build-essential pkg-config cmake clang libclang-dev libasound2-dev ffmpeg
git clone https://github.com/bbusenius/walkietalk.git
cd walkietalk
cargo install --locked --path crates/walkietalk
walkietalk --version
```

## Set up

```bash
walkietalk init          # creates ~/.config/walkietalk/config.toml and credentials.toml
walkietalk devices       # lists the AIOC's audio names and serial path
```

Edit `~/.config/walkietalk/config.toml`:

- `[audio] input` and `output`: the AIOC's `plughw:CARD=...` names from `devices`.
- `[ptt] port`: the AIOC's `/dev/serial/by-id/...` path. It keys with DTR.
- `[wake] wake_phrase`: the phrase you will say to address the agent, plus aliases for
  common mistranscriptions.
- `[agent] backend`, `[stt] backend`, `[tts] backend`: see [backends](docs/BACKENDS.md).

Tokens and API keys go in `credentials.toml` beside the config, never in the
config itself. See the [configuration reference](docs/CONFIGURATION.md).

The PTT port is writable by a group, not by every user. On Omarchy or Arch
that group is `uucp` (Arch has no `dialout` group). On Ubuntu or Debian it is
`dialout`. If `id -nG` does not list it:

```bash
# Omarchy or Arch
sudo usermod -aG uucp "$USER"

# Ubuntu or Debian
sudo usermod -aG dialout "$USER"
```

Log out and back in, then confirm with `id -nG`. Run walkietalk as your
normal user. The [installation guide](docs/INSTALL.md#serial-permissions) has
the permission checks.

```bash
walkietalk models        # download the local Whisper model
walkietalk config-check  # validate without touching anything
walkietalk check         # confirm devices and backends are ready; never transmits
walkietalk morse-check --output id.wav  # write the Morse station ID to a WAV; never transmits
```

## Run

Listen and print replies (receive only, the radio is never keyed):

```bash
walkietalk talk --capture
```

Hold the handheld's talk button, say "Charlotte, why is the sky blue?", and
release. Expect the transcript and the agent's reply in the terminal.

Speak replies on the radio:

```bash
walkietalk talk --capture --transmit
```

Check that the first word is audible and that the gateway radio stops
transmitting afterwards. Ctrl+C stops the bridge and releases the transmitter.

## Guides

- [Relay, a Grok Bot setup assistant](https://x.ai/bot/HVbym4T7Gof6G7C9CFS-V)
- [Installation](docs/INSTALL.md)
- [Configuration reference and commands](docs/CONFIGURATION.md)
- [Backend setup](docs/BACKENDS.md)
- [WhatsApp, Signal, and operator mode](docs/MESSAGING.md)
- [Hermes speech companion](docs/HERMES-SPEECH.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Development](docs/DEVELOPMENT.md)

MIT licensed.
