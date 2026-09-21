# walkietalk

A Linux radio bridge, built with kids one verified phase at a time.

**Phase 1:** control an AIOC's push-to-talk (PTT) line and play a short speech
WAV through its audio interface. **Phase 2:** capture radio speech on the pinned
AIOC input, detect an utterance by energy, and transcribe it locally with
faster-whisper. **Phase 3:** wake name, aliases, and an optional conversation
timeout. **Phase 4:** switch the speech listener in config (faster-whisper
default, then Grok STT). **Phase 5 verified:** a text-only agent interface,
with an offline stub, Charlotte through Hermes, Codex CLI, and Grok Build.
Brad has confirmed those demonstrations. The new `claude` (official CLI
saved login) and `claude_api` (separately billed API key) choices are also confirmed
working by Brad. **Phase 6 verified:** Piper (Amy) and Grok speech, enabled
only by `talk --transmit`. Brad confirmed those demonstrations with the girls
and authorized publication. See the [complete phase 6 demo](docs/PHASE6-DEMO.md).
**Phase 7 verified:** post-transmit mute, crop overlong speech, stuck-key
refusal, and optional station ID that you supply. Brad confirmed the
demonstration, including wake_phrase. See
[the phase 7 demo](docs/PHASE7-DEMO.md). See [the phase checkpoints](docs/PHASES.md).

