# Phase 8: installation demonstration

Brad reviewed the installation instructions and authorized publication.
Optional autostart and phase 9 remain deferred.

The package now includes setup templates, `init`, `config-check`, `--version`,
and automatic loading of a private credentials file. The public README links
installation, configuration, backend, troubleshooting, and developer guides.
Radio, agent, STT, and TTS contracts remain independent.

## Automated results versus observations

Automated checks cover private/no-overwrite setup, exact template/schema parity,
literal credential parsing and precedence, error handling, and all prior phases.
All **644 tests pass**, along with Ruff lint and formatting checks:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
```

A freshly installed wheel has passed help/setup, owner-only permissions, the
offline stub, dry-PTT, and simulated-login checks from outside the source
checkout, without provider requests or hardware.
This uses a new virtual environment on the existing Ubuntu host. CI runs the
installed-wheel check on Python 3.11–3.13.

Brad approved the written installation procedure below for publication. The
checklist remains the steps for installing on a machine.

## Complete checklist

Run from the Walkietalk checkout as your normal user. Keep this terminal open so
the demo variables remain set. These steps do not overwrite `config.local.yaml`
or your usual virtual environment. Dependency installation needs network access.
There is no `--transmit` in this checklist; observe the gateway TX light staying off.

### 1. Install the current work into a fresh environment

```bash
PHASE8_REPO="$PWD"
PHASE8_DEMO=$(mktemp -d /tmp/walkietalk-phase8-demo.XXXXXX)
python3 -m venv "$PHASE8_DEMO/venv"
"$PHASE8_DEMO/venv/bin/python" -m pip install "$PHASE8_REPO"
PHASE8_CLI="$PHASE8_DEMO/venv/bin/walkietalk"
cd "$PHASE8_DEMO"
"$PHASE8_CLI" --version
"$PHASE8_CLI" --help
```

Expect `walkietalk 0.1.0` and help including `init`, `config-check`, `agent-check`,
and `tts-check`. The install is non-editable, and you are now outside the checkout.
No account or hardware is needed. If `python3 -m venv` is unavailable, follow the
Ubuntu prerequisites in [Installation](INSTALL.md#1-install-the-application).

### 2. Create settings and try the offline helper

```bash
"$PHASE8_CLI" init --directory "$PHASE8_DEMO/settings"
"$PHASE8_CLI" -c "$PHASE8_DEMO/settings/config.yaml" config-check
"$PHASE8_CLI" -c "$PHASE8_DEMO/settings/config.yaml" agent-check 'Hello.'
"$PHASE8_CLI" ptt --seconds 0.1
stat -c '%a %n' "$PHASE8_DEMO/settings" "$PHASE8_DEMO/settings/"*
"$PHASE8_CLI" init --directory "$PHASE8_DEMO/settings"
```

Expect `Config OK`, the fixed pretend answer, and `DRY RUN: PTT ON` followed by
`DRY RUN: PTT OFF`. Directory permissions should be 700; both files should be 600.
The second `init` must report `nothing overwritten` and exit 1. That last failure
is intentional. Device placeholders in the template are valid strings for these
checks; they are not a completed hardware setup.

### 3. Show the settings and select a real connection

```bash
nano "$PHASE8_DEMO/settings/config.yaml"
```

Explain the device names, wake phrase/aliases, listening mode and follow-up
window, and the three independent selections: STT, agent, and voice. The follow-up
window controls when the name is needed again; continuous listening has no idle
limit. Do not put credential values in YAML.

Choose a real agent from [Backend setup](BACKENDS.md), and follow that section's
login and config instructions. For the already-demonstrated Grok account, change
only `agent.backend` from `stub` to `grok`; confirm `grok` is on PATH and the model
matches your working config. Reuse the normal saved login; do not log out of a
working account just for this demo. For Hermes/API selections, edit the adjacent
private `credentials.env` with the indicated variable. No shell sourcing is needed.
Keep all unselected fields in the config.

```bash
"$PHASE8_CLI" -c "$PHASE8_DEMO/settings/config.yaml" config-check
"$PHASE8_CLI" -c "$PHASE8_DEMO/settings/config.yaml" agent-check \
  'In one sentence, why does ice float?'
```

Expect your selected agent label and a short `Reply:`. The command uses the real
selected account's quota/billing; it performs no STT, capture, playback, or PTT.
Errors and output must not contain tokens or API keys.

### 4. Select a voice and verify its setup instructions

Follow the chosen voice section in [Backend setup](BACKENDS.md). You may reuse
an already installed model or account; no new purchase is required. For Grok,
set `tts.backend: grok` and reuse `grok login`. For Hermes, use the configured
speech companion and local service token. For local Amy, first install Piper
in this **demo** environment and explicitly obtain the voice if missing:

```bash
# Only needed if choosing Piper:
"$PHASE8_DEMO/venv/bin/python" -m pip install "$PHASE8_REPO[piper]"
"$PHASE8_DEMO/venv/bin/python" -m piper.download_voices \
  --download-dir "$HOME/.cache/walkietalk/piper" en_US-amy-medium
```

Then, for any selected voice:

```bash
"$PHASE8_CLI" -c "$PHASE8_DEMO/settings/config.yaml" tts-check \
  'Our installed radio bridge can make a voice.' --output "$PHASE8_DEMO/voice.wav"
```

Expect the selected voice label and `Speech WAV: ... 48000 Hz, mono PCM16 ...
No hardware opened.` A file was created; it was not played or transmitted.
Pick a new output filename if repeating. No fresh radio transmission is required
for the phase 8 packaging checkpoint.

### 5. Demonstrate a useful login error safely

```bash
"$PHASE8_DEMO/venv/bin/python" "$PHASE8_REPO/scripts/check_installation.py"
```

Expect a clearly labeled simulated expired-login error telling you to run
`codex login`, followed by `PASS: installed wheel...`. The checker uses a fake
CLI and isolated temporary settings, not a real expired account. It makes no
provider requests, opens no hardware, and changes none of your saved logins.
Its exit status is 0 only after the expected error and other installation checks
pass. Watch TX stay off. The message says “installed wheel” because installation
from a source directory also builds and installs a wheel.

### 6. Confirm the instructions are usable

Review the [README](../README.md), [Ubuntu install guide](INSTALL.md), and
[troubleshooting guide](TROUBLESHOOTING.md). Confirm another user could find:

- Exact device names and serial permissions, without running Walkietalk as root.
- Where to change the agent and voice and how to authenticate each chosen route.
- The private credentials file, its environment precedence, and no-secret YAML.
- How to run receive-only, stop, upgrade, and identify the installed checkpoint.
- Implemented/verified versus skipped integrations, and where adapter developers start.

Explain: **“Someone else could install this and plug in their own AI and voice.”**

Report all six results, any confusing instruction, and that TX stayed off. The
fresh demo environment/settings remain under the printed `$PHASE8_DEMO` path;
your normal setup is unchanged. Brad reviewed these instructions and authorized
publication. The checklist stays with the repository as the install procedure.
