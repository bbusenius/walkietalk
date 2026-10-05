# Operator panel

When `[messaging] operator_mode = true`, `walkietalk talk --capture --transmit --panel` shows the live radio log above review controls in the same terminal (TUI). Mapped feature when operator mode is configured.

## Sub-features

- `panel-open` starts talk with `--panel` in a large enough PTY (≥ 60×20).
- `panel-keys` exposes Read/Edit/Approve/Transmit/Deny/Sleep and Tab for approved view when a queue item exists.
- `panel-with-cli` CLI `walkietalk operator …` still works while the panel is open.

## How to get to it (user POV)

- Enable operator mode in config, then run `walkietalk talk --capture --transmit --panel`.
- Use the on-screen keys, or another terminal with `walkietalk operator …`.

## Driving it with walkietalk-cli

Preconditions:

- `config-check` shows `Operator mode:`.
- AIOC hardware OK if using `--transmit` (required by the documented panel command). If hardware is down, skip — do not invent a panel-without-transmit path unless the product documents one.
- PTY/tmux at least 100×40 recommended.

- **Start panel.** `tmux new-session -d -s wt-verify-panel -x 100 -y 40 -- walkietalk talk --capture --transmit --panel`
- **Capture chrome.** After startup, `tmux capture-pane -t wt-verify-panel -p -S -300` into `pane.txt`. Expect review chrome / key legend when the UI is up (`[R] Read` … or equivalent status).
- **Drive (when a queue item exists).** Send keys via tmux only for the action under test; prefer CLI operator for deterministic approve/deny when proving messaging (see operator-cli).
- **Proof.** `pane.txt` shows panel chrome and conversation/queue status. Stop with Ctrl+C to that session only.

## Gotchas

- Panel requires operator mode; without it, `--panel` is not the mapped path.
- WAV / `--once` are refused in operator mode — continuous `--capture` only.
- Only one operator-mode talk per config.
- Keying happens with `--transmit`; same hardware rules as talk-transmit.
