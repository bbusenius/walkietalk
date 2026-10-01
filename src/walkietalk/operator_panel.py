"""Optional fixed operator controls over the same local commands as the CLI."""

from __future__ import annotations

import codecs
import ctypes
import curses
import io
import os
import re
import select
import signal
import sys
import threading
import traceback
import unicodedata
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path

from .config import WalkietalkError
from .operator_mode import (
    MAX_EDIT_CHARS,
    OperatorQueue,
    normalized_change,
    operator_request,
    original_line,
)
from .term import route_logs

MIN_ROWS = 20
MIN_COLUMNS = 60
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def validate_terminal() -> None:
    """Reject redirected/headless panel runs before opening devices or providers."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise WalkietalkError("--panel requires an interactive terminal for input and output")
    if os.environ.get("TERM", "dumb") == "dumb":
        raise WalkietalkError("--panel requires a terminal with cursor positioning (TERM)")
    try:
        curses.setupterm(fd=sys.stdout.fileno())
    except (curses.error, OSError) as exc:
        raise WalkietalkError(f"Cannot initialize the operator terminal: {exc}") from exc


def _clean(text: str) -> str:
    text = _ANSI.sub("", text).expandtabs(4)
    return "".join(c for c in text if c == "\n" or unicodedata.category(c) != "Cc")


def _width(character: str) -> int:
    if unicodedata.combining(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def _clip(text: str, columns: int) -> str:
    result = []
    used = 0
    for character in text:
        used += _width(character)
        if used > columns:
            break
        result.append(character)
    return "".join(result)


@dataclass
class LineEditor:
    """One-line message editor; layout wraps by display width like the review body."""

    initial: str
    revision: int
    heading: str
    buffer: str = ""
    cursor: int = 0

    def __post_init__(self) -> None:
        self.buffer = self.initial
        self.cursor = len(self.buffer)

    def insert(self, text: str) -> bool:
        room = MAX_EDIT_CHARS - len(self.buffer)
        if room <= 0:
            return False
        text = text[:room]
        self.buffer = self.buffer[: self.cursor] + text + self.buffer[self.cursor :]
        self.cursor += len(text)
        return True

    def backspace(self) -> None:
        if self.cursor:
            self.buffer = self.buffer[: self.cursor - 1] + self.buffer[self.cursor :]
            self.cursor -= 1

    def delete(self) -> None:
        self.buffer = self.buffer[: self.cursor] + self.buffer[self.cursor + 1 :]

    def move(self, offset: int) -> None:
        self.cursor = max(0, min(len(self.buffer), self.cursor + offset))

    def home(self) -> None:
        self.cursor = 0

    def end(self) -> None:
        self.cursor = len(self.buffer)

    def clear(self) -> None:
        self.buffer, self.cursor = "", 0

    def layout(self, columns: int) -> tuple[list[str], int, int]:
        """Wrapped rows plus the cursor's row and display column."""
        lines, line, used, row, column = [], [], 0, 0, 0
        for index, character in enumerate(self.buffer):
            width = _width(character)
            if line and used + width > columns:
                lines.append("".join(line))
                line, used = [], 0
            if index == self.cursor:
                row, column = len(lines), used
            line.append(character)
            used += width
        if self.cursor == len(self.buffer):
            if used >= columns:
                lines.append("".join(line))
                line, used = [], 0
            row, column = len(lines), used
        lines.append("".join(line))
        return lines, row, column


def wrap_lines(text: str, columns: int) -> list[str]:
    """Wrap without losing any message words, including wide Unicode characters."""
    lines = []
    for paragraph in _clean(text).split("\n"):
        remaining = paragraph
        while remaining:
            line = _clip(remaining, max(1, columns)) or remaining[0]
            if len(line) < len(remaining) and " " in line and line.rfind(" ") > 0:
                line = line[: line.rfind(" ") + 1]
            lines.append(line.rstrip())
            remaining = remaining[len(line) :].lstrip(" ")
        if not paragraph:
            lines.append("")
    return lines


