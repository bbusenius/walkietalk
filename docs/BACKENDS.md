# Backend setup

Speech recognition (`[stt] backend`), the agent (`[agent] backend`), and the
voice (`[tts] backend`) are chosen independently. Alternatively,
`[agent] backend = "grok-realtime"` handles all three in one speech-to-speech
session. A failed login never switches to another backend or from a
subscription login to billed API access.

| Choice | Recognition | Agent | Voice | Authentication |
| --- | --- | --- | --- | --- |
| Local | `whisper` | `stub` | `piper` | None |
| Hermes | | `hermes` | `hermes` (companion) | Local service bearer token |
| Codex CLI | | `codex` | | Codex's saved ChatGPT login |
| Grok account | `grok` | `grok` (Grok Build CLI) | `grok` | Grok's saved login |
| xAI API | `grok-api` | | `grok-api` | `XAI_API_KEY`, billed |
| Claude CLI | | `claude` | | Claude Code's saved account login |
| Anthropic API | | `claude-api` | | `ANTHROPIC_API_KEY`, billed |
| Grok realtime | combined | `grok-realtime` | combined | `XAI_API_KEY`, billed |

Check each piece on its own before running the radio:

```bash
walkietalk agent-check "In one sentence, why does ice float?"
walkietalk tts-check "This is a voice check." --output check.wav
walkietalk listen check.wav
```

None of these opens the radio.

## Local: Whisper, stub, Piper

The `stub` agent always answers with a fixed pretend reply; it needs nothing.
For Whisper, set `[stt] model` and run `walkietalk models` once. For Piper,
install the program and a voice as described in the
[installation guide](INSTALL.md#5-piper-voice-local-speech).

## Hermes

[Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server)
is an agent environment with its own instructions, skills, memory, and model
credentials. Enable its API server and its Runs API, then:

```toml
[agent]
backend = "hermes"

[agent.hermes]
url = "http://127.0.0.1:8642"          # without /v1
token_env = "WALKIETALK_HERMES_TOKEN"
```

and in `credentials.toml`:

```toml
WALKIETALK_HERMES_TOKEN = "the API server's bearer token"
```

The bridge sends the request, its guidance, and the bounded radio history as
a run, polls it, and stops the run if the reply is abandoned. Raise
`[agent] timeout_seconds` if your environment needs longer. `web_search = false`
asks Hermes not to search; the tools Hermes offers are still decided by its
profile. For Hermes's voice, see [Hermes speech](HERMES-SPEECH.md). Do not
expose the bearer-protected API on an untrusted network; use an SSH tunnel.

## Codex

Install the [Codex CLI](https://developers.openai.com/codex/cli) and log in with
ChatGPT as the user who runs walkietalk:

```bash
codex login
codex login status     # must say "Logged in using ChatGPT"
```

```toml
[agent]
backend = "codex"

[agent.codex]
executable = "codex"
model = ""                 # empty uses the CLI default
reasoning_effort = "low"
```

Each reply runs `codex exec` in an empty temporary directory with a read-only
sandbox, your desktop Codex configuration ignored, and shell tools, apps,
hooks, and sub-agents disabled. Web search is live only when
`[agent] web_search = true`. API-key logins are refused.

## Grok

Install the [Grok Build CLI](https://docs.x.ai/build/overview) and log in:

```bash
grok login
```

The saved login (under `$GROK_HOME`, default `~/.grok`) is shared by three
independent adapters:

- `[agent] backend = "grok"`: the Grok Build CLI. Set `[agent.grok] model` to a
  model in your catalog. Before each reply the bridge checks `grok inspect`:
  API-key login must be disabled, and hooks, MCP servers, plugins, LSP servers,
  and custom model providers must be inactive. The CLI runs with an empty home
  directory (keeping `GROK_HOME` for the login), and only web search is
  allowed, when enabled.
- `[stt] backend = "grok"`: xAI speech-to-text. Wake names and sleep phrases
  are sent as recognition hints; the shutdown code is not.
- `[tts] backend = "grok"`: xAI text-to-speech with `[tts.grok]` voice,
  language, and speed.

The speech adapters reload the login before each request, refresh an expired
token, and retry once after a rejected token. Access still depends on your
account.

## xAI API (billed)

`[stt] backend = "grok-api"` and `[tts] backend = "grok-api"` use an
[xAI API](https://docs.x.ai) key instead of the Grok login:

```toml
XAI_API_KEY = "your billed API key"
```

`[stt] api_key_env` and `[tts.grok] key_env` name the variable. A subscription
login is never used for these, and these keys are never used for the login
backends.

## Claude CLI

Install [Claude Code](https://code.claude.com/docs/en/setup) and log in with
your Claude account:

```bash
claude auth login
claude auth status
```

```toml
[agent]
backend = "claude"

[agent.claude]
executable = "claude"
model = "claude-sonnet-5"
reasoning_effort = "low"
```

Each reply runs `claude -p` in safe, restricted mode in an empty temporary
directory, without user or project settings, hooks, MCP servers, slash
commands, or session persistence; only `WebSearch` is offered when enabled.
The CLI must be logged in with a Claude account (not an API key) and must
support `--safe-mode`, `--restricted`, `--permission-prompts`, and
`--no-session-persistence`; otherwise the bridge refuses before asking.

## Anthropic Messages API (billed)

```toml
[agent]
backend = "claude-api"

[agent.claude_api]
key_env = "ANTHROPIC_API_KEY"
model = "claude-sonnet-5"
reasoning_effort = "low"
```

```toml
ANTHROPIC_API_KEY = "your billed API key"
```

Only a completed final answer is accepted; truncated answers are discarded.
Web search uses Anthropic's server-side tool when enabled.

## Grok realtime

`[agent] backend = "grok-realtime"` streams captured audio to xAI's realtime
voice and plays its spoken reply as it arrives. It uses `XAI_API_KEY` (or
`[agent.realtime] key_env`), billed per use; the Grok login is never used.

Try one turn without the radio:

```bash
walkietalk voice-agent-check question.wav --output reply.wav
walkietalk voice-agent-check question.wav --output reply2.wav --supervised   # simulated keying
```

How it works in `talk`:

- Audio streams to the session while someone speaks, so unaddressed audio is
  uploaded before the wake check. When the utterance ends, the server's own
  transcript goes through the shutdown, sleep, and wake gates.
- Rejected, control, and empty utterances are deleted from the remote
  conversation before any reply is requested.
- The transmitter keys only when audible speech arrives, unkeys around tool
  calls, and the whole reply stays within `max_tx_seconds`. An interrupted
  reply discards the remote conversation so the next turn starts fresh.
- The last `history_turns` exchanges are kept; older ones are deleted from the
  session.
- Confirmations and voice station IDs are spoken by the realtime voice on a
  short separate connection.
- With messaging enabled, the configured `[tts]` voice still reads contact
  messages, and `[stt]` transcribes incoming voice notes when
  `transcribe_voice` is on.

The realtime session uses its own `connect_timeout_seconds` and
`idle_timeout_seconds`, not `[agent] timeout_seconds`. A rejected key stops
`talk`; network failures are retried each second.
