"""Local message review and CLI control. Only the talk loop performs actions."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import select
import socket
import socketserver
import stat
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .audio import Wav
from .config import Config, WalkietalkError
from .term import emit

if TYPE_CHECKING:
    from .messaging import Inbound

OPERATOR_ACTIONS = ("status", "read", "approve", "transmit", "deny", "sleep")
COMMAND_HELP = (
    "Use walkietalk operator status/read/approve/transmit/deny/sleep from another terminal."
)
_DISPLAYED = object()


def _snapshot_view(snapshot: dict, *, approved: bool) -> dict:
    result = dict(snapshot, approved_view=approved)
    if approved:
        result["item"] = result["approved_item"]
        result["revision"] = result["approved_revision"]
    return result


class OperatorReady(Exception):
    """Leave idle capture before processing a local CLI command."""


@dataclass
class ReviewCommand:
    action: str
    revision: int | None
    approved_view: bool = False
    responses: queue.Queue = field(default_factory=queue.Queue)
    cancelled: threading.Event = field(default_factory=threading.Event)


@dataclass(eq=False)
class ReviewItem:
    service: str
    sequence: int
    incoming: Inbound | None = None
    text: str = ""
    audio: Wav | None = None
    transcript: str | None = None

    @property
    def kind(self) -> str:
        return self.incoming.kind if self.incoming else "voice" if self.audio else "text"

    @property
    def previewed(self) -> bool:
        """Approval needs readable content, whether captured already or obtained by Read."""
        return self.kind == "text" or bool(self.transcript and self.transcript.strip())


class OperatorQueue:
    """One review FIFO; approved inbound items remain in the bridge's service queues."""

    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.Lock()
        self._waiting: list[ReviewItem] = []
        self._incoming: dict[tuple[str, str], ReviewItem] = {}
        self._approved: set[tuple[str, str]] = set()
        self._approved_revision = 0
        self._approved_display = (None, None, False)
        self._dispatch: tuple[str, str] | None = None
        self._commands: deque[ReviewCommand] = deque()
        self._active_command: ReviewCommand | None = None
        self._sequence = 0
        self._revision = 0
        self._review_display = (None, None)
        self._announced = -1
        self._displayed: int | None = None
        self.status_provider: Callable[[], str] | None = None
        self._log_lock = threading.Lock()
        self._logs: deque[tuple[str, str]] = deque(maxlen=500)
        self._log_revision = 0

    def record_log(self, kind: str, message: str) -> None:
        with self._log_lock:
            for line in message.splitlines():
                # A changing idle meter occupies one line, keeping radio events visible.
                if line.startswith("RMS ") and self._logs and self._logs[-1][1].startswith("RMS "):
                    self._logs.pop()
                self._logs.append((kind, line))
                self._log_revision += 1

    def recent_logs(self) -> list[tuple[str, str]]:
        with self._log_lock:
            return list(self._logs)

    def changed_logs(self, revision: int | None) -> tuple[int, list[tuple[str, str]] | None]:
        """Copy logs only when their contents changed, including meter replacements."""
        with self._log_lock:
            return self._log_revision, (
                list(self._logs) if revision != self._log_revision else None
            )

    def mark_previewed(self, item: ReviewItem, transcript: str | None = None) -> None:
        with self._lock:
            item.transcript = transcript
            self._update_revisions()

    def add(self, service: str, *, incoming=None, text="", audio=None) -> ReviewItem:
        with self._lock:
            self._sequence += 1
            item = ReviewItem(service, self._sequence, incoming, text, audio)
            if incoming is None and audio is not None and text.strip():
                item.transcript = text.strip()
            if incoming:
                self._incoming[(incoming.service, incoming.identity)] = item
            self._waiting.append(item)
            self._update_revisions()
            return item

    def announce(self) -> None:
        messages = []
        with self._lock:
            if self._announced == self._revision:
                # A new arrival may have filled a previously empty line.
                if self._displayed is not None or not self._waiting:
                    return
            self._announced = self._revision
            if not self._waiting:
                self._displayed = None
                messages.append(("status", "Operator review queue empty."))
            else:
                item = self._waiting[0]
                mode = getattr(self.config, item.service)
                direction = "incoming" if item.incoming else "outgoing"
                alias = mode.sender_alias or mode.wake
                messages.append(
                    (
                        "status",
                        f"Operator review: {direction} {item.service}, {alias}, {item.kind}.",
                    )
                )
                if item.kind == "text":
                    messages.append(("reply", item.incoming.text if item.incoming else item.text))
                elif item.previewed:
                    messages.append(("reply", f"Voice transcript: {item.transcript}"))
                else:
                    messages.append(
                        ("status", "Voice message: read its transcript before approval.")
                    )
                self._displayed = self._revision
        # Output can block; bridge polling must still be able to add messages.
        for kind, message in messages:
            emit(kind, message)

    def _approved_head(self) -> ReviewItem | None:
        heads = {}
        for item in self._incoming.values():
            if item.service not in heads or item.sequence < heads[item.service].sequence:
                heads[item.service] = item
        return min(
            (
                item
                for service, item in heads.items()
                if (service, item.incoming.identity) in self._approved
            ),
            key=lambda item: item.sequence,
            default=None,
        )

    def _update_revisions(self, *, retry: ReviewItem | None = None) -> None:
        """Bind commands to each view's content, including fresh attempts after failure."""
        head = self._waiting[0] if self._waiting else None
        display = (head, head.transcript if head else None)
        if display != self._review_display or (head is not None and head is retry):
            self._revision += 1
            self._displayed = None
            self._review_display = display
        head = self._approved_head()
        dispatched = head is not None and self._dispatch == (head.service, head.incoming.identity)
        display = (head, head.transcript if head else None, dispatched)
        if display != self._approved_display:
            self._approved_revision += 1
            self._approved_display = display

    def _describe(self, item: ReviewItem | None) -> dict | None:
        if item is None:
            return None
        mode = getattr(self.config, item.service)
        return {
            "number": item.sequence,
            "direction": "incoming" if item.incoming else "outgoing",
            "service": item.service,
            "alias": mode.sender_alias or mode.wake,
            "kind": item.kind,
            "text": item.incoming.text if item.incoming else item.text,
            "previewed": item.previewed,
            "transcript": item.transcript,
        }

    def snapshot(self, *, approved: bool = False) -> dict:
        with self._lock:
            head = self._waiting[0] if self._waiting else None
            approved_head = self._approved_head()
            result = {
                "waiting": len(self._waiting),
                "approved": len(self._approved),
                "item": self._describe(head),
                "revision": self._revision if head else None,
                "approved_item": self._describe(approved_head),
                "approved_revision": self._approved_revision if approved_head else None,
                "approved_view": approved,
                "dispatching": self._describe(self._incoming.get(self._dispatch)),
            }
        if self.status_provider is not None:
            result["conversation"] = self.status_provider()
        return _snapshot_view(result, approved=approved)

    def submit(self, line: str, *, revision=_DISPLAYED, approved: bool = False) -> ReviewCommand:
        with self._lock:
            displayed = self._approved_revision if approved else self._displayed
            command = ReviewCommand(
                line.strip().casefold(),
                displayed if revision is _DISPLAYED else revision,
                approved,
            )
            self._commands.append(command)
            return command

    def has_commands(self) -> bool:
        with self._lock:
            return bool(self._commands)

    def next_command(self) -> tuple[str, ReviewItem | None, bool]:
        with self._lock:
            command = self._commands.popleft()
            self._active_command = command
            if command.action == "sleep":
                # Session controls are independent of either message view/revision.
                return command.action, None, command.cancelled.is_set()
            revision = command.revision
            current = self._approved_revision if command.approved_view else self._revision
            stale = command.cancelled.is_set() or (revision is not None and revision != current)
            head = (
                self._approved_head()
                if command.approved_view
                else (self._waiting[0] if self._waiting else None)
            )
            item = head if revision == current else None
            return command.action, item, stale

    def respond(self, kind: str, message: str) -> None:
        with self._lock:
            if self._active_command is not None:
                self._active_command.responses.put({"kind": kind, "message": message})

    def complete_command(self, ok: bool) -> None:
        with self._lock:
            if self._active_command is not None:
                self._active_command.responses.put({"done": True, "ok": ok})
                self._active_command = None

    def close_commands(self) -> None:
        with self._lock:
            commands = list(self._commands)
            if self._active_command is not None:
                commands.append(self._active_command)
                self._active_command = None
            for command in commands:
                command.cancelled.set()
                command.responses.put({"done": True, "ok": False, "message": "Talk stopped."})
            self._commands.clear()

    def finish(self, item: ReviewItem) -> None:
        with self._lock:
            assert self._waiting[0] is item
            self._waiting.pop(0)
            if item.incoming:
                self._approved.add((item.incoming.service, item.incoming.identity))
            self._update_revisions()

    def waiting(self, item: ReviewItem) -> bool:
        with self._lock:
            return item in self._waiting

    def dispatch(self, item: ReviewItem) -> None:
        """Grant one incoming message delivery without changing the conversation."""
        with self._lock:
            if self._dispatch is not None:
                raise WalkietalkError(
                    "An operator delivery is already pending; wait for it to finish"
                )
            key = (item.incoming.service, item.incoming.identity)
            assert key in self._approved
            self._dispatch = key
            self._update_revisions()

    def dispatch_service(self) -> str:
        with self._lock:
            return self._dispatch[0] if self._dispatch is not None else ""

    def dispatched(self, incoming: Inbound | None) -> bool:
        with self._lock:
            return incoming is not None and self._dispatch == (incoming.service, incoming.identity)

    def cancel_dispatch(self) -> bool:
        with self._lock:
            if self._dispatch is None:
                return False
            self._dispatch = None
            self._update_revisions()
            return True

    def approved(self, item: Inbound) -> bool:
        with self._lock:
            return (item.service, item.identity) in self._approved

    def hold(self, incoming: Inbound) -> None:
        """A failed approved delivery requires a new command, even for the same item."""
        with self._lock:
            key = (incoming.service, incoming.identity)
            self._approved.discard(key)
            if self._dispatch == key:
                self._dispatch = None
            item = self._incoming[key]
            if item not in self._waiting:
                self._waiting.append(item)
                self._waiting.sort(key=lambda waiting: waiting.sequence)
            self._update_revisions(retry=item)

    def forget(self, incoming: Inbound) -> None:
        with self._lock:
            key = (incoming.service, incoming.identity)
            self._approved.discard(key)
            if self._dispatch == key:
                self._dispatch = None
            item = self._incoming.pop(key, None)
            if item in self._waiting:
                self._waiting.remove(item)
            self._update_revisions()

    def failed_send(self) -> None:
        with self._lock:
            if self._waiting:
                self._update_revisions(retry=self._waiting[0])


