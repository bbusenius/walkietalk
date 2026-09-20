# Codex text-reply checkpoint

Codex is the third phase 5 adapter, after the stub and Charlotte/Hermes.
Implementation and the live two-turn text check are complete. **Brad confirms
the Codex demonstration works marvelously.** The [Grok Build checkpoint](GROK.md)
has also been confirmed by Brad. Claude Code and Messages API are the current additions; see [their separate setup and accepted checkpoints](CLAUDE.md). Phase 5
implementation and demonstrations are accepted; Brad has authorized publication and merge.

Continuous `talk --capture` now waits indefinitely through silence and resumes
listening after agent/STT errors. See the [continuous listening and remote
shutdown guide](CONTINUOUS.md) for the new controls and complete demonstrations.

## Setup and behavior

Verified locally with **codex-cli 0.155.1**, Brad's saved **ChatGPT** login, and
`gpt-6-astra` (copied from his existing Codex model choice). The adapter follows
[Codex non-interactive execution](https://learn.chatgpt.com/docs/non-interactive-mode)
and its [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).
Walkietalk implements no login flow. Install the official Codex CLI separately
and use `codex login` as the normal user when necessary. `codex login status`
should report `Logged in using ChatGPT`. API-key login is rejected by this adapter;
there is no API-credit fallback. CLI account limits still apply.

In the existing `agent` section, select `backend: "codex"` and include:

```yaml
  codex_executable: "codex"
  codex_model: "gpt-5.6-luna"
  codex_reasoning_effort: "low"
```

These fields are required in every config, even when selecting another agent.
Brad subsequently selected `gpt-5.6-luna` and requested `low` reasoning for radio
replies. The adapter passes `-c 'model_reasoning_effort="low"'` on each new or
resumed turn. `default` omits this override and uses the model default; it does
not inherit desktop reasoning settings. See the [reasoning settings](../README.md#reasoning-effort-for-radio-replies)
for allowed values and how they differ from timeout and reply length.
`codex_executable` is a name on PATH or an absolute executable path, without flags.
`codex_model` is a model identifier; `""` uses the CLI's built-in default.
The adapter ignores the desktop user configuration, so an empty value does not
inherit the model from `~/.codex/config.toml`. See
[config.example.yaml](../config.example.yaml) for the complete schema.

The subprocess runs in a fresh empty temporary directory, using `codex exec`
with a read-only sandbox. Shell tools, connectors/apps, hooks, web search, and
delegation are disabled; desktop MCP configuration is not loaded. Permission
requests are never approved. Strict config validation makes incompatible CLI
versions fail locally. No changes are made to the desktop Codex configuration.

Traffic and instructions go through stdin, never shell interpolation. Only a
successful completed turn and its final-answer file can become a reply. Event
logs, stderr, failed-turn output, and login diagnostics are withheld. The bridge
rejects oversized or malformed final text before retaining conversation history.
The environment excludes provider API keys and the Hermes bearer token.

Follow-ups resume the explicit Codex session created for this invocation, never
`--last` or an unrelated desktop chat. Once `agent.history_turns` is reached,
the adapter starts a fresh native session seeded with only the bounded recent
traffic/reply pairs. This preserves context within the configured budget.
Restarting walkietalk starts a new radio conversation. Codex itself stores its
sessions under its own home directory; the bridge does not delete CLI history.

`agent.timeout_seconds` (default 60; greater than zero, at most 300) includes
login verification and generation. On timeout or Ctrl+C/SIGTERM, the bridge
terminates the CLI process group, then force-stops it if necessary, allowing up
to one extra second for cleanup. Late replies are discarded. This stops local
processes; it is not a guarantee about provider-side cancellation or usage.
Both stdout/stderr and the final-answer file have transport size limits.

Charlotte needs no restart or configuration change for this adapter. Codex does
not use `.env.hermes.local`; that optional file remains useful when switching
back to Hermes. STT, wake handling, radio settings, and future voice selection
remain independent of the selected agent.

## Complete Codex family demonstration

Run these from `/home/brad/Documents/Code/walkietalk` as Brad. Answers appear on
the computer. Keep the gateway **TX light off** throughout; no spoken radio
answers are expected. Stop each running `talk` command with Ctrl+C before editing
configuration or starting another command.

1. **Check the connection.** The local config now selects Grok for its checkpoint. To repeat this Codex
   demo, use `nano config.local.yaml` and set only `agent.backend` to `"codex"`.

   ```sh
   codex --version
   codex login status
   .venv/bin/walkietalk -c config.local.yaml agent-check "What is rain? Answer in one short sentence."
   ```

   Expect version/login information, an `Agent: codex` label, and a short real
   `Reply:` about rain. `agent-check` uses no STT, audio, or serial/PTT.

2. **Ask over the walkie, then follow up.**

   ```sh
   .venv/bin/walkietalk devices
   .venv/bin/walkietalk -c config.local.yaml talk --capture
   ```

   Confirm the configured AIOC name still appears in `devices`; if its card
   number changed, update only the pinned audio names using the exact listing.
   Do not substitute the system default. Current local settings use conversation
   mode with a 10-second window after the answer prints. Say:

   - “Picard epsilon five, what is rain?” (or your current configured wake name).
   - After `Reply:` prints, within 10 seconds: “Why does that happen?”

   Expect a cyan transcript, green accepted traffic with the name removed, and
   a short answer. The unaddressed follow-up should explain the previous answer.
   After the window expires, an unaddressed question is ignored; naming the
   helper again is accepted. End with Ctrl+C. TX stays off in all cases.

3. **Compare the two real helpers with the same radio setup.**

   ```sh
   nano config.local.yaml
   ```

   Change only the `backend` field **inside `agent:`** from `"codex"` to
   `"hermes"`. Preserve STT, wake, audio, PTT, gain, and other agent fields.
   Then run:

   ```sh
   . ./.env.hermes.local
   .venv/bin/walkietalk -c config.local.yaml talk --capture
   ```

   Ask the same named rain question. Expect a real Charlotte reply as text and
   TX off. Ctrl+C, use `nano config.local.yaml` to restore `agent.backend` to
   `"codex"`, and run the same `talk --capture` command again. Expect a real
   Codex reply with the unchanged radio/wake/STT setup. Wording can differ.
   Each restart starts a fresh conversation.

4. **Demonstrate failures without damaging a real login.**

   ```sh
   .venv/bin/python scripts/demo_codex_failures.py
   ```

   This uses a temporary fake CLI with the actual adapter and process supervisor.
   Expect four labeled failures: oversized answer, timeout, login failure, and
   missing CLI. Each prints a local `Error:` and
   `Expected error received; no answer printed and no PTT opened.` No `Reply:`
   should appear. The script exits 0 after the expected failures; each simulated
   bridge command exits 1. TX remains off. This verifies controlled failures,
   not an outage or expired login on the real Codex service.

**Explain:** “First we used a pretend answer to test the connection. Now different
helpers can answer through the same radio bridge. The listener writes down our
words, the name check decides when to respond, and the selected helper answers.
A radio voice comes later.”

Report the radio answer, context-dependent follow-up, two-helper comparison,
failure-demo results, and TX-off observation. Brad has accepted this checkpoint; we proceeded to Grok Build.
The [complete phase 5 checklist](PHASE5-DEMO.md#complete-phase-acceptance-checklist)
still applies; this checkpoint does not complete the whole phase or authorize
committing/publishing.

## Verification record

The coding assistant verified a real rain question and follow-up through the
saved ChatGPT login; both returned short text using the same explicit Codex
session. No live radio capture or TX-light observation was performed by the
coding assistant. Automated coverage includes isolated CLI execution, saved-login
requirements, context rotation, malformed/failure output, size limits, timeouts,
blocked pipes, descendant cleanup, SIGINT/SIGTERM, and no hardware access on the
agent-check path. The controlled failure script passed.

All 230 automated tests passed at this checkpoint, along with Ruff lint, Ruff
format checking, and `git diff --check`. These results are separate from the
family observations. Brad subsequently confirmed that the Codex demonstration
works marvelously; individual failure and TX-light results were not separately
itemized.

Reasoning update: 303 automated tests passed, including explicit effort on new
and resumed turns. A real `gpt-5.6-luna` answer was verified with `reasoning=low`.
