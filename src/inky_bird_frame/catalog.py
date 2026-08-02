"""Immutable approved catalog and mutable pending-candidate storage."""

from __future__ import annotations

import fcntl
import hashlib
import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

from .birds import BirdSpecies
from .errors import CatalogError
from .http import write_json_atomic
from .images import slugify
from .models import QualityReview, ReferencePhoto, SpeciesProfileData
from .timeutil import parse_utc_timestamp

SCHEMA_VERSION = 1
COLLECTION_SCHEMA_VERSION = 1


class CollectionOrigin(StrEnum):
    MANUAL = "manual"
    CATALOG_IMPORT = "catalog_import"
    HISTORICAL_SEED = "historical_seed"


@dataclass(frozen=True)
class CollectionEntry:
    taxon_id: int
    added_at: str
    origin: CollectionOrigin

    def as_dict(self) -> dict[str, object]:
        return {
            "taxon_id": self.taxon_id,
            "added_at": self.added_at,
            "origin": self.origin.value,
        }


@dataclass(frozen=True)
class CollectionState:
    entries: list[CollectionEntry]
    legacy_seed_queue_migrated_at: str | None


@contextmanager
def catalog_state_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "catalog-state.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class CatalogEntry:
    taxon_id: int
    common_name: str
    scientific_name: str
    slug: str
    portrait_path: str
    portrait_sha256: str
    display_path: str
    display_sha256: str
    approved_at: str
    observation_count: int | None = None
    latest_detection_at: str | None = None

    def as_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "taxon_id": self.taxon_id,
            "common_name": self.common_name,
            "scientific_name": self.scientific_name,
            "slug": self.slug,
            "portrait_path": self.portrait_path,
            "portrait_sha256": self.portrait_sha256,
            "display_path": self.display_path,
            "display_sha256": self.display_sha256,
            "approved_at": self.approved_at,
        }
        if self.observation_count is not None:
            value["observation_count"] = self.observation_count
        if self.latest_detection_at is not None:
            value["latest_detection_at"] = self.latest_detection_at
        return value


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def read_collection_state(state_dir: Path) -> CollectionState:
    path = state_dir / "collection.json"
    if not path.exists():
        return CollectionState([], None)
    raw = read_json(path)
    if (
        not isinstance(raw, dict)
        or set(raw)
        != {
            "schema_version",
            "updated_at",
            "legacy_seed_queue_migrated_at",
            "taxa",
        }
        or raw.get("schema_version") != COLLECTION_SCHEMA_VERSION
        or parse_utc_timestamp(raw.get("updated_at")) is None
        or (
            raw.get("legacy_seed_queue_migrated_at") is not None
            and parse_utc_timestamp(raw.get("legacy_seed_queue_migrated_at")) is None
        )
        or not isinstance(raw.get("taxa"), list)
    ):
        raise CatalogError(f"Invalid collection state: {path}")

    entries: list[CollectionEntry] = []
    seen: set[int] = set()
    for item in cast(list[object], raw["taxa"]):
        if not isinstance(item, dict) or set(item) != {"taxon_id", "added_at", "origin"}:
            raise CatalogError(f"Invalid collection entry: {path}")
        taxon_id = item.get("taxon_id")
        added_at = item.get("added_at")
        origin = item.get("origin")
        if (
            not isinstance(taxon_id, int)
            or isinstance(taxon_id, bool)
            or taxon_id <= 0
            or taxon_id in seen
            or not isinstance(added_at, str)
            or parse_utc_timestamp(added_at) is None
            or not isinstance(origin, str)
        ):
            raise CatalogError(f"Invalid collection entry: {path}")
        try:
            parsed_origin = CollectionOrigin(origin)
        except ValueError as exc:
            raise CatalogError(f"Invalid collection entry: {path}") from exc
        seen.add(taxon_id)
        entries.append(CollectionEntry(taxon_id, added_at, parsed_origin))
    return CollectionState(
        sorted(entries, key=lambda entry: entry.taxon_id),
        cast(str | None, raw["legacy_seed_queue_migrated_at"]),
    )


