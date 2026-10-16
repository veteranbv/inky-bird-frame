"""Private bounded upload staging and confirmed history imports.

The original file stays private until the owner finishes selecting checklists.
History and a trip are separate commits. Only the history's atomic operation
marker can prove a commit after a process stops before saving its receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, cast
from uuid import uuid4

from .birdnet_analyzer import (
    STATE_FILENAME as ANALYZER_STATE,
)
from .birdnet_analyzer import (
    analyzer_history_revision,
    import_birdnet_analyzer_csv,
)
from .birds import date_range_for_window, resolve_ebird_archive_species
from .catalog import catalog_state_lock
from .config import AppConfig, DiscoveryProvider
from .ebird_archive import (
    MAX_UNCOMPRESSED_BYTES,
    EbirdChecklist,
    ebird_checklists,
    import_ebird_archive,
    preview_ebird_checklists,
    read_ebird_checklists,
)
from .ebird_archive import (
    STATE_FILENAME as EBIRD_STATE,
)
from .http import write_bytes_atomic
from .journal import (
    COLLECTION_ID,
    MAX_JOURNAL_BYTES,
    ChecklistEvidence,
    CollectionKind,
    JournalCollection,
    JournalError,
    JournalMember,
    JournalService,
    MemberOrigin,
    _dates,
    _label,
    _members,
    _revision,
    member_evidence_matches,
    read_journal,
    save_imported_collection,
)
from .owner_operations import OwnerOperationError
from .settings import confirmed_config
from .timeutil import parse_utc_timestamp

# The browser accepts at most the established decoded CSV bound on the wire as
# well. ZIP payloads still have their own independent decoded-size validation.
MAX_UPLOAD_BYTES = MAX_UNCOMPRESSED_BYTES
# One raw file, one replacement history, its old version and parser overhead
# need headroom. Keep four upload ceilings free before admitting an upload.
MIN_IMPORT_FREE_BYTES = 4 * MAX_UPLOAD_BYTES
IMPORT_KINDS = {"ebird-archive", "birdnet-analyzer"}


def _read_private(path: Path, maximum: int) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("permissions")
            content = handle.read(maximum + 1)
        if len(content) > maximum:
            raise ValueError("size")
        return content
    except (OSError, ValueError) as exc:
        raise OwnerOperationError("Private import storage is unavailable or insecure") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _calendar(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OwnerOperationError("Choose a calendar date, or leave the batch undated")
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError("date")
        return result
    except ValueError as exc:
        raise OwnerOperationError("Use calendar dates in YYYY-MM-DD form") from exc


def _live_names(records: tuple[EbirdChecklist, ...], config: AppConfig) -> set[str]:
    window = date_range_for_window(config.discovery.observation_window)
    return {
        species.scientific_name
        for record in records
        if window.start is None
        or window.end is not None
        and window.start <= record.observed_on <= window.end
        for species in record.species
    }


class HistoryImports:
    """Use only inside the shared owner-operation reservation for mutations."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.root = config.controller.state_dir / "owner-import"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise OwnerOperationError("Import staging must be a private directory with mode 0700")
        self.path = self.root / "upload.bin"
        self.metadata_path = self.root / "preview.json"

    def _read(self) -> dict[str, object] | None:
        if not self.metadata_path.exists():
            return None
        try:
            value = json.loads(_read_private(self.metadata_path, MAX_JOURNAL_BYTES))
            if (
                not isinstance(value, dict)
                or value.get("schema_version") != 1
                or not isinstance(value.get("upload_id"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", value["upload_id"])
                or value.get("kind") not in IMPORT_KINDS
                or parse_utc_timestamp(value.get("expires_at")) is None
            ):
                raise ValueError("metadata")
            return cast(dict[str, object], value)
        except (ValueError, TypeError) as exc:
            raise OwnerOperationError(
                "Import preview is invalid. Use operator recovery to inspect private staging; "
                "confirmed history is not changed."
            ) from exc

    def _write(self, value: dict[str, object]) -> None:
        body = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
        if len(body) > MAX_JOURNAL_BYTES:
            raise OwnerOperationError(
                "This preview exceeds the supported size. "
                "Previously confirmed history and collections remain intact."
            )
        write_bytes_atomic(self.metadata_path, body, mode=0o600)

    def _checked(self, upload_id: str) -> dict[str, object]:
        value = self._read()
        if value is None or value["upload_id"] != upload_id:
            raise OwnerOperationError(
                "This import preview is unavailable. Select the original file again."
            )
        expires = cast(datetime, parse_utc_timestamp(value["expires_at"]))
        if datetime.now(UTC) >= expires:
            raise OwnerOperationError(
                "This preview expired. Cancel it and select the original file again. "
                "Confirmed history remains intact."
            )
        if value.get("status") == "receiving":
            raise OwnerOperationError(
                "The file has not finished uploading. Cancel it and upload again."
            )
        self._recover_status(value)
        return value

    def _recover_status(self, value: dict[str, object]) -> None:
        request_id = value.get("commit_key_hash")
        if (
            value.get("status") == "checked"
            and isinstance(request_id, str)
            and recover_import(self.config.controller.state_dir, request_id)
        ):
            value["status"] = "committed"

    def status(self) -> dict[str, object]:
        value = self._read()
        if value is None:
            return {"preview": None}
        self._recover_status(value)
        # Raw locations, file bytes and taxonomy caches never enter this status.
        return {
            "preview": {
                key: value[key]
                for key in (
                    "upload_id",
                    "kind",
                    "status",
                    "expires_at",
                    "observed_on",
                    "preview",
                    "trip",
                    "draft",
                )
                if key in value
            }
        }

    def cancel(self, upload_id: str | None = None, *, uncommitted_only: bool = False) -> None:
        value = self._read()
        if value is None:
            return
        self._recover_status(value)
        if upload_id is not None and value["upload_id"] != upload_id:
            raise OwnerOperationError("This preview changed. Reload before cancelling.")
        if uncommitted_only and value.get("status") == "committed":
            return
        # Exact application-owned temporary files only; confirmed history and
        # journal state are outside this directory and cannot be removed here.
        self.path.unlink(missing_ok=True)
        self.metadata_path.unlink(missing_ok=True)

    def cleanup_expired(self) -> None:
        value = self._read()
        if value is not None and datetime.now(UTC) >= cast(
            datetime, parse_utc_timestamp(value["expires_at"])
        ):
            self.cancel(cast(str, value["upload_id"]))

    def edit_trip(self, upload_id: str) -> None:
        value = self._checked(upload_id)
        if isinstance(value.get("trip"), dict):
            trip = cast(dict[str, object], value.pop("trip"))
            value["draft"] = trip["choices"]
            self._write(value)

    def begin(self, kind: str, *, observed_on: object = None) -> tuple[str, BinaryIO]:
        if kind not in IMPORT_KINDS:
            raise OwnerOperationError("Choose eBird Archive or BirdNET Analyzer")
        chosen = _calendar(observed_on)
        if kind == "ebird-archive" and chosen is not None:
            raise OwnerOperationError("eBird checklists already carry their own calendar dates")
        old = self._read()
        if old is not None:
            if datetime.now(UTC) < cast(datetime, parse_utc_timestamp(old["expires_at"])):
                raise OwnerOperationError(
                    "Finish or cancel the existing preview before uploading another file"
                )
            self.cancel()
        if shutil.disk_usage(self.root).free < MIN_IMPORT_FREE_BYTES:
            raise OwnerOperationError(
                "Import needs at least 2 GiB free for staging, atomic history replacement "
                "and backup. Free space before retrying."
            )
        identifier = uuid4().hex
        current = datetime.now(UTC)
        self._write(
            {
                "schema_version": 1,
                "upload_id": identifier,
                "kind": kind,
                "observed_on": chosen.isoformat() if chosen else None,
                "status": "receiving",
                "expires_at": (
                    current + timedelta(hours=self.config.controller.owner_import_retention_hours)
                ).isoformat(),
            }
        )
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        return identifier, os.fdopen(descriptor, "wb")

    def use_retained_history(self) -> dict[str, object]:
        if self._read() is not None:
            raise OwnerOperationError("Finish or cancel the existing preview first")
        revision, records = read_ebird_checklists(self.config.controller.state_dir)
        if not records:
            raise OwnerOperationError(
                "No retained eBird history is available. "
                "Upload the complete official archive first."
            )
        current = datetime.now(UTC)
        identifier = uuid4().hex
        self._write(
            {
                "schema_version": 1,
                "upload_id": identifier,
                "kind": "ebird-archive",
                "file_source": "retained",
                "observed_on": None,
                "status": "committed",
                "expires_at": (
                    current + timedelta(hours=self.config.controller.owner_import_retention_hours)
                ).isoformat(),
                "preview": {
                    "history_revision": revision,
                    "stats": {
                        "earliest_observed_on": min(
                            record.observed_on for record in records
                        ).isoformat(),
                        "latest_observed_on": max(
                            record.observed_on for record in records
                        ).isoformat(),
                    },
                },
            }
        )
        return {"upload_id": identifier}

    def finish_upload(self, upload_id: str, digest: str, size: int) -> None:
        value = self._read()
        if value is None or value["upload_id"] != upload_id or value["status"] != "receiving":
            raise OwnerOperationError("The upload was cancelled")
        if not 0 < size <= MAX_UPLOAD_BYTES:
            raise OwnerOperationError("Upload is empty or exceeds the 512 MiB wire limit")
        value.update(status="uploaded", digest=digest, size=size)
        self._write(value)

    def _verify_file(self, value: dict[str, object]) -> None:
        if value.get("file_source") == "retained":
            return
        digest = hashlib.sha256()
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) & 0o077
                    or info.st_size != value.get("size")
                    or info.st_size > MAX_UPLOAD_BYTES
                ):
                    raise ValueError("file")
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != value.get("digest"):
                raise ValueError("digest")
        except (OSError, ValueError) as exc:
            raise OwnerOperationError(
                "The staged file changed. Cancel this preview and upload again."
            ) from exc

    def preview(self, upload_id: str, *, revision: str) -> dict[str, object]:
        config = confirmed_config(self.config, revision, load_secrets=False)
        value = self._checked(upload_id)
        if value.get("file_source") == "retained":
            raise OwnerOperationError(
                "Retained history needs no import. Continue to checklist selection."
            )
        self._verify_file(value)
        if value["kind"] == "ebird-archive":
            history_revision, old = read_ebird_checklists(config.controller.state_dir)
            stats = import_ebird_archive(
                self.path,
                config.controller.state_dir,
                allow_history_reduction=True,
                dry_run=True,
                expected_revision=history_revision,
            ).as_dict()
            incoming = preview_ebird_checklists(self.path)
            old_names, new_names = _live_names(old, config), _live_names(incoming, config)
            incoming_links = {
                (record.checklist_hash, species.scientific_name): record.observed_on
                for record in incoming
                for species in record.species
            }
            with catalog_state_lock(config.controller.state_dir):
                journal_state = read_journal(config.controller.state_dir)
            affected = [
                group.label
                for group in journal_state.collections
                if any(
                    member.evidence and not member_evidence_matches(group, member, incoming_links)
                    for member in group.members
                )
            ]
            enabled = DiscoveryProvider.EBIRD_ARCHIVE in config.discovery.sources
            impact: dict[str, object] = {
                "journal_revision": journal_state.revision,
                "live_source_enabled": enabled,
                "entering_window": sorted(new_names - old_names),
                "leaving_window": sorted(old_names - new_names),
                "affected_collections": affected,
            }
            unchanged = not any(
                stats[name]
                for name in ("added_checklists", "updated_checklists", "removed_checklists")
            )
        else:
            history_revision = analyzer_history_revision(config.controller.state_dir)
            stats = import_birdnet_analyzer_csv(
                self.path,
                config.controller.state_dir,
                observed_on=_calendar(value["observed_on"]),
                dry_run=True,
                expected_revision=history_revision,
            ).as_dict()
            impact = {
                "live_source_enabled": DiscoveryProvider.BIRDNET_ANALYZER
                in config.discovery.sources,
                "undated_notice": "Undated detections remain undated "
                "and do not enter date-based windows.",
            }
            unchanged = not (stats["imported"] or stats["updated"])
        result = {
            "stats": stats,
            "impact": impact,
            "generation_enabled": config.controller.generation_enabled,
            "unchanged": unchanged,
            "history_revision": history_revision,
            "configuration_revision": revision,
            "notice": "History and trip saving are separate. Existing generation policy "
            "applies after a source refresh. This action does not start a model.",
            "previewed_on": date.today().isoformat(),
        }
        preview_hash = _digest(
            {
                "digest": value["digest"],
                "kind": value["kind"],
                "observed_on": value["observed_on"],
                **result,
            }
        )
        result["preview_hash"] = preview_hash
        value.update(status="checked", preview=result)
        self._write(value)
        return {
            "upload_id": upload_id,
            "message": "File checked. Review history changes before confirming.",
            "unchanged": unchanged,
        }

    def commit(
        self, upload_id: str, *, revision: str, preview_hash: str, key: str, allow_reduction: bool
    ) -> dict[str, object]:
        confirmed_config(self.config, revision, load_secrets=False)
        value = self._checked(upload_id)
        preview = value.get("preview")
        if (
            not isinstance(preview, dict)
            or preview.get("preview_hash") != preview_hash
            or preview.get("configuration_revision") != revision
        ):
            raise OwnerOperationError(
                "Preview this file against current settings before committing"
            )
        if preview.get("previewed_on") != date.today().isoformat():
            raise OwnerOperationError(
                "The calendar day changed. Preview current-window changes again."
            )
        impact = cast(dict[str, object], preview["impact"])
        stats = cast(dict[str, object], preview["stats"])
        if (
            impact.get("affected_collections") or stats.get("removed_checklists")
        ) and not allow_reduction:
            raise OwnerOperationError(
                "Confirm the removal or changed evidence shown in the preview before importing"
            )
        self._verify_file(value)
        request_id = hashlib.sha256(key.encode()).hexdigest()
        value["commit_key_hash"] = request_id
        self._write(value)
        result = recover_import(self.config.controller.state_dir, request_id)
        if result is None:
            expected = cast(str, preview["history_revision"])
            if preview["unchanged"]:
                current = (
                    read_ebird_checklists(self.config.controller.state_dir)[0]
                    if value["kind"] == "ebird-archive"
                    else analyzer_history_revision(self.config.controller.state_dir)
                )
                if current != expected:
                    raise OwnerOperationError("History changed. Check this file again.")
                result = {
                    "message": "No history changes. Continue to checklist selection or finish.",
                    "upload_id": upload_id,
                }
            elif value["kind"] == "ebird-archive":
                ebird_stats = import_ebird_archive(
                    self.path,
                    self.config.controller.state_dir,
                    allow_history_reduction=allow_reduction,
                    expected_revision=expected,
                    operation_id=request_id,
                    expected_journal_revision=cast(int, impact["journal_revision"]),
                )
                result = {
                    "message": "History updated. Trip not saved yet.",
                    "upload_id": upload_id,
                    **ebird_stats.as_dict(),
                }
            else:
                analyzer_stats = import_birdnet_analyzer_csv(
                    self.path,
                    self.config.controller.state_dir,
                    observed_on=_calendar(value["observed_on"]),
                    expected_revision=expected,
                    operation_id=request_id,
                )
                result = {
                    "message": "Analyzer history updated. No collection or wall selection changed.",
                    "upload_id": upload_id,
                    **analyzer_stats.as_dict(),
                }
        value.update(status="committed")
        self._write(value)
        return result

    def checklist_choices(
        self, upload_id: str, *, start_date: object, end_date: object
    ) -> dict[str, object]:
        start, end = _dates(start_date, end_date)
        if start is None or end is None:
            raise JournalError("Choose both inclusive trip dates")
        value = self._checked(upload_id)
        if value["kind"] != "ebird-archive" or value["status"] != "committed":
            raise OwnerOperationError(
                "Confirm the eBird history step before choosing trip checklists"
            )
        self._verify_file(value)
        revision, history = read_ebird_checklists(self.config.controller.state_dir)
        original = (
            {record.checklist_hash: record for record in preview_ebird_checklists(self.path)}
            if value.get("file_source") != "retained"
            else {}
        )
        records = [record for record in history if start <= record.observed_on.isoformat() <= end]
        result: dict[str, object] = {
            "history_revision": revision,
            "start_date": start,
            "end_date": end,
            "checklists": [
                {
                    "key": record.checklist_hash,
                    "observed_on": record.observed_on.isoformat(),
                    "location": original[record.checklist_hash].location
                    if record.checklist_hash in original
                    else None,
                    "species_count": len(record.species),
                }
                for record in records
            ],
        }
        if len(json.dumps(result).encode()) > MAX_JOURNAL_BYTES:
            raise OwnerOperationError(
                "This checklist selection exceeds 1 MiB. Choose a narrower date interval; "
                "nothing was truncated."
            )
        return result

    def _trip_group(
        self,
        *,
        history_revision: str,
        checklist_keys: object,
        start_date: object,
        end_date: object,
        label: object,
        collection_id: object,
        revision: object,
    ) -> JournalCollection:
        expected = _revision(revision)
        start, end = _dates(start_date, end_date)
        if start is None or end is None:
            raise JournalError("Choose both inclusive trip dates")
        if not isinstance(collection_id, str) or not COLLECTION_ID.fullmatch(collection_id):
            raise JournalError("Choose a valid new collection identifier")
        if (
            not isinstance(checklist_keys, list)
            or not checklist_keys
            or any(
                not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key)
                for key in checklist_keys
            )
            or len(checklist_keys) != len(set(checklist_keys))
        ):
            raise JournalError("Choose one or more distinct checklists from this preview")
        current, records = read_ebird_checklists(self.config.controller.state_dir)
        if current != history_revision:
            raise OwnerOperationError("eBird history changed. Choose checklists again.")
        by_key = {record.checklist_hash: record for record in records}
        chosen = []
        for key in checklist_keys:
            record = by_key.get(key)
            if record is None or not start <= record.observed_on.isoformat() <= end:
                raise JournalError("A selected checklist is missing or outside the trip dates")
            chosen.append(record)
        by_name = {
            species.scientific_name: species for record in chosen for species in record.species
        }
        members: dict[tuple[int | None, str], JournalMember] = {}
        for name, species in sorted(by_name.items()):
            resolved, _ = resolve_ebird_archive_species(
                [species],
                self.config.controller.state_dir / "ebird-archive-taxonomy-crosswalk.json",
            )
            identity = resolved[0] if resolved else None
            evidence = tuple(
                ChecklistEvidence(record.checklist_hash, name)
                for record in chosen
                if any(item.scientific_name == name for item in record.species)
            )
            key = (identity.taxon_id, "") if identity else (None, name)
            member = JournalMember(
                identity.taxon_id if identity else None,
                identity.common_name if identity else species.common_name,
                identity.scientific_name if identity else name,
                MemberOrigin.EBIRD,
                evidence,
            )
            previous = members.get(key)
            if previous:
                member = replace(
                    member,
                    evidence=tuple(
                        sorted(
                            set((*previous.evidence, *member.evidence)),
                            key=lambda link: (link.checklist_hash, link.scientific_name),
                        )
                    ),
                )
            members[key] = member
        validated = _members(
            [
                {**asdict(member), "evidence": [asdict(link) for link in member.evidence]}
                for member in members.values()
            ]
        )
        return JournalCollection(
            collection_id,
            _label(label),
            validated,
            CollectionKind.TRIP,
            start,
            end,
            expected + 1,
            expected + 1,
        )

    def preview_trip(self, upload_id: str, **choices: object) -> dict[str, object]:
        value = self._checked(upload_id)
        if value["kind"] != "ebird-archive" or value["status"] != "committed":
            raise OwnerOperationError("Complete the history step first")
        required = {
            "history_revision",
            "checklist_keys",
            "start_date",
            "end_date",
            "label",
            "collection_id",
            "revision",
        }
        if set(choices) != required or not isinstance(choices["history_revision"], str):
            raise JournalError("Review a complete checklist selection")
        group = self._trip_group(
            history_revision=choices["history_revision"],
            checklist_keys=choices["checklist_keys"],
            start_date=choices["start_date"],
            end_date=choices["end_date"],
            label=choices["label"],
            collection_id=choices["collection_id"],
            revision=choices["revision"],
        )
        approved = {item["taxon_id"] for item in JournalService(self.config.controller).catalog()}
        result = {
            "label": group.label,
            "matched": len(group.taxon_ids),
            "ready": sum(id in approved for id in group.taxon_ids),
            "unresolved": group.owner_dict()["unresolved"],
            "preview_hash": _digest(asdict(group)),
            "history_revision": choices["history_revision"],
            "notice": "Save fixes this trip's membership. "
            "It does not queue plates or change the wall.",
        }
        value["trip"] = {"choices": choices, "preview": result}
        self._write(value)
        return {
            "upload_id": upload_id,
            "message": "Trip checked. Review matched and unresolved species before saving.",
        }

    def save_trip(self, upload_id: str, *, preview_hash: str) -> dict[str, object]:
        value = self._checked(upload_id)
        trip = value.get("trip")
        if (
            not isinstance(trip, dict)
            or not isinstance(trip.get("choices"), dict)
            or not isinstance(trip.get("preview"), dict)
            or trip["preview"].get("preview_hash") != preview_hash
        ):
            raise JournalError("Preview this trip before saving")
        choices = trip["choices"]
        group = self._trip_group(**choices)
        if _digest(asdict(group)) != preview_hash:
            raise JournalError("This trip's identities changed. Preview it again.")
        with ebird_checklists(self.config.controller.state_dir) as (revision, _):
            if revision != choices["history_revision"]:
                raise OwnerOperationError("eBird history changed. Preview the trip again.")
            state = save_imported_collection(
                self.config.controller.state_dir, revision=choices["revision"], collection=group
            )
        self.cancel(upload_id)
        return {
            "message": "Trip saved. History, generation work and wall selection stay separate.",
            "collection_id": group.collection_id,
            "revision": state.revision,
        }


