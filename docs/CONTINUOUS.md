# Continuous listening and remote shutdown

This is a user-requested addition within phase 5. Automated checks pass.
Brad confirms he tested everything, including the five-item follow-up checklist
and the updated shutdown behavior. Both combined and separate transmissions
are accepted. These are Brad's observations, separate from automated results.
Receive-only `talk --capture` keeps the gateway TX light off. Spoken shutdown
confirmation requires `talk --capture --transmit`.

## What the timers mean

| Setting | Purpose |
| --- | --- |
| Continuous `talk --capture` | No idle limit; silence does not end the program. |
| `listening.conversation_timeout_seconds` | After the reply prints, how long a follow-up may omit the wake phrase. Expiry leaves the bridge listening. |
| `vad.max_utterance_seconds` | Maximum recording length for one utterance; quiet waiting does not accumulate an unbounded recording. |
| `stt.timeout_seconds` | Deadline for transcribing captured speech. |
| `agent.timeout_seconds` | Deadline for producing an answer, independent of answer length and reasoning effort. |
| `shutdown.confirmation_seconds` | Time to start the separate confirmation utterance after shutdown is armed. Default 30 seconds; configurable above zero through 300. |
| `shutdown.arm_confirmation_phrase` | Spoken radio line when the phrase alone arms shutdown. Required when shutdown is enabled. Played only with `talk --transmit`. |
| `shutdown.confirmation_phrase` | Spoken radio line after the code is accepted, including a phrase and code in one transmission. Required when shutdown is enabled. Played only with `talk --transmit`. |
| `wake.confirmation_phrase` | Spoken radio line when the wake phrase arrives with no traffic. Empty stays silent. Played only with `talk --transmit`. |
| `talk --capture --once --timeout 5` | One-shot diagnostic: wait up to five seconds for speech. Default 60; maximum 300. |

Continuous mode rejects an explicit `--timeout` with a usage error explaining
that it belongs to `--once`. `listen` keeps its existing bounded wait.

## Setup

Keep the existing devices, DTR PTT settings, wake phrases, STT, and agent fields.
Add the required top-level section from `config.example.yaml` if absent. It is
disabled in the public example; Brad's ignored local config contains his chosen
phrases and enables it. This example shows the shape with illustrative phrases:

```yaml
shutdown:
  enabled: true
  phrase: "stop listening"
  phrase_aliases: []
  code: "confirm alpha nine"
  code_aliases: ["confirm alpha 9"]
  confirmation_seconds: 30
  arm_confirmation_phrase: "Shutdown armed."
  confirmation_phrase: "Walkietalk shutting down."
```

The arming phrase, code, both confirmation lines, and the wake phrase must differ.
`wake.confirmation_phrase` is the separate line for a wake phrase with no traffic.
Matching uses complete words, ignoring case, whitespace, and punctuation.
Use explicit aliases for alternate STT spellings; there is no fuzzy matching.
Controls work with or without the ordinary wake phrase. Restart after editing.
Like the wake phrase, the shutdown phrase can be followed by its traffic in the
same utterance: “stop listening, confirm alpha nine” shuts down immediately.
Alternatively, “stop listening” alone arms shutdown and speaks
`arm_confirmation_phrase`, then “confirm alpha nine” within the confirmation
window completes it and speaks `confirmation_phrase`. The combined form does not
require prior arming and speaks only `confirmation_phrase`; a previous expired
window does not prevent a new complete command. Both lines require
`talk --capture --transmit`. Receive-only mode prints the status and exits or
keeps listening without keying. A failed phrase acknowledgement leaves shutdown
armed. A failed code confirmation still stops walkietalk after PTT is released.

## Exact family demonstration checklist

From the project directory, as the normal user:

```sh
.venv/bin/walkietalk devices
.venv/bin/walkietalk -c config.local.yaml check
.venv/bin/walkietalk -c config.local.yaml talk --capture
```

