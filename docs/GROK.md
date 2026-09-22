# Grok Build text-reply checkpoint

Grok Build is the fourth phase 5 adapter, after the stub, Charlotte/Hermes, and
Codex. Brad has confirmed those demonstrations. The Grok implementation and live
two-turn text check are complete; **Brad confirms the radio demonstration and
five-item follow-up checklist passed**, including backend switching, context,
controlled failures, continuous listening, shutdown, and TX-off observations.
Claude Code and Messages API are the current additions; see [their setup and accepted checkpoints](CLAUDE.md). Phase 5 implementation and demonstrations are accepted; Brad has authorized publication and merge.

Continuous `talk --capture` now waits indefinitely through silence and resumes
listening after agent/STT errors. See the [continuous listening and remote
shutdown guide](CONTINUOUS.md) for the new controls and complete demonstrations.

## Setup and behavior

Verified locally with **Grok Build 1.0.34**, the existing SuperGrok login, and
**grok-4.6**. This installation rejects the `grok-build` model alias shown in some
upstream examples, so the adapter selects the verified model explicitly.

The adapter uses Grok's official
[headless interface](https://docs.x.ai/build/cli/headless-scripting), the same
single-turn mode as `grok -p`. It passes traffic through
`--prompt-file /dev/stdin --verbatim`, avoiding shell interpolation and prompt-file
reference expansion. The CLI's
[headless reference](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/14-headless-mode.md)
describes these flags, explicit sessions, and structured output.

In the existing `agent` section:

```yaml
  backend: "grok"
  grok_executable: "grok"
  grok_model: "grok-4.6"
  grok_reasoning_effort: "low"
```

Keep all other fields; see [the complete example](../config.example.yaml).
`grok_executable` is an executable name on PATH or an absolute path without flags.
`grok_model` is a first-party Grok model ID available to your CLI account.
These fields are required even when selecting another agent.
`grok_reasoning_effort` passes `--reasoning-effort` on each new or resumed turn.
Brad chose `low` for radio replies, overriding the desktop profile's `xhigh`
without editing that profile. `default` omits the flag and inherits the profile
or model setting. See the [reasoning settings](../README.md#reasoning-effort-for-radio-replies). There is no
model fallback. Brad's local config already contains the fields and selects Grok.

`agent.backend: grok` selects **Grok Build**, which answers text questions.
`stt.backend: grok` independently selects **Grok Voice Transcribe**, which turns
recorded speech into text. No STT code was changed, and speech is never sent to
`grok -p` for transcription.

Install the official Grok CLI separately and use `grok login` as the normal user
when needed. Walkietalk does not implement OAuth, read token contents, or copy
credentials. The CLI owns login, token refresh, and stored sessions under
`$GROK_HOME` or `~/.grok`. No `.env.hermes.local` or `XAI_API_KEY` is needed for this
adapter. The subprocess excludes provider keys and endpoint overrides and sets
`GROK_DISABLE_API_KEY_AUTH=1`; the preflight verifies API-key authentication is
disabled. There is no fallback to a developer API bill. Normal subscription
limits still apply.

The adapter requires a standard first-party Grok profile. Before inference it
checks `grok inspect --json`: active hooks, MCP servers, plugins, or LSP servers,
unverifiable login policy, and custom model/provider definitions cause a clear
local error. Raw configuration and diagnostics are not printed. This restriction
prevents desktop integrations or separate provider credentials from being used
by the radio adapter.

For this verified CLI version, compatibility flags alone did not prevent
headless MCP discovery. Each subprocess therefore receives an empty temporary
`HOME` and an explicit original `GROK_HOME`. Desktop Claude/Cursor configuration
is outside that temporary home; the saved Grok login remains in its original
location. The existing desktop configuration is never edited. Native Grok profile
settings still exist, which is why the preflight is required.

The CLI runs in an empty temporary working directory, with `dontAsk` permission
mode and no subagents. With `agent.web_search: false` (the default) it uses a
read-only sandbox, a deny-all tool rule, web search disabled, and a tool filter
that removes the selected built-in tool. With `agent.web_search: true` the only
offered tool is web search, shell and file tools stay denied, and the sandbox is
`workspace` so the lookup can use the network. These controls apply even when
desktop Grok normally auto-approves tools. The adapter rejects any tool call
other than that lookup, and it rejects active connector status in the result.
Headless mode runs the agent in process; the adapter does not attach to a shared
desktop leader.

Only the successful terminal result from `streaming-messages-json` becomes an
answer. Walkietalk checks the dedicated session ID, OAuth login marker,
permission mode, successful end-of-turn status, and absence of unexpected tool
calls.
Reasoning, intermediate text, stderr, error objects, and incomplete answers are
never used as replies. The common reply cap still applies.

Follow-ups resume only the explicit UUID created for this radio invocation,
never `--continue` or an unnamed resume. When `agent.history_turns` is reached,
a fresh native session is seeded with the bounded recent traffic/reply pairs.
Restarting walkietalk starts a new conversation. Grok retains its own CLI session
history; walkietalk does not delete it.

`agent.timeout_seconds` (default 60; greater than zero and at most 300) covers
inspection and generation. Timeout or Ctrl+C/SIGTERM terminates the subprocess
group, force-stopping it if necessary, with up to one extra second for cleanup.
Late results are discarded. Local process cleanup does not guarantee cancellation
or usage accounting at the provider. Output is bounded before parsing.

## Complete Grok family demonstration

Run from `/home/brad/Documents/Code/walkietalk` as Brad. Keep the gateway **TX
light off** throughout. Answers appear on the computer; speech generation is
phase 6. Charlotte does not need a restart.

1. **Check a typed question.**

   ```sh
   grok --version
   .venv/bin/walkietalk -c config.local.yaml agent-check "What is rain? Answer in one short sentence."
   ```

   Expect `Agent: grok (... Grok Build saved login; text only)` and a short real
   `Reply:` about rain. This command uses no STT, audio, or serial/PTT.
   If the login has expired, run `grok login` in your normal shell, then retry.
   Do not add an API key as a workaround.

2. **Ask over the walkie and follow up.**

   ```sh
   .venv/bin/walkietalk devices
   .venv/bin/walkietalk -c config.local.yaml talk --capture
   ```

   Check that the configured AIOC name appears in `devices`. If ALSA card numbers
   changed, update only the pinned audio names using that listing; do not use the
   system default. The current local config remains in conversation mode with
   a 10-second follow-up window after the answer prints.

   - Say “Picard epsilon five, what is rain?” or your current configured name.
   - After `Reply:` prints, within 10 seconds say “Why does that happen?”

   Expect a cyan transcript, green accepted traffic with the wake phrase removed,
   and a short text answer. The unnamed follow-up should refer to the prior
   answer. After the window expires, unnamed traffic is ignored; naming the
   helper again is accepted. Ctrl+C stops listening. TX stays off.

3. **Compare Grok and Codex with the same setup.** Stop `talk` with Ctrl+C.

   ```sh
   nano config.local.yaml
   ```

   Change only `backend` **under `agent:`** from `"grok"` to `"codex"`.
   Leave `stt.backend`, radio devices, wake settings, gain, and all other fields
   unchanged. Run:

   ```sh
   .venv/bin/walkietalk -c config.local.yaml talk --capture
   ```

   Ask the same named rain question. Expect a real Codex reply as short text and
   TX off. Ctrl+C, restore `agent.backend` to `"grok"` with `nano config.local.yaml`,
   then run the same command and question again. Expect a Grok reply through the
   unchanged listener and radio setup. Wording may differ; each restart starts
   a fresh conversation.

4. **Demonstrate controlled failures.** Stop the listener first.

   ```sh
   .venv/bin/python scripts/demo_grok_failures.py
   ```

   This runs the actual adapter against a temporary fake CLI. Expect four local
   errors: oversized answer, timeout, expired login, and missing CLI. Each prints
   `Expected error received; no answer printed and no PTT opened.` No `Reply:`
   appears. The script exits 0 after the expected failures; each underlying
   bridge command exits 1. Observe TX off. No real account, network, STT, or
   hardware is used; this is controlled failure evidence, not a real outage test.

**Explain:** “First we used a pretend answer to test the connection. Now different
helpers can answer through the same radio bridge. One Grok service writes down
our words; Grok Build is the helper that answers them. Changing the helper does
not change how the radio hears us.”

Report the question, follow-up, backend comparison, failure-demo results, and
TX-off observation. Brad has confirmed both Claude routes work as expected. The
[complete phase 5 checklist](PHASE5-DEMO.md#complete-phase-acceptance-checklist)
still applies, including explicit authorization before committing or publishing.

## Verification record

The coding assistant verified a real rain question and context-dependent
follow-up with the saved CLI login. Both returned short final text using the
same dedicated Grok session. The run passed the OAuth, permission-mode, and
connector checks. No live radio capture or TX-light observation was performed
by the coding assistant.

Automated tests cover profile restrictions, API-key exclusion, isolated home,
final-result parsing, context rotation, deadlines, malformed and oversized
answers, login/missing-CLI errors, and absence of hardware access. Existing
process-group cleanup and radio safeguards remain in the suite. The controlled
failure demonstration passed. Brad subsequently confirmed the radio checkpoint
and the five-item follow-up checklist; the controlled failures are simulations,
not live account outages.

All 273 automated tests passed at this checkpoint. Ruff lint, Ruff formatting,
and `git diff --check` passed. These checks are separate from the radio
demonstration and TX-light observation subsequently confirmed by Brad.

Reasoning update: 303 automated tests passed, including explicit effort on new
and resumed turns. A real `grok-4.6` answer was verified with `reasoning=low`.
