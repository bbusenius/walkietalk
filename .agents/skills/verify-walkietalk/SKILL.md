---
name: verify-walkietalk
description: "Drive walkietalk (CLI radio bridge) the way a user does — config-check, check, talk capture/transmit, and operator panel/CLI. Use when proving walkietalk behavior or after changing talk, operator, radio, or config surfaces."
---

# Verify walkietalk

Walkietalk is a Linux CLI (optional TUI operator panel) that bridges a walkie-talkie to AI agents and WhatsApp/Signal contacts. Primary surface: the `walkietalk` binary. Evidence is terminal transcripts (and a PTY/tmux capture for the panel). Never treat unit tests alone as proof of radio or operator UX.

## Launch

There is no long-lived server for most paths. Prepare the binary, then run each drive in its own shell (or a dedicated tmux/PTY session for `--panel`).

```bash
# From the repo root (preferred when verifying uncommitted work):
cargo build -p walkietalk --release
export PATH="$PWD/target/release:$PATH"
# Or use the installed binary:
# export PATH="$HOME/.cargo/bin:$PATH"
walkietalk --version
```

Ready when `walkietalk --version` prints a version line and exits 0.

Default settings: `~/.config/walkietalk/config.toml` (override with `-c FILE`). Credentials live beside the config; never print them.

Teardown for short commands is automatic (process exits). For continuous `talk --capture` / `--panel`, stop with Ctrl+C in that session only (or `tmux send-keys C-c` to the session you started). Confirm the process you started is gone; do not `pkill walkietalk`.

## Doctor

Read-only readiness. Prefer this before any drive that needs hardware or a live talk:

```bash
helpers/doctor.sh
# equivalent:
walkietalk config-check
walkietalk devices
walkietalk check   # never transmits; may FAIL if AIOC audio/serial missing
```

Interpret:

| Signal | Meaning |
| --- | --- |
| `config-check` exit 0, `Config OK:` | Settings parse; no hardware/network/login touched. Safe for config-check proofs. |
| `devices` lists `AllInOneCable` (or configured card) and AIOC `/dev/serial/by-id/...` | Radio interface present. |
| `check` all `ok` lines, exit 0, `Nothing was opened or transmitted.` | Devices + backends ready for talk. |
| `check` `FAIL` on capture/playback/PTT | Hardware down — skip transmit and live capture; still OK to prove `config-check`. |
| Operator: `[messaging] operator_mode = true` in config (via `config-check` summary `Operator mode:`) | Panel / CLI operator features are in scope. |

Refuse to drive a `talk` instance you did not start. Only one operator-mode `talk` runs per config.

## Drive

Harness: plain shell for one-shot commands; **tmux PTY** for continuous talk and the operator panel.

```bash
# One-shot (record evidence):
helpers/run.sh config-check -- walkietalk config-check

# Continuous talk (receive-only; never keys):
tmux new-session -d -s wt-verify-talk -- walkietalk talk --capture
tmux capture-pane -t wt-verify-talk -p -S -300

# Operator panel (requires operator_mode; keys radio if --transmit):
tmux new-session -d -s wt-verify-panel -x 100 -y 40 -- \
  walkietalk talk --capture --transmit --panel
```

Stable handles: subcommand names (`config-check`, `check`, `talk`, `operator`), flags (`--capture`, `--transmit`, `--panel`, `--once`), and stdout phrases (`Config OK:`, `Nothing was opened or transmitted.`, `Operator mode:`). Do not key the radio (`--transmit`, `ptt --transmit`, `play --transmit`) unless the feature under proof requires it **and** doctor shows AIOC audio + serial present.

Feature recipes live in [`features/`](features/README.md). Drive one mapped feature per prove unless asked for more.

## Evidence

Root (survives cleanup):

```text
.agents/skills/verify-walkietalk/artifacts/<feature-id>/<run-id>/
```

Capture for every proof:

- `cmd.txt` — exact command line
- `stdout.txt` / `stderr.txt` — full streams
- `exit.txt` — exit code
- For panel/talk: `pane.txt` from `tmux capture-pane` (before teardown) and optional `screenshot.png` if a GUI terminal is available

Proof standards:

- Exercise the real user path (`walkietalk …`), not internal test fakes.
- Capture the action and the resulting state (exit code + summarizing lines).
- For transmit features, observe that keying was intended only with `--transmit` and that doctor had hardware; never claim transmit proof from a simulated `ptt` without `--transmit`.
- Mocks only where the product already isolates (unit tests are not this skill).

## Cleanup

```bash
helpers/cleanup.sh <tmux-session-name-if-any>
```

Rules:

- Kill only sessions/processes **this run started** (named tmux session or recorded PID). Never `pkill -f walkietalk`.
- Remove scratch WAVs under `/tmp/walkietalk-verify-*` created by the run.
- **Never delete** `artifacts/` proof directories.
- After cleanup, confirm the evidence directory still exists and contains `exit.txt`.

## Helpers

All under `.agents/skills/verify-walkietalk/helpers/`; invoke from that directory or via absolute path.

| Script | Purpose |
| --- | --- |
| `doctor.sh` | Run `config-check`, `devices`, and `check`; print a one-line hardware verdict |
| `run.sh <feature-id> -- <command…>` | Create `artifacts/<feature-id>/<run-id>/`, run the command, save streams/exit |
| `cleanup.sh [tmux-session]` | End the named tmux session if present; leave artifacts alone |

Example:

```bash
cd .agents/skills/verify-walkietalk
./helpers/doctor.sh
./helpers/run.sh config-check -- walkietalk config-check
./helpers/cleanup.sh
ls artifacts/config-check/*/exit.txt
```

## Maintenance

When commands, flags, or operator UX change, update the matching file under `features/` (see `/maintain-verification-skill`).
