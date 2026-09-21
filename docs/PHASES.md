# Phase checkpoints

Each phase gets its own branch and pull request. At the end, explain what changed
and why, run automated checks, and demonstrate the result with the family.
Do not begin the next phase until the current demonstration passes.

| Phase | Work | Verification |
| --- | --- | --- |
| 0 | Hardware receive proof (already completed outside this repository) | Walkie speech moves the computer's capture meter. |
| 1 | Python PTT and WAV playback | Talk light on/off, speech heard on walkie, interruption releases PTT; automated cleanup tests. |
| 2 | Local speech recognition and simple energy-based utterance detection | Spoken radio question appears correctly as text. |
| 3 | Wake name, explicit STT aliases, optional conversation timeout | Wake prefix required by default; conversation mode accepts follow-ups until configurable timeout. |
| 4 | Pluggable STT: faster-whisper default, then Grok STT | Switch listeners in config without changing radio or wake code. SuperGrok / `grok login` preferred; `XAI_API_KEY` optional and explicit. |
| 5 | Interchangeable agents: stub, Charlotte/Hermes, Codex, Grok Build, Claude Code CLI, Claude Messages API | Verify connections and text replies one adapter at a time, in that order; failure leaves transmitter off. |
| 6 | Interchangeable speech generation, initially Piper | Hear an agent's answer on the walkie. |
| 7 | Strengthen watchdogs, post-transmit mute, callsign handling | Demonstrate timeout, mute, and identification behavior. |
| 8 | Complete installation and user documentation | Follow setup from a clean environment; verify supported integrations. |