def read_collection(state_dir: Path) -> list[CollectionEntry]:
    return read_collection_state(state_dir).entries


def write_collection(
    state_dir: Path,
    entries: list[CollectionEntry],
    *,
    legacy_seed_queue_migrated_at: str | None,
) -> None:
    if (
        legacy_seed_queue_migrated_at is not None
        and parse_utc_timestamp(legacy_seed_queue_migrated_at) is None
    ):
        raise CatalogError("Cannot write invalid collection state")
    seen: set[int] = set()
    for entry in entries:
        if (
            not isinstance(entry.taxon_id, int)
            or isinstance(entry.taxon_id, bool)
            or entry.taxon_id <= 0
            or entry.taxon_id in seen
            or parse_utc_timestamp(entry.added_at) is None
        ):
            raise CatalogError("Cannot write invalid collection state")
        seen.add(entry.taxon_id)
    write_json_atomic(
        state_dir / "collection.json",
        {
            "schema_version": COLLECTION_SCHEMA_VERSION,
            "updated_at": utc_now(),
            "legacy_seed_queue_migrated_at": legacy_seed_queue_migrated_at,
            "taxa": [entry.as_dict() for entry in sorted(entries, key=lambda item: item.taxon_id)],
        },
    )


def add_collection_taxa(
    entries: list[CollectionEntry],
    taxon_ids: set[int],
    origin: CollectionOrigin,
) -> tuple[list[CollectionEntry], list[CollectionEntry]]:
    if any(
        not isinstance(taxon_id, int) or isinstance(taxon_id, bool) or taxon_id <= 0
        for taxon_id in taxon_ids
    ):
        raise ValueError("collection taxon IDs must be positive integers")
    existing_ids = {entry.taxon_id for entry in entries}
    added_at = utc_now()
    added = [
        CollectionEntry(taxon_id, added_at, origin) for taxon_id in sorted(taxon_ids - existing_ids)
    ]
    return sorted([*entries, *added], key=lambda entry: entry.taxon_id), added


def remove_collection_taxa(
    entries: list[CollectionEntry], taxon_ids: set[int]
) -> tuple[list[CollectionEntry], list[CollectionEntry]]:
    removed = [entry for entry in entries if entry.taxon_id in taxon_ids]
    remaining = [entry for entry in entries if entry.taxon_id not in taxon_ids]
    return remaining, removed


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> object:
    try:
        return cast(object, json.loads(path.read_text()))
    except FileNotFoundError as exc:
        raise CatalogError(f"Catalog file not found: {path}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CatalogError(f"Invalid JSON in catalog file: {path}") from exc


def candidate_directory(state_dir: Path, species: BirdSpecies) -> Path:
    return state_dir / "pending" / f"{species.taxon_id}-{slugify(species.common_name)}"


def rejected_directory(state_dir: Path, species: BirdSpecies) -> Path:
    return state_dir / "rejected" / f"{species.taxon_id}-{slugify(species.common_name)}"


def find_taxon_directory(parent: Path, taxon_id: int) -> Path | None:
    matches = sorted(parent.glob(f"{taxon_id}-*"))
    if len(matches) > 1:
        raise CatalogError(f"Multiple directories found for taxon {taxon_id} in {parent}")
    return matches[0] if matches else None


def write_candidate_manifest(
    destination: Path,
    species: BirdSpecies,
    profile: SpeciesProfileData,
    references: list[ReferencePhoto],
    review: QualityReview,
    *,
    generator: str,
    prompt_version: str,
    attempt: int = 1,
    max_attempts: int = 1,
    correction_source_sha256: str | None = None,
) -> Path:
    portrait_path = destination / "portrait.png"
    display_path = destination / "display.png"
    if not portrait_path.is_file() or not display_path.is_file():
        raise CatalogError("Candidate must contain portrait.png and display.png")
    generation: dict[str, object] = {
        "generator": generator,
        "prompt_version": prompt_version,
        "generated_at": utc_now(),
        "attempt": attempt,
        "max_attempts": max_attempts,
    }
    if correction_source_sha256 is not None:
        generation["correction_source_sha256"] = correction_source_sha256
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "pending",
        "taxon_id": species.taxon_id,
        "common_name": species.common_name,
        "scientific_name": species.scientific_name,
        "slug": slugify(species.common_name),
        "profile": profile,
        "references": [reference.as_dict() for reference in references],
        "generation": generation,
        "quality_review": review.as_dict(),
        "assets": {
            "portrait": {"filename": "portrait.png", "sha256": sha256_file(portrait_path)},
            "display": {"filename": "display.png", "sha256": sha256_file(display_path)},
        },
    }
    manifest_path = destination / "manifest.json"
    write_json_atomic(manifest_path, manifest)
    return manifest_path


