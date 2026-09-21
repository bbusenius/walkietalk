"""Check an installed wheel outside the source tree; no network, login, or hardware.

Run with the fresh environment's Python: python /path/to/scripts/check_installation.py
"""

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "LC_ALL"}}
    with tempfile.TemporaryDirectory(prefix="walkietalk-install-check-") as directory:
        root = Path(directory)
        env["HOME"] = str(root)
        env["NO_COLOR"] = "1"
        settings = root / "settings"

        def run(*args, code=0, expected=""):
            result = subprocess.run(
                [sys.executable, "-m", "walkietalk", *args],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            output = result.stdout + result.stderr
            if result.returncode != code or expected not in output:
                raise RuntimeError(f"Installed command failed: {args!r}\n{output}")
            return output

        run("--version", expected="walkietalk 0.1.0")
        run("--help", expected="config-check")
        run("init", "--directory", str(settings), expected="No hardware opened")
        modes = {
            settings: 0o700,
            settings / "config.yaml": 0o600,
            settings / "credentials.env": 0o600,
        }
        for path, expected_mode in modes.items():
            actual = stat.S_IMODE(path.stat().st_mode)
            if actual != expected_mode:
                raise RuntimeError(
                    f"Private mode {expected_mode:o} required, got {actual:o}: {path}"
                )
        config = str(settings / "config.yaml")
        original = (settings / "config.yaml").read_bytes()
        run("init", "--directory", str(settings), code=1, expected="nothing overwritten")
        assert (settings / "config.yaml").read_bytes() == original
        run("-c", config, "config-check", expected="Config OK")
        run("-c", config, "agent-check", "Hello", expected="pretend answer")
        run("ptt", "--seconds", ".01", expected="DRY RUN: PTT OFF")
        # Simulate an expired login through the real Codex adapter, without a real account.
        fake = root / "fake-codex"
        fake.write_text(f"#!{sys.executable}\nimport sys\nprint('Not logged in')\nsys.exit(1)\n")
        fake.chmod(0o700)
        text = (
            original.decode()
            .replace('backend: "stub"', 'backend: "codex"')
            .replace('codex_executable: "codex"', f'codex_executable: "{fake}"')
        )
        (settings / "config.yaml").write_text(text)
        failure = run("-c", config, "agent-check", "Hello", code=1, expected="codex login")
        print("Simulated expired login (fake executable, no real account):")
        print(failure.strip())
        # A fake local rejection cannot fall back to any real service.
        print(
            "PASS: installed wheel, packaged templates, config validation, offline stub, "
            "dry PTT, and simulated expired login. No hardware or provider requests."
        )


if __name__ == "__main__":
    main()
