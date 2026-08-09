"""Private history imported from an official eBird personal-data archive."""

from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import stat
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from io import TextIOWrapper
from pathlib import Path
from typing import TextIO

from .birds import DateRange, EbirdArchiveSpecies, ObservationWindow, date_range_for_window
from .errors import DataSourceError
from .http import write_json_atomic

STATE_FILENAME = "ebird-archive-observations.json"
CSV_FILENAME = "MyEBirdData.csv"
REQUIRED_COLUMNS = (
    "Submission ID",
    "Common Name",
    "Scientific Name",
    "Taxonomic Order",
    "Count",
    "State/Province",
    "County",
    "Location ID",
    "Location",
    "Latitude",
    "Longitude",
    "Date",
    "Time",
    "Protocol",
    "Duration (Min)",
    "All Obs Reported",
    "Distance Traveled (km)",
    "Area Covered (ha)",
    "Number of Observers",
    "Breeding Code",
    "Observation Details",
    "Checklist Comments",
    "ML Catalog Numbers",
)
# A streamed 512 MiB ceiling accommodates millions of personal observations while
# bounding decompression and parsing work for an untrusted or damaged archive.
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class EbirdArchiveImportStats:
    rows: int
    checklists: int
    species: int
    duplicate_rows: int
    added_checklists: int
    updated_checklists: int
    removed_checklists: int
    unchanged_checklists: int
    earliest_observed_on: str
    latest_observed_on: str
    dry_run: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "rows": self.rows,
            "checklists": self.checklists,
            "species": self.species,
            "duplicate_rows": self.duplicate_rows,
            "added_checklists": self.added_checklists,
            "updated_checklists": self.updated_checklists,
            "removed_checklists": self.removed_checklists,
            "unchanged_checklists": self.unchanged_checklists,
            "earliest_observed_on": self.earliest_observed_on,
            "latest_observed_on": self.latest_observed_on,
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True)
class EbirdArchiveHistory:
    species: list[EbirdArchiveSpecies]
    total_checklists: int
    total_observations: int
    total_species: int
    selected_observations: int
    excluded_observations: int
    history_started_at: str
    last_imported_at: str
    earliest_observed_on: str
    latest_observed_on: str

    def details(self) -> dict[str, object]:
        return {
            "total_checklists": self.total_checklists,
            "total_observations": self.total_observations,
            "total_species": self.total_species,
            "selected_observations": self.selected_observations,
            "excluded_observations": self.excluded_observations,
            "history_started_at": self.history_started_at,
            "last_imported_at": self.last_imported_at,
            "earliest_observed_on": self.earliest_observed_on,
            "latest_observed_on": self.latest_observed_on,
        }


@dataclass(frozen=True, order=True)
class _StoredSpecies:
    scientific_name: str
    common_name: str

    def as_dict(self) -> dict[str, str]:
        return {
            "common_name": self.common_name,
            "scientific_name": self.scientific_name,
        }


@dataclass(frozen=True)
class _StoredChecklist:
    observed_on: date
    species: tuple[_StoredSpecies, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "observed_on": self.observed_on.isoformat(),
            "species": [item.as_dict() for item in self.species],
        }


@contextmanager
def _history_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "ebird-archive.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _state_path(state_dir: Path) -> Path:
    return state_dir / STATE_FILENAME


def _require_private_file(path: Path) -> None:
    if path.is_symlink():
        raise DataSourceError("Refusing symlinked eBird archive state")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise DataSourceError("eBird archive state must use mode 0600")