def _manifest_entry(manifest_path: Path, catalog_dir: Path) -> CatalogEntry:
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("status") != "approved":
        raise CatalogError(f"Expected approved manifest: {manifest_path}")
    assets = manifest.get("assets")
    if not isinstance(assets, dict):
        raise CatalogError(f"Manifest has no assets: {manifest_path}")
    portrait = assets.get("portrait")
    display = assets.get("display")
    if not isinstance(portrait, dict) or not isinstance(display, dict):
        raise CatalogError(f"Manifest has invalid assets: {manifest_path}")

    directory = manifest_path.parent
    portrait_file = portrait.get("filename")
    display_file = display.get("filename")
    portrait_hash = portrait.get("sha256")
    display_hash = display.get("sha256")
    scalar_values = (
        manifest.get("taxon_id"),
        manifest.get("common_name"),
        manifest.get("scientific_name"),
        manifest.get("slug"),
        manifest.get("approved_at"),
        portrait_file,
        display_file,
        portrait_hash,
        display_hash,
    )
    if not isinstance(scalar_values[0], int) or any(
        not isinstance(value, str) for value in scalar_values[1:]
    ):
        raise CatalogError(f"Manifest has invalid scalar fields: {manifest_path}")
    portrait_path = directory / cast(str, portrait_file)
    display_path = directory / cast(str, display_file)
    if sha256_file(portrait_path) != portrait_hash or sha256_file(display_path) != display_hash:
        raise CatalogError(f"Asset checksum mismatch: {manifest_path}")
    return CatalogEntry(
        taxon_id=scalar_values[0],
        common_name=cast(str, scalar_values[1]),
        scientific_name=cast(str, scalar_values[2]),
        slug=cast(str, scalar_values[3]),
        portrait_path=str(portrait_path.relative_to(catalog_dir)),
        portrait_sha256=cast(str, portrait_hash),
        display_path=str(display_path.relative_to(catalog_dir)),
        display_sha256=cast(str, display_hash),
        approved_at=cast(str, scalar_values[4]),
    )


def read_catalog_entries(catalog_dir: Path) -> list[CatalogEntry]:
    species_dir = catalog_dir / "species"
    entries = [
        _manifest_entry(path, catalog_dir) for path in sorted(species_dir.glob("*/manifest.json"))
    ]
    entries.sort(key=lambda item: (item.common_name.casefold(), item.taxon_id))
    return entries


def catalog_index_data(entries: list[CatalogEntry]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": max((entry.approved_at for entry in entries), default=None),
        "species": [entry.as_dict() for entry in entries],
    }


def rebuild_catalog_index(catalog_dir: Path) -> list[CatalogEntry]:
    entries = read_catalog_entries(catalog_dir)
    write_json_atomic(
        catalog_dir / "index.json",
        catalog_index_data(entries),
    )
    return entries


def approved_taxon_ids(catalog_dir: Path) -> set[int]:
    entries = read_catalog_entries(catalog_dir)
    return {entry.taxon_id for entry in entries}


