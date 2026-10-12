# Installation

## Platforms

Walkietalk runs on Linux with ALSA audio: Omarchy, Arch Linux, Ubuntu,
Debian, and Raspberry Pi OS (64-bit) on x86_64 or ARM64. Hardware access uses
ALSA devices and a USB serial port; macOS and Windows are not supported.

On a Raspberry Pi 4 or 5, local Whisper `tiny` or `base` keeps transcription
quick; `small` is noticeably slower there. Remote recognition (xAI) or the
realtime voice moves that work off the Pi.

## 1. System packages and Rust

### Omarchy or Arch Linux

```bash
omarchy pkg add base-devel pkgconf cmake clang alsa-lib ffmpeg git rustup
rustup default stable
```

`omarchy pkg add` installs those Arch packages. On plain Arch, without the
`omarchy` command:

```bash
sudo pacman -S --needed base-devel pkgconf cmake clang alsa-lib ffmpeg git rustup
rustup default stable
```

The `rustup` package installs the toolchain manager. `rustup default stable`
downloads Rust 1.95 or newer. `cargo` and `rustc` are then `/usr/bin/cargo`
and `/usr/bin/rustc`. Programs from `cargo install`, including `walkietalk`,
go in `~/.cargo/bin`. Add that directory to `PATH` in `~/.bashrc`:

```bash
export PATH="$HOME/.cargo/bin:$PATH"
```

Open a new shell before running `walkietalk`.

### Ubuntu or Debian

```bash
sudo apt update
sudo apt install build-essential pkg-config cmake clang libclang-dev libasound2-dev ffmpeg git
```