If the AIOC card number changed, update only the device fields to the exact
listed names. The application never substitutes the default microphone.
If using Hermes, supply the local service token through the private
[credentials file or environment](CONFIGURATION.md#credentials).

- [x] **Long silence:** wait at least two minutes without speaking. The process
  keeps waiting. Say your wake phrase and ask “Why does it rain?” Expect a short
  printed answer, followed by listening again.
- [x] **Follow-up and expiry:** within the configured conversation window (ten
  seconds in Brad's current config), ask “Why does that happen?” without the
  name. Expect an answer using prior context. After the next window expires,
  an unaddressed question is ignored. Repeat with the wake phrase; it works.
  The process remains running throughout.
- [x] **Code alone:** send your configured confirmation code without first
  arming. Expect `Shutdown code ignored: shutdown is not armed.` and continued
  listening. No agent answer is requested.
- [x] **Combined command:** say the arming phrase and code in one utterance.
  Expect `Shutdown confirmed; stopping walkietalk.` and the shell prompt.
  Run `echo $?` immediately afterward; expect `0`. Restart with the same
  `talk --capture` command before continuing the remaining checks.
- [x] **Incorrect combined command:** say the arming phrase followed by a wrong
  or incomplete code. Expect a local rejection and continued listening. Extra
  words after the correct code also cause rejection; use the exact phrase/code.
- [x] **Wrong code:** send the arming phrase, release PTT, wait for capture to
  resume, then send a wrong code. Expect `Shutdown cancelled` and continued
  listening. The wrong code is not forwarded to the agent. Arm again to retry.
- [x] **Expired code:** arm again, wait longer than 30 seconds (or your chosen
  confirmation window), then send the code. Expect expiry and the unarmed-code
  message; the process keeps running.
- [x] **Shutdown in two transmissions:** Brad confirms it works with the configured pair.
  To repeat: send the arming phrase. Expect `Shutdown armed`.
  Release PTT and wait for the computer to show it is listening again. Send the
  confirmation code as a separate utterance within the window. Expect
  `Shutdown confirmed; stopping walkietalk.` and the shell prompt. Then run:

  ```sh
  echo $?
  ```

  Expected: `0` (normal exit). Nothing is erased; the computer stays on.
- [x] **Keyboard stop:** start the same continuous command again, then press
  Ctrl+C while waiting. Capture closes and the process exits (status 130).
- [x] **One-shot wait remains available:** run the following and remain quiet:

  ```sh
  .venv/bin/walkietalk -c config.local.yaml talk --capture --once --timeout 5
  echo $?
  ```

  Expected: no-speech error after about five seconds, status `1`. This finite
  diagnostic is separate from everyday continuous listening.
- [x] **Gateway TX light stayed off** during the receive-only steps above.
- [x] **On-air confirmation:** restart with `talk --capture --transmit`. Say the
      phrase and code together. Expect the configured `confirmation_phrase` on
      the walkie, then TX off and a normal exit. A failed spoken confirmation
      still stops the program after PTT is released. Brad confirms the spoken
      confirmation works as expected.
- [x] **Separate acknowledgements:** with `talk --capture --transmit`, a wake
      phrase alone speaks `wake.confirmation_phrase`. The shutdown phrase alone
      speaks `arm_confirmation_phrase` and stays armed. The code then speaks
      `confirmation_phrase` and exits. Phrase and code in one transmission speak
      only `confirmation_phrase`. Brad confirms this works as expected.

Explain: “The bridge stays ready while we're outside. After a pause, we say its
name again. Our two-part shutdown command tells the program to close safely.”

## Failure recovery, verified separately

Run the controlled offline demonstration:

```sh
.venv/bin/python scripts/demo_continuous.py
```

Expected: simulated long silence, agent timeout, transcription timeout,
incorrect and expired confirmation, then successful shutdown and `PASS`.
The script uses the real command loop with fake capture, STT, agent, and clock.
It does not access accounts, audio devices, or PTT; simulated hours pass instantly.
This verifies software behavior, not actual microphone or radio observation.

In continuous mode an agent timeout, expired login, or oversized answer prints
a local error and returns to listening. It does not automatically retry or
switch providers. The next ordinary request requires the wake phrase. Completed
conversation pairs remain available, but a fresh backend session excludes any
partly completed failed turn. Transcription failures also resume listening and
cancel pending shutdown. Hardware capture failures stop with a clear local
error so a disconnected device is not hidden. One-shot commands and
`agent-check` continue to exit with status 1 on failure.

## Current limits

Capture pauses while transcribing or waiting for an agent. Send shutdown
commands when listening resumes; this is not an interrupt for an answer already
in progress. Agent calls still have a configurable deadline. Silence alone
never ends continuous mode, but an actual device failure can.

The controls rely on successful speech recognition. If remote STT is unavailable,
use Ctrl+C locally. Control audio goes to the selected STT backend, but recognized
controls and pending confirmations are consumed before transcript logging and
before the AI agent. The code is audible to other radio listeners, so this is
a convenience control rather than private authentication. An empty or wrong
next utterance cancels arming; repeating the arming phrase restarts its window.
The deadline tests when confirmation speech starts, allowing STT to finish later.

There is no spoken acknowledgement yet. Phase 6 will add voice separately.