def recover_import(state_dir: Path, request_id: str) -> dict[str, object] | None:
    for filename, message, fields in (
        (
            EBIRD_STATE,
            "History updated. Trip not saved yet.",
            {
                "rows",
                "checklists",
                "species",
                "duplicate_rows",
                "added_checklists",
                "updated_checklists",
                "removed_checklists",
                "unchanged_checklists",
                "earliest_observed_on",
                "latest_observed_on",
                "dry_run",
            },
        ),
        (
            ANALYZER_STATE,
            "Analyzer history updated. No collection or wall selection changed.",
            {"rows", "imported", "updated", "duplicates", "dated", "undated", "dry_run"},
        ),
    ):
        path = state_dir / filename
        if not path.exists():
            continue
        try:
            raw = json.loads(_read_private(path, 4 * MAX_UPLOAD_BYTES))
            marker = raw.get("owner_operation")
            if (
                isinstance(marker, dict)
                and marker.get("operation_id") == request_id
                and isinstance(marker.get("result"), dict)
            ):
                stats = marker["result"]
                if set(stats) == fields and all(
                    type(item) in {str, int, bool} for item in stats.values()
                ):
                    return {"message": message, **stats}
        except (ValueError, AttributeError) as exc:
            raise OwnerOperationError(
                "Import commit state is invalid. Inspect history before retrying."
            ) from exc
    return None
