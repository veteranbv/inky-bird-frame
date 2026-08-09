"""Private BirdNET Analyzer CSV detection history."""

from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import math
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from .birds import BirdNetAnalyzerSpecies, ObservationWindow, date_range_for_window
from .errors import DataSourceError
from .http import write_json_atomic

STATE_FILENAME = "birdnet-analyzer-detections.json"
REQUIRED_COLUMNS = (
    "Start (s)",
    "End (s)",
    "Scientific name",
    "Common name",
    "Confidence",
    "File",
)


@dataclass(frozen=True)
class BirdNetAnalyzerImportStats:
    rows: int
    imported: int
    updated: int
    duplicates: int
    dated: int
    undated: int
    dry_run: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "rows": self.rows,
            "imported": self.imported,
            "updated": self.updated,
            "duplicates": self.duplicates,
            "dated": self.dated,
            "undated": self.undated,
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True)
class BirdNetAnalyzerHistory:
    species: list[BirdNetAnalyzerSpecies]
    total_detections: int
    dated_detections: int
    undated_detections: int
    selected_detections: int
    excluded_undated: int
    history_started_at: str
    last_imported_at: str

    def details(self) -> dict[str, object]:
        return {
            "total_detections": self.total_detections,
            "dated_detections": self.dated_detections,
            "undated_detections": self.undated_detections,
            "selected_detections": self.selected_detections,
            "excluded_undated": self.excluded_undated,
            "history_started_at": self.history_started_at,
            "last_imported_at": self.last_imported_at,
        }


@dataclass(frozen=True)
class _StoredDetection:
    common_name: str
    scientific_name: str
    observed_on: date | None

    def as_dict(self) -> dict[str, object]:
        return {
            "common_name": self.common_name,
            "scientific_name": self.scientific_name,
            "observed_on": self.observed_on.isoformat() if self.observed_on is not None else None,
        }


@contextmanager
def _history_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "birdnet-analyzer.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _state_path(state_dir: Path) -> Path:
    return state_dir / STATE_FILENAME


def _require_private_file(path: Path) -> None:
    if path.is_symlink():
        raise DataSourceError(f"Refusing symlinked BirdNET Analyzer state: {path}")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise DataSourceError(f"BirdNET Analyzer state must use mode 0600: {path}")


