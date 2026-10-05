# Talk transmit

`walkietalk talk --capture --transmit` speaks agent/contact replies on the radio. For an **agent proving alone**, use the documented `play WAV --transmit` path (real keying, no second handheld). Simulated `ptt` / `play` without `--transmit` are never TX proof.

## Sub-features

- `talk-tx-agent-solo` — agent-driveable real keying via `walkietalk play WAV --transmit` (preferred for verification).
- `talk-tx-wav-once` — optional fuller reply cycle: `talk WAV --once --transmit` with a disposable `-c` that has `operator_mode = false` (needs STT/agent/TTS).
- `talk-tx-live` — continuous `talk --capture --transmit` with a human wake from a second radio (not agent-solo).
- `ptt-sim-vs-real` — `walkietalk ptt` is simulated unless `--transmit`; not a substitute for TX proof.

## How to get to it (user POV)

- Agent-solo: synthesize or supply a short mono WAV, then `walkietalk play that.wav --transmit`.
- Live talk: `walkietalk talk --capture --transmit`, hold a second handheld PTT, say the wake/SARNEG + question, release; hear the reply on the gateway radio.
- Optional once: `walkietalk talk utterance.wav --once --transmit` (refused when `operator_mode = true` in the active config).

## Driving it with walkietalk-cli

### Agent-solo TX (preferred — no second radio, no spoken wake)

Preconditions:

- `helpers/doctor.sh` reports `hardware=ready` (AIOC audio + serial; `check` exit 0).
- Authorized to key: only run with `--transmit` when proving TX on this station.
- No other process holds the PTT serial (do not start a second `talk` / `play` / `ptt` while one is open).
- Current config is fine — `play` does not care about `operator_mode`. Do **not** rewrite `~/.config/walkietalk/config.toml`.

Steps:

1. Prepare a short mono WAV within `radio` speech budget (max_tx_seconds − settle_seconds). Example (no network):

   ```bash
   ffmpeg -y -f lavfi -i "sine=frequency=800:duration=1.2" -ar 16000 -ac 1 \
     /tmp/walkietalk-verify-solo-tx.wav
   ```

   Or `walkietalk tts-check "test" --output /tmp/walkietalk-verify-solo-tx.wav` (needs voice backend).

2. Run via the harness (records evidence):

   ```bash
   helpers/run.sh talk-transmit -- walkietalk play /tmp/walkietalk-verify-solo-tx.wav --transmit
   ```

   Or use `helpers/solo-tx.sh` which builds the tone WAV and invokes `run.sh`.

3. **Proof (must all appear in stdout):**
   - `Transmitting with <config>, PTT on <serial-by-id>` — consent + armed port.
   - `WAV: … Hz mono, …s, gain …` — clip accepted for playback.
   - `Finished; transmitter released.` — key-down completed and released.
   - Exit code `0`.
   - Do **not** open the PTT serial from another process to “watch DTR”; that can steal the port or key the radio. Trust talk/play’s own status lines.

4. Cleanup: `helpers/cleanup.sh` (removes `/tmp/walkietalk-verify-*.wav`); confirm radio is quiet. Never `pkill walkietalk`.

### Optional: talk WAV once (reply cycle)

Use only when you need STT → agent → TTS → TX, not for the minimum solo keying proof.

1. Copy config to a disposable file; set `operator_mode = false` there only. Pass `-c` that file. Point credentials at the real credentials file if needed (`--credentials`), or keep the copy beside a credentials symlink — never print secrets; never edit Brad’s live config permanently.
2. Build an utterance WAV that contains the configured wake (or SARNEG code when `[sarneg] enabled = true`) plus a short question — typically via `tts-check`.
3. `helpers/run.sh talk-transmit -- walkietalk -c /tmp/…/config.toml talk /tmp/…/utterance.wav --once --transmit`
4. Proof: wake/reply lines in stdout **and** the same transmit consent + `Finished; transmitter released.` (or equivalent release after reply). Skip/partial if backends fail.

### Live talk (human second radio)

- `tmux new-session -d -s wt-verify-tx -- walkietalk talk --capture --transmit`
- One wake + short question from a second radio.
- Pane shows transcript + reply; gateway keys during reply and unkeys after; Ctrl+C releases TX.
- Not agent-solo — document when used.

If hardware is down: **skip** transmit; document `check` FAIL lines. Never claim proof from simulated `ptt` / `play` without `--transmit`.

## Gotchas

- Never use `--transmit` without AIOC audio + serial from doctor.
- `ptt` / `play` **without** `--transmit` print `DRY RUN:` — simulations only.
- Operator mode refuses WAV / `--once` for `talk`; use `play --transmit` for solo, or a disposable `-c` with `operator_mode = false` for talk-once.
- Power loss / `kill -9` can leave hardware keyed; prefer clean process exit; turn the radio off if it stays keyed.
- Only one process may own the PTT serial at a time.
