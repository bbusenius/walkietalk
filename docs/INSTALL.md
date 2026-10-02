# Installation

## Platforms

Walkietalk runs on Linux with ALSA audio: Ubuntu, Debian, and Raspberry Pi
OS (64-bit) on x86_64 or ARM64. Hardware access uses ALSA devices and a USB
serial port; macOS and Windows are not supported.

On a Raspberry Pi 4 or 5, local Whisper `tiny` or `base` keeps transcription
quick; `small` is noticeably slower there. Remote recognition (xAI) or the
realtime voice moves that work off the Pi.

## 1. System packages and Rust

```bash
sudo apt update
sudo apt install build-essential pkg-config cmake clang libclang-dev libasound2-dev ffmpeg git
```

| Package | Why |
| --- | --- |
| `build-essential`, `pkg-config` | Compiling |
| `cmake`, `clang`, `libclang-dev` | Building whisper.cpp for local speech recognition |
| `libasound2-dev` | ALSA audio |
| `ffmpeg` | Voice notes and the Hermes speech companion; not needed otherwise |

Install Rust with [rustup](https://rustup.rs) (Rust 1.95 or newer):

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

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
Anything that can transmit requires `-c` to be given explicitly.

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

Piper is a separate program. One way to install it:

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
radio, then:

```bash
walkietalk devices
```

Copy the AIOC's `plughw:CARD=...,DEV=0` name into `[audio] input` and
`output`, and its `/dev/serial/by-id/...` path into `[ptt] port`. These names
survive reconnects, unlike card numbers. `walkietalk devices --all` lists every
ALSA device if you need something unusual.

Keep the desktop's sound server away from the AIOC: in your desktop sound
settings, set the AIOC's profile to "Off" (or disable it in `pavucontrol`).
Otherwise it may be busy when walkietalk opens it.

### Serial permissions

```bash
ls -l /dev/serial/by-id/
id -nG
```

If the port belongs to the `dialout` group and you are not in it:

```bash
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
the same user, absolute paths, and an explicit `-c`, and test the exact
command by hand first.

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
