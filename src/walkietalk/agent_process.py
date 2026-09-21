"""Bounded Linux CLI execution. Prompts use stdin; diagnostics stay local."""

import os
import selectors
import signal
import subprocess
import time
from pathlib import Path

from .config import WalkietalkError
from .session import uninterrupted_cleanup

MAX_PROCESS_OUTPUT_BYTES = 1024 * 1024
MAX_FINAL_BYTES = 65536


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def run_cli(
    command: list[str],
    *,
    prompt: bytes,
    cwd: str,
    env: dict[str, str],
    deadline: float,
    final_path: Path | None = None,
    max_final_bytes: int = MAX_FINAL_BYTES,
    name: str = "Codex",
    executable_setting: str = "agent.codex_executable",
) -> tuple[int, bytes, bytes]:
    """Drain both pipes with byte limits; kill the entire group on every exit.

    A descendant retaining a pipe or a CLI refusing to read stdin cannot bypass
    the deadline. No threads, shell interpolation, or unbounded communicate().
    """
    if time.monotonic() >= deadline:
        raise WalkietalkError(f"{name} timed out; reply discarded")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
            bufsize=0,
            umask=0o077,
        )
    except FileNotFoundError:
        raise WalkietalkError(
            f"{name} CLI not found; install it and check {executable_setting}"
        ) from None
    except OSError:
        raise WalkietalkError(f"Cannot start {name} CLI; check executable permissions") from None
    output = {"stdout": bytearray(), "stderr": bytearray()}
    sent = 0
    total = 0
    try:
        with selectors.DefaultSelector() as selector:
            for stream_name in ("stdout", "stderr"):
                stream = getattr(process, stream_name)
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, stream_name)
            if prompt:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                process.stdin.close()
            while selector.get_map() or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WalkietalkError(f"{name} timed out; reply discarded")
                if final_path is not None and final_path.exists():
                    if final_path.stat().st_size > max_final_bytes:
                        raise WalkietalkError(
                            f"{name} final output exceeded the transport limit; discarded"
                        )
                for key, _ in selector.select(min(0.05, remaining)):
                    stream, stream_name = key.fileobj, key.data
                    if stream_name == "stdin":
                        try:
                            sent += os.write(stream.fileno(), prompt[sent : sent + 8192])
                        except BrokenPipeError:
                            sent = len(prompt)
                        if sent == len(prompt):
                            selector.unregister(stream)
                            stream.close()
                    else:
                        data = os.read(stream.fileno(), 8192)
                        if not data:
                            selector.unregister(stream)
                            stream.close()
                            continue
                        total += len(data)
                        if total > MAX_PROCESS_OUTPUT_BYTES:
                            raise WalkietalkError(
                                f"{name} output exceeded the transport limit; discarded"
                            )
                        output[stream_name].extend(data)
            return process.returncode, bytes(output["stdout"]), bytes(output["stderr"])
    finally:
        # Keep shutdown bounded even if the CLI or one of its children hangs.
        # A second stop signal must not interrupt process-group cleanup.
        with uninterrupted_cleanup():
            _signal_group(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            finally:
                _signal_group(process.pid, signal.SIGKILL)
                process.wait(timeout=0.5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