def _timestamp(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise DataSourceError(f"Invalid BirdNET Analyzer {label}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DataSourceError(f"Invalid BirdNET Analyzer {label}") from exc
    if parsed.tzinfo is None:
        raise DataSourceError(f"Invalid BirdNET Analyzer {label}")
    return parsed.isoformat()


def _parse_detection(value: object) -> _StoredDetection:
    if not isinstance(value, dict):
        raise DataSourceError("Invalid BirdNET Analyzer detection state")
    common_name = value.get("common_name")
    scientific_name = value.get("scientific_name")
    raw_observed_on = value.get("observed_on")
    if (
        not isinstance(common_name, str)
        or not common_name.strip()
        or not isinstance(scientific_name, str)
        or not scientific_name.strip()
        or (raw_observed_on is not None and not isinstance(raw_observed_on, str))
    ):
        raise DataSourceError("Invalid BirdNET Analyzer detection state")
    try:
        observed_on = date.fromisoformat(raw_observed_on) if raw_observed_on is not None else None
    except ValueError as exc:
        raise DataSourceError("Invalid BirdNET Analyzer detection date") from exc
    if observed_on is not None and observed_on.isoformat() != raw_observed_on:
        raise DataSourceError("Invalid BirdNET Analyzer detection date")
    return _StoredDetection(common_name.strip(), scientific_name.strip(), observed_on)


def _read_state(
    path: Path, *, required: bool
) -> tuple[dict[str, _StoredDetection], str | None, str | None]:
    try:
        path.stat()
    except FileNotFoundError:
        if required:
            raise DataSourceError(
                "BirdNET Analyzer history is missing; import an Analyzer CSV first"
            ) from None
        return {}, None, None
    _require_private_file(path)
    try:
        payload: object = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DataSourceError(f"Invalid BirdNET Analyzer state: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise DataSourceError(f"Unsupported BirdNET Analyzer state: {path}")
    raw_detections = payload.get("detections")
    if not isinstance(raw_detections, dict):
        raise DataSourceError(f"Invalid BirdNET Analyzer state: {path}")
    detections: dict[str, _StoredDetection] = {}
    for fingerprint, raw_detection in raw_detections.items():
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise DataSourceError("Invalid BirdNET Analyzer detection fingerprint")
        detections[fingerprint] = _parse_detection(raw_detection)
    history_started_at = _timestamp(payload.get("history_started_at"), "history start")
    last_imported_at = _timestamp(payload.get("last_imported_at"), "last import")
    return detections, history_started_at, last_imported_at


def _parse_number(row: dict[str, str | None], name: str, line: int) -> float:
    value = row.get(name)
    try:
        parsed = float(value) if value is not None else math.nan
    except ValueError as exc:
        raise DataSourceError(f"Invalid {name} on BirdNET Analyzer CSV row {line}") from exc
    if not math.isfinite(parsed):
        raise DataSourceError(f"Invalid {name} on BirdNET Analyzer CSV row {line}")
    return parsed


def _parse_csv_row(
    row: dict[str, str | None], line: int, observed_on: date | None
) -> tuple[str, _StoredDetection]:
    start = _parse_number(row, "Start (s)", line)
    end = _parse_number(row, "End (s)", line)
    confidence = _parse_number(row, "Confidence", line)
    common_name = row.get("Common name")
    scientific_name = row.get("Scientific name")
    source_file = row.get("File")
    if (
        start < 0
        or end <= start
        or not 0 <= confidence <= 1
        or not isinstance(common_name, str)
        or not common_name.strip()
        or not isinstance(scientific_name, str)
        or not scientific_name.strip()
        or not isinstance(source_file, str)
        or not source_file.strip()
    ):
        raise DataSourceError(f"Invalid BirdNET Analyzer CSV row {line}")
    identity = json.dumps(
        [source_file.strip(), start.hex(), end.hex()],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    fingerprint = hashlib.sha256(identity).hexdigest()
    return fingerprint, _StoredDetection(common_name.strip(), scientific_name.strip(), observed_on)


def import_birdnet_analyzer_csv(
    csv_path: Path,
    state_dir: Path,
    *,
    observed_on: date | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> BirdNetAnalyzerImportStats:
    current = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0).isoformat()
    with _history_lock(state_dir):
        detections, history_started_at, _ = _read_state(_state_path(state_dir), required=False)
        rows = imported = updated = duplicates = 0
        try:
            handle = csv_path.open("r", encoding="utf-8-sig", newline="")
        except OSError as exc:
            raise DataSourceError("Could not read BirdNET Analyzer CSV") from exc
        with handle:
            try:
                reader = csv.DictReader(handle, strict=True)
                missing = [
                    name for name in REQUIRED_COLUMNS if name not in (reader.fieldnames or ())
                ]
                if missing:
                    raise DataSourceError(
                        "BirdNET Analyzer CSV is missing required columns: " + ", ".join(missing)
                    )
                for line, row in enumerate(reader, start=2):
                    rows += 1
                    fingerprint, detection = _parse_csv_row(row, line, observed_on)
                    existing = detections.get(fingerprint)
                    if (
                        existing is not None
                        and existing.observed_on is not None
                        and detection.observed_on is not None
                        and existing.observed_on != detection.observed_on
                    ):
                        raise DataSourceError(
                            "BirdNET Analyzer segment was already imported with a different date"
                        )
                    if existing is not None and detection.observed_on is None:
                        detection = _StoredDetection(
                            detection.common_name,
                            detection.scientific_name,
                            existing.observed_on,
                        )
                    if existing == detection:
                        duplicates += 1
                        continue
                    detections[fingerprint] = detection
                    if existing is None:
                        imported += 1
                    else:
                        updated += 1
            except csv.Error as exc:
                raise DataSourceError("Invalid BirdNET Analyzer CSV") from exc
        if rows == 0:
            raise DataSourceError("BirdNET Analyzer CSV contains no detection rows")
        dated = sum(item.observed_on is not None for item in detections.values())
        undated = len(detections) - dated
        if not dry_run:
            write_json_atomic(
                _state_path(state_dir),
                {
                    "schema_version": 1,
                    "history_started_at": history_started_at or current,
                    "last_imported_at": current,
                    "detections": {
                        fingerprint: detection.as_dict()
                        for fingerprint, detection in sorted(detections.items())
                    },
                },
                mode=0o600,
            )
        return BirdNetAnalyzerImportStats(
            rows=rows,
            imported=imported,
            updated=updated,
            duplicates=duplicates,
            dated=dated,
            undated=undated,
            dry_run=dry_run,
        )


def read_birdnet_analyzer_history(
    state_dir: Path,
    *,
    window: ObservationWindow,
    limit: int,
    today: date | None = None,
) -> BirdNetAnalyzerHistory:
    if limit <= 0:
        raise ValueError("BirdNET Analyzer species_limit must be greater than zero")
    with _history_lock(state_dir):
        detections, history_started_at, last_imported_at = _read_state(
            _state_path(state_dir), required=True
        )
    selected_range = date_range_for_window(window, today)
    counts: dict[str, int] = {}
    common_names: dict[str, str] = {}
    excluded_undated = 0
    for detection in detections.values():
        if selected_range.start is not None:
            if detection.observed_on is None:
                excluded_undated += 1
                continue
            if selected_range.end is None or not (
                selected_range.start <= detection.observed_on <= selected_range.end
            ):
                continue
        counts[detection.scientific_name] = counts.get(detection.scientific_name, 0) + 1
        common_names[detection.scientific_name] = detection.common_name
    species = [
        BirdNetAnalyzerSpecies(common_names[name], name, count) for name, count in counts.items()
    ]
    species.sort(key=lambda item: (-item.detection_count, item.common_name.casefold()))
    dated = sum(item.observed_on is not None for item in detections.values())
    return BirdNetAnalyzerHistory(
        species=species[:limit],
        total_detections=len(detections),
        dated_detections=dated,
        undated_detections=len(detections) - dated,
        selected_detections=sum(counts.values()),
        excluded_undated=excluded_undated,
        history_started_at=history_started_at or "",
        last_imported_at=last_imported_at or "",
    )
