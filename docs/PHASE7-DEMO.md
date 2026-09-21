# Phase 7 demonstration — watchdogs, mute, optional callsign

Phase 6 is merged. Phase 7 family demonstration is accepted. Brad confirmed
mute, cutoff, timeout, wake_phrase, conversation mode, and clean unkey, and
authorized publication. Automated checks and live radio observations stay
separate. Walkietalk does not invent a station ID.

## Config

```yaml
radio:
  max_tx_seconds: 10
  settle_seconds: 0.2
  post_tx_mute_seconds: 2
  callsign: ""
  callsign_mode: "end_of_reply"
  callsign_interval_seconds: 900
```

Leave `callsign` empty unless you are putting **your granted GMRS ID** in
`config.local.yaml`. Do not copy an example ID. Mode `off` or an empty callsign
means no spoken identification.

Restart after editing config.

## Automated (no radio)

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
```

## Family radio checklist

Use `talk --capture --transmit` with the usual wake phrase.

1. **Overlong cutoff:** force a long spoken answer (or a test WAV longer than
   `max_tx_seconds` minus settle). Expect the transmitter to unkey at the cap,
   not stay keyed.
2. **Post-TX mute:** after a normal reply, watch the TX light go off, then wait
   the configured mute before the bridge listens again. Do not expect it to
   catch a question during that mute.
3. **Conversation timeout:** after mute, the follow-up window still starts at
   unkey. Stay quiet past `conversation_timeout_seconds` and confirm the wake
   name is required again.
4. **Callsign (optional):** only if you set your real ID. Expect it spoken after
   the answer in the same transmission. No music or sound effects.
5. **Ctrl+C** during playback still releases PTT.

Receive-only `talk --capture` does not key, mute, or speak an ID.
