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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

from .catalog import catalog_state_lock, read_collection, read_json
from .codex_runner import parse_species_profile
from .config import AppConfig, ControllerConfig, DiscoveryProvider
from .controller import current_discovery_species, read_generation_queue, read_generation_work
from .errors import CatalogError, GenerationError, InkyBirdFrameError
from .http import write_bytes_atomic
from .notifications import notification_status
from .server import featured_plate_entry, project_catalog
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
class JournalCollection:
    collection_id: str
    label: str
    taxon_ids: tuple[int, ...]


@dataclass(frozen=True)
class JournalState:
    revision: int = 0
    collections: tuple[JournalCollection, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "revision": self.revision,
            "collections": [asdict(group) for group in self.collections],
        }


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
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
            raise JournalError("This journal needs a compatible Inky version")
        revision = _revision(raw["revision"])
        groups = raw["collections"]
        if not isinstance(groups, list):
            raise JournalError("Journal collections are invalid")
        parsed: list[JournalCollection] = []
        seen: set[str] = set()
        for group in groups:
            if not isinstance(group, dict) or set(group) != {"collection_id", "label", "taxon_ids"}:
                raise JournalError("Journal collection is invalid")
            key = group["collection_id"]
            if not isinstance(key, str) or not COLLECTION_ID.fullmatch(key) or key in seen:
                raise JournalError("Journal collection identity is invalid")
            seen.add(key)
            parsed.append(JournalCollection(key, _label(group["label"]), _taxa(group["taxon_ids"])))
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
        if collection_id is None:
            groups.append(JournalCollection(uuid4().hex, name, taxa))
        else:
            found = next(
                (i for i, group in enumerate(groups) if group.collection_id == collection_id), None
            )
            if found is None:
                raise JournalConflictError("This collection was removed. Reload the journal.")
            groups[found] = JournalCollection(collection_id, name, taxa)
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
    write_bytes_atomic(state_dir / "journal.json", encoded, mode=0o600)


class JournalService:
    def __init__(self, config: ControllerConfig, *, app_config: AppConfig | None = None) -> None:
        self.config = config
        self.app_config = app_config

    def catalog(self) -> list[dict[str, object]]:
        path = self.config.catalog_dir / "index.json"
        if not path.exists():
            return []
        projected, _ = project_catalog(read_json(path))
        return cast(list[dict[str, object]], projected["species"])

    def snapshot(self) -> dict[str, object]:
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
                birds.append(
                    {
                        "taxon_id": taxon_id,
                        "common_name": identity.common_name if identity else f"Taxon {taxon_id}",
                        "scientific_name": identity.scientific_name if identity else "",
                        "known": True,
                        "saved": taxon_id in saved,
                        "plate_ready": False,
                        "sources": list(identity.sources) if identity else [],
                        "portrait_url": None,
                    }
                )
            return {
                "revision": state.revision,
                "collections": state.as_dict()["collections"],
                "saved_taxon_ids": saved,
                "active_taxon_ids": active_ids,
                "featured_taxon_id": featured.get("taxon_id") if featured else None,
                "birds": birds,
                "management": self.management(approved=approved_ids),
            }

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
            "configuration_ownership": "managed",
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
