"""Incremental PCM playback in an isolated worker; the caller owns PTT.

The writer thread keeps pipe backpressure out of the WebSocket receive loop.
The caller bounds queued PCM by its TX budget and closes this worker from its
independent watchdog if audio I/O stalls.
"""

import queue
import subprocess
import sys
import threading
import time

from .audio import Playback
from .config import Config, WalkietalkError


class StreamingPlayback(Playback):
    def __init__(self, config: Config):
        self.command = [
            sys.executable,
            "-m",
            "walkietalk.worker",
            "--stream",
            config.output_device,
            str(config.gain),
            "24000",
        ]
        self.process = None
        self.errors = None
        self._queue = queue.Queue()
        self._done = threading.Event()
        self._error = None
        self._writer = None
        self._close_lock = threading.Lock()

    def prepare(self, timeout: float = 10) -> None:
        if self.process is not None:
            return
        self._queue = queue.Queue()
        self._done.clear()
        self._error = None
        super().prepare(timeout)
        self._writer = threading.Thread(target=self._write, daemon=True)
        self._writer.start()

    def __call__(self, pcm: bytes, rate: int, deadline: float) -> None:
        if rate != 24000 or self.process is None:
            raise WalkietalkError("Streaming playback requires prepared 24 kHz PCM")
        if time.monotonic() >= deadline:
            raise WalkietalkError("Transmit time limit reached")
        self._check_error()
        self._queue.put(pcm)

    def _write(self) -> None:
        process = self.process
        try:
            while (pcm := self._queue.get()) is not None:
                view = memoryview(pcm)
                while view:
                    written = process.stdin.write(view)
                    if not written:
                        raise OSError("Audio worker stopped accepting PCM")
                    view = view[written:]
            process.stdin.close()  # EOF tells the child to drain its output stream.
            if process.wait():
                raise WalkietalkError("Streaming audio worker failed")
        except Exception as exc:
            self._error = exc
        finally:
            self._done.set()

    def _check_error(self) -> None:
        if self._error is not None:
            raise WalkietalkError(f"Streaming playback failed: {self._error}") from self._error

    def finish(self, deadline: float) -> None:
        self._queue.put(None)
        if not self._done.wait(max(0, deadline - time.monotonic())):
            raise WalkietalkError("Transmit time limit reached; playback stopped")
        self._check_error()

    def close(self) -> None:
        with self._close_lock:
            self._close()

    def _close(self) -> None:
        # Kill before joining: a writer may be blocked in a pipe or process.wait.
        process = self.process
        if process is not None and process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        self._queue.put(None)
        if self._writer is not None:
            self._writer.join(timeout=1)
        if process is not None:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        if self.errors is not None:
            self.errors.close()
        self.process = None
        self.errors = None
        self._writer = None
