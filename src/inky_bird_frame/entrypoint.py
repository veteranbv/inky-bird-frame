"""Process entry point for bounded logs in managed macOS LaunchAgents."""

from __future__ import annotations

import io
import logging
import logging.handlers
import os
import sys
from pathlib import Path

LOG_PATH_ENV = "INKY_BIRD_MANAGED_LOG_PATH"
LOG_MAX_BYTES = 16 * 1024 * 1024
LOG_BACKUP_COUNT = 3


class _RotatingStream(io.TextIOBase):
    def __init__(self, path: Path, descriptor: int) -> None:
        self._descriptor = descriptor
        self._handler = _RotatingHandler(path, descriptor)
        self._handler.terminator = ""
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._handler.bind_descriptor()

    def write(self, value: str) -> int:
        if not isinstance(value, str):
            raise TypeError("log streams accept text only")
        if value:
            self._handler.handle(logging.LogRecord("inky", logging.INFO, "", 0, value, (), None))
        return len(value)

    def flush(self) -> None:
        self._handler.flush()

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._descriptor


class _RotatingHandler(logging.handlers.RotatingFileHandler):
    def __init__(self, path: Path, descriptor: int) -> None:
        self._descriptor = descriptor
        super().__init__(
            path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
        )
        os.chmod(path, 0o600)

    def bind_descriptor(self) -> None:
        if self.stream is None:
            raise OSError("managed log stream is closed")
        os.dup2(self.stream.fileno(), self._descriptor)

    def doRollover(self) -> None:  # noqa: N802 - logging.handlers API
        super().doRollover()
        self.bind_descriptor()

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - logging API
        error = sys.exc_info()[1]
        if error is not None:
            raise error
        raise OSError("managed log write failed")


def _managed_streams(path: Path) -> tuple[_RotatingStream, _RotatingStream]:
    if not path.is_absolute() or path.suffix != ".log":
        raise ValueError("managed log path must be an absolute .log file")
    error_path = path.with_name(f"{path.stem}.error.log")
    os.umask(0o077)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    stdout = _RotatingStream(path, 1)
    stderr = _RotatingStream(error_path, 2)
    return stdout, stderr


def main() -> int:
    path = os.environ.get(LOG_PATH_ENV)
    if path:
        sys.stdout, sys.stderr = _managed_streams(Path(path))
    from inky_bird_frame.cli import main as cli_main

    return cli_main()
