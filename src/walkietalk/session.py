"""The parent owns PTT and the deadline; a child handles blocking audio I/O."""

import signal
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager

from .config import WalkietalkError


class PTTHardwareError(WalkietalkError):
    """PTT control failed; stop instead of retrying a message on uncertain hardware."""


def _control_ptt(action: Callable[[], None], operation: str) -> None:
    try:
        action()
    except (WalkietalkError, OSError) as exc:
        raise PTTHardwareError(f"PTT {operation} failed: {exc}") from exc


@contextmanager
def handle_stop_signals():
    def stop(signum, frame):
        raise KeyboardInterrupt

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@contextmanager
def uninterrupted_cleanup():
    # signal.signal only works on the main thread; live realtime talk may
    # finish supervised PTT from the warm-session worker thread.
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def transmit(ptt, action: Callable[[float], None], max_seconds: float) -> None:
    try:
        _control_ptt(ptt.open, "open")
        deadline = time.monotonic() + max_seconds
        _control_ptt(ptt.on, "assertion")
        action(deadline)
    finally:
        # Even a failed/partially completed assertion must attempt release.
        # A second Ctrl+C must not interrupt the release/close sequence.
        with uninterrupted_cleanup():
            try:
                _control_ptt(ptt.off, "release")
            finally:
                _control_ptt(ptt.close, "close")