def _timestamp(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise DataSourceError(f"Invalid eBird archive {label}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DataSourceError(f"Invalid eBird archive {label}") from exc
    if parsed.tzinfo is None:
        raise DataSourceError(f"Invalid eBird archive {label}")
    return parsed.isoformat()


def _parse_date(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise DataSourceError(f"Invalid eBird archive {label}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise DataSourceError(f"Invalid eBird archive {label}") from exc
    if parsed.isoformat() != value:
        raise DataSourceError(f"Invalid eBird archive {label}")
    return parsed


def _parse_species(value: object) -> _StoredSpecies:
    if not isinstance(value, dict):
        raise DataSourceError("Invalid eBird archive species state")
    common_name = value.get("common_name")
    scientific_name = value.get("scientific_name")
    if (
        not isinstance(common_name, str)
        or not common_name.strip()
        or not isinstance(scientific_name, str)
        or not scientific_name.strip()
    ):
        raise DataSourceError("Invalid eBird archive species state")
    return _StoredSpecies(scientific_name.strip(), common_name.strip())


def _parse_checklist(value: object) -> _StoredChecklist:
    if not isinstance(value, dict):
        raise DataSourceError("Invalid eBird archive checklist state")
    observed_on = _parse_date(value.get("observed_on"), "checklist date")
    raw_species = value.get("species")
    if not isinstance(raw_species, list) or not raw_species:
        raise DataSourceError("Invalid eBird archive checklist state")
    species = tuple(sorted(_parse_species(item) for item in raw_species))
    scientific_names = {item.scientific_name for item in species}
    if len(scientific_names) != len(species):
        raise DataSourceError("Duplicate species in eBird archive checklist state")
    return _StoredChecklist(observed_on, species)


def _read_state(
    path: Path, *, required: bool
) -> tuple[dict[str, _StoredChecklist], str | None, str | None, date | None, date | None]:
    try:
        path.stat()
    except FileNotFoundError:
        if required:
            raise DataSourceError(
                "eBird archive history is missing; import Download My Data first"
            ) from None
        return {}, None, None, None, None
    except OSError as exc:
        raise DataSourceError("Could not read eBird archive state") from exc
    _require_private_file(path)
    try:
        payload: object = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DataSourceError("Invalid eBird archive state") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise DataSourceError("Unsupported eBird archive state")
    raw_checklists = payload.get("checklists")
    if not isinstance(raw_checklists, dict):
        raise DataSourceError("Invalid eBird archive state")
    checklists: dict[str, _StoredChecklist] = {}
    for fingerprint, raw_checklist in raw_checklists.items():
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise DataSourceError("Invalid eBird archive checklist fingerprint")
        checklists[fingerprint] = _parse_checklist(raw_checklist)
    history_started_at = _timestamp(payload.get("history_started_at"), "history start")
    last_imported_at = _timestamp(payload.get("last_imported_at"), "last import")
    earliest = _parse_date(payload.get("earliest_observed_on"), "earliest date")
    latest = _parse_date(payload.get("latest_observed_on"), "latest date")
    actual_dates = [item.observed_on for item in checklists.values()]
    if not actual_dates or earliest != min(actual_dates) or latest != max(actual_dates):
        raise DataSourceError("Invalid eBird archive date range")
    return checklists, history_started_at, last_imported_at, earliest, latest


def _validate_input_size(size: int) -> None:
    if size > MAX_UNCOMPRESSED_BYTES:
        raise DataSourceError("eBird archive exceeds the safe uncompressed size limit")


@contextmanager
def _open_archive_csv(archive_path: Path) -> Iterator[TextIO]:
    try:
        is_zip = zipfile.is_zipfile(archive_path)
    except OSError as exc:
        raise DataSourceError("Could not read eBird archive") from exc
    if not is_zip:
        try:
            _validate_input_size(archive_path.stat().st_size)
            with archive_path.open("r", encoding="utf-8-sig", newline="") as handle:
                yield handle
        except OSError as exc:
            raise DataSourceError("Could not read eBird archive") from exc
        return
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) != 1 or Path(members[0].filename).name != CSV_FILENAME:
                raise DataSourceError(
                    f"Official eBird archive must contain exactly one {CSV_FILENAME}"
                )
            member = members[0]
            member_mode = member.external_attr >> 16
            if (
                member.filename != Path(member.filename).name
                or stat.S_ISLNK(member_mode)
                or member.flag_bits & 0x1
            ):
                raise DataSourceError("Unsafe eBird archive member")
            _validate_input_size(member.file_size)
            with (
                archive.open(member) as raw,
                TextIOWrapper(raw, encoding="utf-8-sig", newline="") as handle,
            ):
                yield handle
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise DataSourceError("Could not read eBird archive") from exc


def _required_text(row: dict[str, str | None], name: str, line: int) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DataSourceError(f"Invalid {name} on eBird archive row {line}")
    return value.strip()


def _parse_archive(
    archive_path: Path,
) -> tuple[dict[str, _StoredChecklist], int, int, date, date]:
    builders: dict[str, tuple[date, dict[str, _StoredSpecies]]] = {}
    rows = duplicate_rows = 0
    with _open_archive_csv(archive_path) as handle:
        try:
            reader = csv.DictReader(handle, strict=True)
            fieldnames = reader.fieldnames or []
            if len(fieldnames) != len(set(fieldnames)):
                raise DataSourceError("eBird archive contains duplicate columns")
            missing = [name for name in REQUIRED_COLUMNS if name not in fieldnames]
            if missing:
                raise DataSourceError(
                    "eBird archive is missing required columns: " + ", ".join(missing)
                )
            for line, row in enumerate(reader, start=2):
                if len(row) != len(fieldnames):
                    raise DataSourceError(f"Malformed eBird archive row {line}")
                rows += 1
                submission_id = _required_text(row, "Submission ID", line)
                common_name = _required_text(row, "Common Name", line)
                scientific_name = _required_text(row, "Scientific Name", line)
                observed_on_text = _required_text(row, "Date", line)
                observed_on = _parse_date(observed_on_text, f"date on row {line}")
                fingerprint = hashlib.sha256(submission_id.encode()).hexdigest()
                checklist = builders.get(fingerprint)
                if checklist is None:
                    species_by_name: dict[str, _StoredSpecies] = {}
                    builders[fingerprint] = (observed_on, species_by_name)
                else:
                    checklist_date, species_by_name = checklist
                    if checklist_date != observed_on:
                        raise DataSourceError(
                            f"Conflicting dates for one eBird checklist on row {line}"
                        )
                species = _StoredSpecies(scientific_name, common_name)
                existing = species_by_name.get(scientific_name)
                if existing is not None:
                    if existing != species:
                        raise DataSourceError(
                            f"Conflicting names for one eBird observation on row {line}"
                        )
                    duplicate_rows += 1
                    continue
                species_by_name[scientific_name] = species
        except csv.Error as exc:
            raise DataSourceError("Invalid eBird archive CSV") from exc
    if rows == 0 or not builders:
        raise DataSourceError("eBird archive contains no observation rows")
    checklists = {
        fingerprint: _StoredChecklist(observed_on, tuple(sorted(species.values())))
        for fingerprint, (observed_on, species) in builders.items()
    }
    dates = [item.observed_on for item in checklists.values()]
    return checklists, rows, duplicate_rows, min(dates), max(dates)


def import_ebird_archive(
    archive_path: Path,
    state_dir: Path,
    *,
    now: datetime | None = None,
    allow_history_reduction: bool = False,
    dry_run: bool = False,
) -> EbirdArchiveImportStats:
    current = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0).isoformat()
    with _history_lock(state_dir):
        existing, history_started_at, _, _, _ = _read_state(_state_path(state_dir), required=False)
        incoming, rows, duplicate_rows, earliest, latest = _parse_archive(archive_path)
        removed = existing.keys() - incoming.keys()
        if removed and not allow_history_reduction:
            raise DataSourceError(
                "eBird archive omits previously imported checklists; rerun with "
                "--allow-history-reduction only after confirming the export is complete"
            )
        added = incoming.keys() - existing.keys()
        common = incoming.keys() & existing.keys()
        updated = {key for key in common if incoming[key] != existing[key]}
        unchanged = common - updated
        unique_species = {
            species.scientific_name
            for checklist in incoming.values()
            for species in checklist.species
        }
        if not dry_run:
            write_json_atomic(
                _state_path(state_dir),
                {
                    "schema_version": 1,
                    "history_started_at": history_started_at or current,
                    "last_imported_at": current,
                    "earliest_observed_on": earliest.isoformat(),
                    "latest_observed_on": latest.isoformat(),
                    "checklists": {
                        fingerprint: checklist.as_dict()
                        for fingerprint, checklist in sorted(incoming.items())
                    },
                },
                mode=0o600,
            )
        return EbirdArchiveImportStats(
            rows=rows,
            checklists=len(incoming),
            species=len(unique_species),
            duplicate_rows=duplicate_rows,
            added_checklists=len(added),
            updated_checklists=len(updated),
            removed_checklists=len(removed),
            unchanged_checklists=len(unchanged),
            earliest_observed_on=earliest.isoformat(),
            latest_observed_on=latest.isoformat(),
            dry_run=dry_run,
        )


def read_ebird_archive_history(
    state_dir: Path,
    *,
    window: ObservationWindow,
    limit: int,
    date_range: DateRange | None = None,
    today: date | None = None,
) -> EbirdArchiveHistory:
    if limit <= 0:
        raise ValueError("eBird archive species_limit must be greater than zero")
    with _history_lock(state_dir):
        checklists, history_started_at, last_imported_at, earliest, latest = _read_state(
            _state_path(state_dir), required=True
        )
    selected_range = date_range or date_range_for_window(window, today)
    counts: dict[str, int] = {}
    latest_names: dict[str, tuple[date, str]] = {}
    excluded_observations = 0
    total_observations = 0
    for checklist in checklists.values():
        total_observations += len(checklist.species)
        selected = selected_range.start is None or (
            selected_range.end is not None
            and selected_range.start <= checklist.observed_on <= selected_range.end
        )
        if not selected:
            excluded_observations += len(checklist.species)
            continue
        for species in checklist.species:
            counts[species.scientific_name] = counts.get(species.scientific_name, 0) + 1
            current_name = latest_names.get(species.scientific_name)
            if current_name is None or checklist.observed_on >= current_name[0]:
                latest_names[species.scientific_name] = (
                    checklist.observed_on,
                    species.common_name,
                )
    selected_species = [
        EbirdArchiveSpecies(latest_names[name][1], name, count) for name, count in counts.items()
    ]
    selected_species.sort(key=lambda item: (-item.observation_count, item.common_name.casefold()))
    return EbirdArchiveHistory(
        species=selected_species[:limit],
        total_checklists=len(checklists),
        total_observations=total_observations,
        total_species=len(
            {
                species.scientific_name
                for checklist in checklists.values()
                for species in checklist.species
            }
        ),
        selected_observations=sum(counts.values()),
        excluded_observations=excluded_observations,
        history_started_at=history_started_at or "",
        last_imported_at=last_imported_at or "",
        earliest_observed_on=earliest.isoformat() if earliest is not None else "",
        latest_observed_on=latest.isoformat() if latest is not None else "",
    )
