# Claude text agents: choose CLI login or Messages API

Brad requested both connections after initially deferring Claude. They are two
explicit agent selections; neither falls back to the other or to another agent.
Phase 5 prints answers on the computer. It adds no speech generation or PTT.

| Selection | Connection and authentication | Verification |
| --- | --- | --- |
| `claude` | Official, unmodified `claude -p`; the CLI's saved Claude account login | Claude Code 2.1.277 / `claude-sonnet-5`: real answer and contextual follow-up verified by coding assistant; Brad confirms it works as expected |
| `claude_api` | Anthropic Messages API; explicitly supplied API key and separate API billing | Automated fake-HTTP and controlled failure checks pass; Brad confirms the live adapter works as expected |

Brad's confirmation accepts both remaining adapter checkpoints. The coding
assistant did not make a live Anthropic API call. Individual Claude failure and
TX-light results were not separately itemized in Brad's latest message; earlier
phase-wide failure and TX-off demonstrations remain accepted. Brad has now
explicitly authorized the phase 5 commit, publication, and merge.

Current automated checkpoint: **439 tests passed**, Ruff lint and format checks
passed, and all nine cases in `scripts/demo_claude_failures.py` produced the
expected local errors. These checks do not substitute for Brad's TX-light and
family observations.

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
```

`agent.backend: claude` selects the official CLI; `claude_api` selects the API.
Both routes are text
agents, not Charlotte's Hermes environment: they do not inherit her skills or
memory. STT remains independent. See [the capability review](STT-CAPABILITIES.md).

## Required configuration

Add these six fields to the existing `agent` section even when another backend
is selected. The exact-field schema requires them. Brad's local config has been
extended without replacing existing values. The latest local validation shows
`agent.backend: claude`, `stt.backend: grok`, and `ptt.line: dtr`; the agent
selection was changed locally during this checkpoint and left as found.

The CLI backend was renamed from `claude_code` to `claude`. Existing configs
also rename its fields to `claude_executable`, `claude_model`, and
`claude_reasoning_effort`; the API fields are unchanged. Brad's local config
has been migrated. The former backend name and field names are no longer accepted.

```yaml
  claude_executable: "claude"
  claude_model: "claude-sonnet-5"
  claude_reasoning_effort: "low"
  claude_api_key_env: "ANTHROPIC_API_KEY"
  claude_api_model: "claude-sonnet-5"
  claude_api_reasoning_effort: "low"