**Current checkpoint:** Brad has confirmed the stub, Hermes, Codex, and Grok
demonstrations, plus continuous listening and both remote shutdown forms.
Brad also confirms both Claude routes work as expected. Phase 5 is merged in
[PR #5](https://github.com/bbusenius/walkietalk/pull/5). Phase 6 family
demonstrations of Amy and Grok speech passed; Brad authorized publication.
Phase 6 is merged in [PR #6](https://github.com/bbusenius/walkietalk/pull/6),
merge commit `ab37520`.
Spoken shutdown confirmation and optional `tts.normalize: peak` are in; Brad
confirms both work. Phase 7 family demonstration passed; Brad authorized
publication. The latest automated checks passed all 578 tests, lint, and
formatting.
See [Claude setup and demos](docs/CLAUDE.md).
The [Hermes/Codex STT capability review](docs/STT-CAPABILITIES.md) found no ready
transcription endpoint in the inspected interfaces. Brad chose to keep the
existing local Whisper option instead of adding a Hermes service. Codex STT is
not supported; its agent continues to work with the existing listeners.
See the [complete phase 5 acceptance checklist](docs/PHASE5-DEMO.md).

Phase 1 verification is complete: the family observed the Python PTT pulse,
heard the spoken WAV on the receiving walkie with PTT released afterward, and
confirmed that Ctrl+C releases PTT immediately. The girls understand the talk
control demonstration. Phase 1's 36 automated tests and the CI checks passed.
Phase 2 family radio transcription passed and is merged in
[PR #2](https://github.com/bbusenius/walkietalk/pull/2).

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
.venv/bin/walkietalk talk speech.wav
.venv/bin/walkietalk agent-check "What is rain?"
```

`ptt` logs simulated PTT ON/OFF without opening serial or audio hardware.
`play speech.wav` also defaults to simulation, validating the file but making
no sound. `listen speech.wav` runs energy detection and local transcription on
that file; it does not open the AIOC or PTT. `talk speech.wav` does the same
capture path, then applies the wake gate and prints the selected agent's text reply.
The default `stub` prints a fixed pretend answer. `agent-check` sends typed traffic
directly to the agent, without a wake gate, STT, audio, or PTT; with the stub it
works entirely offline. Real
transmission still requires both a configuration file and `--transmit`. Live
radio transcription uses `listen --capture` or `talk --capture` and is
receive-only unless `talk` also receives `--transmit`.

## Spoken answers with Piper or Grok (phase 6)

Add the optional Piper engine, explicitly download Amy, and extend an existing
config with the required `tts` section below. Keep your radio, STT, wake, and agent
settings. Brad's local config has already been extended.

```sh
uv pip install --python .venv/bin/python -e '.[dev,piper]'
.venv/bin/python -m piper.download_voices --download-dir "$HOME/.cache/walkietalk/piper" en_US-amy-medium
```

```yaml
tts:
  backend: "piper"
  piper_executable: "piper"
  piper_model: "~/.cache/walkietalk/piper/en_US-amy-medium.onnx"
  timeout_seconds: 30
  grok_voice: "eve"
  grok_language: "en"
  grok_speed: 1.0
  grok_api_key_env: "XAI_API_KEY"
  normalize: "off"
```

The matching `.onnx.json` file must stay alongside the model. Relative paths are
resolved from the config directory. Piper is optional for text-only operation
and for Grok speech;
voice selection is independent of the agent and STT. Select `tts.backend: grok`
for Grok speech with your saved SuperGrok login, or explicitly select `grok_api`
for billed API-key access. See [Grok voice setup and demonstrations](docs/GROK-TTS.md).
There is no automatic fallback between voice backends or billing routes.

```sh
mkdir -p recordings
.venv/bin/walkietalk -c config.local.yaml tts-check "Hello. This is Amy." --output recordings/amy.wav
# Real radio replies, only when ready for the family demonstration:
.venv/bin/walkietalk -c config.local.yaml talk --capture --transmit
```

`tts-check` creates a new WAV without opening hardware and refuses to overwrite
an existing file. For pip-managed environments, install `.[dev,piper]` with pip.
Piper is a separate GPL-3.0 engine; voice terms are linked in the
[setup and complete demonstration checklist](docs/PHASE6-DEMO.md).

The bridge generates speech before keying. With a 10-second TX cap and
0.2-second settle, playback is cropped to 9.8 seconds so an overlong answer is
cut off instead of holding the transmitter. `tts.timeout_seconds` controls
synthesis time, separately from agent and radio limits. Capture remains closed
during processing and playback. After unkey, `radio.post_tx_mute_seconds`
(default 2 in the example) waits before the next listen; 0 disables the mute.
The follow-up window opens after playback and PTT release.

`radio.callsign` is your granted station ID, or empty for no spoken ID.
Walkietalk never invents a callsign. `radio.callsign_mode` is `off`,
`end_of_reply` (after each spoken answer), or `interval` (first answer, then
every `radio.callsign_interval_seconds`). The ID is synthesized with the
selected voice and sent in the same transmission as the answer. No music or
sound effects.

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

`audio.gain` controls outgoing WAV playback, including every voice backend.
Use any positive finite number: `0.5` halves the signal amplitude, `1.0` keeps
the original level, and `1.5` amplifies it. Values above `1.0` can clip peaks
and degrade sound quality; samples are clamped to prevent overflow.

`tts.normalize` is `off` (default: keep the engine's own level) or `peak`
(scale each synthesized reply so its loudest sample fills the WAV). Piper and
Grok can return different levels; `peak` makes them use the same digital
headroom. `audio.gain` still applies afterward, so with `peak` start gain at
`1.0` or you will clip immediately. `play` of an existing file is unchanged.
Restart after editing config. Capture and STT are unaffected.
`radio.max_tx_seconds` defaults to 10. It must be a finite number greater than
0; there is no extra software ceiling. The program enforces the value you set.
`radio.settle_seconds` defaults to 0.2, allowing PTT to settle before playback.
`radio.post_tx_mute_seconds` is 0 through 30 (example 2). `radio.callsign` stays
empty until you put your real ID there.
The WAV plus settle time must fit the transmit limit.

`vad.energy_threshold` is RMS from 0 to 1; begin with 0.02 and tune from the
logged values. `vad.hangover_ms` is how long silence may last before an
utterance ends (400 ms). `vad.max_utterance_seconds` caps a single capture
(12 seconds, at most 30). `stt.backend` is `faster-whisper` (default), `grok` (Grok Voice Transcribe using
the SuperGrok Plus login from `grok login`), or `grok_api` (explicit
`XAI_API_KEY` billing). `stt.model` is `tiny` or `base` for faster-whisper.
`stt.timeout_seconds` bounds transcription. Walkietalk never silently switches
listeners.

`listening.mode` is `wake_phrase` (default: say the name every time) or
`conversation` (name once, then follow-ups until
`listening.conversation_timeout_seconds`, starting at 60). `wake.primary` is
the name; `wake.aliases` lists extra spellings for speech-to-text mistakes.

Phase 5 adds required `agent` and `shutdown` sections. For an existing config,
append these sections without replacing any device, wake, or STT settings:

```yaml
agent:
  backend: "stub"
  max_reply_chars: 600
  history_turns: 8
  timeout_seconds: 60
  hermes_url: "http://127.0.0.1:8642"
  hermes_token_env: "WALKIETALK_HERMES_TOKEN"
  codex_executable: "codex"
  codex_model: ""
  grok_executable: "grok"
  grok_model: "grok-4.6"
  codex_reasoning_effort: "low"
  grok_reasoning_effort: "low"
  claude_executable: "claude"
  claude_model: "claude-sonnet-5"
  claude_reasoning_effort: "low"
  claude_api_key_env: "ANTHROPIC_API_KEY"
  claude_api_model: "claude-sonnet-5"
  claude_api_reasoning_effort: "low"

shutdown:
  enabled: false
  phrase: ""
  phrase_aliases: []
  code: ""
  code_aliases: []
  confirmation_seconds: 30
  confirmation_phrase: ""
```

`stub`, `hermes`, `codex`, `grok`, `claude`, and `claude_api` are implemented.
`claude` selects the official CLI; `claude_api` selects billed API access. Other backend names fail
explicitly; there is no fallback. `max_reply_chars` is an integer from 1 to 2000;
longer answers are discarded with a local error. `history_turns` is an integer
from 1 to 32, counting completed traffic/reply pairs. Each input is also limited
to 4000 characters. The bridge supplies a short spoken-style answer instruction
and retains bounded history in memory for this invocation only. Closing the wake
window preserves history; restarting starts a fresh conversation. Continuous
`talk --capture` reports agent or transcription failures locally and resumes
listening, requiring the wake phrase again. After an agent failure it starts a
fresh backend session with only the completed traffic/reply pairs. One-shot
commands still exit with an error on failure.

The stub needs no account or network, but a separately selected remote STT
backend still does. No voice or transmission is added in phase 5.

Claude has two explicit authentication routes: `claude` invokes the official
CLI with its saved account login; `claude_api` uses `ANTHROPIC_API_KEY` and separate
API billing. Walkietalk does not implement Claude OAuth or copy CLI credentials.
Account terms and usage limits still apply; see [setup, limitations, and demos](docs/CLAUDE.md).
Both use low reasoning effort by default and preserve only bounded radio context.
Neither provides STT: no supported standalone transcription interface was found
in the checked Claude CLI or Messages API. Any existing STT backend can feed them.

### Continuous listening and remote shutdown

Run `.venv/bin/walkietalk -c config.local.yaml talk --capture` to leave the bridge
listening indefinitely. Quiet periods do not stop it. The conversation timeout
only decides whether you need to say the wake phrase again; it does not set the
program's lifetime. Audio device failures still stop the program with an error.
For a bounded diagnostic, use `talk --capture --once --timeout 5`. `--timeout`
is accepted only with `--once` for live `talk` (default 60 seconds, maximum 300).
`listen` retains its separate bounded wait, and each utterance retains its
`vad.max_utterance_seconds` recording limit.

Remote shutdown is optional. Set `shutdown.enabled: true`, a `phrase`, a
different `code`, and `confirmation_phrase` in your local config. Say the phrase followed
by the code in one utterance, or say the phrase alone, release the walkie's PTT,
then send the code within `confirmation_seconds` (default 30, maximum 300).
This closes walkietalk normally; it does not delete anything or shut down the
computer. The ordinary wake phrase is optional for these controls. Case and
punctuation are ignored; spelling differences need explicit aliases. A wrong or
empty next utterance cancels, expiry cancels, and the code alone cannot shut
down the bridge unless shutdown is already armed.

After the code is accepted, `talk --capture --transmit` speaks `confirmation_phrase` with
the selected voice, then exits. Receive-only `talk --capture` still prints the
confirmation and exits without keying. If speech generation or playback fails,
the error is local, PTT is released, and the program still stops. Controls are
handled before the agent and omitted from transcript logs; a pending
confirmation is never forwarded to the agent. STT still receives the audio, so
remote STT must be working. Anyone listening on the radio can hear the code.
Capture currently pauses during transcription and an agent reply: send controls
while the bridge is listening. See the
[complete continuous-listening and shutdown demonstration](docs/CONTINUOUS.md).

### Reasoning effort for radio replies

`agent.codex_reasoning_effort` and `agent.grok_reasoning_effort` explicitly select
how much reasoning to request. Both are required fields, defaulting to `low` for
radio replies. Brad selected `low` for both in the local config. The startup
agent label displays the requested effort. Restart walkietalk after changing it.

Codex accepts `default`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, or
`ultra`; Grok accepts `default`, `none`, `minimal`, `low`, `medium`, `high`, `xhigh`,
or `max`. The selected model must support the requested level; these lists do
not imply every model supports every value. Walkietalk does not substitute an
effort if the CLI rejects it. The special `default` value omits the override:
Codex uses its model default because desktop config is ignored; Grok inherits
its profile/model setting. `default` does not mean `low`.

Reasoning effort is separate from answer length, the request deadline, and the
follow-up window. A short answer can still require substantial reasoning.

The installed Hermes Runs API has **no per-request reasoning override**. Its
agent factory reads Charlotte's `agent.reasoning_effort` from the Hermes profile,
currently `medium`. Walkietalk leaves this unchanged and does not offer a YAML
field that the server would ignore. No Charlotte source change or restart was
needed. Revisit this only when the installed API supports request overrides.

### Charlotte through Hermes

Select `agent.backend: hermes` and set `hermes_url` to the existing Charlotte
API base URL (not a model-provider endpoint). `hermes_token_env` names the
environment variable holding the local API bearer token. Never put its value
in YAML. Walkietalk leaves the provider, model, instructions, tools, and memory
under Charlotte's configured Hermes environment.

The verified integration uses Hermes **0.19.0**. Enabling its existing API
required local Charlotte profile settings and a gateway restart, with no
Charlotte source-code or image changes. The [setup record](docs/PHASE5-DEMO.md#local-setup-already-completed)
lists the server settings, Docker connection, toolsets, and backup/undo details.
Use the API base URL without a `/v1` suffix; the adapter appends its endpoint paths.

For the existing local setup, run from this checkout:

```sh
. ./.env.hermes.local
.venv/bin/walkietalk -c config.local.yaml agent-check "What is rain? Answer in one short sentence."
```

Expect a short real `Reply:` on screen. This check uses no STT, audio, or PTT.
The `.env.hermes.local` file is optional and is **not loaded automatically**.
It is a private, gitignored convenience file that exports
`WALKIETALK_HERMES_TOKEN`. A launcher, shell, or service can supply the variable
instead. Hermes's matching `API_SERVER_KEY` lives in its own profile environment;
storing it there alone does not give the separate walkietalk process access.
Keep provider credentials in Hermes, and update both sides when rotating the
local API token.

`agent.timeout_seconds` is a finite number greater than zero and at most 300
(default 60), bounding the complete request, including polling and response
reading. It is independent of STT timeout, wait-for-speech timeout, and the
conversation window. On timeout/interruption/approval-required work the adapter
requests a stop, allowing at most two extra seconds for that cleanup. It never
approves permission requests and never switches providers itself.

For slower questions, change the existing `agent.timeout_seconds` in
`config.local.yaml` from `60` to, for example, `120`, then restart walkietalk.
Charlotte does not need a restart for this change. The reply-length cap
(`agent.max_reply_chars`) and follow-up window
(`listening.conversation_timeout_seconds`) remain independent.

The adapter verifies Hermes's run/status/stop capabilities, uses `/v1/runs`,
sends the dedicated session ID and bounded conversation history, and accepts only
completed final text. HTTP bodies, tool output, and error details are not replies.
Hermes cancellation is cooperative: the server stops at a safe interruption
point. If submission is not acknowledged or stop cannot be confirmed, walkietalk
reports that uncertainty locally and discards the answer.

See [Hermes setup, observed version, and family commands](docs/PHASE5-DEMO.md#charlottehermes-checkpoint).

### Codex CLI

Select `agent.backend: codex`. The adapter uses the official `codex exec` CLI
with its saved ChatGPT login, final-answer output, and a dedicated conversation.
It runs in a read-only sandbox with shell tools, connectors, hooks, web search,
and delegation disabled. API-key login is rejected; there is no billing fallback.
`codex_executable` is an executable name on PATH or an absolute path without flags.
`codex_model` selects the model; `""` uses the CLI's built-in default because the
adapter ignores desktop user configuration. Both fields are required for all agents.
`agent.timeout_seconds` also applies to Codex; timeout and stop signals terminate
the CLI process group. No Charlotte restart or Hermes token is needed.

Verified with Codex 0.155.1 and Brad's saved ChatGPT login. See the
[Codex configuration, context limits, and complete demonstration](docs/CODEX.md).

### Grok Build CLI

Select `agent.backend: grok` for the text-answering helper. This is independent
of `stt.backend: grok`, which uses Grok Voice Transcribe. The agent uses the
installed Grok Build CLI and its saved `grok login` session; API-key authentication
is disabled, with no fallback to developer API billing. Set `grok_executable` to
an executable name or absolute path and `grok_model` to a first-party model ID
available to your CLI. Both fields are required for all backends.

Verified with Grok Build 1.0.34 and `grok-4.6`. The adapter isolates its home and
working directories, checks the profile, denies tools, and retains the original
Grok home for CLI-owned login and dedicated session history. It enforces the
common timeout and reply cap. See the [Grok setup and full demo](docs/GROK.md)
for profile requirements, context limits, expected results, and failure commands.

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

3. **Live radio sentence.** After the program prints `Waiting for someone to talk`, say a
   clear sentence on a handheld walkie without long pauses, then stop talking:

   ```sh
   .venv/bin/walkietalk -c config.local.yaml listen --capture
   ```

   Capture should end about 0.4 seconds after you stop. The TX light stays off.
   The screen shows roughly what you said, for example:

   ```text
   Receive-only: PTT will not be opened.
   Waiting for someone to talk (give up after 60s; threshold 0.020)...
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
   `Waiting for someone to talk`, then press Ctrl+C before talking. Expect `Stopped;
   capture closed.`, exit status 130, and no TX light.

**Explain to the girls:** “The computer writes down what we said so the AI can
understand the question.”

Phase 2 is complete.

## Phase 3 family demonstration

The computer stays receive-only. Do not pass `--transmit`. Watch the gateway TX
light: it must stay off. Use `talk`, not `listen`, so the wake gate runs.

On a color terminal, **Transcript** is cyan, **Accepted** / **Traffic** are
green, **Ignored** is yellow, and RMS waiting lines are dim. The same words
appear without color. Set `NO_COLOR=1` to turn color off.

1. **Default mode: the name is required every time.** Keep
   `listening.mode: wake_phrase` in `config.local.yaml`.

   ```sh
   .venv/bin/walkietalk -c config.local.yaml check
   .venv/bin/walkietalk -c config.local.yaml talk --capture
   ```

   Expect `Mode: wake_phrase` and `waiting for wake "charlotte"`. Then:
   1. Speak a sentence **without** the name → `Ignored (say "charlotte" first).`
   2. Say **Charlotte** plus traffic → `Accepted`, `Traffic:` with the name
      removed, `Reply: This is a pretend answer. The radio bridge brought me your words.`,
      still waiting for the name.
   3. Speak again **without** the name → ignored again.
   4. Ctrl+C. Expect `Stopped; capture closed.`, exit 130, TX light off.

2. **Conversation mode: follow-ups until a quiet pause.** Edit
   `config.local.yaml` to:

   ```yaml
   listening:
     mode: "conversation"
     conversation_timeout_seconds: 10
   ```

   Restart:

   ```sh
   .venv/bin/walkietalk -c config.local.yaml talk --capture
   ```

   1. Name + traffic → accepted, then `State: awake (10s left).` Saying only
      the name is enough to open the window (`Wake heard; listening for traffic.`).
   2. Before those 10 seconds end, more traffic **without** the name →
      `Accepted (follow-up).` The 10 seconds restart after that text reply prints.
   3. Wait; when the window ends you should see `Follow-up window ended` even
      if nobody is talking. Then speak without the name → ignored.
   4. Say the name again → accepted.

3. **The timeout is configurable.** Change `conversation_timeout_seconds` to
   `5`, restart, and show that the awake window is shorter. Silence does not
   keep it awake.

**Explain to the girls:** “Say the helper’s name, then your traffic — a question,
a command, or anything you need. After a quiet pause, say the name again.”

Phase 3 family demonstration passed and is merged in
[PR #3](https://github.com/bbusenius/walkietalk/pull/3).

## Phase 4 family demonstration

Switch the listener in config. Radio, capture, and wake code stay the same. Do
not pass `--transmit`. TX light stays off.

**Explain:** “If one listener has trouble with a voice, we can plug in a different
one.”

1. **Default faster-whisper still works.** Keep `stt.backend: faster-whisper`.

   ```sh
   .venv/bin/walkietalk -c config.local.yaml check
   .venv/bin/walkietalk -c config.local.yaml talk --capture --once
   ```

   Expect `Listener: faster-whisper (base)` and a normal transcript / traffic
   line.

2. **Grok Voice Transcribe via SuperGrok Plus.** Sign in once with `grok login`
   if needed, then set:

   ```yaml
   stt:
     backend: "grok"
     model: "base"
     timeout_seconds: 30
   ```

   ```sh
   .venv/bin/walkietalk -c config.local.yaml talk --capture --once
   ```

   Expect `Listener: grok (grok-voice-transcribe-2.0; SuperGrok Plus login)` and
   a transcript. Same child sentence as step 1. This is not Grok Build and does
   not use `XAI_API_KEY`. If login is missing, the error tells you to run
   `grok login`.

3. **Optional billed API only if you choose it.** `stt.backend: grok_api` needs
   `XAI_API_KEY`. That is API credits, not SuperGrok Plus. Missing key must
   error; it must not fall back to faster-whisper or SuperGrok login.

Phase 4 family demonstration passed for faster-whisper and SuperGrok `grok`.
The billed `grok_api` path was skipped. Phase 4 is merged in
[PR #4](https://github.com/bbusenius/walkietalk/pull/4).

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
