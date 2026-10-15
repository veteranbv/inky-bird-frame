"""Private journal selections and allowlisted owner projections.

Selections are not observations. This module never writes discovery, the legacy
Saved birds collection, generation work, the public catalog or panel telemetry.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import cast
from uuid import uuid4

from .catalog import catalog_state_lock, read_collection, read_json
from .codex_runner import parse_species_profile
from .config import AppConfig, ControllerConfig, DiscoveryProvider
from .controller import current_discovery_species, read_generation_queue, read_generation_work
from .errors import CatalogError, DataSourceError, GenerationError, InkyBirdFrameError
from .http import write_bytes_atomic
from .notifications import notification_status
from .server import featured_plate_entry, project_catalog
from .settings import current_config
from .timeutil import parse_utc_timestamp

# Larger than the HTTP edit bound, to hold many small independently edited groups.
MAX_JOURNAL_BYTES = 1024 * 1024
MAX_LABEL_CHARACTERS = 128
COLLECTION_ID = re.compile(r"[0-9a-f]{32}\Z")


class JournalError(InkyBirdFrameError):
    """An owner-safe journal error; never includes a local path or raw payload."""


class JournalConflictError(JournalError):
    """The owner edited a stale revision."""


@dataclass(frozen=True)
class ChecklistEvidence:
    checklist_hash: str
    scientific_name: str


class MemberOrigin(StrEnum):
    SAVED = "saved-selection"
    EBIRD = "ebird-checklist"
    COMMUNITY = "community-report"


@dataclass(frozen=True)
class JournalMember:
    taxon_id: int | None
    common_name: str = ""
    scientific_name: str = ""
    origin: MemberOrigin = MemberOrigin.SAVED
    evidence: tuple[ChecklistEvidence, ...] = ()


class CollectionKind(StrEnum):
    SELECTION = "selection"
    FAVORITES = "favorites"
    TRIP = "personal-trip"
    COMMUNITY = "community-history"
    IMPORTED = "imported-selection"


@dataclass(frozen=True)
class JournalCollection:
    collection_id: str
    label: str
    members: tuple[JournalMember, ...]
    kind: CollectionKind = CollectionKind.SELECTION
    start_date: str | None = None
    end_date: str | None = None
    created_revision: int = 0
    updated_revision: int = 0

    @property
    def taxon_ids(self) -> tuple[int, ...]:
        return tuple(member.taxon_id for member in self.members if member.taxon_id is not None)

    def owner_dict(self) -> dict[str, object]:
        return {
            "collection_id": self.collection_id,
            "label": self.label,
            "taxon_ids": list(self.taxon_ids),
            "kind": self.kind.value,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "created_revision": self.created_revision,
            "updated_revision": self.updated_revision,
            "member_origins": [
                {
                    "taxon_id": member.taxon_id,
                    "scientific_name": member.scientific_name,
                    "origin": member.origin.value,
                }
                for member in self.members
            ],
            "unresolved": [
                {"common_name": member.common_name, "scientific_name": member.scientific_name}
                for member in self.members
                if member.taxon_id is None
            ],
        }


@dataclass(frozen=True)
class JournalState:
    revision: int = 0
    collections: tuple[JournalCollection, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "revision": self.revision,
            "collections": [asdict(group) for group in self.collections],
        }

    def owner_dict(self) -> dict[str, object]:
        return {
            "revision": self.revision,
            "collections": [group.owner_dict() for group in self.collections],
        }


def member_evidence_matches(
    group: JournalCollection, member: JournalMember, records: Mapping[tuple[str, str], date]
) -> bool:
    """At least one source record must still support this species in the trip."""
    return any(
        (observed := records.get((link.checklist_hash, link.scientific_name))) is not None
        and (
            group.start_date is None
            or group.end_date is not None
            and group.start_date <= observed.isoformat() <= group.end_date
        )
        for link in member.evidence
    )


def _visible_text(value: object, *, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise JournalError("Collection names and identities must be text")
    text = unicodedata.normalize("NFC", value.strip())
    if len(text) > maximum or any(unicodedata.category(char).startswith("C") for char in text):
        raise JournalError("Collection identity contains unsupported characters or is too long")
    return text


def _dates(start: object, end: object) -> tuple[str | None, str | None]:
    if start is None and end is None:
        return None, None
    if not isinstance(start, str) or not isinstance(end, str):
        raise JournalError("Choose both calendar dates, or leave both empty")
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        if first.isoformat() != start or last.isoformat() != end or first > last:
            raise ValueError("range")
    except ValueError as exc:
        raise JournalError("Use an inclusive start and end date in YYYY-MM-DD form") from exc
    return start, end


def _members(value: object) -> tuple[JournalMember, ...]:
    if not isinstance(value, list):
        raise JournalError("Collection members are invalid")
    parsed: list[JournalMember] = []
    seen: set[tuple[str, str]] = set()
    for member in value:
        if not isinstance(member, dict) or set(member) != {
            "taxon_id",
            "common_name",
            "scientific_name",
            "origin",
            "evidence",
        }:
            raise JournalError("Collection member is invalid")
        taxon_id = member["taxon_id"]
        if taxon_id is not None and (type(taxon_id) is not int or taxon_id <= 0):
            raise JournalError("Collection species identity is invalid")
        common, scientific = (
            _visible_text(member["common_name"]),
            _visible_text(member["scientific_name"]),
        )
        if taxon_id is None and not (common or scientific):
            raise JournalError("An unresolved member needs its source name")
        try:
            origin = MemberOrigin(member["origin"])
        except (ValueError, TypeError) as exc:
            raise JournalError("Collection member origin is unsupported") from exc
        key = (
            ("taxon", str(taxon_id))
            if taxon_id is not None
            else ("unresolved", (scientific or common).casefold())
        )
        if key in seen:
            raise JournalError("A collection cannot contain a species twice")
        seen.add(key)
        evidence: list[ChecklistEvidence] = []
        links = member["evidence"]
        if not isinstance(links, list) or origin != MemberOrigin.EBIRD and links:
            raise JournalError("Collection evidence is invalid")
        for link in links:
            if not isinstance(link, dict) or set(link) != {"checklist_hash", "scientific_name"}:
                raise JournalError("Collection evidence link is invalid")
            fingerprint = link["checklist_hash"]
            if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
                raise JournalError("Collection evidence identity is invalid")
            source_name = _visible_text(link["scientific_name"])
            if not source_name:
                raise JournalError("Collection evidence needs its source species name")
            evidence.append(ChecklistEvidence(fingerprint, source_name))
        if len(set(evidence)) != len(evidence):
            raise JournalError("Collection evidence repeats a source record")
        parsed.append(JournalMember(taxon_id, common, scientific, origin, tuple(evidence)))
    return tuple(
        sorted(
            parsed,
            key=lambda member: (
                member.taxon_id is None,
                member.taxon_id or 0,
                member.scientific_name.casefold(),
                member.common_name.casefold(),
            ),
        )
    )


def _taxa(value: object) -> tuple[int, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in value
    ):
        raise JournalError("Choose valid species identifiers")
    ids = cast(list[int], value)
    if len(ids) != len(set(ids)):
        raise JournalError("A collection cannot contain a species twice")
    return tuple(sorted(ids))


def _label(value: object) -> str:
    if not isinstance(value, str):
        raise JournalError("Give this collection a name")
    label = unicodedata.normalize("NFC", value.strip())
    if (
        not label
        or len(label) > MAX_LABEL_CHARACTERS
        or any(unicodedata.category(char).startswith("C") for char in label)
    ):
        raise JournalError("Use 1–128 visible characters for the collection name")
    return label


def _revision(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise JournalError("Refresh this page before saving")
    return value


def read_journal(state_dir: Path) -> JournalState:
    path = state_dir / "journal.json"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return JournalState()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise JournalError("Journal state must be a private regular file with mode 0600")
    try:
        with path.open("rb") as handle:
            encoded = handle.read(MAX_JOURNAL_BYTES + 1)
        if len(encoded) > MAX_JOURNAL_BYTES:
            raise JournalError("Journal state exceeds the supported size")
        raw = json.loads(encoded)
        if not isinstance(raw, dict) or set(raw) != {"schema_version", "revision", "collections"}:
            raise JournalError("Journal state is invalid; restore its matching backup")
        if type(raw["schema_version"]) is not int or raw["schema_version"] not in {1, 2}:
            raise JournalError("This journal needs a compatible Inky version")
        revision = _revision(raw["revision"])
        groups = raw["collections"]
        if not isinstance(groups, list):
            raise JournalError("Journal collections are invalid")
        parsed: list[JournalCollection] = []
        seen: set[str] = set()
        for group in groups:
            fields = (
                {"collection_id", "label", "taxon_ids"}
                if raw["schema_version"] == 1
                else {
                    "collection_id",
                    "label",
                    "members",
                    "kind",
                    "start_date",
                    "end_date",
                    "created_revision",
                    "updated_revision",
                }
            )
            if not isinstance(group, dict) or set(group) != fields:
                raise JournalError("Journal collection is invalid")
            key = group["collection_id"]
            if not isinstance(key, str) or not COLLECTION_ID.fullmatch(key) or key in seen:
                raise JournalError("Journal collection identity is invalid")
            seen.add(key)
            if raw["schema_version"] == 1:
                parsed.append(
                    JournalCollection(
                        key,
                        _label(group["label"]),
                        tuple(JournalMember(taxon_id) for taxon_id in _taxa(group["taxon_ids"])),
                    )
                )
                continue
            try:
                kind = CollectionKind(group["kind"])
            except (ValueError, TypeError) as exc:
                raise JournalError("Collection kind is unsupported") from exc
            start, end = _dates(group["start_date"], group["end_date"])
            if kind in {CollectionKind.TRIP, CollectionKind.COMMUNITY} and start is None:
                raise JournalError("History collections need their original calendar date range")
            created, updated = (
                _revision(group["created_revision"]),
                _revision(group["updated_revision"]),
            )
            if not created <= updated <= revision:
                raise JournalError("Collection revision is invalid")
            parsed.append(
                JournalCollection(
                    key,
                    _label(group["label"]),
                    _members(group["members"]),
                    kind,
                    start,
                    end,
                    created,
                    updated,
                )
            )
        if sum(group.kind == CollectionKind.FAVORITES for group in parsed) > 1:
            raise JournalError("Journal has more than one Favorites group")
        return JournalState(revision, tuple(parsed))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JournalError(
            "Journal state cannot be read; check storage or restore its backup"
        ) from exc


def save_collection(
    state_dir: Path,
    *,
    revision: object,
    label: object,
    taxon_ids: object,
    collection_id: object = None,
) -> JournalState:
    expected = _revision(revision)
    name = _label(label)
    taxa = _taxa(taxon_ids)
    if collection_id is not None and (
        not isinstance(collection_id, str) or not COLLECTION_ID.fullmatch(collection_id)
    ):
        raise JournalError("Choose an existing collection")
    with catalog_state_lock(state_dir):
        state = read_journal(state_dir)
        if state.revision != expected:
            raise JournalConflictError("This journal changed in another tab. Reload before saving.")
        groups = list(state.collections)
        members = tuple(JournalMember(taxon_id) for taxon_id in taxa)
        if collection_id is None:
            groups.append(
                JournalCollection(
                    uuid4().hex,
                    name,
                    members,
                    created_revision=state.revision + 1,
                    updated_revision=state.revision + 1,
                )
            )
        else:
            found = next(
                (i for i, group in enumerate(groups) if group.collection_id == collection_id), None
            )
            if found is None:
                raise JournalConflictError("This collection was removed. Reload the journal.")
            current = groups[found]
            if current.kind == CollectionKind.FAVORITES:
                raise JournalError("Add or remove Favorites on each bird's page")
            retained = {
                member.taxon_id: member for member in current.members if member.taxon_id is not None
            }
            groups[found] = replace(
                current,
                label=name,
                updated_revision=state.revision + 1,
                members=tuple(retained.get(taxon_id, JournalMember(taxon_id)) for taxon_id in taxa)
                + tuple(member for member in current.members if member.taxon_id is None),
            )
        result = JournalState(state.revision + 1, tuple(groups))
        _write_journal(state_dir, result)
        return result


def remove_collection(state_dir: Path, *, revision: object, collection_id: object) -> JournalState:
    expected = _revision(revision)
    with catalog_state_lock(state_dir):
        state = read_journal(state_dir)
        if state.revision != expected:
            raise JournalConflictError(
                "This journal changed in another tab. Reload before removing."
            )
        groups = tuple(group for group in state.collections if group.collection_id != collection_id)
        if any(
            group.collection_id == collection_id and group.kind == CollectionKind.FAVORITES
            for group in state.collections
        ):
            raise JournalError("Remove individual bookmarks from Favorites instead")
        if len(groups) == len(state.collections):
            raise JournalConflictError("This collection was already removed. Reload the journal.")
        result = JournalState(state.revision + 1, groups)
        _write_journal(state_dir, result)
        return result


def _write_journal(state_dir: Path, state: JournalState) -> None:
    encoded = (json.dumps(state.as_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_JOURNAL_BYTES:
        raise JournalError(
            "Journal storage is full. Back it up and remove unused collections first."
        )
    path = state_dir / "journal.json"
    previous = read_json(path) if path.exists() else None
    if isinstance(previous, dict) and previous.get("schema_version") == 1:
        backup = state_dir / "journal-v1-backup.json"
        if not backup.exists():
            write_bytes_atomic(backup, path.read_bytes(), mode=0o600)
    write_bytes_atomic(path, encoded, mode=0o600)


def set_favorite(
    state_dir: Path, *, revision: object, member: JournalMember, favorite: bool
) -> JournalState:
    expected = _revision(revision)
    if member.taxon_id is None:
        raise JournalError("An unresolved species cannot become a bookmark yet")
    member = _members([{**asdict(member), "origin": MemberOrigin.SAVED, "evidence": []}])[0]
    with catalog_state_lock(state_dir):
        state = read_journal(state_dir)
        if state.revision != expected:
            raise JournalConflictError("This journal changed. Reload before changing Favorites.")
        group = next(
            (group for group in state.collections if group.kind == CollectionKind.FAVORITES), None
        )
        if group is None:
            if not favorite:
                return state
            group = JournalCollection(
                uuid4().hex,
                "Favorites",
                (),
                CollectionKind.FAVORITES,
                created_revision=state.revision + 1,
                updated_revision=state.revision + 1,
            )
        if (member.taxon_id in group.taxon_ids) == favorite:
            return state
        members = [item for item in group.members if item.taxon_id != member.taxon_id]
        if favorite:
            members.append(replace(member, origin=MemberOrigin.SAVED, evidence=()))
        group = replace(
            group,
            members=tuple(sorted(members, key=lambda item: item.taxon_id or 0)),
            updated_revision=state.revision + 1,
        )
        groups = [item for item in state.collections if item.kind != CollectionKind.FAVORITES]
        groups.append(group)
        result = JournalState(state.revision + 1, tuple(groups))
        _write_journal(state_dir, result)
        return result


def save_imported_collection(
    state_dir: Path, *, revision: object, collection: JournalCollection
) -> JournalState:
    expected = _revision(revision)
    with catalog_state_lock(state_dir):
        state = read_journal(state_dir)
        existing = next(
            (
                group
                for group in state.collections
                if group.collection_id == collection.collection_id
            ),
            None,
        )
        # A reconnect may repeat a successful save. Return that exact immutable
        # creation, but never overwrite a group edited since the import.
        if existing is not None and existing == collection:
            return state
        if state.revision != expected or existing is not None:
            raise JournalConflictError("This journal changed. Preview the import again.")
        result = JournalState(state.revision + 1, (*state.collections, collection))
        _write_journal(state_dir, result)
        return result


class JournalService:
    def __init__(self, config: ControllerConfig, *, app_config: AppConfig | None = None) -> None:
        self.config = config
        self.app_config = app_config

    def catalog(self) -> list[dict[str, object]]:
        path = self.config.catalog_dir / "index.json"
        if not path.exists():
            return []
        raw = read_json(path)
        # The canonical approved index has no approval timestamp before its
        # first plate. Active-catalog timestamp validation remains unchanged.
        if (
            isinstance(raw, dict)
            and type(raw.get("schema_version")) is int
            and raw
            == {
                "schema_version": 1,
                "generated_at": None,
                "species": [],
            }
        ):
            return []
        projected, _ = project_catalog(raw)
        return cast(list[dict[str, object]], projected["species"])

    def favorite(self, *, revision: object, taxon_id: object, favorite: object) -> JournalState:
        if type(favorite) is not bool:
            raise JournalError("Choose whether to keep this bookmark")
        _taxa([taxon_id])
        bird = next(
            (
                bird
                for bird in cast(list[dict[str, object]], self.snapshot()["birds"])
                if bird["taxon_id"] == taxon_id
            ),
            None,
        )
        if bird is None:
            raise JournalError("Choose a bird from your journal or available artwork")
        return set_favorite(
            self.config.state_dir,
            revision=revision,
            favorite=favorite,
            member=JournalMember(
                cast(int, taxon_id),
                cast(str, bird["common_name"]),
                cast(str, bird["scientific_name"]),
            ),
        )

    def export_collection(self, collection_id: str) -> dict[str, object]:
        with catalog_state_lock(self.config.state_dir):
            group = next(
                (
                    group
                    for group in read_journal(self.config.state_dir).collections
                    if group.collection_id == collection_id
                ),
                None,
            )
        if group is None:
            raise JournalError("Choose an existing named collection to export")
        birds = {
            bird["taxon_id"]: bird
            for bird in cast(list[dict[str, object]], self.snapshot()["birds"])
        }
        members = []
        for member in group.members:
            bird = birds.get(member.taxon_id, {})
            members.append(
                {
                    "taxon_id": member.taxon_id,
                    "common_name": bird.get("common_name", member.common_name),
                    "scientific_name": bird.get("scientific_name", member.scientific_name),
                    "origin": member.origin.value,
                }
            )
        return {
            "format": "inky-collection",
            "schema_version": 1,
            "label": group.label,
            "start_date": group.start_date,
            "end_date": group.end_date,
            "members": members,
        }

    def prepare_collection_import(self, document: object) -> tuple[JournalCollection, str]:
        if (
            not isinstance(document, dict)
            or set(document)
            != {"format", "schema_version", "label", "start_date", "end_date", "members"}
            or document["format"] != "inky-collection"
            or type(document["schema_version"]) is not int
            or document["schema_version"] != 1
        ):
            raise JournalError("Choose a supported Inky collection export")
        label = _label(document["label"])
        start, end = _dates(document["start_date"], document["end_date"])
        raw_members = document["members"]
        if not isinstance(raw_members, list):
            raise JournalError("The collection export has no valid member list")
        birds = cast(list[dict[str, object]], self.snapshot()["birds"])
        by_id = {bird["taxon_id"]: bird for bird in birds}
        by_scientific: dict[str, list[dict[str, object]]] = {}
        for bird in birds:
            scientific = cast(str, bird["scientific_name"])
            if scientific:
                by_scientific.setdefault(scientific, []).append(bird)
        parsed = []
        for item in raw_members:
            if not isinstance(item, dict) or set(item) != {
                "taxon_id",
                "common_name",
                "scientific_name",
                "origin",
            }:
                raise JournalError(
                    "Portable members must not contain private source links or extra fields"
                )
            candidate = _members([{**item, "evidence": []}])[0]
            match = by_id.get(candidate.taxon_id)
            if match is None and candidate.taxon_id is None:
                matches = by_scientific.get(candidate.scientific_name, [])
                match = matches[0] if len(matches) == 1 else None
            if (
                match is not None
                and candidate.scientific_name
                and candidate.scientific_name != match["scientific_name"]
            ):
                match = None
            if match is not None:
                candidate = replace(
                    candidate,
                    taxon_id=cast(int, match["taxon_id"]),
                    common_name=cast(str, match["common_name"]),
                    scientific_name=cast(str, match["scientific_name"]),
                )
            else:
                candidate = replace(
                    candidate,
                    taxon_id=None,
                    common_name=candidate.common_name or f"Unmatched taxon {candidate.taxon_id}",
                )
            parsed.append({**asdict(candidate), "evidence": []})
        members = _members(parsed)
        group = JournalCollection("", label, members, CollectionKind.IMPORTED, start, end)
        digest = hashlib.sha256(json.dumps(asdict(group), sort_keys=True).encode()).hexdigest()
        return group, digest

    def collection_import_preview(self, document: object) -> dict[str, object]:
        group, digest = self.prepare_collection_import(document)
        approved = {entry["taxon_id"] for entry in self.catalog()}
        return {
            "label": group.label,
            "start_date": group.start_date,
            "end_date": group.end_date,
            "resolved": len(group.taxon_ids),
            "ready": sum(id in approved for id in group.taxon_ids),
            "unresolved": group.owner_dict()["unresolved"],
            "preview_hash": digest,
            "notice": "Imported selections are not verified personal sightings. "
            "No source history, wall view or generation work will change.",
        }

    def import_collection(
        self, *, document: object, revision: object, preview_hash: object, collection_id: object
    ) -> JournalState:
        expected = _revision(revision)
        if not isinstance(collection_id, str) or not COLLECTION_ID.fullmatch(collection_id):
            raise JournalError("Use a new collection identifier for this import")
        group, digest = self.prepare_collection_import(document)
        if digest != preview_hash:
            raise JournalConflictError("Taxonomy or this file changed. Preview the import again.")
        group = replace(
            group,
            collection_id=collection_id,
            created_revision=expected + 1,
            updated_revision=expected + 1,
        )
        return save_imported_collection(self.config.state_dir, revision=expected, collection=group)

    def snapshot(self) -> dict[str, object]:
        if self.app_config is not None:
            self.app_config = current_config(self.app_config)
        with catalog_state_lock(self.config.state_dir):
            state = read_journal(self.config.state_dir)
            saved = [entry.taxon_id for entry in read_collection(self.config.state_dir)]
            active_path = self.config.state_dir / "active-catalog.json"
            active, _ = (
                project_catalog(read_json(active_path))
                if active_path.exists()
                else ({"species": []}, {})
            )
            active_species = cast(list[dict[str, object]], active["species"])
            featured = featured_plate_entry(active)
            active_ids = [cast(int, entry["taxon_id"]) for entry in active_species]
            observed = current_discovery_species(self.app_config) if self.app_config else []
            queued = read_generation_queue(self.app_config) if self.app_config else []
            identities = {bird.taxon_id: bird for bird in [*queued, *observed]}
            selected_identities = {
                member.taxon_id: member
                for group in state.collections
                for member in group.members
                if member.taxon_id is not None
            }
            known = (
                set(saved)
                | set(active_ids)
                | {bird.taxon_id for bird in observed}
                | {taxon for group in state.collections for taxon in group.taxon_ids}
            )
            birds = []
            for entry in self.catalog():
                taxon_id = cast(int, entry["taxon_id"])
                birds.append(
                    {
                        "taxon_id": taxon_id,
                        "common_name": entry["common_name"],
                        "scientific_name": entry["scientific_name"],
                        "known": taxon_id in known,
                        "saved": taxon_id in saved,
                        "plate_ready": True,
                        "sources": list(identities[taxon_id].sources)
                        if taxon_id in identities
                        else [],
                        "portrait_url": f"/v1/owner/journal/birds/{taxon_id}/portrait",
                        "portrait_sha256": entry["portrait_sha256"],
                    }
                )
            approved_ids = {cast(int, bird["taxon_id"]) for bird in birds}
            for taxon_id in sorted(known - approved_ids):
                identity = identities.get(taxon_id)
                selected = selected_identities.get(taxon_id)
                birds.append(
                    {
                        "taxon_id": taxon_id,
                        "common_name": identity.common_name
                        if identity
                        else selected.common_name
                        if selected and selected.common_name
                        else f"Taxon {taxon_id}",
                        "scientific_name": identity.scientific_name
                        if identity
                        else selected.scientific_name
                        if selected
                        else "",
                        "known": True,
                        "saved": taxon_id in saved,
                        "plate_ready": False,
                        "sources": list(identity.sources) if identity else [],
                        "portrait_url": None,
                    }
                )
            result = {
                "revision": state.revision,
                "collections": state.owner_dict()["collections"],
                "saved_taxon_ids": saved,
                "active_taxon_ids": active_ids,
                "featured_taxon_id": featured.get("taxon_id") if featured else None,
                "birds": birds,
                "management": self.management(approved=approved_ids),
            }
        # Do not hold the catalog lock while taking the history lock: trip saves
        # take history first, then catalog. Keep the curated membership unchanged.
        if any(member.evidence for group in state.collections for member in group.members):
            from .ebird_archive import STATE_FILENAME, read_ebird_checklists

            try:
                _, records = read_ebird_checklists(self.config.state_dir)
                available = (self.config.state_dir / STATE_FILENAME).is_file()
                links = {
                    (record.checklist_hash, species.scientific_name): record.observed_on
                    for record in records
                    for species in record.species
                }
            except DataSourceError:
                available, links = False, {}
            projected = cast(list[dict[str, object]], result["collections"])
            for group, projection in zip(state.collections, projected, strict=True):
                projection["evidence"] = [
                    {
                        "taxon_id": member.taxon_id,
                        "scientific_name": member.scientific_name,
                        "status": "unavailable"
                        if not available
                        else "linked"
                        if member_evidence_matches(group, member, links)
                        else "source-changed",
                    }
                    for member in group.members
                    if member.evidence
                ]
        return result

    def management(self, *, approved: set[int]) -> dict[str, object]:
        config = self.app_config
        if config is None:
            return {"available": False}
        # The projection has already checked the catalog index. Status polling
        # must not re-hash every full-resolution image. Serving an image still
        # checks its actual bytes, and generation keeps its own approval checks.
        work = read_generation_work(config, approved=approved)
        sources: list[dict[str, object]] = []
        discovery_path = self.config.state_dir / "discovery.json"
        diagnostics = read_json(discovery_path) if discovery_path.exists() else {}
        providers = diagnostics.get("providers", []) if isinstance(diagnostics, dict) else []
        if not isinstance(providers, list):
            raise JournalError("Source diagnostics are invalid; inspect the controller state")
        for provider in DiscoveryProvider:
            result = next(
                (
                    row
                    for row in providers
                    if isinstance(row, dict) and row.get("name") == provider.value
                ),
                {},
            )
            enabled = provider in config.discovery.sources
            status = result.get("status")
            counts = [result.get(field) for field in ("species_count", "unresolved_count")]
            if any(value is not None and (type(value) is not int or value < 0) for value in counts):
                raise JournalError("Source diagnostic counts are invalid")
            sources.append(
                {
                    "name": provider.value,
                    "enabled": enabled,
                    "status": status
                    if enabled and isinstance(status, str) and status in {"ok", "error"}
                    else "not_checked"
                    if enabled
                    else "off",
                    "species_count": counts[0] if enabled else None,
                    "unresolved_count": counts[1] if enabled else None,
                }
            )
        panel_path = self.config.state_dir / "display-last-success.json"
        panel = read_json(panel_path) if panel_path.exists() else {}
        succeeded_at = panel.get("succeeded_at") if isinstance(panel, dict) else None
        return {
            "available": True,
            "configuration_ownership": config.controller.configuration_ownership,
            "generation_enabled": config.controller.generation_enabled,
            "sources": sources,
            "window": config.discovery.observation_window.value,
            "refresh_minutes": config.schedule.refresh_minutes,
            "work": {
                "complete": work.complete,
                "discovery_status": work.discovery_status,
                "can_run": [bird.taxon_id for bird in work.actionable],
                "waiting": [record.taxon_id for record in work.deferred],
                "needs_attention": sorted(
                    {record.taxon_id for record in work.manual_retry_required}
                    | {entry.species.taxon_id for entry in work.terminal_blocked}
                ),
            },
            "notifications": notification_status(config),
            "last_confirmed_panel_update": succeeded_at
            if parse_utc_timestamp(succeeded_at)
            else None,
        }

    def _entry(self, taxon_id: int) -> dict[str, object]:
        entry = next((entry for entry in self.catalog() if entry["taxon_id"] == taxon_id), None)
        if entry is None:
            raise JournalError("This species has no approved plate in the local catalog")
        return entry

    def _file(self, relative: str) -> Path:
        try:
            root = self.config.catalog_dir.resolve()
            path = root / relative
            if path.resolve() != path or not path.is_relative_to(root) or not path.is_file():
                raise JournalError("This approved plate is unavailable; check the local catalog")
            return path
        except (OSError, RuntimeError) as exc:
            raise JournalError(
                "This approved plate is unavailable; check the local catalog"
            ) from exc

    def portrait(self, taxon_id: int) -> bytes:
        entry = self._entry(taxon_id)
        path = self._file(cast(str, entry["portrait_path"]))
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != entry["portrait_sha256"]:
            raise JournalError(
                "This plate failed its integrity check; restore the approved catalog"
            )
        return body

    def profile(self, taxon_id: int) -> dict[str, object]:
        entry = self._entry(taxon_id)
        relative = str(Path(cast(str, entry["portrait_path"])).with_name("manifest.json"))
        manifest = read_json(self._file(relative))
        if not isinstance(manifest, dict) or manifest.get("status") != "approved":
            raise JournalError("This bird profile is unavailable; check the local catalog")
        try:
            profile = parse_species_profile(manifest.get("profile"))
        except GenerationError:
            return {
                "taxon_id": taxon_id,
                "profile": None,
                "notice": "This older plate has no complete readable profile. "
                "Its art remains available.",
            }
        if profile["taxon_id"] != taxon_id or any(
            profile[field] != entry[field] for field in ("common_name", "scientific_name")
        ):
            raise CatalogError("Approved profile identity does not match its catalog entry")
        return {"taxon_id": taxon_id, "profile": profile}
