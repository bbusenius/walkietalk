# Talk capture (receive-only)

`walkietalk talk --capture` listens on the configured capture device, wake-gates, and prints transcripts and replies. Without `--transmit`, the radio is never keyed.

## Sub-features

- `talk-capture-continuous` runs until Ctrl+C.
- `talk-capture-once` handles one utterance with `--once` (optional `--timeout`).
- `talk-wav` reads an utterance from a WAV file instead of live capture.

## How to get to it (user POV)

- `walkietalk talk --capture`
- `walkietalk talk --capture --once --timeout 30`
- `walkietalk talk path/to/utterance.wav`

## Driving it with walkietalk-cli

Preconditions:

- `check` passes for capture device **or** you use a WAV input (no live device).
- Prefer WAV or skip when AIOC capture is missing.
- Do **not** pass `--transmit`.

- **WAV once (safe when hardware down if STT/agent OK).** Prepare a short mono WAV fixture if the recipe supplies one; run `helpers/run.sh talk-capture -- walkietalk talk "$WAV" --once` only when a fixture exists. Otherwise skip and record the unmet precondition.
- **Live capture.** Only when doctor shows the configured capture device. Start `tmux new-session -d -s wt-verify-talk -- walkietalk talk --capture`, speak the configured wake (or SARNEG code), capture the pane, then Ctrl+C that session.
- **Proof.** Transcript shows wake handling and a reply (or a clear backend error). Confirm no `--transmit` was used.

## Gotchas

- With SARNEG enabled, the agent wakes on the SARNEG code, not the plain wake phrase.
- Continuous capture has no idle timeout; always stop the session you started.
- Operator mode refuses WAV input and `--once` for `talk` — use continuous capture (and see operator features).
