"""Validate approved plates and publish them through owner-bypassed catalog PRs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from typing import cast

from PIL import Image

from .catalog import (
    CatalogEntry,
    catalog_index_data,
    catalog_state_lock,
    clear_catalog_staging,
    has_passing_sourced_review,
    is_bounded_generation,
    read_catalog_entries,
    read_json,
    rebuild_catalog_index,
    sha256_file,
)
from .config import AppConfig, PublicCatalogConfig
from .errors import CatalogPublishError
from .http import write_json_atomic
from .timeutil import parse_utc_timestamp

_ALLOWED_SPECIES_FILES = frozenset(
    {
        "display.png",
        "manifest.json",
        "portrait.png",
        "profile.json",
        "quality-review.json",
    }
)
_PRIVATE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization_confirmed_at",
        "birdbuddy",
        "catalog_dir",
        "checkout_dir",
        "controller_url",
        "coordinates",
        "country_code",
        "geocoder",
        "geocoder_attribution",
        "geoapify_api_key",
        "geoapify_api_key_env",
        "latitude",
        "longitude",
        "email",
        "observation_count",
        "password",
        "place_name",
        "postal_code",
        "postcard_id",
        "radius_km",
        "refresh_token",
        "state_dir",
        "feeder_id",
        "workspace_dir",
        "zip_code",
    }
)
_PRIVATE_KEY_IDENTIFIERS = frozenset(key.replace("_", "") for key in _PRIVATE_KEYS)
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_WINDOWS_UNC_PATH = re.compile(r"^(?:\\\\|//)[^\\/]")
_CREDENTIALS_IN_URL = re.compile(r"(https?://)[^/@\s]+@")
_GIT_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_GITHUB_REMOTE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<repository>[^/\s]+/[^/\s]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_IMAGE_DIMENSIONS = {"portrait.png": (1200, 1600), "display.png": (1600, 1200)}
_COMMAND_TIMEOUT_SECONDS = 180
_SHA256 = re.compile(r"[0-9a-f]{64}")


@contextmanager
def exclusive_publish_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "catalog-publish.lock").open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CatalogPublishError("Another catalog publication is running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _check_json_privacy(value: object, source: Path) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise CatalogPublishError(f"JSON object has a non-string key: {source}")
            identifier = re.sub(r"[^a-z0-9]", "", key.casefold())
            if identifier in _PRIVATE_KEY_IDENTIFIERS:
                raise CatalogPublishError(f"Private field {key!r} found in {source}")
            _check_json_privacy(child, source)
        return
    if isinstance(value, list):
        for child in value:
            _check_json_privacy(child, source)
        return
    if isinstance(value, str) and (
        value.startswith("file://")
        or Path(value).is_absolute()
        or _WINDOWS_ABSOLUTE_PATH.match(value) is not None
        or _WINDOWS_UNC_PATH.match(value) is not None
    ):
        raise CatalogPublishError(f"Local path found in catalog JSON: {source}")


def _validate_image(path: Path, expected_size: tuple[int, int]) -> None:
    try:
        with Image.open(path) as image:
            if image.format != "PNG" or image.size != expected_size:
                raise CatalogPublishError(
                    f"{path} must be a {expected_size[0]}x{expected_size[1]} PNG"
                )
            image.verify()
        with Image.open(path) as image:
            if image.info or image.getexif():
                raise CatalogPublishError(f"Image metadata is not allowed in {path}")
    except CatalogPublishError:
        raise
    except (OSError, RuntimeError, SyntaxError) as exc:
        raise CatalogPublishError(f"Invalid catalog image: {path}") from exc


def _legacy_seed_review_passed(review: object, generation: object) -> bool:
    if not isinstance(review, dict) or not isinstance(generation, dict):
        return False
    score_fields = (
        "species_accuracy",
        "anatomy_accuracy",
        "text_accuracy",
        "composition_quality",
    )
    return (
        generation.get("generator") == "User-approved seed image"
        and review.get("passed") is True
        and review.get("location_free") is True
        and all(
            isinstance(review.get(field), int)
            and not isinstance(review.get(field), bool)
            and cast(int, review[field]) >= 4
            for field in score_fields
        )
    )


def _legacy_sourced_review_passed(review: object, generation: object) -> bool:
    if not isinstance(generation, dict):
        return False
    return generation.get("prompt_version") == "field-journal-v1" and has_passing_sourced_review(
        review
    )


def _validate_catalog_root(catalog_dir: Path, *, allow_create: bool) -> None:
    if catalog_dir.exists():
        if catalog_dir.is_symlink() or not catalog_dir.is_dir():
            raise CatalogPublishError(f"Catalog root must be a directory: {catalog_dir}")
    elif allow_create:
        parent = catalog_dir.parent
        if parent.is_symlink() or not parent.is_dir():
            raise CatalogPublishError(f"Catalog parent must be a directory: {parent}")
        catalog_dir.mkdir()
    else:
        raise CatalogPublishError(f"Catalog root does not exist: {catalog_dir}")

    species_root = catalog_dir / "species"
    if species_root.exists():
        if species_root.is_symlink() or not species_root.is_dir():
            raise CatalogPublishError(f"Species root must be a directory: {species_root}")
    elif allow_create:
        species_root.mkdir()
    else:
        raise CatalogPublishError(f"Species root does not exist: {species_root}")


def _validate_species_directory(directory: Path) -> tuple[int, str]:
    if directory.is_symlink() or not directory.is_dir():
        raise CatalogPublishError(f"Species path must be a directory, not a symlink: {directory}")
    files = {path.name for path in directory.iterdir()}
    unexpected = files - _ALLOWED_SPECIES_FILES
    missing = {"manifest.json", "portrait.png", "display.png"} - files
    if unexpected:
        raise CatalogPublishError(
            f"Unexpected catalog files in {directory}: {', '.join(sorted(unexpected))}"
        )
    if missing:
        raise CatalogPublishError(
            f"Required catalog files missing from {directory}: {', '.join(sorted(missing))}"
        )
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            raise CatalogPublishError(f"Catalog entries must be regular files: {path}")

    manifest = read_json(directory / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise CatalogPublishError(f"Unsupported catalog manifest: {directory / 'manifest.json'}")
    taxon_id = manifest.get("taxon_id")
    slug = manifest.get("slug")
    if (
        manifest.get("status") != "approved"
        or not isinstance(taxon_id, int)
        or isinstance(taxon_id, bool)
        or not isinstance(slug, str)
        or directory.name != f"{taxon_id}-{slug}"
    ):
        raise CatalogPublishError(f"Manifest identity does not match {directory}")
    _catalog_migration_records(manifest, directory / "manifest.json")

    review = manifest.get("quality_review")
    generation = manifest.get("generation")
    assets = manifest.get("assets")
    if not isinstance(assets, dict):
        raise CatalogPublishError(f"Manifest has no asset map: {directory}")
    for asset_name, expected_filename in (
        ("portrait", "portrait.png"),
        ("display", "display.png"),
    ):
        asset = assets.get(asset_name)
        if not isinstance(asset, dict) or asset.get("filename") != expected_filename:
            raise CatalogPublishError(
                f"Manifest {asset_name} asset must use {expected_filename}: {directory}"
            )
        checksum = asset.get("sha256")
        if (
            not isinstance(checksum, str)
            or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
            or sha256_file(directory / expected_filename) != checksum
        ):
            raise CatalogPublishError(
                f"Manifest {asset_name} checksum does not match {expected_filename}: {directory}"
            )
    automated = has_passing_sourced_review(review) and is_bounded_generation(generation)
    legacy = _legacy_seed_review_passed(review, generation) or _legacy_sourced_review_passed(
        review, generation
    )
    if not automated and not legacy:
        raise CatalogPublishError(f"Manifest lacks a publishable quality review: {directory}")

    for path in directory.glob("*.json"):
        payload = read_json(path)
        _check_json_privacy(payload, path)
        if path.name == "profile.json" and payload != manifest.get("profile"):
            raise CatalogPublishError(f"Profile does not match the manifest: {directory}")
        if path.name == "quality-review.json" and payload != review:
            raise CatalogPublishError(f"Quality review does not match the manifest: {directory}")
    for filename, dimensions in _IMAGE_DIMENSIONS.items():
        _validate_image(directory / filename, dimensions)
    return taxon_id, slug


def validate_public_catalog(catalog_dir: Path) -> list[CatalogEntry]:
    _validate_catalog_root(catalog_dir, allow_create=False)
    root_entries = {path.name for path in catalog_dir.iterdir()}
    unexpected_root_entries = root_entries - {"index.json", "species"}
    if unexpected_root_entries:
        unexpected = ", ".join(sorted(unexpected_root_entries))
        raise CatalogPublishError(f"Unexpected catalog root entries in {catalog_dir}: {unexpected}")
    index_path = catalog_dir / "index.json"
    if index_path.is_symlink() or not index_path.is_file():
        raise CatalogPublishError(f"Catalog index must be a regular file: {index_path}")
    species_root = catalog_dir / "species"
    directories = sorted(path for path in species_root.iterdir() if path.name != ".DS_Store")
    seen_taxa: set[int] = set()
    for directory in directories:
        taxon_id, _ = _validate_species_directory(directory)
        if taxon_id in seen_taxa:
            raise CatalogPublishError(f"Catalog contains duplicate taxon ID {taxon_id}")
        seen_taxa.add(taxon_id)
    entries = read_catalog_entries(catalog_dir)
    if len(entries) != len(directories):
        raise CatalogPublishError(f"Every species directory needs one manifest: {species_root}")
    index = read_json(index_path)
    _check_json_privacy(index, index_path)
    if index != catalog_index_data(entries):
        raise CatalogPublishError(f"Catalog index does not match species manifests: {catalog_dir}")
    return entries


def _trees_match(left: Path, right: Path) -> bool:
    left_files = sorted(path.relative_to(left) for path in left.rglob("*") if path.is_file())
    right_files = sorted(path.relative_to(right) for path in right.rglob("*") if path.is_file())
    return left_files == right_files and all(
        (left / relative).read_bytes() == (right / relative).read_bytes() for relative in left_files
    )


def _parse_catalog_migration_record(
    raw: object,
    source: Path,
) -> tuple[str, str, str, str]:
    if not isinstance(raw, dict) or set(raw) != {"reason", "replaces"}:
        raise CatalogPublishError(f"Invalid catalog migration record: {source}")
    reason = raw.get("reason")
    replaces = raw.get("replaces")
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or reason != reason.strip()
        or not isinstance(replaces, dict)
        or set(replaces) != {"approved_at", "display_sha256", "portrait_sha256"}
    ):
        raise CatalogPublishError(f"Invalid catalog migration record: {source}")
    approved_at = replaces.get("approved_at")
    display_sha256 = replaces.get("display_sha256")
    portrait_sha256 = replaces.get("portrait_sha256")
    if (
        not isinstance(approved_at, str)
        or parse_utc_timestamp(approved_at) is None
        or not isinstance(display_sha256, str)
        or _SHA256.fullmatch(display_sha256) is None
        or not isinstance(portrait_sha256, str)
        or _SHA256.fullmatch(portrait_sha256) is None
    ):
        raise CatalogPublishError(f"Invalid catalog migration record: {source}")
    return reason, approved_at, display_sha256, portrait_sha256


def _catalog_migration_records(
    manifest: dict[str, object],
    source: Path,
) -> list[tuple[str, str, str, str]]:
    raw = manifest.get("catalog_migration")
    if raw is None:
        return []
    if not isinstance(raw, dict) or set(raw) not in (
        {"reason", "replaces"},
        {"history", "reason", "replaces"},
    ):
        raise CatalogPublishError(f"Invalid catalog migration record: {source}")
    history = raw.get("history", [])
    if not isinstance(history, list) or ("history" in raw and not history):
        raise CatalogPublishError(f"Invalid catalog migration record: {source}")
    current = {key: value for key, value in raw.items() if key != "history"}
    records = [
        *(_parse_catalog_migration_record(item, source) for item in history),
        _parse_catalog_migration_record(current, source),
    ]
    approved_times = [parse_utc_timestamp(record[1]) for record in records]
    candidate_approved_at = parse_utc_timestamp(manifest.get("approved_at"))
    if (
        any(value is None for value in approved_times)
        or candidate_approved_at is None
        or any(
            previous >= current_time
            for previous, current_time in zip(approved_times, approved_times[1:], strict=False)
            if previous is not None and current_time is not None
        )
        or approved_times[-1] is None
        or approved_times[-1] >= candidate_approved_at
        or len({record[1:] for record in records}) != len(records)
    ):
        raise CatalogPublishError(f"Invalid catalog migration record: {source}")
    return records


def _catalog_migration_payload(record: tuple[str, str, str, str]) -> dict[str, object]:
    reason, approved_at, display_sha256, portrait_sha256 = record
    return {
        "reason": reason,
        "replaces": {
            "approved_at": approved_at,
            "display_sha256": display_sha256,
            "portrait_sha256": portrait_sha256,
        },
    }


def _manifest_asset_sha256(manifest: dict[str, object], asset_name: str) -> str:
    assets = manifest.get("assets")
    asset = assets.get(asset_name) if isinstance(assets, dict) else None
    checksum = asset.get("sha256") if isinstance(asset, dict) else None
    if not isinstance(checksum, str) or _SHA256.fullmatch(checksum) is None:
        raise CatalogPublishError(f"Manifest has invalid {asset_name} checksum")
    return checksum


def _validate_catalog_replacement(
    base_directory: Path,
    candidate_directory: Path,
    *,
    allow_ancestor: bool = False,
) -> None:
    base = read_json(base_directory / "manifest.json")
    candidate = read_json(candidate_directory / "manifest.json")
    if not isinstance(base, dict) or not isinstance(candidate, dict):
        raise CatalogPublishError("Catalog replacement manifests must be JSON objects")
    records = _catalog_migration_records(candidate, candidate_directory / "manifest.json")
    if not records:
        raise CatalogPublishError(
            f"Catalog contribution changed immutable taxon {base.get('taxon_id')}"
        )
    identity_fields = ("taxon_id", "common_name", "scientific_name", "slug")
    if any(base.get(field) != candidate.get(field) for field in identity_fields):
        raise CatalogPublishError("Catalog replacement changed the approved taxon identity")
    base_approval = (
        base.get("approved_at"),
        _manifest_asset_sha256(base, "display"),
        _manifest_asset_sha256(base, "portrait"),
    )
    matching_index = next(
        (index for index, record in enumerate(records) if record[1:] == base_approval),
        None,
    )
    if matching_index is None or (not allow_ancestor and matching_index != len(records) - 1):
        raise CatalogPublishError("Catalog migration does not match the approved base artifacts")
    candidate_approved_at = parse_utc_timestamp(candidate.get("approved_at"))
    replaced_approved_at = parse_utc_timestamp(records[matching_index][1])
    if (
        candidate_approved_at is None
        or replaced_approved_at is None
        or candidate_approved_at <= replaced_approved_at
    ):
        raise CatalogPublishError("Catalog replacement must have a newer approval timestamp")
    if (
        not allow_ancestor
        and _manifest_asset_sha256(candidate, "display") == records[-1][2]
        and _manifest_asset_sha256(candidate, "portrait") == records[-1][3]
    ):
        raise CatalogPublishError("Catalog replacement does not change an approved image")


def _trees_match_without_catalog_migration(left: Path, right: Path) -> bool:
    left_files = sorted(path.relative_to(left) for path in left.rglob("*") if path.is_file())
    right_files = sorted(path.relative_to(right) for path in right.rglob("*") if path.is_file())
    if left_files != right_files:
        return False
    for relative in left_files:
        if relative != Path("manifest.json"):
            if (left / relative).read_bytes() != (right / relative).read_bytes():
                return False
            continue
        left_manifest = read_json(left / relative)
        right_manifest = read_json(right / relative)
        if not isinstance(left_manifest, dict) or not isinstance(right_manifest, dict):
            return False
        left_manifest.pop("catalog_migration", None)
        right_manifest.pop("catalog_migration", None)
        if left_manifest != right_manifest:
            return False
    return True


def _validate_catalog_migration_convergence(
    destination_directory: Path,
    candidate_directory: Path,
) -> None:
    destination = read_json(destination_directory / "manifest.json")
    candidate = read_json(candidate_directory / "manifest.json")
    if not isinstance(destination, dict) or not isinstance(candidate, dict):
        raise CatalogPublishError("Catalog replacement manifests must be JSON objects")
    destination_records = _catalog_migration_records(
        destination, destination_directory / "manifest.json"
    )
    candidate_records = _catalog_migration_records(candidate, candidate_directory / "manifest.json")
    if (
        not _trees_match_without_catalog_migration(destination_directory, candidate_directory)
        or destination_records != candidate_records[: len(destination_records)]
    ):
        raise CatalogPublishError(
            f"Catalog taxon {destination.get('taxon_id')} conflicts with immutable local approval"
        )


def validate_catalog_additions(
    base_catalog: Path,
    candidate_catalog: Path,
) -> list[CatalogEntry]:
    base_entries = validate_public_catalog(base_catalog)
    candidate_entries = validate_public_catalog(candidate_catalog)
    candidate_by_taxon = {entry.taxon_id: entry for entry in candidate_entries}

    for base_entry in base_entries:
        candidate_entry = candidate_by_taxon.get(base_entry.taxon_id)
        if candidate_entry is None:
            raise CatalogPublishError(
                f"Catalog contribution removed immutable taxon {base_entry.taxon_id}"
            )
        base_directory = base_catalog / "species" / f"{base_entry.taxon_id}-{base_entry.slug}"
        candidate_directory = (
            candidate_catalog / "species" / f"{candidate_entry.taxon_id}-{candidate_entry.slug}"
        )
        if base_directory.name != candidate_directory.name:
            raise CatalogPublishError(
                f"Catalog contribution changed immutable taxon {base_entry.taxon_id}"
            )
        if not _trees_match(base_directory, candidate_directory):
            _validate_catalog_replacement(base_directory, candidate_directory)

    base_taxa = {entry.taxon_id for entry in base_entries}
    additions = [entry for entry in candidate_entries if entry.taxon_id not in base_taxa]
    for entry in additions:
        manifest = read_json(
            candidate_catalog / "species" / f"{entry.taxon_id}-{entry.slug}" / "manifest.json"
        )
        if isinstance(manifest, dict) and manifest.get("catalog_migration") is not None:
            raise CatalogPublishError(
                f"New catalog taxon {entry.taxon_id} cannot declare a replacement migration"
            )
    return additions


def replace_public_catalog_taxon(
    source_catalog: Path,
    destination_catalog: Path,
    taxon_id: int,
    reason: str,
) -> dict[str, object]:
    replacement_reason = reason.strip()
    if not replacement_reason:
        raise ValueError("Catalog replacement requires a non-empty reason")
    source_entries = validate_public_catalog(source_catalog)
    destination_entries = validate_public_catalog(destination_catalog)
    source_entry = next((entry for entry in source_entries if entry.taxon_id == taxon_id), None)
    destination_entry = next(
        (entry for entry in destination_entries if entry.taxon_id == taxon_id), None
    )
    if source_entry is None:
        raise CatalogPublishError(f"Source catalog does not contain taxon {taxon_id}")
    if destination_entry is None:
        raise CatalogPublishError(f"Destination catalog does not contain taxon {taxon_id}")
    if (
        source_entry.common_name,
        source_entry.scientific_name,
        source_entry.slug,
    ) != (
        destination_entry.common_name,
        destination_entry.scientific_name,
        destination_entry.slug,
    ):
        raise CatalogPublishError("Catalog replacement identity differs from the approved taxon")

    source = source_catalog / "species" / f"{taxon_id}-{source_entry.slug}"
    destination = destination_catalog / "species" / f"{taxon_id}-{destination_entry.slug}"
    destination_manifest = read_json(destination / "manifest.json")
    if not isinstance(destination_manifest, dict):
        raise CatalogPublishError(f"Invalid approved manifest: {destination / 'manifest.json'}")
    migration_history = _catalog_migration_records(
        destination_manifest, destination / "manifest.json"
    )
    replacement_record: dict[str, object] = {
        "reason": replacement_reason,
        "replaces": {
            "approved_at": destination_entry.approved_at,
            "display_sha256": destination_entry.display_sha256,
            "portrait_sha256": destination_entry.portrait_sha256,
        },
    }

    with TemporaryDirectory(prefix=".catalog-replace-", dir=destination_catalog.parent) as temp:
        transaction = Path(temp)
        staged = transaction / "replacement" / source.name
        backup = transaction / "approved" / destination.name
        backup.parent.mkdir()
        shutil.copytree(source, staged)
        staged_manifest_path = staged / "manifest.json"
        staged_manifest = read_json(staged_manifest_path)
        if not isinstance(staged_manifest, dict):
            raise CatalogPublishError(f"Invalid replacement manifest: {staged_manifest_path}")
        if migration_history:
            replacement_record["history"] = [
                _catalog_migration_payload(record) for record in migration_history
            ]
        staged_manifest["catalog_migration"] = replacement_record
        write_json_atomic(staged_manifest_path, staged_manifest)
        _validate_species_directory(staged)
        _validate_catalog_replacement(destination, staged)

        destination.replace(backup)
        try:
            staged.replace(destination)
            rebuild_catalog_index(destination_catalog)
            validate_public_catalog(destination_catalog)
        except BaseException as error:
            if destination.exists():
                shutil.rmtree(destination)
            backup.replace(destination)
            rebuild_catalog_index(destination_catalog)
            raise CatalogPublishError("Catalog replacement failed and was rolled back") from error

    return {
        "replaced": {
            "taxon_id": source_entry.taxon_id,
            "common_name": source_entry.common_name,
            "scientific_name": source_entry.scientific_name,
            "slug": source_entry.slug,
        },
        "reason": replacement_reason,
        "replaces": replacement_record["replaces"],
    }


def _restore_sync_replacements(transaction: Path, destination_species: Path) -> None:
    backup_root = transaction / "approved"
    if not backup_root.exists():
        return
    if backup_root.is_symlink() or not backup_root.is_dir():
        raise CatalogPublishError(f"Invalid catalog sync backup path: {backup_root}")
    for backup in sorted(backup_root.iterdir()):
        _validate_species_directory(backup)
        destination = destination_species / backup.name
        if destination.exists():
            _validate_species_directory(destination)
            shutil.rmtree(destination)
        backup.replace(destination)


def _sync_transaction_prefix(destination_catalog: Path, *, committed: bool) -> str:
    catalog_digest = hashlib.sha256(str(destination_catalog.resolve()).encode()).hexdigest()[:16]
    state = "committed" if committed else "active"
    return f".catalog-sync-{state}-{catalog_digest}-"


def _sync_transactions(destination_catalog: Path, *, committed: bool) -> list[Path]:
    prefix = _sync_transaction_prefix(destination_catalog, committed=committed)
    return sorted(destination_catalog.parent.glob(f"{prefix}*"))


def _validate_sync_transaction(transaction: Path) -> None:
    if transaction.is_symlink() or not transaction.is_dir():
        raise CatalogPublishError(f"Invalid catalog sync transaction path: {transaction}")


def sync_public_catalog(
    source_catalog: Path,
    destination_catalog: Path,
    *,
    taxon_ids: set[int] | None = None,
    allow_replacements: bool = False,
) -> dict[str, object]:
    source_entries = validate_public_catalog(source_catalog)
    all_source_taxa = {entry.taxon_id for entry in source_entries}
    requested_taxa = all_source_taxa if taxon_ids is None else taxon_ids
    missing_taxa = requested_taxa - all_source_taxa
    if missing_taxa:
        missing = ", ".join(str(taxon_id) for taxon_id in sorted(missing_taxa))
        raise CatalogPublishError(f"Source catalog does not contain taxon {missing}")
    source_by_taxon = {
        entry.taxon_id: entry for entry in source_entries if entry.taxon_id in requested_taxa
    }

    _validate_catalog_root(destination_catalog, allow_create=True)
    destination_species = destination_catalog / "species"
    committed_transactions = _sync_transactions(destination_catalog, committed=True)
    for transaction in committed_transactions:
        _validate_sync_transaction(transaction)
        shutil.rmtree(transaction)
    active_transactions = _sync_transactions(destination_catalog, committed=False)
    for transaction in active_transactions:
        _validate_sync_transaction(transaction)
        _restore_sync_replacements(transaction, destination_species)
        shutil.rmtree(transaction)
    if (destination_catalog / "index.json").exists():
        try:
            validate_public_catalog(destination_catalog)
        except CatalogPublishError:
            if not active_transactions:
                raise
            for directory in destination_species.iterdir():
                _validate_species_directory(directory)
            rebuild_catalog_index(destination_catalog)
            validate_public_catalog(destination_catalog)
    elif any(destination_species.iterdir()):
        for directory in destination_species.iterdir():
            _validate_species_directory(directory)
        rebuild_catalog_index(destination_catalog)
        validate_public_catalog(destination_catalog)

    published: list[dict[str, object]] = []
    replaced: list[dict[str, object]] = []
    existing: list[int] = []

    transaction = Path(
        mkdtemp(
            prefix=_sync_transaction_prefix(destination_catalog, committed=False),
            dir=destination_catalog.parent,
        )
    )
    try:
        for taxon_id, entry in sorted(source_by_taxon.items()):
            source = source_catalog / "species" / f"{taxon_id}-{entry.slug}"
            matches = sorted(destination_species.glob(f"{taxon_id}-*"))
            if len(matches) > 1:
                raise CatalogPublishError(
                    f"Catalog contains multiple directories for taxon {taxon_id}"
                )
            if matches:
                destination = matches[0]
                if destination.name != source.name:
                    raise CatalogPublishError(
                        f"Catalog taxon {taxon_id} conflicts with immutable local approval"
                    )
                if not _trees_match(source, destination):
                    if not allow_replacements:
                        raise CatalogPublishError(
                            f"Catalog taxon {taxon_id} conflicts with immutable local approval"
                        )
                    if _trees_match_without_catalog_migration(destination, source):
                        _validate_catalog_migration_convergence(destination, source)
                    else:
                        _validate_catalog_replacement(
                            destination,
                            source,
                            allow_ancestor=True,
                        )
                    staged = transaction / "replacement" / source.name
                    backup = transaction / "approved" / destination.name
                    backup.parent.mkdir(exist_ok=True)
                    shutil.copytree(source, staged)
                    _validate_species_directory(staged)
                    destination.replace(backup)
                    staged.replace(destination)
                    replaced.append(
                        {
                            "taxon_id": entry.taxon_id,
                            "common_name": entry.common_name,
                            "scientific_name": entry.scientific_name,
                            "slug": entry.slug,
                        }
                    )
                    continue
                existing.append(taxon_id)
                continue
            staged = transaction / source.name
            shutil.copytree(source, staged)
            _validate_species_directory(staged)
            staged.replace(destination_species / source.name)
            published.append(
                {
                    "taxon_id": entry.taxon_id,
                    "common_name": entry.common_name,
                    "scientific_name": entry.scientific_name,
                    "slug": entry.slug,
                }
            )

        rebuild_catalog_index(destination_catalog)
        validate_public_catalog(destination_catalog)
    except BaseException:
        _restore_sync_replacements(transaction, destination_species)
        rebuild_catalog_index(destination_catalog)
        transaction.mkdir(exist_ok=True)
        raise
    committed_transaction = transaction.with_name(
        transaction.name.replace(
            _sync_transaction_prefix(destination_catalog, committed=False),
            _sync_transaction_prefix(destination_catalog, committed=True),
            1,
        )
    )
    transaction.replace(committed_transaction)
    shutil.rmtree(committed_transaction)
    return {"published": published, "replaced": replaced, "already_present": existing}


def _redact_command_output(value: str) -> str:
    return _CREDENTIALS_IN_URL.sub(r"\1[REDACTED]@", value.strip())


def _run(
    arguments: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CatalogPublishError(f"Unable to run {Path(arguments[0]).name}") from exc
    if check and result.returncode != 0:
        detail = _redact_command_output(result.stderr or result.stdout)
        raise CatalogPublishError(
            f"{Path(arguments[0]).name} {arguments[1]} failed: {detail or 'unknown error'}"
        )
    return result


def _git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-C", str(repository), *arguments], check=check)


def _gh(
    publication: PublicCatalogConfig,
    *arguments: str,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run([str(publication.gh_path), *arguments], input_text=input_text, check=check)


def _remote_repository(remote_url: str) -> str | None:
    match = _GITHUB_REMOTE.fullmatch(remote_url.strip())
    return match.group("repository") if match is not None else None


def _validate_checkout(checkout: Path, publication: PublicCatalogConfig) -> str:
    if checkout.is_symlink() or not checkout.is_dir():
        raise CatalogPublishError(f"Repository checkout does not exist: {checkout}")
    repository = publication.repository
    if repository is None:
        raise CatalogPublishError("Catalog repository is not configured")
    top_level = _git(checkout, "rev-parse", "--show-toplevel").stdout.strip()
    if Path(top_level).resolve() != checkout.resolve():
        raise CatalogPublishError("checkout_dir must be the Git repository root")
    if _GIT_REMOTE_NAME.fullmatch(publication.remote) is None:
        raise CatalogPublishError("Catalog remote has an invalid Git remote name")
    _git(checkout, "check-ref-format", "--branch", publication.base_branch)
    remote_url = _git(checkout, "remote", "get-url", publication.remote).stdout.strip()
    remote_repository = _remote_repository(remote_url)
    if remote_repository is None or remote_repository.casefold() != repository.casefold():
        raise CatalogPublishError("Catalog remote does not match the configured repository")

    owner = repository.split("/", maxsplit=1)[0]
    try:
        authenticated = _gh(publication, "api", "user", "--jq", ".login").stdout.strip()
    except CatalogPublishError as exc:
        auth_status = _gh(
            publication,
            "auth",
            "status",
            "--hostname",
            "github.com",
            check=False,
        )
        if auth_status.returncode != 0:
            detail = _redact_command_output(auth_status.stderr or auth_status.stdout)
            raise CatalogPublishError(
                f"GitHub CLI authentication check failed: {detail or 'unknown error'}"
            ) from exc
        raise
    if authenticated.casefold() != owner.casefold():
        raise CatalogPublishError(f"GitHub CLI must be authenticated as repository owner {owner!r}")
    return repository


def _commit_message(published: list[dict[str, object]]) -> str:
    if len(published) == 1:
        return f"Publish {published[0]['common_name']}"
    if published:
        return f"Publish {len(published)} bird plates"
    return "Rebuild catalog index"


def _catalog_digest(catalog_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in catalog_dir.rglob("*") if item.is_file()):
        relative = path.relative_to(catalog_dir).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_staged_catalog(
    worktree: Path,
    published: list[dict[str, object]],
) -> list[str]:
    expected_directories = {
        f"catalog/species/{item['taxon_id']}-{item['slug']}/"
        for item in published
        if isinstance(item.get("taxon_id"), int) and isinstance(item.get("slug"), str)
    }
    output = _git(
        worktree,
        "diff",
        "--cached",
        "--name-status",
        "--no-renames",
    ).stdout
    paths: list[str] = []
    for line in output.splitlines():
        try:
            status, path = line.split("\t", maxsplit=1)
        except ValueError as exc:
            raise CatalogPublishError("Unable to parse staged catalog changes") from exc
        paths.append(path)
        if path == "catalog/index.json":
            if status not in {"A", "M"}:
                raise CatalogPublishError("Catalog index may only be added or modified")
            continue
        if status != "A" or not any(path.startswith(prefix) for prefix in expected_directories):
            raise CatalogPublishError(f"Publication attempted an unexpected change: {path}")
        if Path(path).name not in _ALLOWED_SPECIES_FILES:
            raise CatalogPublishError(f"Publication attempted an unexpected species file: {path}")
    if published and "catalog/index.json" not in paths:
        raise CatalogPublishError("Catalog publication did not update catalog/index.json")
    return paths


def _pull_request(
    publication: PublicCatalogConfig,
    repository: str,
    branch: str,
) -> dict[str, object] | None:
    raw = _gh(
        publication,
        "pr",
        "list",
        "--repo",
        repository,
        "--head",
        branch,
        "--state",
        "all",
        "--limit",
        "1",
        "--json",
        "number,url,state,headRefOid",
    ).stdout
    try:
        pull_requests = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CatalogPublishError("GitHub CLI returned invalid pull request data") from exc
    if not isinstance(pull_requests, list) or any(
        not isinstance(item, dict) for item in pull_requests
    ):
        raise CatalogPublishError("GitHub CLI returned invalid pull request data")
    return cast(dict[str, object], pull_requests[0]) if pull_requests else None


def _create_pull_request(
    publication: PublicCatalogConfig,
    repository: str,
    branch: str,
    title: str,
    published: list[dict[str, object]],
) -> str:
    birds = "\n".join(f"- {item['common_name']} ({item['scientific_name']})" for item in published)
    body = (
        "Automated catalog publication from the trusted controller.\n\n"
        "Validated additions:\n"
        f"{birds}\n\n"
        "Only immutable, location-neutral files under `catalog/` are included.\n"
    )
    return _gh(
        publication,
        "pr",
        "create",
        "--repo",
        repository,
        "--base",
        publication.base_branch,
        "--head",
        branch,
        "--title",
        title,
        "--body-file",
        "-",
        input_text=body,
    ).stdout.strip()


def run_catalog_publish(config: AppConfig, *, dry_run: bool = False) -> dict[str, object]:
    publication = config.public_catalog
    if not publication.enabled:
        raise CatalogPublishError("Catalog publishing is disabled")
    checkout = publication.checkout_dir
    if checkout is None:
        raise CatalogPublishError("Catalog checkout_dir is not configured")

    with exclusive_publish_lock(config.controller.state_dir):
        repository = _validate_checkout(checkout, publication)
        remote_ref = f"refs/remotes/{publication.remote}/{publication.base_branch}"
        _git(
            checkout,
            "fetch",
            "--prune",
            publication.remote,
            f"+refs/heads/{publication.base_branch}:{remote_ref}",
        )
        _git(checkout, "worktree", "prune")
        work_parent = config.controller.state_dir / "catalog-publish-work"
        work_parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="publish-", dir=work_parent) as temporary:
            source_snapshot = Path(temporary) / "source-catalog"
            with catalog_state_lock(config.controller.state_dir):
                # Approval staging debris would fail the snapshot root check.
                clear_catalog_staging(config.controller.catalog_dir)
                try:
                    shutil.copytree(
                        config.controller.catalog_dir,
                        source_snapshot,
                        symlinks=True,
                    )
                except FileNotFoundError as exc:
                    raise CatalogPublishError("Approved local catalog does not exist") from exc
            validate_public_catalog(source_snapshot)

            worktree = Path(temporary) / "checkout"
            _git(checkout, "worktree", "add", "--detach", str(worktree), remote_ref)
            try:
                sync = sync_public_catalog(source_snapshot, worktree / "catalog")
                published = sync["published"]
                if not isinstance(published, list) or any(
                    not isinstance(item, dict) for item in published
                ):
                    raise CatalogPublishError("Publisher produced an invalid change summary")
                typed_published = cast(list[dict[str, object]], published)
                changed = bool(
                    _git(
                        worktree,
                        "status",
                        "--porcelain",
                        "--untracked-files=all",
                        "--",
                        "catalog",
                    ).stdout.strip()
                )
                result: dict[str, object] = {
                    **sync,
                    "changed": changed,
                    "dry_run": dry_run,
                    "pushed": False,
                    "merged": False,
                    "commit": None,
                    "pull_request": None,
                }
                if not changed:
                    return result
                if not typed_published:
                    raise CatalogPublishError("Catalog changed without adding an approved species")

                _git(worktree, "add", "--", "catalog")
                _git(worktree, "diff", "--cached", "--check")
                result["paths"] = _validate_staged_catalog(worktree, typed_published)
                if dry_run:
                    return result

                base_commit = _git(worktree, "rev-parse", "HEAD").stdout.strip()
                branch = f"catalog/publish-{_catalog_digest(source_snapshot)[:8]}-{base_commit[:8]}"
                remote_branch = _git(
                    checkout,
                    "ls-remote",
                    "--heads",
                    publication.remote,
                    f"refs/heads/{branch}",
                ).stdout.strip()
                if remote_branch:
                    remote_commit = remote_branch.split(maxsplit=1)[0]
                    branch_ref = f"refs/remotes/{publication.remote}/catalog-publication"
                    _git(
                        checkout,
                        "fetch",
                        publication.remote,
                        f"+refs/heads/{branch}:{branch_ref}",
                    )
                    staged_tree = _git(worktree, "write-tree").stdout.strip()
                    remote_tree = _git(
                        checkout, "rev-parse", f"{branch_ref}^{{tree}}"
                    ).stdout.strip()
                    if remote_tree != staged_tree:
                        raise CatalogPublishError(
                            f"Existing publication branch {branch!r} has unexpected content"
                        )
                    commit = remote_commit
                else:
                    _git(
                        worktree,
                        "-c",
                        f"user.name={publication.commit_name}",
                        "-c",
                        f"user.email={publication.commit_email}",
                        "commit",
                        "-m",
                        _commit_message(typed_published),
                    )
                    commit = _git(worktree, "rev-parse", "HEAD").stdout.strip()
                    _git(worktree, "push", publication.remote, f"HEAD:refs/heads/{branch}")
                result["pushed"] = True
                result["commit"] = commit
                result["branch"] = branch

                pull_request = _pull_request(publication, repository, branch)
                if pull_request is None:
                    pull_request_url = _create_pull_request(
                        publication,
                        repository,
                        branch,
                        _commit_message(typed_published),
                        typed_published,
                    )
                else:
                    existing_url = pull_request.get("url")
                    if not isinstance(existing_url, str):
                        raise CatalogPublishError("Existing publication PR has no URL")
                    pull_request_url = existing_url
                    if pull_request.get("state") == "CLOSED":
                        raise CatalogPublishError(
                            "Existing publication PR was closed without merge"
                        )
                    head_sha = pull_request.get("headRefOid")
                    if head_sha != commit:
                        raise CatalogPublishError("Existing publication PR has an unexpected head")
                result["pull_request"] = pull_request_url

                _gh(
                    publication,
                    "pr",
                    "merge",
                    pull_request_url,
                    "--repo",
                    repository,
                    "--admin",
                    "--squash",
                    "--delete-branch",
                    "--match-head-commit",
                    commit,
                )
                state = _gh(
                    publication,
                    "pr",
                    "view",
                    pull_request_url,
                    "--repo",
                    repository,
                    "--json",
                    "state",
                    "--jq",
                    ".state",
                ).stdout.strip()
                if state != "MERGED":
                    raise CatalogPublishError(f"Catalog pull request did not merge: {state}")
                result["merged"] = True
                return result
            finally:
                _git(checkout, "worktree", "remove", "--force", str(worktree), check=False)
                _git(checkout, "worktree", "prune", check=False)
