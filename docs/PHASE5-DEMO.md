# Phase 5: text replies, one adapter at a time

Phase 5 implementation, automated checks, and family checkpoints are **accepted**;
Brad has explicitly authorized committing, pushing, and merging the phase. Brad reports showing the stub to the
girls and confirms that the Charlotte/Hermes demonstration works. Brad also
confirms the Codex demonstration works marvelously. Brad has now confirmed the
Grok Build radio checkpoint, including follow-up context, backend switching,
controlled failures, and the TX-off observations.
See the [complete Grok checkpoint instructions](GROK.md).
The current addition is [continuous listening and remote shutdown](CONTINUOUS.md),
requested by Brad after discovering the old 60-second idle exit. Its automated
checks pass. Brad reports testing everything, including the five-item follow-up
checklist and shutdown with phrase and code together or in separate transmissions.
That checkpoint is accepted. Brad subsequently requested both Claude routes:
`claude` and `claude_api`. They are implemented with automated checks;
Claude Code passed the coding assistant's real two-turn text check. Brad now
confirms that both `claude` and `claude_api` work as expected, completing the
remaining adapter checkpoints. This records Brad's live confirmation separately
from automated checks; it does not claim the coding assistant made a live API call.
To repeat the checks, follow the
[complete Claude demonstration commands and expected results](CLAUDE.md#receive-only-family-demonstration).
The requested
Hermes/Codex STT [capability review](STT-CAPABILITIES.md) is complete: Brad chose
existing local Whisper over a new Hermes service, and no standalone Codex STT
contract was verified. No new transcription adapter or billed substitute was added.
No commits, pushes,
PRs, merges, or phase 6 work before the requested checkpoints and authorization.

## Complete phase acceptance checklist

- [x] Stub demonstration: Brad reports showing it to the girls. Detailed results
      for the cap test and TX observation were not separately reported.
- [x] Charlotte/Hermes: verify the supported API and Charlotte's configured
      environment (instructions, skills, tools, memory), then demonstrate a
      short real answer. Local bearer token comes from the environment; Hermes
      retains upstream provider credentials. Brad confirms the demonstration
      works and reports expected timeouts on slower questions.
- [x] Codex: installed `codex exec`, saved ChatGPT login, and real follow-up
      verified. Brad confirms the demonstration works marvelously.
- [x] Grok Build: verify the installed `grok -p` interface and account login;
      demonstrate a short final answer. This is separate from Grok Voice
      Transcribe STT. No fallback to billed xAI chat API. Pause for the demo.
- [x] Claude Code: official `claude -p` adapter accepted. The coding assistant
      verified a real answer and contextual follow-up; Brad confirms it works
      as expected after the rename to `claude`.
- [x] Claude API: Brad confirms `claude_api` works as expected, providing live
      confirmation beyond the coding assistant's fake-HTTP checks. No live-key
      deferral is needed.
- [x] Both Claude adapters have controlled timeout, oversize, authentication,
      and transport-failure coverage with hardware-access guards. These are
      automated results; Brad's general confirmation did not separately itemize
      each Claude failure case or TX-light observation. The earlier phase-wide
      failure and TX-off demonstrations were already accepted.
- [x] Inspect the requested Hermes/Codex STT expansion. The Hermes service is
      deferred by Brad; Codex has no verified standalone transcription contract
      in the inspected interfaces. Neither is labeled supported for STT.
- [x] Compare at least two real backends: select each in config, restart for a
      fresh dedicated radio session, and ask the same question. Radio, wake,
      STT, and voice choices must not change with the agent selection.
- [x] Conversation: addressed question followed by “Why does that happen?”
      without the name uses earlier traffic and answers in the same session.
      The follow-up window begins after the answer prints. Expiry requires the
      name again but preserves bounded history. Restart creates a fresh session.
- [x] Limits/failures: oversized reply, controlled timeout, and controlled login
      failure produce clear local errors, no answer text, and no TX light.
      Automated tests also verify missing CLI, malformed/empty output, permission
      failure, cancellation, and discarded late answers for real transports.
- [x] Diagnostics, errors, and credentials never enter final answer text.
      Spoken traffic is never interpolated into shell commands.
- [x] Continuous listening: leave the channel quiet for more than a minute,
      then ask an addressed question. Wake-window expiry must not stop the app.
      Controlled agent/STT failures recover to listening; device failures remain
      explicit. Follow the [exact commands and expected results](CONTINUOUS.md).
- [x] Remote shutdown: unarmed code alone, incorrect combined command, wrong
      confirmation, and expired confirmation leave the program running. The
      correct pair together or in separate utterances exits normally, with the
      gateway TX light off. Brad confirms the current demonstrations, including
      both shutdown forms and the five-item follow-up checklist.
- [x] Complete lint, formatting, and all phase 1–5 automated tests: 439 passed.
      Preserve
      parent-owned PTT and isolated playback safeguards; keep version `0.1.0`.
- [x] Brad has confirmed the family checkpoints, including both Claude routes;
      the earlier family explanations and demonstrations remain accepted.
- [x] Brad explicitly authorized committing, pushing, and merging the phase.

Every family demonstration in this phase is **receive-only**: answers appear on
the computer; the gateway TX light stays off. No `--transmit` commands are used.
No on-air answer is expected until phase 6.

## Adapter status

| Adapter | Implementation / automated verification | Actual connection / family observation |
| --- | --- | --- |
| Stub | Implemented; offline contract, caps, history, and CLI tests | Brad reports showing it to the girls |
| Charlotte/Hermes | Implemented; Runs API, bounded deadline, cancellation, authentication, and context tests | Hermes 0.19.0: two real text turns verified by coding assistant; Brad confirms the family demonstration works |
| Codex CLI | Implemented; saved-login, context, subprocess deadline and cleanup tests | Codex 0.155.1, ChatGPT login: two real text turns verified; Brad confirms the family demonstration works |
| Grok Build CLI | Implemented; saved-login policy, final-output parsing, context and failure tests | Grok 1.0.34 / grok-4.6: real text turns verified; Brad confirms radio demonstration and five-item follow-up checklist |
| Claude Code CLI | Implemented; isolated official CLI, saved login, bounded context, deadline and failure tests | CLI 2.1.277 / claude-sonnet-5: real answer and contextual follow-up verified by coding assistant; Brad confirms `claude` works as expected |
| Claude Messages API | Implemented; explicit API key, bounded HTTP, final-text and failure tests | Brad confirms `claude_api` works as expected; no live API call by coding assistant |

The two Claude choices are `claude` for the official CLI and `claude_api` for
billed API access. No Claude STT endpoint was verified. Hermes failure tests use controlled transports;
successful live questions separately verify the actual API connection.

The current automated checkpoint has **439 passing tests**, Ruff lint and format
checks, and all nine offline Claude failure demonstrations. The prior continuous
listener/shutdown checkpoint passed its offline demonstration as well. Claude
details are recorded in [its checkpoint guide](CLAUDE.md). These results
do not substitute for the family radio checklist. Existing configs also need
the new required `shutdown` section shown in `config.example.yaml`; keep all
existing device, wake, STT, and agent settings when adding it.

## Charlotte/Hermes checkpoint

The coding assistant inspected the running Charlotte image, which contains
Hermes **0.19.0**, and enabled its supported API in the existing profile. The
gateway runs in `/workspace` with Charlotte's `AGENTS.md`, configured external
skills, profile memory, and API toolsets matching the CLI toolsets. The adapter
does not select an upstream model/provider. This is Charlotte's agent environment.

The integration uses the documented [Hermes Runs API](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server),
with its status and stop operations, and [HTTPX async requests](https://www.python-httpx.org/async/)
under an overall deadline. The installed API source was checked as well as the
documentation. `agent-check` and `talk` share the same adapter.

### Local setup already completed

- Charlotte's existing profile `.env` at `~/.hermes-charlotte/.env` received the
  API settings below. This is distinct from Charlotte's repository-local `.env`.
  The default profile is mounted at `/opt/data` in Docker; installations using
  `CHARLOTTE_HERMES_HOME` must use that profile's location instead.
- `~/.hermes-charlotte/config.yaml` gained `platform_toolsets.api_server`, matching
  the existing CLI toolsets. The gateway was restarted. No Charlotte source-code,
  Docker image, or Hermes implementation changes were needed.
- The host connects directly to the container's private Docker bridge address,
  recorded in ignored `config.local.yaml`. Inside the container the API binds
  `0.0.0.0`; it has **no published host port**. The bearer token is still required.
- The bridge token is in ignored `.env.hermes.local`, with owner-only read/write
  permissions. Provider credentials stayed in Hermes. No tokens were printed.
- A private `walkietalk-api-backup-*` directory in Charlotte's profile preserves
  her prior `.env` and `config.yaml`. Restoring those files and restarting her
  gateway undoes the API setup; do not restore over later profile changes.
- `config.local.yaml` now selects `hermes`. Existing audio, PTT, wake, aliases,
  STT, gain, and conversation settings were preserved.

The server-side additions have this shape. The bearer token below is a
placeholder; actual token values must remain in private environment storage:

```dotenv
API_SERVER_ENABLED=true
API_SERVER_HOST=0.0.0.0
API_SERVER_PORT=8642
API_SERVER_MODEL_NAME=charlotte
API_SERVER_KEY=<private-random-local-bearer-token>
```

The verified profile's API toolsets are `hermes-cli`, `browser`, `image_gen`,
`delegation`, `google_maps`, and `grok_mcp`, matching that profile's `cli` list.
For another profile, deliberately choose tools from its existing configuration;
optional MCP toolsets must be configured there. Preserve other platform entries.
Charlotte's repository records the server integration in
`runtime/hermes/walkietalk.md`, linked from its runtime README.

For another installation, enable the supported API in the intended Charlotte
profile, configure its authentication and toolsets, and use an appropriate local
URL. For Docker, publish only to host loopback if choosing published ports, such
as `127.0.0.1:8642:8642`; a container-side loopback bind cannot receive traffic
from the host. Keep remote connections behind HTTPS or a trusted tunnel.
The client URL is the API base URL, **without a `/v1` suffix**. Private Docker
addresses can change when the container is recreated; rediscover the address
instead of assuming the example loopback URL reaches a container.

To undo the server integration, disable `API_SERVER_ENABLED`, remove the API
toolset entry if it was added solely for walkietalk, and coordinate a restart of
the existing Charlotte gateway. Remove the unused local API token when no client
needs it. The initial private profile backup is
`~/.hermes-charlotte/walkietalk-api-backup-b_4sr7kj/`; compare before restoring so
later profile edits are preserved. A gateway restart briefly interrupts chat
integrations; do not start a second gateway against the same live profile.
Selecting `agent.backend: stub` changes only walkietalk and needs no Charlotte
restart. No further runtime changes are required to use the already-tested setup.

All fields in the `agent` section are required, including for the stub:

```yaml
agent:
  backend: "hermes"
  max_reply_chars: 600
  history_turns: 8
  timeout_seconds: 60
  hermes_url: "http://127.0.0.1:8642" # use your actual local API base URL
  hermes_token_env: "WALKIETALK_HERMES_TOKEN" # variable name, never the secret
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
```

`timeout_seconds` is >0–300 seconds; timeout cleanup may take up to two extra
seconds. Stop is cooperative on the Hermes server. A lost submission response
can leave server work without a known run ID; the bridge reports that case and
does not retry automatically or claim cancellation. A failed stop is also
reported locally. Late replies are discarded. Raw server errors are withheld.

### Exact family demonstration

Run from the project root as Brad. Keep the gateway TX light visible for every
radio step. No spoken answers are expected yet.

1. **Supply the local token through the private [credentials file or environment](CONFIGURATION.md#credentials), then ask Charlotte a typed question.**

   ```sh
   .venv/bin/walkietalk -c config.local.yaml agent-check "What is rain? Answer in one short sentence."
   ```

   Expect `Agent: hermes (Charlotte environment; text only)` and a short actual
   explanation, prefixed `Reply:`. The coding assistant observed: “Rain is water
   that falls from clouds when tiny droplets get too heavy to stay up.” Wording
   can differ. This command opens no STT, audio, or PTT.

2. **Ask from the handheld.**

   ```sh
   .venv/bin/walkietalk devices
   .venv/bin/walkietalk -c config.local.yaml talk --capture --once
   ```

   Wait for `Waiting for someone to talk`, then say the configured wake phrase
   plus “What is rain?” Expect the transcript, accepted traffic with the wake
   phrase removed, then a short real `Reply:`. The command exits successfully.
   The gateway TX light must stay off.

3. **Ask a follow-up in conversation mode.** The existing local configuration
   already selects conversation mode with a 10-second window.

   ```sh
   .venv/bin/walkietalk -c config.local.yaml talk --capture
   ```

   Name + “What is rain?” → a short answer and `State: awake`. Within the window
   after the answer prints, ask “Why does that happen?” without the name. Expect
   `Accepted (follow-up)` and an answer about the same topic. In the coding
   assistant's live text check, Charlotte explained that droplets join and
   gravity pulls them down. Let the window expire; unaddressed speech must be
   ignored. Name + traffic is accepted again. Ctrl+C ends capture with exit 130.
   TX stays off throughout. These radio steps verify timing and recognition;
   a text-only context check cannot establish those observations.

4. **Show controlled failures without changing real credentials.**

   ```sh
   .venv/bin/python scripts/demo_agent_failures.py
   ```

   This is explicitly a simulated service, with no network, capture, or PTT.
   It exercises the actual CLI and Hermes adapter. Expect three local errors:
   reply exceeds 600 characters; timeout after 0.2 seconds (the fake service
   receives a stop request); authentication denied. Each ends with
   `Expected error received; no answer printed and no PTT opened.` The script
   exits 0 when all three expected failures are verified; each underlying CLI
   invocation exits 1. No `Reply:` line, diagnostic body, or token should appear.
   Observe that the gateway TX light remains off. This is controlled failure
   evidence, not an outage test of your real account.

**Explain:** “The pretend helper always said the same thing. Now our bridge
hands the words to Charlotte, and she can answer and remember what we just
asked. Her answer is still on the screen; giving it a radio voice comes later.”

Report the radio answer, follow-up, failure-demo results, and TX-off observation.
Brad has confirmed this checkpoint. The [Codex checkpoint](CODEX.md) now includes
the second real-backend comparison; Brad subsequently accepted Codex. The Grok guide includes another backend comparison.

If the API becomes unreachable after recreating the container, rediscover its
private address with `docker inspect <Charlotte-container> --format
'{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'` and update only
`agent.hermes_url`. Do not switch providers or use another account as a fallback.
The original checkpoint sourced an optional `.env.hermes.local` in each terminal.
Phase 8 also supports a private `credentials.env` beside the selected config, or
an explicit `--env-file`; see [credentials](CONFIGURATION.md#credentials).
The variable name comes from `agent.hermes_token_env`. Hermes holds the matching
server token as `API_SERVER_KEY` in its own environment; storing it there alone
does not export it to the separate Walkietalk process. Keep token values out of
YAML, and do not load Hermes's whole provider-credential environment into Walkietalk.

The agent deadline is already configurable: change the existing
`agent.timeout_seconds` in `config.local.yaml` from 60 to, for example, 120,
then restart walkietalk. No Charlotte restart is needed. Allowed values are
finite numbers greater than zero and at most 300. This gives Charlotte more
time to think; the separate `agent.max_reply_chars` still limits answer length.

## Revisit the stub checkpoint

Run these commands from the walkietalk directory as your normal user.
Select only `agent.backend: stub` to revisit this checkpoint; keep the remaining
fields. Restore `hermes` afterward to run the current demonstration.

1. **Try the pretend helper without radio or STT.**

   ```sh
   .venv/bin/walkietalk agent-check "What is rain?"
   ```

   Expect:

   ```text
   Text-only agent check: no STT, audio, or PTT.
   Agent: stub (offline pretend answer)
   Reply: This is a pretend answer. The radio bridge brought me your words.
   ```

   This is a fixed answer, not an explanation of rain. It works offline.

2. **Check the existing configuration.** Preserve all local settings and add
   only the new section if it is absent (already added on Brad's working copy):

   ```yaml
   agent:
     backend: "stub"
     max_reply_chars: 600
     history_turns: 8
     timeout_seconds: 60
     hermes_url: "http://127.0.0.1:8642" # preserve your existing local URL
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
   ```

   ```sh
   .venv/bin/walkietalk devices
   .venv/bin/walkietalk -c config.local.yaml check
   .venv/bin/walkietalk -c config.local.yaml agent-check "What is rain?"
   ```

   Expect the exact pinned AIOC names in `devices`/`check`, your existing STT and
   wake settings, `Agent: stub (offline pretend answer)`, and the same fixed
   answer from `agent-check`. Rediscover changed card numbers; never substitute
   the system default. `check` also checks serial permissions; receive-only
   `talk --capture` does not require serial access. The local Grok STT selection
   still needs its existing account and network even though the stub does not.

3. **Send traffic on a handheld.** Keep the gateway TX light visible.

   ```sh
   .venv/bin/walkietalk -c config.local.yaml talk --capture --once
   ```

   Wait for `Waiting for someone to talk`, then say your configured wake phrase
   followed by “What is rain?” Expect the transcript, `Accepted (wake name)`,
   `Traffic: What is rain?` (wake phrase removed), and the fixed `Reply:` above.
   The command exits successfully. The gateway TX light must remain off.

4. **Check the conversation path with the pretend helper.** If conversation
   mode is already configured, keep its existing timeout. Otherwise select
   `listening.mode: conversation` with a convenient window such as 10 seconds.

   ```sh
   .venv/bin/walkietalk -c config.local.yaml talk --capture
   ```

   Name + traffic gives the fixed answer and `State: awake`. Before the window
   ends, say “Why does that happen?” Expect `Accepted (follow-up)` and the same
   fixed answer. Let the window expire; expect `Follow-up window ended` and
   ordinary unaddressed speech to be ignored. Say the name again to restart.
   Ctrl+C ends capture, with exit status 130. TX stays off throughout.

   The stub does **not** understand the follow-up. Automated tests inspect the
   context passed through the interface; meaningful real-agent recall must be
   demonstrated later and remains unchecked above.

5. **Prove the reply cap without altering local settings.** This temporary
   config keeps the same hardware, STT, and wake settings but lowers the reply
   limit to 10 characters. Run the entire block, then say wake + traffic when
   prompted to talk:

   ```sh
   .venv/bin/python - <<'PY'
   import subprocess
   import tempfile
   from pathlib import Path
   import yaml

   data = yaml.safe_load(Path("config.local.yaml").read_text())
   data["agent"]["max_reply_chars"] = 10
   with tempfile.TemporaryDirectory(prefix="walkietalk-cap-") as directory:
       path = Path(directory) / "config.yaml"
       path.write_text(yaml.safe_dump(data))
       result = subprocess.run([
           ".venv/bin/walkietalk", "-c", str(path),
           "talk", "--capture", "--once",
       ])
       print(f"Exit status: {result.returncode} (expected 1)")
   PY
   ```

   Expect `Error: Agent reply exceeds agent.max_reply_chars (10); reply discarded`,
   no `Reply:` line, exit status 1, and gateway TX light off. Other errors do not
   count as this demonstration passing. The temporary file is cleaned up and
   `config.local.yaml` is unchanged.

**Explain:** “First we used a pretend answer to test the connection. The radio
bridge sends our words to a helper and gets words back. Next we will connect a
real helper.” After the real adapters work: “Now different helpers can answer
through the same radio bridge.”

Brad reports showing the stub to the girls. Passing that checkpoint does not
complete phase 5 or authorize publishing.

## Automated checks

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
```

Tests use fake STT/audio/agents and never transmit. The complete family checklist
above remains separate from these automated results.

Current stub checkpoint results (coding assistant):

- All 152 tests passed, including the existing phase 1–4 checks.
- Ruff lint and format checks passed; `git diff --check` passed.
- `agent-check` returned the fixed answer with defaults and the local config.
- Host device enumeration confirmed the pinned AIOC audio name and stable
  serial path. The restricted sandbox itself could not see those devices.
- No live capture or TX observation was performed by the coding assistant.
- Brad subsequently reported showing the stub to the girls; detailed individual
  cap-test and TX observations were not separately reported.

Current Hermes checkpoint results (coding assistant):

- All 190 tests passed, including the existing phase 1–4 safeguards, transport
  deadlines, Ctrl+C/SIGTERM stop requests, authentication and malformed-output
  failures, reply limits, and history behavior.
- Ruff lint, format check, and `git diff --check` passed.
- The controlled failure demonstration script produced all three expected
  local errors; no network, audio, STT, or PTT was used.
- The actual Charlotte API reported healthy, version 0.19.0. A real question
  about rain and a context-dependent follow-up both returned short final text
  through the adapter in the same dedicated conversation.
- No Hermes live radio demonstration was performed by the coding assistant.
  Brad subsequently confirmed the demonstration works perfectly and reported
  timeout errors on slower questions. Individual failure cases and TX-light
  details were not separately itemized in his report.
- Codex and Grok Build have since passed their live text checks. Brad accepted
  Codex; see the [current Grok checkpoint](GROK.md). Both Claude routes have since been accepted; see [its current checkpoint](CLAUDE.md).
- Publication was held until the full phase demonstration; Brad has since authorized commit, push, and merge.

Current Codex checkpoint results (coding assistant):

- All 230 tests passed; Ruff lint, format checking, and `git diff --check` passed.
- Real two-turn Codex text conversation verified using saved ChatGPT login;
  the explicit native session ID stayed the same for the follow-up.
- Controlled oversized-output, timeout, login, and missing-CLI demonstrations
  passed without hardware or account changes.
- Brad subsequently confirmed the Codex demonstration works marvelously.
  Individual failure and TX-light results were not separately itemized.
- Brad subsequently confirmed the Grok Build family demo; Brad has also confirmed both Claude routes work as expected.
  Brad has since authorized phase 5 publication and merge.

Current Grok Build checkpoint results (coding assistant):

- All 273 tests passed; Ruff lint, format checking, and `git diff --check` passed.
- Grok Build 1.0.34 / grok-4.6 returned a real rain answer and follow-up in the
  same dedicated session with the saved login.
- Controlled oversized-answer, timeout, login, and missing-CLI demonstrations
  passed with no account, network, STT, or hardware use.
- Brad confirms the [Grok family demonstration](GROK.md) and follow-up checklist; Brad has also confirmed both Claude routes work as expected. Brad has since authorized phase 5 commit, publication, and merge.

Reasoning configuration checkpoint:

- Brad selected `low` for Codex and Grok radio answers. Their YAML settings are
  explicit and displayed in the agent label. Restart the listener to apply them.
- All 303 tests, Ruff lint, formatting, and `git diff --check` passed. Real
  `gpt-5.6-luna` and `grok-4.6` replies were verified with the low override.
- The running Hermes Runs handler and agent factory were inspected. They do not
  support a request-level reasoning override; Charlotte's medium profile setting
  remains in effect. No Hermes code, profile, or restart was required.
- To repeat the Grok radio demo at low reasoning, use the same `talk --capture`
  commands in [the complete guide](GROK.md). Expect `reasoning=low` in the agent
  label, a short text answer, contextual follow-up, and TX off. Hardware
  observations at this setting remain separate from the live text checks.
