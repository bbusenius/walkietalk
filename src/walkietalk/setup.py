"""Packaged configuration templates and private, literal credential loading."""

import os
import re
import shlex
import stat
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

from .config import WalkietalkError

MAX_CREDENTIAL_BYTES = 65536


def initialize(directory: Path) -> None:
    """Create a new private setup directory; never replace existing configuration."""
    contents = {
        "config.yaml": files("walkietalk").joinpath("data/config.example.yaml").read_bytes(),
        "credentials.env": files("walkietalk")
        .joinpath("data/credentials.env.example")
        .read_bytes(),
    }
    directory = directory.expanduser()
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        raise WalkietalkError(
            f"Setup directory already exists: {directory}; nothing overwritten. "
            "Choose a new --directory or edit your existing config."
        ) from None
    for name, data in contents.items():
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)


def read_credentials(path: Path, *, required: bool) -> dict[str, str]:
    """Only regular, owner-private files; no shell execution or ambient .env search."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if not required:
            return {}
        raise WalkietalkError(f"Credentials file not found: {path}") from None
    except OSError:
        raise WalkietalkError(
            f"Cannot open credentials file: {path}; use a regular private file"
        ) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise WalkietalkError(
                f"Credentials file must be owned by your user and private: {path}. "
                "Use chmod 600 on a regular file."
            )
        with os.fdopen(fd, "rb") as stream:
            fd = -1  # The stream now owns the descriptor, including read-error cleanup.
            data = stream.read(MAX_CREDENTIAL_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > MAX_CREDENTIAL_BYTES:
        raise WalkietalkError("Credentials file exceeds 64 KiB; contents withheld")
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise WalkietalkError("Credentials file must be UTF-8; contents withheld") from None
    values = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        try:
            if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key in values:
                raise ValueError
            words = shlex.split(value, comments=True, posix=True)
            if len(words) > 1 or any("\x00" in word for word in words):
                raise ValueError
        except ValueError:
            raise WalkietalkError(
                f"Invalid credentials assignment at line {number}; contents withheld. "
                "Use one literal KEY=value per line, with quotes around spaces or #."
            ) from None
        values[key] = words[0] if words else ""
    return values


@contextmanager
def credentials_environment(config: Path | None, explicit: Path | None, *, disabled=False):
    """Load only the selected file and restore the environment when the command ends."""
    values = {}
    if not disabled:
        # Placeholder -c paths must not select credentials.env from the working directory.
        selected = config.expanduser() if config is not None else None
        if explicit is not None:
            path = explicit.expanduser()
        elif selected is not None and selected.is_file():
            path = selected.absolute().parent / "credentials.env"
        else:
            path = None
        if path is not None:
            values = read_credentials(path, required=explicit is not None)
    added = {key: value for key, value in values.items() if key not in os.environ}
    try:
        os.environ.update(added)
        yield
    finally:
        for key in added:
            os.environ.pop(key, None)
