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
| Grok Voice realtime | Combined via `agent.backend: grok_realtime` | Combined | Combined | Explicit `XAI_API_KEY`; native audio and control transcripts |

The table describes adapters implemented in Walkietalk. Available models and
account access depend on the provider. Hermes, Codex, and Claude are not valid
`stt.backend` selections; use `faster-whisper`, `grok`, or `grok_api` with those
text agents. Realtime is a combined voice path selected through `agent.backend`.

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
Follow the [Hermes API documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server)
and confirm the supported Runs endpoints.

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

Expect `Reply:` followed by text. Repeat the connection check after CLI upgrades.
Walkietalk ignores desktop Codex configuration, including its default model and
reasoning settings. Requests run in an empty temporary directory with a read-only
sandbox; shell tools, hooks, connectors, and delegation are disabled. Incompatible
CLI versions fail locally. Codex retains its own session files.

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
Eligibility and quotas depend on the account and upstream service; a saved login
alone cannot guarantee access.

Select `agent.backend: grok`, `stt.backend: grok`, and/or `tts.backend: grok` as
needed. The agent's `grok_model` and `grok_reasoning_effort` affect answers only.
Voice uses `tts.grok_voice` (example `eve`), `grok_language`, and `grok_speed`.

The text agent requires a standard first-party Grok profile. Its `grok inspect
--json` preflight rejects active hooks, MCP servers, plugins, LSP servers, custom
model/provider definitions, or an unverifiable login policy. It uses a temporary
home and working directory while retaining the original `GROK_HOME` for saved
login and sessions. Only web search is permitted when `agent.web_search` is true.

Speech adapters read `auth.json` from the Grok home, refresh expired grants, and
retry a rejected login once on HTTP 401. HTTP 403 remains an access error. STT
reloads the shared login before use to pick up token refreshes by TTS or the CLI.
TTS voice, language (`en` or `auto`, for example), and speed (0.7–1.5) are
independent of the agent model and playback gain. An unavailable voice is an
error; the adapter does not substitute another voice.

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

It records one utterance; expect `Transcript:` and TX off.

## Explicit billed APIs

Set `stt.backend: grok_api` and/or `tts.backend: grok_api` only if you intend
to use the [xAI developer API](https://docs.x.ai). Put `XAI_API_KEY="your-key"`
in your private credentials file. STT requires that exact environment variable;
voice can use a different name through `tts.grok_api_key_env`.
Use the same transcription/WAV checks above. SuperGrok subscriptions and xAI
developer API billing are separate; these adapters never borrow the CLI login.
Speech to Speech realtime (`agent.backend: grok_realtime`) also requires
explicit API credits via `XAI_API_KEY` (`agent.realtime.api_key_env`); see the
[realtime configuration](CONFIGURATION.md#combined-voice-agent-optional-realtime).
Radio output requires `--transmit`.

For Anthropic's [Messages API](https://platform.claude.com/docs/en/api/messages),
select `agent.backend: claude_api` and set `ANTHROPIC_API_KEY="your-key"` in the
private file. YAML contains only its name in `claude_api_key_env`.
Choose an available model with `claude_api_model`; `claude_api_reasoning_effort`
is independent of CLI effort. This is explicitly billed API usage, not a Claude
subscription. Run the same typed `agent-check` as above. No CLI or Hermes
fallback occurs. Only completed final text is accepted; truncated responses and
thinking blocks do not become radio answers. Web search follows `agent.web_search`.

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
Set `claude_executable` to its executable name or an absolute path. The CLI must
support `--safe-mode`, `--restricted`, `--permission-prompts`, and
`--no-session-persistence`; missing flags produce an update error before inference.
Requests run in an empty temporary directory without user/project settings,
hooks, MCP tools, or permission prompts. Only `WebSearch` is offered when enabled.
The adapter supplies bounded radio history on each request and disables session
persistence. Desktop chats are not resumed.

Run `agent-check` with a short question. Expect a short `Reply:`; failures remain
local and never become radio speech. This adapter and `claude_api` supply text
only. Neither provides a Walkietalk STT or TTS backend.

## Grok realtime voice

Select `agent.backend: grok_realtime` and supply the billed key named by
`agent.realtime.api_key_env` (default `XAI_API_KEY`). This route uses no SuperGrok
login. See [realtime settings](CONFIGURATION.md#combined-voice-agent-optional-realtime)
for model, voice, and timeout configuration.

Test a recorded utterance and save a reply without playback or PTT:

```bash
walkietalk -c "$HOME/.config/walkietalk/config.yaml" voice-agent-check \
  utterance.wav --output realtime-check.wav
```

This sends audio to xAI and uses API credits. Choose a new output filename.
Use `talk --capture` to test continuous wake/control handling with printed replies,
or add `--transmit` for radio playback. Captured audio streams directly to xAI;
native transcripts gate replies and controls. Separate STT and TTS backends are
unused. Audio uploaded before wake validation can include unaddressed traffic.

## CLI compatibility and conversation history

Codex, Grok, and Claude adapters accept only successful final answers; intermediate
output and CLI diagnostics are not replies. They check required isolation features
and reject unexpected tools or configuration. Repeat `agent-check` after upgrades
before starting the radio loop.

Each Walkietalk invocation has its own bounded radio conversation. Codex and Grok
resume explicit sessions and rotate them when the history limit is reached;
Claude replays bounded history without saving a desktop session. Restarting
Walkietalk starts fresh. The text-agent timeout includes login/profile checks
and generation. Cleanup of a stopped CLI process group can add about one second;
late output is discarded.

## Check a combined setup

After each selected connection works separately, run `check`, then `talk
--capture` with your config. Ask a named question. In conversation mode, ask
an unaddressed follow-up before the configured window expires. Printed replies
should use the prior radio context. TX remains off without `--transmit`.

Use `tts-check` for each selected voice before enabling transmission. Its output
filename must be new. A successful computer-only check does not establish radio
volume, key-up timing, or cleanup on your hardware; follow the final steps in
[Installation](INSTALL.md#5-receive-then-enable-replies-on-air).
