# Backend setup and verification

Choose speech recognition (`stt.backend`), the answering agent (`agent.backend`),
and the speaking voice (`tts.backend`) independently. Changing one does not
require changing the radio or wake settings. Failed authentication never switches
backends or switches a subscription to API billing.

Start with the complete config from `walkietalk init`. YAML examples below show
fields to edit, not complete replacement files. All commands assume your virtual
environment is activated and your config is at `~/.config/walkietalk/config.yaml`.
Put service credentials in the adjacent private `credentials.env`, as described
in [Configuration](CONFIGURATION.md#credentials). Official CLI logins stay in their
own stores. Run the CLIs and Walkietalk as the same normal Linux user.

## Available connections

| Selection | Speech recognition | Answering agent | Speaking voice | Authentication |
| --- | --- | --- | --- | --- |
| Local engines | `faster-whisper` | `stub` (fixed pretend answer) | `piper` | None; download models first |
| Hermes | Not implemented | `hermes` | `hermes` via companion | Local service bearer token; provider credentials stay in Hermes |
| Codex | No adapter | `codex` | No adapter | Official Codex CLI saved ChatGPT login |
| Grok account | `grok` Voice Transcribe | `grok` Build CLI | `grok` Voice API | Official Grok saved login; account entitlement required |
| xAI developer API | `grok_api` | No direct chat adapter | `grok_api` | Explicit `XAI_API_KEY`; separate API billing |
| Claude Code CLI | No adapter | `claude` | No adapter | Official Claude CLI saved account login |
| Anthropic Messages API | No adapter | `claude_api` | No adapter | Explicit `ANTHROPIC_API_KEY`; separate API billing |
| Grok Voice realtime | Combined via `agent.backend: grok_realtime` | Combined | Combined | Explicit `XAI_API_KEY`; live audio in/out through `talk`; native wake/control transcripts; on-air validation pending |

These are implemented adapters, not a promise that every provider model, account
tier, CLI release, or upstream configuration works. The recorded family checks
cover local Whisper/Piper, Hermes agent and voice, Codex, Grok account speech and
agent, and both Claude agent routes. Live xAI API-key STT/TTS checks remain
explicitly skipped; their transport/auth/failure paths have automated coverage.
Hermes speech has live coverage for the demonstrated profile, with fake-provider
tests for other selections. See [phase records](PHASES.md),
[Hermes speech](HERMES-TTS.md), and [STT capability limits](STT-CAPABILITIES.md).

## Local speech and the offline stub

The default agent `stub` needs no network, account, or model:

```bash
walkietalk -c "$HOME/.config/walkietalk/config.yaml" agent-check 'Hello.'
```

Expect a fixed pretend answer. For `stt.backend: faster-whisper`, choose `tiny`
or `base` in `stt.model`, then run `walkietalk -c ... models` with your full config
path. This is an explicit model download; subsequent transcription is local.

For `tts.backend: piper`, install the optional Piper extra and download the Amy
voice using the exact [installation commands](INSTALL.md#3-select-the-three-backends).
Piper receives final answer text only. Its voice/model, normalization, and playback
gain are independent of the selected agent.

## Hermes: an agent environment and an optional voice

Hermes is an agent environment with its own instructions, skills, memory, model,
and provider credentials. A direct model API call does not reproduce that context.
Enable the intended profile's Runs API, using its documented bearer authentication.
For Charlotte, follow [Charlotte's Walkietalk setup](https://github.com/bbusenius/charlotte/blob/master/runtime/hermes/README.md#walkietalk).
Other deployments should follow the [Hermes API documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server)
and confirm the supported Runs endpoints. The initial agent check used Hermes 0.19.0.

Edit these fields in the complete config:

```yaml
agent:
  backend: hermes
  hermes_url: "http://127.0.0.1:8642"
  hermes_token_env: "WALKIETALK_HERMES_TOKEN"
```

Put `WALKIETALK_HERMES_TOKEN="your-service-token"` in `credentials.env`.
This is the local service's bearer token, not an upstream model API key.
The bridge needs permission to contact Hermes; it does not read the server's
environment automatically. A private credentials file avoids sourcing shell
files for each terminal. Exported variables remain supported.

```bash
walkietalk -c "$HOME/.config/walkietalk/config.yaml" agent-check \
  'In one sentence, why does ice float?'
```

Expect a short answer from that environment. Agent reasoning stays controlled
by its Hermes profile. Increase `agent.timeout_seconds` if its normal work needs
longer; reply length remains independently capped by `agent.max_reply_chars`.
`agent.web_search: false` tells Hermes not to search. `true` allows a public
web lookup in that instruction. Hermes still uses the tools configured on its
profile; this bridge cannot remove them.

For voice, deploy the separate [Hermes speech companion](HERMES-TTS.md) using
Hermes's Python, profile, engines, and provider credentials. Set `tts.backend:
hermes`, `tts.hermes_url` to its reachable base URL (default loopback port 8643),
and `tts.hermes_token_env` to its service-token variable. Voice selection belongs
in Hermes's `tts.provider` configuration; Walkietalk's Grok/Piper fields do not
override it. Agent and speech URLs are distinct. Do not expose bearer-authenticated
HTTP to an untrusted network; use a protected connection such as an SSH tunnel.

```bash
walkietalk -c "$HOME/.config/walkietalk/config.yaml" tts-check \
  'This voice comes from my Hermes environment.' --output hermes-check.wav
```

Expect a saved WAV with no playback/PTT. There is no Hermes transcription adapter;
use local Whisper or an explicitly selected Grok STT adapter.

## Codex agent

Install the official CLI using [Codex installation instructions](https://developers.openai.com/codex/cli),
then use its [saved ChatGPT login](https://learn.chatgpt.com/docs/auth):

```bash
codex --version
codex login
codex login status
```

Select `agent.backend: codex`. `codex_executable: codex` uses PATH; an absolute
path is also accepted. Leave `codex_model` empty for the CLI default or choose a
model available to your account. `codex_reasoning_effort: low` favors quick
answers; `default` leaves the model default. Walkietalk uses `codex exec` in a
dedicated radio session with tools constrained by the adapter. Web search stays
off unless `agent.web_search` is true. The adapter does not resume your desktop
conversation. API-key-only authentication is rejected by this route.

```bash
walkietalk -c "$HOME/.config/walkietalk/config.yaml" agent-check \
  'In one sentence, why does ice float?'
```

Expect `Reply:` followed by text. The original verification used Codex 0.155.1;
after upgrading a CLI, repeat the connection check. See [adapter details](CODEX.md).

## Grok account: separate agent, STT, and voice

Install the official CLI from [Grok Build](https://docs.x.ai/build/overview).
Complete its [login](https://docs.x.ai/build/cli/reference) as your normal user:

```bash
grok --version
grok login
```

The agent uses the supported `grok -p` interface. STT uses Voice Transcribe
`POST /v1/stt`; it does not send audio to Grok Build. Voice uses `POST /v1/tts`.
All three account adapters reuse the saved Grok session under `$GROK_HOME` or
`~/.grok`; Walkietalk does not implement OAuth or acquire a replacement API key.
The speech account checks used SuperGrok Plus. Eligibility and quotas depend on
the account and upstream service; a saved login alone cannot guarantee access.

Select `agent.backend: grok`, `stt.backend: grok`, and/or `tts.backend: grok` as
needed. The agent's `grok_model` and `grok_reasoning_effort` affect answers only.
Voice uses `tts.grok_voice` (example `eve`), `grok_language`, and `grok_speed`.

```bash
walkietalk -c "$HOME/.config/walkietalk/config.yaml" agent-check \
  'In one sentence, why does ice float?'
walkietalk -c "$HOME/.config/walkietalk/config.yaml" tts-check \
  'This is a Grok voice check.' --output grok-check.wav
```

For selected Grok STT, run:

```bash
walkietalk -c "$HOME/.config/walkietalk/config.yaml" listen --capture
```

It records one utterance; expect `Transcript:` and TX off. See
[Grok agent](GROK.md) and [Grok voice](GROK-TTS.md) for adapter specifics.
Select `agent.backend: grok_realtime` for live speech-to-speech in `talk`.
Captured audio streams directly to xAI; replies play incrementally, and native
input transcripts handle wake/shutdown controls. Separate STT and TTS backends
are not opened. See [realtime setup](GROK-REALTIME-PLAN.md).

## Explicit billed APIs

Set `stt.backend: grok_api` and/or `tts.backend: grok_api` only if you intend
to use the [xAI developer API](https://docs.x.ai). Put `XAI_API_KEY="your-key"`
in your private credentials file. STT requires that exact environment variable;
voice can use a different name through `tts.grok_api_key_env`.
Use the same transcription/WAV checks above. SuperGrok subscriptions and xAI
developer API billing are separate; these adapters never borrow the CLI login.
Speech to Speech realtime (`agent.backend: grok_realtime`) also requires
explicit API credits via `XAI_API_KEY` (`agent.realtime.api_key_env`); see the
[realtime setup](GROK-REALTIME-PLAN.md). Radio output requires `--transmit`.

For Anthropic's [Messages API](https://platform.claude.com/docs/en/api/messages),
select `agent.backend: claude_api` and set `ANTHROPIC_API_KEY="your-key"` in the
private file. YAML contains only its name in `claude_api_key_env`.
Choose an available model with `claude_api_model`; `claude_api_reasoning_effort`
is independent of CLI effort. This is explicitly billed API usage, not a Claude
subscription. Run the same typed `agent-check` as above. No CLI or Hermes
fallback occurs. See [Claude setup](CLAUDE.md) for both routes and account limits.

## Claude CLI agent

Install the official CLI using [Claude Code setup](https://code.claude.com/docs/en/setup).
Use its [authentication commands](https://code.claude.com/docs/en/cli-reference):

```bash
claude --version
claude auth login
claude auth status
```

Select `agent.backend: claude`, set `claude_model` to an available model, and
use `claude_reasoning_effort: low` or a model-supported value. Walkietalk invokes
the official noninteractive `claude -p` interface; it does not implement login,
read Claude credential stores, or accept API-key-only login for this route.
Account eligibility and permitted use remain governed by the provider; see
[Claude notes](CLAUDE.md). The original verification used CLI 2.1.277.

Run `agent-check` with a short question. Expect a short `Reply:`; failures remain
local and never become radio speech. This adapter and `claude_api` supply text
only. Neither provides a Walkietalk STT or TTS backend.

## Check a combined setup

After each selected connection works separately, run `check`, then `talk
--capture` with your config. Ask a named question. In conversation mode, ask
an unaddressed follow-up before the configured window expires. Printed replies
should use the prior radio context. TX remains off without `--transmit`.

Use `tts-check` for each selected voice before enabling transmission. Its output
filename must be new. A successful computer-only check does not establish radio
volume, key-up timing, or cleanup on your hardware; follow the final steps in
[Installation](INSTALL.md#5-receive-then-enable-replies-on-air).