def operator_directory() -> Path:
    if _OperatorSocketServer is None or not hasattr(socket, "AF_UNIX") or not hasattr(os, "getuid"):
        raise WalkietalkError("Operator mode requires Unix domain sockets")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path(f"/run/user/{os.getuid()}")
    if runtime or base.exists():
        try:
            info = base.lstat()
        except OSError as exc:
            raise WalkietalkError(f"Cannot use operator runtime directory: {exc}") from exc
        if (
            not base.is_absolute()
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise WalkietalkError("Operator runtime directory must be private and owned by you")
        directory = base / "walkietalk-operator"
    else:
        # Stable across TMPDIR and systemd PrivateTmp, including headless sessions.
        directory = Path.home() / ".cache" / "walkietalk" / "operator"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise WalkietalkError("Operator runtime directory is not owned by the current user")
    directory.chmod(0o700)
    return directory


def operator_socket(config_path: Path) -> Path:
    directory = operator_directory()
    identity = hashlib.sha256(str(config_path.expanduser().resolve()).encode()).hexdigest()[:24]
    return directory / f"{identity}.sock"


def _send_response(connection: socket.socket, response: dict) -> None:
    connection.sendall(json.dumps(response).encode() + b"\n")


def _read_response(connection: socket.socket, stream, deadline: float) -> dict:
    """Read one complete JSON response, including unrestricted review content."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    connection.settimeout(remaining)
    line = stream.readline()
    if not line.endswith(b"\n"):
        raise WalkietalkError("Operator connection closed; check status before retrying.")
    response = json.loads(line)
    if not isinstance(response, dict):
        raise WalkietalkError("Invalid response from operator controls")
    return response


class _OperatorHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        control = self.server.control
        command = None
        try:
            self.connection.settimeout(5)
            snapshot = control.review.snapshot()
            _send_response(self.connection, {"snapshot": snapshot})
            line = self.rfile.readline(1025)
            if not line.endswith(b"\n") or len(line) > 1024:
                return
            request = json.loads(line)
            action = request.get("action") if isinstance(request, dict) else None
            approved = request.get("approved", False) if isinstance(request, dict) else False
            if (
                not isinstance(action, str)
                or action not in OPERATOR_ACTIONS
                or type(approved) is not bool
            ):
                _send_response(
                    self.connection, {"done": True, "ok": False, "message": COMMAND_HELP}
                )
                return
            snapshot = _snapshot_view(snapshot, approved=approved)
            if action == "status":
                _send_response(self.connection, {"done": True, "ok": True})
                return
            revision = None if action == "sleep" else request.get("revision", snapshot["revision"])
            if (
                action != "sleep"
                and "revision" in request
                and (type(revision) is not int or revision != snapshot["revision"])
            ):
                _send_response(
                    self.connection,
                    {
                        "done": True,
                        "ok": False,
                        "message": "Displayed item changed; review it again.",
                    },
                )
                return
            if action != "sleep" and snapshot["item"] is None:
                _send_response(
                    self.connection,
                    {
                        "done": True,
                        "ok": False,
                        "message": "No approved incoming message waiting for delivery."
                        if approved
                        else "No message waiting for operator review.",
                    },
                )
                return
            command = control.review.submit(action, revision=revision, approved=approved)
            _send_response(
                self.connection,
                {"kind": "status", "message": "Command queued; waiting for the radio to be idle."},
            )
            while not control.closed.is_set():
                if select.select([self.connection], [], [], 0)[0]:
                    # The client sends one request only. EOF cancels an unstarted command.
                    return
                try:
                    response = command.responses.get(timeout=0.1)
                except queue.Empty:
                    continue
                _send_response(self.connection, response)
                if response.get("done"):
                    return
            _send_response(self.connection, {"done": True, "ok": False, "message": "Talk stopped."})
        except (OSError, ValueError):
            return
        finally:
            if command is not None:
                command.cancelled.set()


if hasattr(socketserver, "UnixStreamServer"):

    class _OperatorSocketServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
        block_on_close = False

else:
    _OperatorSocketServer = None


class OperatorServer:
    """Accept local CLI requests; capture/TX and every action stay in the talk loop."""

    def __init__(self, review: OperatorQueue | None, config_path: Path):
        self.review = review
        self.path = operator_socket(config_path)
        self.closed = threading.Event()
        self._lock_file = None
        self._server = None
        self._thread = None
        self._bound = False

    def acquire(self) -> None:
        """Reserve this config before starting any messaging processes."""
        import fcntl

        if self._lock_file is not None:
            return
        lock_file = self.path.with_suffix(".lock").open("a")
        try:
            os.fchmod(lock_file.fileno(), 0o600)
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            raise WalkietalkError("Operator mode is already running for this config") from None
        except BaseException:
            lock_file.close()
            raise
        self._lock_file = lock_file

    def start(self) -> None:
        if self.review is None:
            raise WalkietalkError("Operator review queue is not ready")
        self.acquire()
        try:
            if self.path.exists() or self.path.is_symlink():
                info = self.path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise WalkietalkError("Operator socket path contains an unexpected file")
                self.path.unlink()  # The instance lock proves this socket is stale.
            self._server = _OperatorSocketServer(
                str(self.path), _OperatorHandler, bind_and_activate=False
            )
            self._server.control = self
            self._server.server_bind()
            self._bound = True
            self.path.chmod(0o600)
            self._server.server_activate()
            self._thread = threading.Thread(target=self._serve, name="operator-cli", daemon=True)
            self._thread.start()
        except BaseException:
            self.close()
            raise

    def _serve(self) -> None:
        try:
            self._server.serve_forever(poll_interval=0.1)
        finally:
            self.closed.set()

    def close(self) -> None:
        self.closed.set()
        if self.review is not None:
            self.review.close_commands()
        if self._server is not None:
            if self._thread is not None:
                self._server.shutdown()
                self._thread.join(timeout=1)
            self._server.server_close()
        if self._bound:
            self.path.unlink(missing_ok=True)
            self._bound = False
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None


def _operator_connection(config_path: Path | None, timeout: float) -> socket.socket:
    if config_path is not None:
        candidates = [operator_socket(config_path)]
    else:
        candidates = sorted(operator_directory().glob("*.sock"))
    connections = []
    try:
        for path in candidates:
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                continue
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(min(timeout, 5))
            try:
                connection.connect(str(path))
            except (FileNotFoundError, ConnectionRefusedError):
                connection.close()
                continue
            except BaseException:
                connection.close()
                raise
            connections.append(connection)
        if not connections:
            target = " for this config" if config_path is not None else ""
            raise WalkietalkError(
                f"No operator-mode talk instance is running{target}. "
                "Start talk --capture with messaging.operator_mode: true."
            )
        if len(connections) > 1:
            raise WalkietalkError(
                "More than one operator-mode talk instance is running. "
                "Use --config to select the one to control."
            )
        return connections.pop()
    finally:
        for connection in connections:
            connection.close()


def operator_request(
    config_path: Path | None,
    action: str,
    *,
    timeout: float = 120,
    log=emit,
    revision=_DISPLAYED,
    show_snapshot: bool = True,
    approved: bool = False,
) -> bool:
    """Print the review head and stream the result from the running talk instance."""
    deadline = time.monotonic() + timeout
    with _operator_connection(config_path, timeout) as connection:
        with connection.makefile("rb") as response_file:
            try:
                snapshot = _read_response(connection, response_file, deadline)["snapshot"]
                snapshot = _snapshot_view(snapshot, approved=approved and action != "sleep")
                item = snapshot["item"]
                if show_snapshot:
                    log(
                        "status",
                        f"Review queue: {snapshot['waiting']} waiting; "
                        f"{snapshot['approved']} approved for radio delivery.",
                    )
                    if snapshot.get("conversation"):
                        log("status", f"Conversation: {snapshot['conversation']}")
                    if item and action != "sleep":
                        log(
                            "status",
                            f"Item {item['number']}: {item['direction']} {item['service']}, "
                            f"{item['alias']}, {item['kind']}.",
                        )
                        if item["kind"] == "text":
                            log("reply", item["text"])
                        else:
                            log(
                                "status",
                                "Voice transcript ready."
                                if item["previewed"]
                                else "Voice message: run operator read before approval.",
                            )
                            if item.get("transcript"):
                                log("reply", f"Voice transcript: {item['transcript']}")
                    elif action != "sleep":
                        log(
                            "status",
                            "Approved incoming queue empty."
                            if approved
                            else "Operator review queue empty.",
                        )
                request = {"action": action}
                if approved and action != "sleep":
                    request["approved"] = True
                if revision is not _DISPLAYED and action != "sleep":
                    request["revision"] = revision
                _send_response(connection, request)
                while True:
                    response = _read_response(connection, response_file, deadline)
                    if response.get("message"):
                        log(response.get("kind", "status"), response["message"])
                    if response.get("done"):
                        return response["ok"] is True
            except TimeoutError:
                raise WalkietalkError(
                    "Operator command timed out. It may already have begun; check operator status "
                    "before retrying, or use --timeout for a longer wait."
                ) from None
            except (KeyError, ValueError, TypeError):
                raise WalkietalkError("Invalid response from operator controls") from None