Install Rust with [rustup](https://rustup.rs) (Rust 1.95 or newer):

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

The installer adds `~/.cargo/bin` to `PATH` for new shells.

| Omarchy or Arch | Ubuntu or Debian | Why |
| --- | --- | --- |
| `base-devel` | `build-essential` | Compiling |
| `pkgconf` | `pkg-config` | Finds libraries while compiling |
| `cmake`, `clang` | `cmake`, `clang`, `libclang-dev` | Building whisper.cpp. On Arch, `clang` includes libclang |
| `alsa-lib` | `libasound2-dev` | ALSA audio. On Arch the headers are in `alsa-lib` |
| `ffmpeg` | `ffmpeg` | Voice notes and the Hermes speech companion; not needed otherwise |
| `git` | `git` | Cloning the repository |

## 2. Build and install

```bash
git clone https://github.com/bbusenius/walkietalk.git
cd walkietalk
cargo install --locked --path crates/walkietalk
walkietalk --version
```

This puts `walkietalk` in `~/.cargo/bin`. The first build compiles
whisper.cpp and takes a few minutes.

## 3. Create your settings

```bash
walkietalk init
walkietalk config-check
```

`init` creates `~/.config/walkietalk/` (private, mode 700) with `config.toml`
and an owner-only `credentials.toml`. It never overwrites an existing
directory; use `walkietalk init --directory PATH` for another location, and
pass that config with `-c PATH/config.toml`.

Commands read `~/.config/walkietalk/config.toml` unless you pass `-c FILE`.

## 4. Local speech recognition

With `[stt] backend = "whisper"`, download the configured model once:

```bash
walkietalk models
```

Models are English whisper.cpp models from Hugging Face (`tiny` about 78 MB,
`base` about 148 MB, `small` about 488 MB), checked against their published
SHA-256 and stored in `~/.local/share/walkietalk/models/`. Normal listening
never downloads anything.

## 5. Piper voice (local speech)

Piper is a separate program.

On Omarchy or Arch:

```bash
omarchy pkg add python-pipx
pipx install piper-tts
```

On plain Arch, `sudo pacman -S --needed python-pipx` is the same install. The
Arch package named `piper` configures gaming mice; it is a different program.
`pipx` puts `piper` in `~/.local/bin`. Omarchy already has that directory on
`PATH`. On plain Arch, run `pipx ensurepath` and open a new shell if `piper`
is still not found.

On Ubuntu or Debian:

```bash
sudo apt install pipx
pipx install piper-tts
```

Download a voice (both files, kept side by side):

```bash
mkdir -p ~/.local/share/walkietalk/piper
cd ~/.local/share/walkietalk/piper
base=https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium
curl -LO $base/en_US-amy-medium.onnx
curl -LO $base/en_US-amy-medium.onnx.json
```

The template config already points `[tts.piper] model` at that file. Check it:

```bash
walkietalk tts-check "Hello. This is Amy." --output amy.wav
```

Expect a 48 kHz mono WAV and "No hardware opened."

## 6. Connect the AIOC

Plug the AIOC into the computer with a USB data cable and into the gateway
radio.

On Omarchy and Arch, ALSA ships with extended device hints off
(`defaults.namehint.extended off` in `/usr/share/alsa/alsa.conf`). Until you
turn them on, `walkietalk devices` prints no cards, and a
`plughw:CARD=<name>` setting fails `check`. `devices --all` still lists
numbered names such as `plughw:CARD=0,DEV=0`, and those numbers change when
you replug the interface.

Create `~/.asoundrc` if you do not have one, and add this line:

```
defaults.namehint.extended on
```

Open a new shell. The desktop default stays PipeWire; the line only adds the
hardware names to the device list.

```bash
walkietalk devices
```

Copy the AIOC's `plughw:CARD=...,DEV=0` name into `[audio] input` and
`output`, and its `/dev/serial/by-id/...` path into `[ptt] port`. These names
survive reconnects, unlike card numbers. `walkietalk devices --all` lists every
ALSA device if you need something unusual.

Keep the desktop's sound server away from the AIOC: in your desktop sound
settings, set the AIOC's profile to "Off" (or disable it in `pavucontrol`).
Otherwise it may be busy when walkietalk opens it. On Omarchy the sound server
is PipeWire, and `pavucontrol` is not installed. Install it if you want that
window.

### Serial permissions

```bash
ls -l /dev/serial/by-id/
id -nG
```

The port's group is `uucp` on Omarchy and Arch, and `dialout` on Ubuntu and
Debian. Arch has no `dialout` group. If you are not in the port's group:

```bash
# Omarchy or Arch
sudo usermod -aG uucp "$USER"

# Ubuntu or Debian
sudo usermod -aG dialout "$USER"
```

Log out and back in, then confirm with `id -nG`. Run walkietalk as your
normal user, never with `sudo`.

```bash
walkietalk check
```

`check` confirms the audio names, the serial port's permissions, and the
backends without opening a stream or keying the radio.

## 7. Messaging tools (optional)

| Feature | Program |
| --- | --- |
| WhatsApp | [`wacli`](https://github.com/openclaw/wacli#install) |
| Signal | [`signal-cli`](https://github.com/AsamK/signal-cli#installation) |
| Voice notes | `ffmpeg`, with the `libopus` encoder for outgoing notes |

They must be on `PATH` for the user running walkietalk. See
[messaging](MESSAGING.md).

## 8. Hermes speech companion (optional)

Only needed for `[tts] backend = "hermes"`; see [Hermes speech](HERMES-SPEECH.md).

```bash
cargo install --locked --path crates/walkietalk-hermes-speech
```

## Running as a service

Walkietalk does not install a service. If you add a systemd user unit, use
the same user and absolute paths, and test the exact command by hand first.

## Upgrade and remove

```bash
cd walkietalk
git pull --ff-only
cargo install --locked --path crates/walkietalk
walkietalk config-check
```

Compare `crates/walkietalk/templates/config.toml` with your config after an
upgrade; `config-check` reports unknown or invalid settings.

To remove: `cargo uninstall walkietalk`. Your settings, models, voices, and
CLI logins stay; delete them separately if you want them gone.