class _LogStream(io.TextIOBase):
    """Collect ordinary Python prints as well as structured emit() events."""

    def __init__(self, original, sink, kind: str):
        self.original, self.sink, self.kind = original, sink, kind
        self._lock = threading.Lock()
        self._pending = ""

    @property
    def encoding(self):
        return self.original.encoding

    def fileno(self):
        return self.original.fileno()

    def writable(self):
        return True

    def write(self, text):
        with self._lock:
            self._pending += text.replace("\r", "\n")
            while "\n" in self._pending:
                line, self._pending = self._pending.split("\n", 1)
                if line:
                    self.sink(self.kind, line)
        return len(text)

    def flush(self):
        with self._lock:
            if self._pending:
                self.sink(self.kind, self._pending)
                self._pending = ""


@contextmanager
def _terminal_output():
    """Give ncurses its own terminal FILE*, independent of descriptors 1 and 2."""
    import _curses

    # Python omits newterm/set_term; use the same ncurses library as _curses.
    native = ctypes.CDLL(_curses.__file__)
    libc = ctypes.CDLL(None, use_errno=True)
    native.newterm.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p]
    native.newterm.restype = ctypes.c_void_p
    native.set_term.argtypes = [ctypes.c_void_p]
    native.set_term.restype = ctypes.c_void_p
    native.delscreen.argtypes = [ctypes.c_void_p]
    native.delscreen.restype = None
    libc.fdopen.argtypes = [ctypes.c_int, ctypes.c_char_p]
    libc.fdopen.restype = ctypes.c_void_p
    libc.fclose.argtypes = [ctypes.c_void_p]
    libc.fclose.restype = ctypes.c_int
    with ExitStack() as cleanup:
        files = []
        for descriptor, mode in ((1, b"w"), (0, b"r")):
            duplicate = os.dup(descriptor)
            file = libc.fdopen(duplicate, mode)
            if not file:
                os.close(duplicate)
                raise OSError(ctypes.get_errno(), "Cannot open operator terminal stream")
            cleanup.callback(libc.fclose, file)
            files.append(file)
        previous = native.set_term(None)
        native.set_term(previous)
        screen = native.newterm(os.environ["TERM"].encode(), *files)
        try:
            if not screen:
                raise WalkietalkError("Cannot initialize isolated operator terminal")
            yield
        finally:
            native.set_term(previous)
            if screen:
                native.delscreen(screen)


@contextmanager
def _native_logs(sink):
    """Drain native/subprocess stdout and stderr while curses uses its own stream."""
    stopped = threading.Event()
    threads = []

    def drain(descriptor: int, original, kind: str) -> None:
        stream = _LogStream(original, sink, kind)
        decoder = codecs.getincrementaldecoder(original.encoding or "utf-8")(errors="replace")
        try:
            while True:
                if not select.select([descriptor], [], [], 0.1)[0]:
                    if stopped.is_set():
                        break
                    continue
                data = os.read(descriptor, 4096)
                if not data:
                    break
                stream.write(decoder.decode(data))
            stream.write(decoder.decode(b"", final=True))
            stream.flush()
        finally:
            os.close(descriptor)

    try:
        with ExitStack() as restore:
            for descriptor, original, kind in ((1, sys.stdout, "event"), (2, sys.stderr, "error")):
                original.flush()
                saved = os.dup(descriptor)
                restore.callback(os.close, saved)
                restore.callback(os.dup2, saved, descriptor)
                reader, writer = os.pipe()
                thread = threading.Thread(
                    target=drain,
                    args=(reader, original, kind),
                    name=f"operator-native-{kind}",
                    daemon=True,
                )
                try:
                    thread.start()
                except BaseException:
                    os.close(reader)
                    raise
                else:
                    threads.append(thread)
                    os.dup2(writer, descriptor)
                finally:
                    os.close(writer)
            yield
    finally:
        stopped.set()
        for thread in threads:
            thread.join(timeout=2)


