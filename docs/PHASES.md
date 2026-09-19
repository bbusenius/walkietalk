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
continuously holding PTT. These settings are not accepted by the phase 1 config.

## Review and return to a checkpoint

1. Implement and test the phase locally without committing or pushing it.
2. Brad runs the code and explains to the girls what it does and why. Record
   automated results and live observations separately.
3. Only after that demonstration and explanation, commit the phase and open its
   pull request with the tests and demonstration results.
4. After review, merge and tag the accepted commit (for example, `phase-1`).
   Start the next phase from that checkpoint and preserve its tag.

The initial phase 1 PTT checkpoint uses `phase-1-ptt`; the full `phase-1` tag
waits for the remaining live demonstrations. Inspect the initial checkpoint
in a separate directory without changing current work:

```sh
git fetch origin --tags
git worktree add --detach ../walkietalk-phase-1-ptt phase-1-ptt
```

A checkpoint restores source code. Recreate that version's environment and config
as documented; tags do not restore firmware, Linux permissions, or account sessions.

| Checkpoint | Branch / PR | State |
| --- | --- | --- |
| Baseline | `8ba1ee1` | Empty source baseline before phase 1; hardware receive already proved separately. |
| Phase 1: PTT checkpoint | `phase-1-ptt-playback`; snapshot `phase-1-ptt` | Brad and the girls ran the Python PTT pulse, observed the TX light turn on/off, and explained its meaning. Brad authorized committing and merging this checkpoint. Live WAV playback and interruption demos remain pending before phase 2. |
