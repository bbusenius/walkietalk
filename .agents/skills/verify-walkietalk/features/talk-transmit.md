# Talk transmit

`walkietalk talk --capture --transmit` speaks agent/contact replies on the radio. This is the real keying path when AIOC hardware is up.

## Sub-features

- `talk-tx-live` continuous capture with transmit.
- `talk-tx-once` one utterance then exit (when allowed by config/mode).
- `ptt-sim-vs-real` `walkietalk ptt` is simulated unless `--transmit` — not a substitute for talk TX proof.

## How to get to it (user POV)

- `walkietalk talk --capture --transmit`
- Hold the handheld PTT, say the wake/SARNEG + question, release; hear the reply on the gateway radio; confirm TX stops after.

## Driving it with walkietalk-cli

Preconditions:

- `walkietalk check` exit 0 (AIOC audio + serial OK).
- User/operator present to monitor the station while it transmits (product responsibility note in README).
- If hardware is down: **skip** this feature; document `check` FAIL lines. Do not prove transmit via simulated `ptt`.

- **Start.** `tmux new-session -d -s wt-verify-tx -- walkietalk talk --capture --transmit`
- **Exercise.** One wake + short question from a second radio (or documented lab procedure).
- **Observe.** Pane shows transcript + reply; gateway radio keys during reply and unkeys after; Ctrl+C releases TX.
- **Proof.** `pane.txt` plus operator notes that TX was audible and released. Optional: short observation log `tx-notes.txt` in the artifact dir.
- **Cleanup.** Ctrl+C the tmux session you started; confirm serial not left keyed (radio quiet).

## Gotchas

- Never use `--transmit` on a machine without the AIOC path from config.
- `ptt` / `play` without `--transmit` are simulations — do not report them as talk-transmit proof.
- Power loss / `kill -9` can leave hardware keyed; prefer clean Ctrl+C; turn the radio off if it stays keyed.