```

Select `agent.backend: claude` or `agent.backend: claude_api`, then restart.
Models must be explicit first-party IDs, with no model fallback. The example
ID was checked against the [current model catalog](https://platform.claude.com/docs/en/models/overview).
Both effort settings accept `default`, `low`, `medium`, `high`, `xhigh`, or `max`;
the chosen model/account must support the level. `default` omits the override,
which is useful for models without effort support. Unsupported combinations
fail locally when the service rejects them; they do not switch models.

The existing `agent.timeout_seconds` (default 60, maximum 300),
`max_reply_chars` (default 600, maximum 2000), and `history_turns` (default 8,
maximum 32) apply to both. Reasoning effort, reply length, and time are separate
limits. The API output-token budget is `max(2048, 2 * max_reply_chars)` including
any reasoning tokens; a truncated response is discarded, even if under the
character cap. Low effort does not guarantee that every question meets the deadline.

## CLI setup and behavior

Install/update the [official CLI](https://code.claude.com/docs/en/setup) and log
in as the same Linux user who runs Walkietalk:

```sh
claude --version
claude auth status
# Only if a login is needed:
claude auth login
```

Walkietalk checks for the required isolation flags before requesting an answer.
The verified version is **2.1.277**; older CLIs missing those flags fail with an
update instruction. Walkietalk never reads, copies, or refreshes Claude account
tokens. It asks the CLI for authentication status and requires saved first-party
Claude account login. API-key-only authentication must use `claude_api` instead.

Each request runs in an empty temporary directory. The adapter uses
`--safe-mode`, `--restricted`, no user/project settings, disabled hooks, denied
MCP tools, an empty strict MCP configuration, disabled slash commands/browser
integration, and no permission prompts. Built-in tools stay empty unless
`agent.web_search` is `true`, which offers only `WebSearch`. It sends traffic
on stdin and replaces the system prompt with the bridge's instructions.
`--no-session-persistence` avoids saving/resuming desktop chats; each request
replays only this invocation's bounded completed radio history. The radio
session ID remains the same across follow-ups. Restarting starts fresh.

The environment allowlist preserves the official login location (`HOME` and
optional `CLAUDE_CONFIG_DIR`), but excludes API keys, injected OAuth tokens,
alternative-provider routing, and desktop-agent configuration variables.
It deliberately does not use `--bare`, which skips saved-login authentication.
See [headless operation](https://code.claude.com/docs/en/headless) and the
[CLI flag reference](https://code.claude.com/docs/en/cli-reference). Organization
managed policy still applies; this adapter does not bypass that policy.

Only a successful final result from the expected model/session becomes answer
text. Tool activity other than an allowed web lookup, malformed output,
permission failures, and unsuccessful results are discarded. Rate-limit metadata is ignored. The overall timeout
includes preflight and generation; the subprocess supervisor terminates the
process group on exit, timeout, or interruption. Cleanup can add about one second.

## API setup and behavior

`claude_api` requires the environment variable named by `claude_api_key_env`.
The default is `ANTHROPIC_API_KEY`; YAML contains only that variable's name.
Enter a key without displaying it or putting it in shell history:

```bash
read -rsp 'Anthropic API key: ' ANTHROPIC_API_KEY
echo
export ANTHROPIC_API_KEY
```

This uses billed Anthropic API access, independently of a Claude subscription
or CLI login. It never invokes the CLI or reads its credentials. A missing key
fails before HTTP. No live API request was made during implementation because
no key was present. Brad subsequently confirmed the API adapter works as expected.
Selecting another backend never calls this API.

Requests go to `POST https://api.anthropic.com/v1/messages`, with `x-api-key`,
`anthropic-version: 2023-06-01`, and JSON content headers. The existing HTTPX
dependency supplies transport under one overall asynchronous deadline covering
connect and the entire response body. There are no redirects, ambient proxies,
automatic retries, SDK, or MCP. Tools stay absent unless `agent.web_search` is
`true`, which adds the server-side web search tool with a limit of three
lookups in that request. The bridge supplies the system instructions
and bounded user/assistant history. Only completed final text blocks are kept;
thinking blocks never become answers. Error bodies and credentials are withheld.
Timeout closes the connection and discards late output; it cannot guarantee
that Anthropic stops server-side work or avoids billing for it.
See the [Messages API](https://platform.claude.com/docs/en/api/messages/create).

## Accounts and family use

Use your own authorized account or API key and check the applicable terms,
usage limits, organization rules, and any extra-usage settings. Using the
official CLI does not guarantee that every shared-account arrangement is
permitted. See [Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance).
First-party Claude accounts are for adults 18+, and account credentials should
not be shared. For a product serving minors through the API, review the
[Usage Policy's additional requirements](https://www.anthropic.com/legal/aup)
and [age requirement](https://support.claude.com/en/articles/9307344).
Supervise the family demo and explain that replies are from AI; phase 5 shows
the selected agent in the computer log. No spoken preamble or age gate is added.

## Receive-only family demonstration

Run from the project directory as Brad. Keep the gateway **TX light off** in
every step. Answers appear only on the computer. These commands repeat the
accepted Claude checkpoint; the stub/Hermes/Codex/Grok and shutdown demos remain
accepted in the [complete phase checklist](PHASE5-DEMO.md).

1. Prepare three temporary configs from your current setup. These differ only in
   the agent selection; the original local config is not edited:

   ```sh
   .venv/bin/python - <<'PY'
   from pathlib import Path
   import yaml
   data = yaml.safe_load(Path('config.local.yaml').read_text())
   for backend in ('claude', 'claude_api', 'grok'):
       data['agent']['backend'] = backend
       path = Path('/tmp') / f'walkietalk-{backend}.yaml'
       path.write_text(yaml.safe_dump(data, sort_keys=False))
       path.chmod(0o600)
       print(path)
   PY
   ```

2. Check the CLI with typed traffic:

   ```sh
   .venv/bin/walkietalk -c /tmp/walkietalk-claude.yaml agent-check "Why does a rainbow have different colors? Answer in one sentence."
   ```

   Expect `Agent: claude (...; CLI saved login; reasoning low)` and a short
   `Reply:`. No audio, STT, or PTT is opened by `agent-check`.

3. Start the continuous radio listener:

   ```sh
   .venv/bin/walkietalk -c /tmp/walkietalk-claude.yaml talk --capture
   ```

   Say your configured wake phrase followed by the same rainbow question.
   Expect `Transcript:`, `Accepted`, `Traffic:` without the wake prefix, then a
   short `Reply:`. No on-air answer is expected. In conversation mode, after the
   reply prints and while the follow-up window is open, send “What did I just
   ask about?” without the wake phrase. Expect a reply about rainbow colors.
   The current local window is 10 seconds; this never limits how long the
   listener runs. After expiry, the wake phrase is required again. Ctrl+C exits.

4. Verify the API's missing-key error, regardless of the current shell's key:

   ```sh
   env -u ANTHROPIC_API_KEY .venv/bin/walkietalk -c /tmp/walkietalk-claude_api.yaml agent-check "Hello"
   ```

   Expect exit status 1 and `Error: claude_api requires a valid ANTHROPIC_API_KEY ...
   billed Anthropic API ...`, with no `Reply:` and no HTTP request. If you changed
   the configured environment variable name, use that name with `env -u`.

5. **Only with an API key and a deliberate decision to use API credits**, load
   the key as above, then run:

   ```sh
   .venv/bin/walkietalk -c /tmp/walkietalk-claude_api.yaml agent-check "Why does a rainbow have different colors? Answer in one sentence."
   .venv/bin/walkietalk -c /tmp/walkietalk-claude_api.yaml talk --capture
   ```

   Expect `Agent: claude_api (...; ANTHROPIC_API_KEY; billed API; reasoning low)`.
   Repeat the named question and the unaddressed contextual follow-up in this
   same invocation. Expect short text and TX off. Ctrl+C exits. If no API key is
   available, report this live check as **pending or explicitly deferred**, not passed.

6. Run controlled failures for both adapters without changing real credentials:

   ```sh
   .venv/bin/python scripts/demo_claude_failures.py
   ```

   Expect nine simulated failures: CLI oversize, timeout, login, missing binary;
   API missing key, oversize, timeout, HTTP 401, HTTP 500. Each prints a local
   `Error:` and `Expected error received; no answer printed and no PTT opened.`
   No `Reply:` should appear. The script uses temporary fake CLI/HTTP transports
   and guards against hardware access; observe TX off separately.

7. Switch back to Grok using the third temporary config:

   ```sh
   .venv/bin/walkietalk -c /tmp/walkietalk-grok.yaml talk --capture
   ```

   Expect the Grok agent and the same radio, wake, and STT settings.
   Ask the same named question; this is a fresh conversation. Ctrl+C or your
   configured shutdown phrase/code ends the listener.

   Your original `config.local.yaml` is untouched by these steps. To keep a
   preferred helper for normal use, change only its `agent.backend` and restart.

Family explanation: “The same radio bridge can ask different AI helpers. This
choice uses the coding app's login; that choice uses a separately paid usage key.
The listener that turns our voices into words is still a separate choice.”

Report the observed results and any explicit API-demo deferral before authorizing
a phase 5 commit or publication. No phase 6 work is included.
