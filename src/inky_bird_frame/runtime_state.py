"""Bounded private state for scheduler activity and explicit retry requests."""

from __future__ import annotations

import fcntl
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from .errors import CatalogError
from .http import write_json_atomic

MAX_STATE_BYTES = 256 * 1024


@contextmanager
def state_lock(path: Path, *, nonblocking: bool = False) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise CatalogError("Runtime state lock is unsafe")
        os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def state_lock_held(path: Path) -> bool:
    """Probe for an exclusive holder without creating a file or waiting."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return False
            try:
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except OSError:
        pass
    return False


def read_state(path: Path) -> dict[str, object] | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATE_BYTES:
                raise ValueError("size or type")
            body = handle.read(MAX_STATE_BYTES + 1)
        if len(body) > MAX_STATE_BYTES:
            raise ValueError("size")
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("object")
        return cast(dict[str, object], value)
    except (ValueError, UnicodeError) as exc:
        raise CatalogError(
            "Runtime state is invalid; inspect retained controller evidence"
        ) from exc


def write_state(path: Path, value: dict[str, object]) -> None:
    if len(json.dumps(value).encode()) > MAX_STATE_BYTES:
        raise CatalogError("Runtime state allowance is full")
    write_json_atomic(path, value, mode=0o600)
