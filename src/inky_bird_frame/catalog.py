"""Immutable approved catalog and mutable pending-candidate storage."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from .birds import BirdSpecies
from .errors import CatalogError
from .images import slugify
from .models import QualityReview, ReferencePhoto, SpeciesProfileData

SCHEMA_VERSION = 1


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

    def as_dict(self) -> dict[str, object]:
        return {
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


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def read_json(path: Path) -> object:
    try:
        return cast(object, json.loads(path.read_text()))
    except FileNotFoundError as exc:
        raise CatalogError(f"Catalog file not found: {path}") from exc
    except json.JSONDecodeError as exc:
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
) -> Path:
    portrait_path = destination / "portrait.png"
    display_path = destination / "display.png"
    if not portrait_path.is_file() or not display_path.is_file():
        raise CatalogError("Candidate must contain portrait.png and display.png")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "pending",
        "taxon_id": species.taxon_id,
        "common_name": species.common_name,
        "scientific_name": species.scientific_name,
        "slug": slugify(species.common_name),
        "profile": profile,
        "references": [reference.as_dict() for reference in references],
        "generation": {
            "generator": generator,
            "prompt_version": prompt_version,
            "generated_at": utc_now(),
        },
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


def rebuild_catalog_index(catalog_dir: Path) -> list[CatalogEntry]:
    species_dir = catalog_dir / "species"
    entries = [
        _manifest_entry(path, catalog_dir) for path in sorted(species_dir.glob("*/manifest.json"))
    ]
    entries.sort(key=lambda item: (item.common_name.casefold(), item.taxon_id))
    write_json_atomic(
        catalog_dir / "index.json",
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": max((entry.approved_at for entry in entries), default=None),
            "species": [entry.as_dict() for entry in entries],
        },
    )
    return entries


def approved_taxon_ids(catalog_dir: Path) -> set[int]:
    entries = rebuild_catalog_index(catalog_dir)
    return {entry.taxon_id for entry in entries}


def approve_candidate(state_dir: Path, catalog_dir: Path, taxon_id: int) -> CatalogEntry:
    source = find_taxon_directory(state_dir / "pending", taxon_id)
    if source is None:
        raise CatalogError(f"No pending candidate exists for taxon {taxon_id}")
    manifest_path = source / "manifest.json"
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("status") != "pending":
        raise CatalogError(f"Candidate manifest is not pending: {manifest_path}")
    review = manifest.get("quality_review")
    if not isinstance(review, dict) or review.get("passed") is not True:
        raise CatalogError("Candidate did not pass automated quality review")

    slug = manifest.get("slug")
    if not isinstance(slug, str):
        raise CatalogError("Candidate manifest has no slug")
    destination = catalog_dir / "species" / f"{taxon_id}-{slug}"
    if destination.exists():
        raise CatalogError(
            f"Taxon {taxon_id} is already approved; use an explicit replacement workflow"
        )

    approved_manifest = dict(manifest)
    approved_manifest["status"] = "approved"
    approved_manifest["approved_at"] = utc_now()
    write_json_atomic(manifest_path, approved_manifest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    shutil.rmtree(source)
    entries = rebuild_catalog_index(catalog_dir)
    return next(entry for entry in entries if entry.taxon_id == taxon_id)


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
    destination = state_dir / "rejected" / source.name
    if destination.exists():
        destination = destination.with_name(
            f"{destination.name}-{datetime.now(UTC).timestamp():.0f}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.replace(destination)
    return destination