def has_passing_sourced_review(review: object) -> bool:
    if not isinstance(review, dict):
        return False
    correction_findings = review.get("correction_findings", [])
    score_fields = (
        "species_accuracy",
        "anatomy_accuracy",
        "text_accuracy",
        "composition_quality",
    )
    if (
        review.get("passed") is not True
        or review.get("location_free") is not True
        or not isinstance(correction_findings, list)
        or bool(correction_findings)
        or any(
            not isinstance(review.get(field), int)
            or isinstance(review.get(field), bool)
            or cast(int, review[field]) < 4
            for field in score_fields
        )
    ):
        return False
    sources = review.get("verification_sources")
    if not isinstance(sources, list):
        return False
    urls = {
        source.get("url")
        for source in sources
        if isinstance(source, dict)
        and isinstance(source.get("title"), str)
        and bool(source["title"].strip())
        and isinstance(source.get("url"), str)
        and source["url"].startswith("https://")
    }
    return len(urls) >= 2


def is_bounded_generation(generation: object) -> bool:
    if not isinstance(generation, dict):
        return False
    attempt = generation.get("attempt")
    max_attempts = generation.get("max_attempts")
    correction_source_sha256 = generation.get("correction_source_sha256")
    return (
        isinstance(attempt, int)
        and not isinstance(attempt, bool)
        and isinstance(max_attempts, int)
        and not isinstance(max_attempts, bool)
        and 1 <= attempt <= max_attempts
        and (
            correction_source_sha256 is None
            or (
                isinstance(correction_source_sha256, str)
                and len(correction_source_sha256) == 64
                and all(character in "0123456789abcdef" for character in correction_source_sha256)
            )
        )
    )


def clear_catalog_staging(catalog_dir: Path, name: str | None = None) -> None:
    staging = catalog_dir / ".staging"
    target = staging if name is None else staging / name
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    if name is not None:
        with suppress(OSError):
            staging.rmdir()


def _existing_destination_manifest(destination: Path) -> dict[str, object] | None:
    existing_path = destination / "manifest.json"
    if not existing_path.is_file():
        return None
    try:
        existing = read_json(existing_path)
    except CatalogError:
        return None
    return existing if isinstance(existing, dict) else None


def _same_candidate(manifest: dict[str, object], existing: dict[str, object]) -> bool:
    # Crash debris is a byte-for-byte copy of the pending candidate apart from
    # the approval fields; anything else must go through explicit replacement
    # so an intentional correction is never silently discarded.
    ignored = ("status", "approved_at")
    pending_view = {key: value for key, value in manifest.items() if key not in ignored}
    existing_view = {key: value for key, value in existing.items() if key not in ignored}
    return existing.get("status") == "approved" and pending_view == existing_view


def _valid_destination_entry(destination: Path, catalog_dir: Path) -> bool:
    # Full manifest-entry validation covers required fields and asset
    # checksums, so the pending copy is only dropped for a complete approval.
    try:
        _manifest_entry(destination / "manifest.json", catalog_dir)
    except (CatalogError, OSError):
        return False
    return True


