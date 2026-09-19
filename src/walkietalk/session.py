"""The parent owns PTT and the deadline; a child handles blocking audio I/O."""

import signal
import time
from collections.abc import Callable
from contextlib import contextmanager


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
    previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def transmit(ptt, action: Callable[[float], None], max_seconds: float) -> None:
    try:
        ptt.open()
        deadline = time.monotonic() + max_seconds
        ptt.on()
        action(deadline)
    finally:
        # Even a failed/partially completed assertion must attempt release.
        # A second Ctrl+C must not interrupt the release/close sequence.
        with uninterrupted_cleanup():
            try:
                ptt.off()
            finally:
                ptt.close()
