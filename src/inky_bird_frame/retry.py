"""Durable per-taxon retry scheduling for generation work."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .catalog import utc_now
from .errors import CatalogError
from .http import write_json_atomic


@dataclass(frozen=True)
class RetryRecord:
    taxon_id: int
    attempts: int
    error_type: str
    error: str
    first_failed_at: datetime
    last_failed_at: datetime
    next_attempt_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "taxon_id": self.taxon_id,
            "attempts": self.attempts,
            "error_type": self.error_type,
            "error": self.error,
            "first_failed_at": self.first_failed_at.isoformat(),
            "last_failed_at": self.last_failed_at.isoformat(),
            "next_attempt_at": self.next_attempt_at.isoformat(),
        }


class RetryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._records = self._read()

    def _read(self) -> dict[int, RetryRecord]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text())
        except json.JSONDecodeError as exc:
            raise CatalogError(f"Invalid retry state: {self.path}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise CatalogError(f"Unsupported retry state: {self.path}")
        records = raw.get("records")
        if not isinstance(records, list):
            raise CatalogError(f"Invalid retry state: {self.path}")
        parsed: dict[int, RetryRecord] = {}
        for item in records:
            record = _parse_record(item, self.path)
            if record.taxon_id in parsed:
                raise CatalogError(f"Duplicate taxon in retry state: {self.path}")
            parsed[record.taxon_id] = record
        return parsed

    def get(self, taxon_id: int) -> RetryRecord | None:
        return self._records.get(taxon_id)

    def due(self, taxon_id: int, now: datetime) -> bool:
        record = self.get(taxon_id)
        return record is None or record.next_attempt_at <= now

    def record_failure(
        self,
        taxon_id: int,
        error: Exception,
        *,
        now: datetime,
        initial_minutes: int,
        maximum_minutes: int,
        fixed_minutes: int | None = None,
    ) -> RetryRecord:
        previous = self.get(taxon_id)
        attempts = (previous.attempts if previous is not None else 0) + 1
        delay = (
            fixed_minutes
            if fixed_minutes is not None
            else min(initial_minutes * (2 ** (attempts - 1)), maximum_minutes)
        )
        record = RetryRecord(
            taxon_id=taxon_id,
            attempts=attempts,
            error_type=type(error).__name__,
            error=str(error),
            first_failed_at=previous.first_failed_at if previous is not None else now,
            last_failed_at=now,
            next_attempt_at=now + timedelta(minutes=delay),
        )
        self._records[taxon_id] = record
        self._write()
        return record

    def clear(self, taxon_id: int) -> None:
        if self._records.pop(taxon_id, None) is not None:
            self._write()

    def deferred(self, taxon_ids: set[int], now: datetime) -> list[RetryRecord]:
        return sorted(
            (
                record
                for taxon_id, record in self._records.items()
                if taxon_id in taxon_ids and record.next_attempt_at > now
            ),
            key=lambda record: record.next_attempt_at,
        )

    def outstanding(self, taxon_ids: set[int]) -> list[RetryRecord]:
        return sorted(
            (record for taxon_id, record in self._records.items() if taxon_id in taxon_ids),
            key=lambda record: record.next_attempt_at,
        )

    def records(self) -> list[RetryRecord]:
        return sorted(self._records.values(), key=lambda record: record.next_attempt_at)

    def _write(self) -> None:
        write_json_atomic(
            self.path,
            {
                "schema_version": 1,
                "updated_at": utc_now(),
                "records": [
                    record.as_dict()
                    for record in sorted(self._records.values(), key=lambda item: item.taxon_id)
                ],
            },
        )


def _parse_datetime(value: object, source: Path) -> datetime:
    if not isinstance(value, str):
        raise CatalogError(f"Invalid retry timestamp: {source}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CatalogError(f"Invalid retry timestamp: {source}") from exc
    if parsed.tzinfo is None:
        raise CatalogError(f"Retry timestamp has no timezone: {source}")
    return parsed.astimezone(UTC)


def _parse_record(raw: object, source: Path) -> RetryRecord:
    if not isinstance(raw, dict):
        raise CatalogError(f"Invalid retry record: {source}")
    taxon_id = raw.get("taxon_id")
    attempts = raw.get("attempts")
    error_type = raw.get("error_type")
    error = raw.get("error")
    if (
        not isinstance(taxon_id, int)
        or isinstance(taxon_id, bool)
        or taxon_id <= 0
        or not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or attempts <= 0
        or not isinstance(error_type, str)
        or not error_type
        or not isinstance(error, str)
        or not error
    ):
        raise CatalogError(f"Invalid retry record: {source}")
    return RetryRecord(
        taxon_id=taxon_id,
        attempts=attempts,
        error_type=error_type,
        error=error,
        first_failed_at=_parse_datetime(raw.get("first_failed_at"), source),
        last_failed_at=_parse_datetime(raw.get("last_failed_at"), source),
        next_attempt_at=_parse_datetime(raw.get("next_attempt_at"), source),
    )
