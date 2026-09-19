# Phase checkpoints

Each phase gets its own branch and pull request. At the end, explain what changed
and why, run automated checks, and demonstrate the result with the family.
Do not begin the next phase until the current demonstration passes.

| Phase | Work | Verification |
| --- | --- | --- |
| 0 | Hardware receive proof (already completed outside this repository) | Walkie speech moves the computer's capture meter. |
| 1 | Python PTT and WAV playback | Talk light on/off, speech heard on walkie, interruption releases PTT; automated cleanup tests. |
| 2 | Local speech recognition and simple energy-based utterance detection | Spoken radio question appears correctly as text. |
| 3 | Wake name, explicit STT aliases, optional conversation timeout, then pluggable STT | Wake prefix required by default; conversation mode accepts follow-ups until configurable timeout; switch STT in config without changing radio or wake code. |
| 4 | Interchangeable agents: stub, Charlotte/Hermes, Codex, Grok Build, Claude Code | Verify connections and text replies one adapter at a time, in that order; failure leaves transmitter off. |
| 5 | Interchangeable speech generation, initially Piper | Hear an agent's answer on the walkie. |
| 6 | Strengthen watchdogs, post-transmit mute, callsign handling | Demonstrate timeout, mute, and identification behavior. |
| 7 | Complete installation and user documentation | Follow setup from a clean environment; verify supported integrations. |

The future agent order places Grok before Claude Code. CLI adapters reuse their
own normal account login; walkietalk does not implement provider OAuth. Hermes
integration uses its supported API. Availability and installed interfaces must
be verified during phase 4.

Wake settings planned for phase 3 include `primary`, explicit `aliases` for STT
mistakes, and a `conversation_timeout_seconds` setting (initially 60 seconds).
Default mode requires a wake name each time. Conversation mode requires the name
once, restarts the follow-up window after each successful answer finishes, and
requires the name again after silence exceeds the timeout. It never means
continuously holding PTT.

After that wake work, phase 3 finishes with a first-class `SttBackend` plug
(`transcribe(audio) -> text`). Default remains local faster-whisper (`tiny` or
`base`). The first extra backend is Grok STT: prefer SuperGrok Plus / `grok login`
under the same Linux user; `XAI_API_KEY` is an explicit optional API adapter
only. Do not silently switch from account login to API billing. Capture, energy
detection, and wake matching stay in walkietalk. These settings are not accepted
until phase 3.

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
| Phase 2: STT listen | `phase-2-stt-listen` | Brad and his daughter ran the live radio transcription. It worked as expected; automated checks passed. Commit and PR remain unauthorized until Brad says to publish. |

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
