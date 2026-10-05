# Operator CLI

`walkietalk operator` controls a running operator-mode `talk` from another terminal (status, read, edit, approve, transmit, deny, sleep). Partial without a live talk session: status then reports that nothing is running.

## Sub-features

- `operator-status` shows review head / queue when talk is up.
- `operator-read-edit-approve` reviews and releases or denies an item.
- `operator-no-session` documents the failure mode when no talk is running.

## How to get to it (user POV)

- With operator-mode talk already running: `walkietalk operator status` (and read/edit/approve/deny/sleep/transmit).
- Pass `-c` if several configs could be live.

## Driving it with walkietalk-cli

Preconditions:

- Operator mode enabled in config.
- For full proof: a `talk --capture` (usually with `--transmit`) you started is running.
- Without a session: only prove the no-session error path.

- **No session (safe partial).** `helpers/run.sh operator-cli -- walkietalk operator status`. Capture non-zero or clear "not running" messaging — do not claim full operator UX.
- **With session.** Start talk (see operator-panel or talk-transmit), then `walkietalk operator status`, `read`, etc., matching docs/MESSAGING.md.
- **Proof.** Transcripts of status before/after an approve or deny when a real queued item exists; otherwise record skip/partial.

## Gotchas

- Commands bind to the item they showed; if the head changes, the command is refused.
- Voice notes must be `read` before approve/edit.
- This path is **partial** in CI or when messaging tools/hardware are down — say partial, do not equate to panel proof.