Phase 7 is merged in [PR #8](https://github.com/bbusenius/walkietalk/pull/8),
merge commit `416b6e4`. The Hermes speech follow-up is demonstrated and merged
in [PR #9](https://github.com/bbusenius/walkietalk/pull/9), merge commit `976b592`.
Its automated results and family observations are recorded separately in the
[Hermes checkpoint](HERMES-TTS.md).

Phase 8 packages private setup files, installation checks, and public
installation, configuration, and backend guides on `phase-8-installation`.
Brad reviewed the [installation instructions](PHASE8-DEMO.md) and authorized
publication. Optional autostart and phase 9 remain deferred.

Phase 5 is demonstrated and merged in [PR #5](https://github.com/bbusenius/walkietalk/pull/5),
merge commit `ebdb8e7`. Phase 6 family demonstrations of Piper (Amy) and Grok
speech are complete. Brad showed the girls and authorized publication. Phase 6
is merged in [PR #6](https://github.com/bbusenius/walkietalk/pull/6), merge
commit `ab37520`. Spoken shutdown confirmation and optional `tts.normalize: peak`
are follow-ups on main; Brad confirms both. Phase 7 family demonstration is
accepted: mute, cutoff, timeout, wake_phrase, conversation mode, and clean
unkey. Brad authorized publication. See the
[complete Amy setup and demonstration checklist](PHASE6-DEMO.md).
See the [complete acceptance checklist and current demo](PHASE5-DEMO.md).
The stub, Hermes, Codex, and Grok Build adapters are implemented. Brad showed the stub to the girls;
the coding assistant verified real Hermes text replies and follow-up context.
Brad has confirmed the Hermes, Codex, and Grok Build family demonstrations,
including the [Grok checkpoint](GROK.md) and five-item follow-up checklist.
Brad subsequently requested both Claude routes: `claude` for official CLI
saved login and `claude_api` for separately billed Messages API access. Both are
implemented with automated coverage. A real Claude CLI question and follow-up
passed; Brad now confirms both Claude routes work as expected. See
[Claude setup and exact demonstrations](CLAUDE.md).
The [STT capability review](STT-CAPABILITIES.md) is complete: Brad declined a
new Hermes transcription service in favor of the existing local Whisper option;
no supported standalone Codex transcription interface was found. Neither is
advertised as an STT backend, and no billed API substitute was added.

Brad requested two additions within phase 5: continuous listening through
indefinite silence, and configurable phrase-and-code remote program shutdown. Both
are implemented with automated coverage. Brad confirmed the
[family demonstration checks](CONTINUOUS.md), including both shutdown forms.
Agent/STT failures return to listening in continuous mode; hardware
capture failures still stop with a clear error. This adds no TTS or transmission.

The agent order continues after Grok Build with Claude Code, then Claude API.
CLI adapters reuse their
own normal account login; walkietalk does not implement provider OAuth. Hermes
integration uses its supported API. Availability and installed interfaces must
be verified during phase 5.

Wake settings implemented in phase 3 include `primary`, explicit `aliases` for STT
mistakes, and a `conversation_timeout_seconds` setting (initially 60 seconds).
Default mode requires a wake name each time. Conversation mode requires the name
once, restarts the follow-up window after each successful answer finishes, and
requires the name again after silence exceeds the timeout. It never means
continuously holding PTT.

Phase 4 adds a first-class `SttBackend` plug (`transcribe(audio) -> text`).
Default remains local faster-whisper (`tiny` or `base`). The first extra
backend is Grok Voice Transcribe (`POST /v1/stt`) using the SuperGrok Plus
session from `grok login` (`~/.grok/auth.json`). `XAI_API_KEY` is an explicit
optional `grok_api` adapter only. Do not silently switch from account login to
API billing. Capture, energy detection, and wake matching stay in walkietalk.

## Review and return to a checkpoint

1. Implement and test the phase locally without committing or pushing it.
2. Brad runs the code and explains to the girls what it does and why. Record
   automated results and live observations separately.
3. Only after that demonstration and explanation, commit the phase and open its
   pull request with the tests and demonstration results.
4. After review, merge and tag the accepted commit (for example, `phase-1`).
   Start the next phase from that checkpoint and preserve its tag.

The initial phase 1 PTT checkpoint uses `phase-1-ptt`. The remaining live
demonstrations have now passed on the same implementation. The full `phase-1`
tag has not yet been published. Inspect the initial checkpoint in a separate
directory without changing current work:

```sh
git fetch origin --tags
git worktree add --detach ../walkietalk-phase-1-ptt phase-1-ptt
```

A checkpoint restores source code. Recreate that version's environment and config
as documented; tags do not restore firmware, Linux permissions, or account sessions.

| Checkpoint | Branch / PR | State |
| --- | --- | --- |
| Baseline | `8ba1ee1` | Empty source baseline before phase 1; hardware receive already proved separately. |
| Phase 1: PTT checkpoint | `phase-1-ptt-playback`; snapshot `phase-1-ptt` | Brad and the girls ran the Python PTT pulse, observed the TX light turn on/off, and explained its meaning. Brad authorized committing and merging this checkpoint. Brad subsequently confirmed that both live WAV playback and Ctrl+C release passed on this implementation. All phase 1 verification is complete. |
| Phase 2: STT listen | `phase-2-stt-listen`; [PR #2](https://github.com/bbusenius/walkietalk/pull/2) | Brad and his daughter ran the live radio transcription. It worked as expected; automated checks and CI passed. Merged to `main`. |
| Phase 3: wake gate | `phase-3-wake-gate`; [PR #3](https://github.com/bbusenius/walkietalk/pull/3) | Brad demoed wake phrase and conversation mode with the girls. Merged to `main`. |
| Phase 4: STT plug | `phase-4-stt-plug`; [PR #4](https://github.com/bbusenius/walkietalk/pull/4) | Brad verified faster-whisper and SuperGrok Grok Voice Transcribe on the radio. `grok_api` live key test skipped. Merged to `main`. |
| Phase 5: agent plug | `phase-5-agent-plug`; [PR #5](https://github.com/bbusenius/walkietalk/pull/5) | Stub shown to the girls. Hermes text connection verified and family demonstration confirmed by Brad. Codex demonstration confirmed. Grok Build radio demonstration, backend comparison, failures, continuous listening, and shutdown confirmed by Brad. Claude CLI text check passed; Brad confirms both Claude routes work as expected. Hermes STT service deferred by Brad; standalone Codex STT interface not verified. Brad authorized committing, publishing, and merging phase 5. |
| Phase 6: TTS plug | `phase-6-piper-voice`; [PR #6](https://github.com/bbusenius/walkietalk/pull/6) | Brad confirmed Piper (Amy) and Grok spoken replies with the girls. Optional `grok_api` live key test skipped. Automated checks passed. Merged to `main` as `ab37520`. |
| Phase 7: watchdogs | `phase-7-watchdogs` | Brad confirmed mute, overlong cutoff, conversation timeout, wake_phrase, and clean unkey. Optional spoken ID stays off unless the operator sets a callsign. Automated checks passed. Brad authorized committing, publishing, and merging phase 7. |
| Hermes speech follow-up | `hermes-tts`; [PR #9](https://github.com/bbusenius/walkietalk/pull/9) | Brad confirmed spoken replies through the configured Hermes environment; merged as `976b592`. |
| Phase 8: installation | `phase-8-installation` | Brad reviewed the installation instructions and authorized publication. Automated installation checks passed. Optional autostart remains deferred. |

## Phase 1 verification results

- [x] Python one-second PTT pulse: gateway TX light on, then off.
- [x] Spoken WAV heard on receiving walkie; gateway released PTT afterward.
- [x] Ctrl+C during transmission released PTT immediately.
- [x] Family explanation of talk control and purpose completed.
- [x] All 36 automated tests passed; lint, formatting, build, and CI passed.

The live observations were reported by Brad. Phase 1 documentation on this
branch preserves that completion record.

## Phase 2 verification results

The live observations were reported by Brad after the family demonstration.

- [x] `walkietalk models` downloaded faster-whisper base locally (148 MB).
- [x] File `listen` on `recordings/phase1-front-center.wav` printed `Transcript: center.`
- [x] Handheld walkie sentence appeared as text; capture ended after silence.
- [x] Gateway TX light stayed off during listen (receive-only).
- [x] Ctrl+C during wait closed capture; TX light stayed off.
- [x] Family explanation: the computer writes down what we said so the AI can understand the question.
- [x] Automated tests (65 passed), lint, formatting, and package build passed on this branch.

## Phase 3 verification results

The live observations were reported by Brad after the family demonstration.

- [x] Default `wake_phrase` mode: no name ignored; name + traffic accepted; next
      traffic without the name ignored.
- [x] Conversation mode: follow-up without the name accepted inside the window;
      ignored after timeout; name required again.
- [x] Changing `conversation_timeout_seconds` changes the window; silence does
      not keep it awake.
- [x] Gateway TX light stayed off; Ctrl+C closed capture.
- [x] Family explanation: say the name, then your traffic; after a quiet pause,
      say the name again.
- [x] Automated tests (98 passed), lint, formatting, and package build passed on this branch.

## Phase 4 verification results

The live observations were reported by Brad after the family demonstration.

- [x] faster-whisper still transcribes live radio traffic.
- [x] `stt.backend: grok` uses SuperGrok Plus login for Grok Voice Transcribe;
      does not fall back to faster-whisper or `XAI_API_KEY`.
- [x] Optional `grok_api` live key test skipped; missing-key error is covered
      by automated tests.
- [x] Gateway TX light stayed off.
- [x] Family explanation: if one listener has trouble with a voice, we can plug
      in a different one.
- [x] Automated tests (111 passed), lint, and formatting passed on this branch.

## Phase 5 verification results — accepted and authorized for publication

- [x] Offline stub through `AgentBackend`; bridge reply cap and bounded context.
- [x] Automated tests (152 passed), lint, and formatting passed.
- [x] Text-only `agent-check` with default and local config returned the fixed reply.
- [x] Host enumeration found the configured AIOC audio device and serial path.
- [x] Brad reports showing the stub to the girls. Individual failure-demo results
      and detailed TX observations were not separately reported.
- [x] Hermes 0.19.0 API enabled in Charlotte's existing profile and workspace;
      real short answer and context-dependent follow-up returned as text.
- [x] Hermes checkpoint: 190 tests, lint, formatting, and controlled failure
      demonstration passed. Ctrl+C/SIGTERM request server cancellation.
- [x] Brad confirms the Charlotte/Hermes demonstration works perfectly; slower
      questions sometimes hit the configured 60-second deadline. Individual
      failure cases and TX-light details were not separately itemized.
- [x] Codex 0.155.1: real text answer and follow-up in the same dedicated session
      verified with saved ChatGPT login; controlled failure demonstration passed.
- [x] Brad confirms the Codex demonstration works marvelously; detailed failure
      and TX-light results were not separately itemized.
- [x] Grok Build 1.0.34 / grok-4.6: real question and follow-up verified in the
      same dedicated session using saved login; controlled failures passed.
- [x] Brad confirms the Grok Build family demonstration and backend comparison.
- [x] Continuous listening and remote shutdown: 365 automated tests, lint,
      formatting, and offline recovery/shutdown demonstration pass.
- [x] Brad confirmed successful two-step remote shutdown after correcting the
      arming phrase used in the demonstration.
- [x] At Brad's request the phrase and code also work in one utterance;
      automated checks cover both forms, and Brad confirms the radio demonstration.
- [x] Brad confirms long-silence, recovery, shutdown rejection, and TX-light observations.
- [x] Brad requested both Claude adapters after the earlier deferral; both now have
      automated coverage. Claude Code 2.1.277 passed a real two-turn text check.
- [x] Full suite: 439 tests passed, lint/format checks passed; nine controlled
      Claude failure demonstrations passed without account or hardware access.
- [x] Brad confirms both `claude` and `claude_api` work as expected. This is live
      user confirmation, separate from the automated failure coverage above;
      individual Claude failure/TX observations were not separately itemized.
- [x] Inspect Hermes/Codex STT feasibility. Hermes service deferred by Brad;
      no standalone Codex transcription contract verified, so no adapter shipped.
- [x] Phase implementation and demonstration checkpoints accepted.
- [x] Brad explicitly authorized committing, pushing, and merging phase 5.

See [the complete phase checklist and demonstration commands](PHASE5-DEMO.md).


## Phase 6 verification — accepted and authorized for publication

- [x] Thin local Piper `TtsBackend`; Amy chosen by Brad and downloaded explicitly.
- [x] Grok TTS adapter with saved subscription login and separate explicit API-key mode.
      [Grok setup and demonstration checkpoint](GROK-TTS.md): real saved-login
      synthesis produced a 3.430-second WAV; API live check pending.
- [x] Real local synthesis: Amy produced a 3.599-second, 48 kHz mono PCM16 WAV.
- [x] `tts-check` exports without hardware; `talk` stays text-only by default.
- [x] Spoken replies require explicit config and `--transmit`; finished audio is
      bounded before PTT opens, using the existing supervised playback worker.
- [x] All 549 automated tests, Ruff lint, formatting, and source/wheel build pass.
- [x] Automated coverage for speech failures, history recovery, follow-up timing,
      and PTT release on playback errors and interruption.
- [x] Five controlled voice failure demonstrations pass without hardware access.
- [x] Brad reports that Amy replies work well on the radio.
- [x] Brad confirms Piper and Grok speech work; he showed the girls. Individual
      gain/settle, follow-up-window, second-helper, controlled-failure, Ctrl+C,
      and shutdown radio observations were not separately itemized.
- [x] Optional `grok_api` live key test skipped; missing-key error is covered
      by automated tests.
- [x] Family explanation complete; Brad authorized phase 6 publication.

The [phase 6 checklist](PHASE6-DEMO.md) gives every command and expected result.
No live TTS inference or RF transmission runs in CI. Phase 7 family demonstration
is accepted. Brad authorized publication. Walkietalk does not invent a station ID.
