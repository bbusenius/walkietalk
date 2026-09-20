"""Optional ANSI color for live logs. Words stay readable without color."""

from __future__ import annotations

import os
import sys
from typing import TextIO

RESET = "\033[0m"
STYLES = {
    "transcript": "\033[1;36m",  # bold cyan
    "accepted": "\033[1;32m",  # bold green
    "ignored": "\033[33m",  # yellow
    "status": "\033[1m",  # bold
    "meter": "\033[2m",  # dim
    "event": "\033[36m",  # cyan
    "warn": "\033[33m",
    "error": "\033[1;31m",
    "reply": "\033[35m",  # magenta
}


def _enable_windows_color() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        handle = ctypes.windll.kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        return


def color_enabled(stream: TextIO | None = None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    stream = stream or sys.stdout
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(stream, "isatty") and stream.isatty()


def style(kind: str, text: str, *, stream: TextIO | None = None) -> str:
    code = STYLES.get(kind)
    if not code or not color_enabled(stream):
        return text
    return f"{code}{text}{RESET}"


def emit(kind: str, text: str, *, file: TextIO | None = None) -> None:
    file = file or sys.stdout
    print(style(kind, text, stream=file), file=file, flush=True)


def capture_log(message: str) -> None:
    if message.startswith("RMS "):
        kind = "meter"
    elif message.startswith("Waiting for someone to talk") or message.startswith("Listening on"):
        kind = "meter"
    elif message.startswith("Warning") or message.startswith("Ignored short"):
        kind = "warn"
    else:
        kind = "event"
    emit(kind, message)


_enable_windows_color()
