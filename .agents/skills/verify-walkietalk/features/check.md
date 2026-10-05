# Hardware and backend check

`walkietalk check` confirms configured capture/playback devices exist, the PTT serial port is accessible, and STT/agent/TTS/messaging backends are ready. It never opens audio streams, never keys the transmitter, and never sends agent requests.

## Sub-features

- `check-devices` verifies ALSA input/output names from config.
- `check-ptt` verifies the serial path exists (does not key).
- `check-backends` verifies STT/agent/TTS (and messaging tools when configured).
- `check-summary` prints listening/sleep/SARNEG/shutdown/station-ID summary and `Nothing was opened or transmitted.`

## How to get to it (user POV)

- Run `walkietalk check` after wiring the AIOC and editing `[audio]` / `[ptt]`.
- Run after changing backends or messaging tools.

## Driving it with walkietalk-cli

Preconditions:

- `walkietalk` on `PATH` and a valid config (`config-check` already OK).
- For a full pass: AIOC present as the configured `plughw:CARD=…` and `/dev/serial/by-id/…` AIOC path.
- If AIOC is absent, expect `FAIL` lines and exit ≠ 0; that still proves the check path ran without transmitting.

- **Run check.** `helpers/run.sh check -- walkietalk check`.
- **Hardware up.** Exit `0`, every device/backend line starts with `ok`, and stdout ends with context plus `Nothing was opened or transmitted.`
- **Hardware down.** Exit non-zero, `FAIL` on missing capture/playback/PTT; backends may still be `ok`. Proof is the transcript showing failures **and** that nothing was transmitted.
- **Proof.** Save streams under `artifacts/check/<run-id>/`. Assert the `Nothing was opened or transmitted.` line when present.

## Gotchas

- Laptop sound cards (`sofsoundwire`, etc.) are not the AIOC; `devices` listing only those means skip transmit features.
- A connected phone/tablet USB serial entry is not the AIOC PTT port.
- Do not "fix" a FAIL by pointing config at the laptop mic during verification of radio paths.
