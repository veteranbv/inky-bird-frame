from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from inky_bird_frame.entrypoint import _RotatingHandler


def _run_log_process(log_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    code = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "import inky_bird_frame.entrypoint as entrypoint\n"
        "entrypoint.LOG_MAX_BYTES = 64\n"
        "entrypoint.LOG_BACKUP_COUNT = 2\n"
        "sys.stdout, sys.stderr = entrypoint._managed_streams(Path(os.environ['TEST_LOG_PATH']))\n"
        f"{body}\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "TEST_LOG_PATH": str(log_path)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_managed_log_rotates_both_streams_and_rebinds_process_descriptors(tmp_path: Path) -> None:
    log_path = tmp_path / "support" / "logs" / "serve.log"
    result = _run_log_process(
        log_path,
        "print('old-' + 'a' * 54)\n"
        "print('new output')\n"
        "os.write(1, b'direct output\\n')\n"
        "print('old-' + 'b' * 54, file=sys.stderr)\n"
        "print('new error', file=sys.stderr)\n"
        "os.write(2, b'direct error\\n')",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert log_path.read_text() == "new output\ndirect output\n"
    assert (log_path.parent / "serve.log.1").read_text() == f"old-{'a' * 54}\n"
    assert (log_path.parent / "serve.error.log").read_text() == "new error\ndirect error\n"
    assert (log_path.parent / "serve.error.log.1").read_text() == f"old-{'b' * 54}\n"
    assert stat.S_IMODE(log_path.parent.stat().st_mode) == 0o700
    for path in log_path.parent.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_managed_log_prunes_older_backups_and_restricts_existing_files(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "refresh.log"
    log_path.parent.mkdir(mode=0o755)
    log_path.write_text("previous run\n")
    log_path.chmod(0o644)
    result = _run_log_process(log_path, "\n".join("print('x' * 60)" for _ in range(5)))

    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in log_path.parent.iterdir()) == [
        "refresh.error.log",
        "refresh.log",
        "refresh.log.1",
        "refresh.log.2",
    ]
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(log_path.parent.stat().st_mode) == 0o700


def test_managed_log_rejects_relative_path(tmp_path: Path) -> None:
    result = _run_log_process(Path("relative.log"), "print('not reached')")
    assert result.returncode != 0
    assert "managed log path must be an absolute .log file" in result.stderr


def test_unmanaged_cli_keeps_terminal_output() -> None:
    environment = {
        key: value for key, value in os.environ.items() if key != "INKY_BIRD_MANAGED_LOG_PATH"
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from inky_bird_frame.entrypoint import main; raise SystemExit(main())",
            "--help",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "usage: inky-bird-frame" in result.stdout
    assert result.stderr == ""


def test_managed_log_write_failure_is_not_silenced(tmp_path: Path) -> None:
    handler = _RotatingHandler(tmp_path / "serve.log", 1)
    record = logging.LogRecord("inky", logging.INFO, "", 0, "message", (), None)
    try:
        with (
            patch.object(handler, "shouldRollover", side_effect=OSError("write unavailable")),
            pytest.raises(OSError, match="write unavailable"),
        ):
            handler.handle(record)
    finally:
        handler.close()
