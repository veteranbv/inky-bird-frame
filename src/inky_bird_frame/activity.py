"""Secret-free runtime observations, not job authority or a generation trigger."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

from .catalog import utc_now
from .errors import CatalogError
from .runtime_state import read_state, state_lock, write_state
from .timeutil import parse_utc_timestamp

_current: ContextVar[Activity | None] = ContextVar("controller_activity", default=None)
_FIELDS = {
    "run_id",
    "status",
    "first_started_at",
    "started_at",
    "updated_at",
    "finished_at",
    "last_success_at",
    "next_run_at",
    "stage",
    "taxon_id",
    "common_name",
    "attempt",
    "species_count",
    "unresolved_count",
}
_KEYS = {
    "generation",
    "refresh",
    "job-refresh",
    "job-generate",
    "job-catalog-publish",
    "job-notifications",
    *{
        f"source-{name}"
        for name in (
            "inaturalist",
            "ebird",
            "ebird-archive",
            "birdweather",
            "birdbuddy",
            "birdnet-go",
            "birdnet-analyzer",
        )
    },
}


def _rows(state_dir: Path) -> dict[str, dict[str, object]]:
    raw = read_state(state_dir / "activity.json")
    if raw is None:
        return {}
    rows = raw.get("entries")
    if raw.get("schema_version") != 1 or not isinstance(rows, dict) or set(rows) - _KEYS:
        raise CatalogError("Activity state is invalid")
    for row in rows.values():
        if not isinstance(row, dict) or set(row) - _FIELDS:
            raise CatalogError("Activity record is invalid")
        for field, value in row.items():
            if value is None:
                continue
            if field in {"taxon_id", "attempt", "species_count", "unresolved_count"}:
                valid = type(value) is int and value >= 0
            elif field.endswith("_at"):
                valid = parse_utc_timestamp(value) is not None
            else:
                valid = isinstance(value, str) and len(value) <= 200
            if not valid:
                raise CatalogError("Activity field is invalid")
    return cast(dict[str, dict[str, object]], rows)


def _write(state_dir: Path, rows: dict[str, dict[str, object]]) -> None:
    write_state(state_dir / "activity.json", {"schema_version": 1, "entries": rows})


class Activity:
    def __init__(self, state_dir: Path | None, key: str) -> None:
        self.state_dir = state_dir
        self.key = key
        self.run_id = uuid4().hex
        self.succeeded = True

    def update(self, **values: object) -> None:
        if self.state_dir is None:
            return
        # Telemetry failure must not change valid catalog or generation outcomes.
        with suppress(OSError, CatalogError), state_lock(self.state_dir / "activity.lock"):
            rows = _rows(self.state_dir)
            row = rows.get(self.key, {})
            if row.get("run_id") != self.run_id:
                return
            row.update(values, updated_at=utc_now())
            rows[self.key] = row
            _write(self.state_dir, rows)


def progress(stage: str, **values: object) -> None:
    activity = _current.get()
    if activity is not None:
        activity.update(stage=stage, **values)


@contextmanager
def activity_run(state_dir: Path | None, key: str) -> Iterator[Activity]:
    if key not in _KEYS:
        raise ValueError("Unknown activity")
    activity = Activity(state_dir, key)
    if state_dir is not None:
        with suppress(OSError, CatalogError), state_lock(state_dir / "activity.lock"):
            rows = _rows(state_dir)
            old = rows.get(key, {})
            now = utc_now()
            rows[key] = {
                "run_id": activity.run_id,
                "status": "running",
                "stage": "starting",
                "first_started_at": old.get("first_started_at") or now,
                "started_at": now,
                "updated_at": now,
                "last_success_at": old.get("last_success_at"),
                "finished_at": old.get("finished_at"),
            }
            _write(state_dir, rows)
    stop = threading.Event()

    def heartbeat() -> None:
        while not stop.wait(10):
            activity.update()

    thread = threading.Thread(target=heartbeat, daemon=True)
    if state_dir is not None:
        thread.start()
    token = _current.set(activity)
    try:
        yield activity
    except BaseException:
        activity.succeeded = False
        raise
    finally:
        _current.reset(token)
        stop.set()
        if state_dir is not None:
            thread.join(timeout=2)
        now = utc_now()
        activity.update(
            status="succeeded" if activity.succeeded else "failed",
            finished_at=now,
            **({"last_success_at": now} if activity.succeeded else {}),
        )


def next_job_run(state_dir: Path, name: str, seconds: float) -> None:
    key = "job-" + name
    if key not in _KEYS:
        return
    with suppress(OSError, CatalogError), state_lock(state_dir / "activity.lock"):
        rows = _rows(state_dir)
        row = rows.setdefault(key, {})
        row["next_run_at"] = (datetime.now(UTC) + timedelta(seconds=max(0, seconds))).isoformat()
        _write(state_dir, rows)


def read_activity(state_dir: Path) -> dict[str, object]:
    try:
        rows = _rows(state_dir)
    except (OSError, CatalogError):
        return {"available": False, "entries": {}}
    now = datetime.now(UTC)
    for row in rows.values():
        updated = parse_utc_timestamp(row.get("updated_at"))
        if row.get("status") == "running" and (
            updated is None or not timedelta(seconds=-5) <= now - updated <= timedelta(seconds=35)
        ):
            row["status"] = "unknown"
        row.pop("run_id", None)
    return {"available": True, "entries": rows}