class OperatorPanel:
    """Curses owns one display thread; radio actions still belong to talk's main loop."""

    def __init__(self, review: OperatorQueue, config_path: Path):
        self.review, self.config_path = review, config_path
        self.closed = threading.Event()
        self._ready = threading.Event()
        self._clear_keys = threading.Event()
        self._lock = threading.Lock()
        self._busy = False
        self._result = ("status", "")
        self._delivery_result: tuple[str, str] | None = None
        self._error: Exception | None = None
        self._thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._output = ExitStack()
        self._streams: list[_LogStream] = []
        self._scroll = 0
        self._body_key = None
        self._body: list[str] = []
        self._approved_view = False
        self._colors: dict[str, int] = {}
        self._log_revision: int | None = None
        self._log_key = None
        self._log_entries: list[tuple[str, str]] = []
        self._log_lines: list[tuple[str, str]] = []
        self._editor: LineEditor | None = None
        self._cursor_visible = False
        self._saved_escdelay: int | None = None

    def start(self) -> None:
        try:
            self._output.enter_context(route_logs(self.review.record_log))
            for original, redirect, kind in (
                (sys.stdout, redirect_stdout, "event"),
                (sys.stderr, redirect_stderr, "error"),
            ):
                stream = _LogStream(original, self.review.record_log, kind)
                self._streams.append(stream)
                self._output.enter_context(redirect(stream))
            self._thread = threading.Thread(target=self._run, name="operator-panel", daemon=True)
            self._thread.start()
            if not self._ready.wait(5):
                raise WalkietalkError("Operator terminal did not initialize")
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self) -> None:
        if self.closed.is_set():
            detail = f": {self._error}" if self._error else ""
            raise WalkietalkError(f"Operator panel closed{detail}")

    def close(self) -> None:
        self.closed.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        # The caller closes operator controls first, unblocking any command worker.
        if self._worker is not None:
            self._worker.join(timeout=2)
        for stream in self._streams:
            stream.flush()
        self._output.close()

    def _run(self) -> None:
        try:
            curses.wrapper(self._isolated_display)
        except Exception as exc:
            self._error = exc
        finally:
            self.closed.set()
            self._ready.set()

    def _isolated_display(self, original_screen) -> None:
        # wrapper initializes Python's curses state; newterm then supplies the
        # actual display. Restore shell modes before newterm saves them, leaving
        # the first screen active so wrapper can end it exactly once.
        curses.reset_shell_mode()
        with _terminal_output():
            screen = curses.initscr()
            try:
                curses.noecho()
                curses.cbreak()
                screen.keypad(True)
                try:
                    curses.start_color()
                except curses.error:
                    pass
                with _native_logs(self.review.record_log):
                    self._display(screen)
            except BaseException as exc:
                # A retained traceback must not keep a window wrapper alive
                # past delscreen, including frames inside _display and _draw.
                errors, seen = [exc], set()
                while errors:
                    error = errors.pop()
                    if error is None or id(error) in seen:
                        continue
                    seen.add(id(error))
                    traceback.clear_frames(error.__traceback__)
                    errors.extend((error.__cause__, error.__context__))
                raise exc.with_traceback(None) from None
            finally:
                self._use_edit_escdelay(False)
                try:
                    screen.keypad(False)
                    curses.echo()
                    curses.nocbreak()
                    curses.endwin()
                finally:
                    # Release Python's wrapper while this stdscr is current.
                    del screen

    def _progress(self, kind: str, message: str) -> None:
        with self._lock:
            self._result = (kind, message)

    def delivery_status(self, kind: str, message: str) -> None:
        """Keep actual playback results visible after the command was scheduled."""
        with self._lock:
            self._delivery_result = (kind, message)

    def _command(
        self, action: str, revision: int | None, approved: bool = False, text=None
    ) -> None:
        keep_edit = False
        try:
            options = {"approved": True} if approved else {}
            if text is not None:
                options["text"] = text
            ok = operator_request(
                self.config_path,
                action,
                revision=revision,
                show_snapshot=False,
                log=self._progress,
                **options,
            )
            if not ok:
                with self._lock:
                    kind, message = self._result
                    self._result = ("warn" if kind == "status" else kind, message)
                keep_edit = action == "edit"
        except (WalkietalkError, OSError) as exc:
            self._progress("error", str(exc))
            self.review.record_log("error", str(exc))
            keep_edit = action == "edit"
        finally:
            if keep_edit and text is not None:
                self.review.record_log("warn", f"Unsent edit: {text}")
            with self._lock:
                self._busy = False
            # Do not apply keystrokes accumulated during a send to the next item.
            self._clear_keys.set()

    def _dispatch(self, key: int, displayed: dict, body_rows: int) -> None:
        if key == 3:
            os.kill(os.getpid(), signal.SIGINT)
            return
        if key == 9:
            with self._lock:
                if not self._busy:
                    self._approved_view = not displayed.get("approved_view", False)
            return
        if key in (curses.KEY_UP, curses.KEY_PPAGE, curses.KEY_HOME):
            self._scroll = (
                0
                if key == curses.KEY_HOME
                else max(0, self._scroll - (body_rows if key == curses.KEY_PPAGE else 1))
            )
            return
        if key in (curses.KEY_DOWN, curses.KEY_NPAGE, curses.KEY_END):
            last = max(0, len(self._body) - body_rows)
            self._scroll = (
                last
                if key == curses.KEY_END
                else min(last, self._scroll + (body_rows if key == curses.KEY_NPAGE else 1))
            )
            return
        action = {
            ord("r"): "read",
            ord("e"): "edit",
            ord("a"): "approve",
            ord("t"): "transmit",
            ord("d"): "deny",
            ord("s"): "sleep",
        }.get(key + 32 if ord("A") <= key <= ord("Z") else key)
        item = displayed["item"]
        if action is None:
            return
        with self._lock:
            if self._busy:
                return
            self._delivery_result = None
            if item is None and action != "sleep":
                self._result = (
                    ("warn", "Press Tab to Approved, then T to transmit.")
                    if action == "transmit"
                    and displayed["approved"]
                    and not displayed.get("approved_view", False)
                    else ("status", "No message in this view.")
                )
                return
            if action == "edit":
                blocked = item.get("edit_block")
                if blocked:
                    self._result = ("warn", blocked)
                else:
                    self._editor = LineEditor(
                        item["text"] if item["kind"] == "text" else item["transcript"],
                        displayed["revision"],
                        "Editing " + self._heading(item),
                    )
                    self._scroll = 0
                    self._use_edit_escdelay(True)
                    self._result = ("status", "Enter saves, Esc cancels.")
                return
            if action == "approve" and displayed.get("approved_view", False):
                self._result = ("warn", "This message is already approved; T transmits it.")
                return
            if action == "transmit":
                if item["direction"] != "incoming":
                    self._result = (
                        "warn",
                        "Transmit applies to incoming messages; A sends outgoing.",
                    )
                    return
                if displayed.get("dispatching"):
                    self._result = ("warn", "An operator delivery is already pending.")
                    return
            if (
                action in {"approve", "transmit"}
                and item["kind"] == "voice"
                and not item["previewed"]
            ):
                self._result = ("warn", "Press R to obtain this voice message's transcript.")
                return
            self._busy = True
            self._result = ("status", f"{action.capitalize()} requested; waiting for the radio.")
        self._worker = threading.Thread(
            target=self._command,
            args=(
                (action, None, False)
                if action == "sleep"
                else (action, displayed["revision"], displayed.get("approved_view", False))
            ),
            name="operator-panel-command",
            daemon=True,
        )
        self._worker.start()

    def _heading(self, item: dict) -> str:
        return (
            f"Item {item['number']} | {item['direction'].title()} | "
            f"{item['service'].title()} | {item['alias']} | {item['kind'].title()}"
        )

    def _edit_key(self, screen, key, *, more: bool | None = None) -> bool:
        """Handle one editing key. False means drop the rest of this input batch."""
        editor = self._editor
        if key == -1 or key == curses.KEY_RESIZE:
            return True
        if key in ("\x03", 3):
            os.kill(os.getpid(), signal.SIGINT)
            return False
        if key == "\x1b":
            # Alt+letter arrives as Esc followed by that letter. Drop the rest.
            try:
                curses.flushinp()
            except curses.error:
                pass
            self._close_editor(("status", "Edit cancelled; message unchanged."))
            return False
        if key in ("\n", "\r", curses.KEY_ENTER):
            pending = self._input_pending(screen) if more is None else more
            if pending:
                editor.insert(" ")  # a newline inside pasted text
            else:
                self._save_edit()
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
            editor.backspace()
        elif key == curses.KEY_DC:
            editor.delete()
        elif key == curses.KEY_LEFT:
            editor.move(-1)
        elif key == curses.KEY_RIGHT:
            editor.move(1)
        elif key in (curses.KEY_HOME, "\x01"):
            editor.home()
        elif key in (curses.KEY_END, "\x05"):
            editor.end()
        elif key == "\x15":
            editor.clear()
        elif isinstance(key, str) and (key.isprintable() or key.isspace()):
            if not editor.insert(" " if key.isspace() else key):
                self._progress("warn", f"Edit limit is {MAX_EDIT_CHARS} characters.")
        # Every other key (Up/Down, PgUp/PgDn, function keys) is ignored.
        return True

    @staticmethod
    def _input_pending(screen) -> bool:
        """Pasted text arrives in one burst; a lone Enter has nothing behind it."""
        screen.timeout(0)
        try:
            key = screen.get_wch()
        except curses.error:
            return False
        finally:
            screen.timeout(100)
        if isinstance(key, str):
            curses.unget_wch(key)
        else:
            curses.ungetch(key)
        return True

    def _close_editor(self, result: tuple[str, str]) -> None:
        self._editor = None
        self._body_key = None  # rebuild the message body on the next draw
        self._use_edit_escdelay(False)
        with self._lock:
            self._result = result

    def _save_edit(self) -> None:
        editor = self._editor
        text = normalized_change(editor.buffer, editor.initial)
        if text is None:
            self._close_editor(("status", "Edit cancelled; message unchanged."))
            return
        self._close_editor(("status", "Edit requested; waiting for the radio."))
        with self._lock:
            self._busy = True
            self._delivery_result = None
        self._worker = threading.Thread(
            target=self._command,
            args=("edit", editor.revision, False, text),
            name="operator-panel-command",
            daemon=True,
        )
        self._worker.start()

    def _use_edit_escdelay(self, editing: bool) -> None:
        """Use a short Esc wait only while editing, so arrows keep working otherwise."""
        try:
            if editing:
                if self._saved_escdelay is None:
                    self._saved_escdelay = curses.get_escdelay()
                    curses.set_escdelay(25)
            elif self._saved_escdelay is not None:
                curses.set_escdelay(self._saved_escdelay)
                self._saved_escdelay = None
        except curses.error:
            pass

    def _read_keys(self, screen) -> list:
        """Read one key, then every other key already waiting, before the next redraw."""
        try:
            key = screen.get_wch()
        except curses.error:
            return [-1]
        keys = [key]
        screen.timeout(0)
        try:
            while True:
                try:
                    keys.append(screen.get_wch())
                except curses.error:
                    return keys
        finally:
            screen.timeout(100)

    def _handle_keys(self, screen, keys, snapshot, body_rows) -> None:
        for index, key in enumerate(keys):
            if not body_rows:
                return
            if self._editor is not None:
                if not self._edit_key(screen, key, more=index + 1 < len(keys)):
                    return
            else:
                self._dispatch(ord(key) if isinstance(key, str) else key, snapshot, body_rows)

    def _set_cursor_visible(self, visible: bool) -> None:
        if visible == self._cursor_visible:
            return
        try:
            curses.curs_set(1 if visible else 0)
        except curses.error:
            pass
        self._cursor_visible = visible

    def _display(self, screen) -> None:
        screen.timeout(100)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if curses.has_colors() and not os.environ.get("NO_COLOR"):
            background = curses.COLOR_BLACK
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                pass
            for number, (kind, foreground) in enumerate(
                (
                    ("event", curses.COLOR_CYAN),
                    ("accepted", curses.COLOR_GREEN),
                    ("warn", curses.COLOR_YELLOW),
                    ("error", curses.COLOR_RED),
                    ("reply", curses.COLOR_MAGENTA),
                ),
                start=1,
            ):
                curses.init_pair(number, foreground, background)
                self._colors[kind] = curses.color_pair(number)
            self._colors["transcript"] = self._colors["event"]
        self._ready.set()
        while not self.closed.is_set():
            if self._clear_keys.is_set():
                self._clear_keys.clear()
                curses.flushinp()
            snapshot = self.review.snapshot(approved=self._approved_view)
            body_rows = self._draw(screen, snapshot)
            self._handle_keys(screen, self._read_keys(screen), snapshot, body_rows)

    def _put(self, screen, row: int, text: str, kind="status", *, column=2) -> None:
        height, width = screen.getmaxyx()
        if not 0 <= row < height or width <= column + 1:
            return
        attributes = self._colors.get(kind, 0)
        if kind == "meter":
            attributes |= curses.A_DIM
        try:
            screen.addstr(
                row, column, _clip(_clean(text).replace("\n", " "), width - column - 2), attributes
            )
        except curses.error:
            # A resize can happen between getmaxyx and drawing a line.
            pass

    def _draw(self, screen, snapshot: dict) -> int:
        height, width = screen.getmaxyx()
        screen.erase()
        if height < MIN_ROWS or width < MIN_COLUMNS:
            self._put(
                screen, 0, f"Resize to at least {MIN_COLUMNS}x{MIN_ROWS} for operator controls."
            )
            self._put(screen, 1, "Radio continues. Ctrl+C stops.")
            self._set_cursor_visible(self._editor is not None)
            screen.refresh()
            return 0
        separator = height - max(14, min(20, height // 2))
        try:
            screen.border()
            screen.hline(separator, 1, curses.ACS_HLINE, width - 2)
        except curses.error:
            pass
        self._put(screen, 0, " Radio log ")
        revision, entries = self.review.changed_logs(self._log_revision)
        self._log_revision = revision
        if entries is not None:
            self._log_entries = entries
        log_key = (revision, width, separator)
        if log_key != self._log_key:
            logs = []
            for kind, message in reversed(self._log_entries):
                logs.extend((kind, line) for line in reversed(wrap_lines(message, width - 4)))
                if len(logs) >= separator - 1:
                    break
            self._log_lines = list(reversed(logs[: separator - 1]))
            self._log_key = log_key
        logs = self._log_lines
        for row, (kind, line) in enumerate(logs[-(separator - 1) :], start=1):
            self._put(screen, row, line, kind)
        approved = snapshot.get("approved_view", False)
        self._put(
            screen,
            separator,
            " Operator: Approved (Tab: Review) "
            if approved
            else " Operator: Review (Tab: Approved) ",
        )
        self._put(
            screen,
            separator + 1,
            f"Conversation: {snapshot.get('conversation', 'waiting for wake')}",
        )
        self._put(
            screen,
            separator + 2,
            f"Waiting for review: {snapshot['waiting']}    "
            f"Approved for delivery: {snapshot['approved']}",
        )
        editor = self._editor
        item = snapshot["item"]
        if editor is not None:
            heading = editor.heading
            body = ""
            ready = "Editing; Enter saves, Esc cancels."
            if snapshot.get("revision") != editor.revision:
                warning = "Message changed while editing; Enter will be rejected."
                if self._result != ("warn", warning):
                    self._progress("warn", warning)
        elif item:
            heading = self._heading(item)
            if item.get("edited"):
                heading += " | Edited"
            body = (
                item["text"]
                if item["kind"] == "text"
                else (item.get("transcript") or "Voice message. Press R to read its transcript.")
            )
            if item.get("edited"):
                body = f"{body}\n\n{original_line(item['original'])}"
            ready = (
                ("Approved; T transmits" if approved else "Ready for review")
                if item["kind"] == "text" or item["previewed"]
                else "Read voice transcript before approval"
            )
        else:
            blocked = approved and snapshot["approved"] > 0
            heading = (
                "Approved delivery blocked"
                if blocked
                else "Approved queue empty"
                if approved
                else "Review queue empty"
            )
            body = (
                "Earlier messages need review. Press Tab to Review."
                if blocked
                else "No approved messages waiting."
                if approved
                else "No messages waiting for review."
            )
            ready = ""
        self._put(screen, separator + 4, heading)
        if editor is not None:
            action_lines = ["[Enter] Save [Esc] Cancel [Ctrl+C] Stop"]
        else:
            message_actions = (
                "[R] Read [T] Transmit [D] Deny"
                if approved
                else "[R] Read [E] Edit [A] Approve [T] Transmit [D] Deny"
            )
            session_actions = "[S] Sleep [Ctrl+C] Stop"
            actions = f"{message_actions} {session_actions}"
            action_lines = (
                [actions] if len(actions) <= width - 4 else [message_actions, session_actions]
            )
        ready_row = height - len(action_lines) - 3
        body_rows = ready_row - (separator + 6)
        cursor = None
        if editor is not None:
            lines, cursor_row, cursor_column = editor.layout(width - 4)
            visible = max(0, len(lines) - body_rows)
            self._scroll = min(self._scroll, visible)
            self._scroll = min(max(self._scroll, cursor_row - body_rows + 1), cursor_row)
            self._scroll = min(self._scroll, visible)
            for row, line in enumerate(
                lines[self._scroll : self._scroll + body_rows], separator + 6
            ):
                self._put(screen, row, line, "reply")
            if len(lines) > body_rows:
                end = min(len(lines), self._scroll + body_rows)
                ready += f" | Lines {self._scroll + 1}-{end}/{len(lines)}"
            cursor = (separator + 6 + cursor_row - self._scroll, 2 + cursor_column)
        else:
            key = (approved, snapshot["revision"], body, width)
            if key != self._body_key:
                self._scroll = 0
                self._body_key = key
                self._body = wrap_lines(body, width - 4)
            self._scroll = min(self._scroll, max(0, len(self._body) - body_rows))
            for row, line in enumerate(
                self._body[self._scroll : self._scroll + body_rows], separator + 6
            ):
                self._put(screen, row, line, "reply")
            if len(self._body) > body_rows:
                end = min(len(self._body), self._scroll + body_rows)
                ready += f" | Lines {self._scroll + 1}-{end}/{len(self._body)}; Up/Down, PgUp/PgDn"
            if snapshot.get("dispatching"):
                ready += f" | Delivering item {snapshot['dispatching']['number']}"
        self._put(screen, ready_row, ready)
        with self._lock:
            busy, (kind, result) = self._busy, self._delivery_result or self._result
        self._put(screen, ready_row + 1, result, kind)
        if busy:
            self._put(screen, height - 2, "Working; shortcuts paused | Ctrl+C Stop")
        else:
            for row, line in enumerate(action_lines, ready_row + 2):
                self._put(screen, row, line)
        self._set_cursor_visible(editor is not None)
        if cursor is not None:
            screen.move(*cursor)
        screen.refresh()
        return body_rows
