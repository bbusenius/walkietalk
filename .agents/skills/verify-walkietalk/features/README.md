# walkietalk verification map

Maintained source for verifying user-facing walkietalk behavior. Read this index, then the matching feature file.

## Baseline preconditions

- `walkietalk` on `PATH` (repo `target/release` or `~/.cargo/bin`).
- Settings at `~/.config/walkietalk/config.toml` unless `-c` is passed.
- Run `helpers/doctor.sh` first when anything looks off.
- Prefer a disposable `-c` only when the recipe needs isolation; default config is fine for read-only checks.
- Never drive a `talk` instance started outside this verification run.
- **Do not key the radio** unless the feature requires transmit and doctor shows AIOC audio + serial.

## Driving conventions

- Start from baseline unless preconditions say otherwise.
- Treat every command as literal; keep flags unchanged.
- One-shot commands: `helpers/run.sh <feature-id> -- walkietalk …`.
- Continuous talk / panel: dedicated tmux session; capture the pane before cleanup.
- Record feature ID and entry point with every artifact.

## Proof and skip reporting

- Capture command, stdout, stderr, and exit code.
- Mutation/side-effect proof (files, sends, PTT) needs a second observable, not only a success line.
- If hardware is missing, report the unmet precondition and skip transmit/capture paths — do not mark them verified via `config-check`.
- CLI operator without a live operator-mode `talk` is partial; say so.

## Feature entry contract

Each feature file: H1 + one paragraph, then exactly four H2s: `Sub-features`, `How to get to it (user POV)`, `Driving it with walkietalk-cli`, `Gotchas`.

## Features

- [Config check](./config-check.md) — validate settings without hardware, network, or logins.
- [Hardware and backend check](./check.md) — confirm devices and backends; never transmits.
- [Talk capture (receive-only)](./talk-capture.md) — listen and print; radio never keyed.
- [Talk transmit](./talk-transmit.md) — speak replies on the radio when AIOC hardware is up (`talk --capture --transmit`).
- [Operator panel](./operator-panel.md) — TUI review controls with `talk … --panel` when operator mode is configured.
- [Operator CLI](./operator-cli.md) — `walkietalk operator …` against a running operator-mode talk (partial without a live session).
