# Config check

Config check validates the settings file and summarizes configured backends without touching hardware, network, or logins.

## Sub-features

- `config-ok` reports `Config OK:` and exit 0 for a valid config.
- `config-summary` prints speech recognition, agent, voice, listening, messaging, and operator-mode summary lines.
- `config-fail` reports problems for an invalid `-c` file (when deliberately tested).

## How to get to it (user POV)

- Run `walkietalk config-check` after `init` or any settings edit.
- Run `walkietalk config-check -c FILE` against an alternate settings file.

## Driving it with walkietalk-cli

Preconditions:

- `walkietalk` on `PATH`.
- A readable config (default `~/.config/walkietalk/config.toml` or `-c`).
- No need for AIOC hardware.

- **Validate default config.** Run `helpers/run.sh config-check -- walkietalk config-check`. Exit code `0`. stdout contains `Config OK:` and `No hardware, network, or login was checked.`
- **Confirm summary lines.** Same run: stdout names speech recognition, agent, and voice backends; if messaging is configured, wake phrases appear; if operator mode is on, stdout contains `Operator mode:`.
- **Proof.** Keep `artifacts/config-check/<run-id>/{cmd,stdout,stderr,exit}.txt`. `exit.txt` is `0` and stdout matches the phrases above.

## Gotchas

- `config-check` success does **not** prove audio, serial, or agent login — use `check` / `agent-check` for those.
- Never paste `credentials.toml` into evidence; the command should not print secret values.
- Unknown TOML fields are errors; Brad's WIP config may differ from committed templates — drive the file the user actually uses unless the recipe isolates with `-c`.