def approve_candidate(state_dir: Path, catalog_dir: Path, taxon_id: int) -> CatalogEntry:
    source = find_taxon_directory(state_dir / "pending", taxon_id)
    if source is None:
        raise CatalogError(f"No pending candidate exists for taxon {taxon_id}")
    manifest_path = source / "manifest.json"
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("status") not in ("pending", "approved"):
        raise CatalogError(f"Candidate manifest is not pending: {manifest_path}")
    review = manifest.get("quality_review")
    if not isinstance(review, dict) or review.get("passed") is not True:
        raise CatalogError("Candidate did not pass automated quality review")

    slug = manifest.get("slug")
    if not isinstance(slug, str):
        raise CatalogError("Candidate manifest has no slug")
    destination = catalog_dir / "species" / f"{taxon_id}-{slug}"
    clear_catalog_staging(catalog_dir, destination.name)
    if destination.exists():
        existing = _existing_destination_manifest(destination)
        if existing is None:
            # No readable manifest is never a valid approval, only debris from
            # an interrupted legacy copy: discard it and approve again.
            shutil.rmtree(destination)
        elif not _same_candidate(manifest, existing):
            raise CatalogError(
                f"Taxon {taxon_id} is already approved; use an explicit replacement workflow"
            )
        elif _valid_destination_entry(destination, catalog_dir):
            shutil.rmtree(source)
            entries = rebuild_catalog_index(catalog_dir)
            return next(entry for entry in entries if entry.taxon_id == taxon_id)
        else:
            # Same candidate but an incomplete copy: discard it and approve again.
            shutil.rmtree(destination)

    approved_manifest = dict(manifest)
    approved_manifest["status"] = "approved"
    approved_manifest["approved_at"] = utc_now()
    staged = catalog_dir / ".staging" / destination.name
    shutil.copytree(source, staged)
    write_json_atomic(staged / "manifest.json", approved_manifest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged.rename(destination)
    # An empty .staging directory would fail the publish root allowlist.
    with suppress(OSError):
        staged.parent.rmdir()
    shutil.rmtree(source)
    entries = rebuild_catalog_index(catalog_dir)
    return next(entry for entry in entries if entry.taxon_id == taxon_id)


def _available_rejected_destination(state_dir: Path, name: str) -> Path:
    destination = state_dir / "rejected" / name
    counter = 1
    while destination.exists():
        destination = destination.with_name(f"{name}-{counter}")
        counter += 1
    return destination


def withdraw_approved_candidate(
    state_dir: Path,
    catalog_dir: Path,
    taxon_id: int,
    reason: str,
) -> Path:
    rejection_reason = reason.strip()
    if not rejection_reason:
        raise CatalogError("Approved replacement requires a non-empty rejection reason")
    source = find_taxon_directory(catalog_dir / "species", taxon_id)
    if source is None:
        raise CatalogError(f"No approved candidate exists for taxon {taxon_id}")
    manifest_path = source / "manifest.json"
    manifest = read_json(manifest_path)
    if (
        not isinstance(manifest, dict)
        or manifest.get("taxon_id") != taxon_id
        or manifest.get("status") != "approved"
    ):
        raise CatalogError(f"Approved candidate manifest is invalid: {manifest_path}")

    destination = _available_rejected_destination(state_dir, source.name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(source, destination)
    except Exception:
        if destination.exists():
            shutil.rmtree(destination)
        with suppress(OSError):
            destination.parent.rmdir()
        raise
    try:
        rejected_manifest = dict(manifest)
        rejected_manifest["status"] = "rejected"
        rejected_manifest["rejected_at"] = utc_now()
        rejected_manifest["rejection_reason"] = rejection_reason
        write_json_atomic(destination / "manifest.json", rejected_manifest)
        shutil.rmtree(source)
        rebuild_catalog_index(catalog_dir)
    except Exception as error:
        try:
            if source.exists():
                shutil.rmtree(source)
            shutil.copytree(destination, source)
            write_json_atomic(source / "manifest.json", manifest)
        except Exception as rollback_error:
            raise CatalogError(
                "Approved candidate withdrawal failed and could not be rolled back; "
                f"recovery copy retained at {destination}: {rollback_error}"
            ) from error
        shutil.rmtree(destination)
        with suppress(OSError):
            destination.parent.rmdir()
        raise
    return destination


def reject_candidate(state_dir: Path, taxon_id: int, reason: str) -> Path:
    source = find_taxon_directory(state_dir / "pending", taxon_id)
    if source is None:
        raise CatalogError(f"No pending candidate exists for taxon {taxon_id}")
    manifest = read_json(source / "manifest.json")
    if not isinstance(manifest, dict):
        raise CatalogError("Candidate manifest is invalid")
    manifest["status"] = "rejected"
    manifest["rejected_at"] = utc_now()
    manifest["rejection_reason"] = reason
    write_json_atomic(source / "manifest.json", manifest)
    destination = _available_rejected_destination(state_dir, source.name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.replace(destination)
    return destination
