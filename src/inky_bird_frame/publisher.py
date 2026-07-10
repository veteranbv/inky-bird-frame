"""Validate and publish immutable approved plates to a dedicated Git repository."""

from __future__ import annotations

import fcntl
import re
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from PIL import Image

from .catalog import (
    CatalogEntry,
    catalog_state_lock,
    has_passing_sourced_review,
    is_bounded_generation,
    read_json,
    rebuild_catalog_index,
)
from .config import AppConfig, PublicCatalogConfig
from .errors import CatalogPublishError

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
        "catalog_dir",
        "checkout_dir",
        "controller_url",
        "coordinates",
        "latitude",
        "longitude",
        "observation_count",
        "place_name",
        "radius_km",
        "state_dir",
        "workspace_dir",
        "zip_code",
    }
)
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_CREDENTIALS_IN_URL = re.compile(r"(https?://)[^/@\s]+@")
_GIT_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_IMAGE_DIMENSIONS = {"portrait.png": (1200, 1600), "display.png": (1600, 1200)}


@contextmanager
def exclusive_publish_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "catalog-publish.lock").open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CatalogPublishError("Another public catalog publication is running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _check_json_privacy(value: object, source: Path) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise CatalogPublishError(f"JSON object has a non-string key: {source}")
            if key.casefold() in _PRIVATE_KEYS:
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
    ):
        raise CatalogPublishError(f"Local path found in public catalog JSON: {source}")


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
        raise CatalogPublishError(f"Invalid public catalog image: {path}") from exc


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


def _validate_species_directory(directory: Path) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise CatalogPublishError(f"Species path must be a directory, not a symlink: {directory}")
    files = {path.name for path in directory.iterdir()}
    unexpected = files - _ALLOWED_SPECIES_FILES
    missing = {"manifest.json", "portrait.png", "display.png"} - files
    if unexpected:
        raise CatalogPublishError(
            f"Unexpected public catalog files in {directory}: {', '.join(sorted(unexpected))}"
        )
    if missing:
        raise CatalogPublishError(
            f"Required public catalog files missing from {directory}: {', '.join(sorted(missing))}"
        )
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            raise CatalogPublishError(f"Public catalog entries must be regular files: {path}")

    manifest = read_json(directory / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise CatalogPublishError(f"Unsupported public manifest: {directory / 'manifest.json'}")
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


def validate_public_catalog(catalog_dir: Path) -> list[CatalogEntry]:
    species_root = catalog_dir / "species"
    directories = sorted(path for path in species_root.glob("*") if path.name != ".DS_Store")
    for directory in directories:
        _validate_species_directory(directory)
    entries = rebuild_catalog_index(catalog_dir)
    if len(entries) != len(directories):
        raise CatalogPublishError(
            f"Every public species directory needs one manifest: {species_root}"
        )
    index = read_json(catalog_dir / "index.json")
    _check_json_privacy(index, catalog_dir / "index.json")
    return entries


def _trees_match(left: Path, right: Path) -> bool:
    left_files = sorted(path.relative_to(left) for path in left.rglob("*") if path.is_file())
    right_files = sorted(path.relative_to(right) for path in right.rglob("*") if path.is_file())
    return left_files == right_files and all(
        (left / relative).read_bytes() == (right / relative).read_bytes() for relative in left_files
    )


def sync_public_catalog(source_catalog: Path, destination_catalog: Path) -> dict[str, object]:
    source_entries = validate_public_catalog(source_catalog)
    source_by_taxon = {entry.taxon_id: entry for entry in source_entries}
    destination_species = destination_catalog / "species"
    destination_species.mkdir(parents=True, exist_ok=True)
    published: list[dict[str, object]] = []
    existing: list[int] = []

    for taxon_id, entry in sorted(source_by_taxon.items()):
        source = source_catalog / "species" / f"{taxon_id}-{entry.slug}"
        matches = sorted(destination_species.glob(f"{taxon_id}-*"))
        if len(matches) > 1:
            raise CatalogPublishError(
                f"Public catalog contains multiple directories for taxon {taxon_id}"
            )
        if matches:
            if matches[0].name != source.name or not _trees_match(source, matches[0]):
                raise CatalogPublishError(
                    f"Public catalog taxon {taxon_id} conflicts with immutable local approval"
                )
            existing.append(taxon_id)
            continue
        shutil.copytree(source, destination_species / source.name)
        published.append(
            {
                "taxon_id": entry.taxon_id,
                "common_name": entry.common_name,
                "scientific_name": entry.scientific_name,
            }
        )

    validate_public_catalog(destination_catalog)
    return {"published": published, "already_present": existing}


def _redact_git_output(value: str) -> str:
    return _CREDENTIALS_IN_URL.sub(r"\1[REDACTED]@", value.strip())


def _git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CatalogPublishError(f"Unable to run git {arguments[0]}") from exc
    if check and result.returncode != 0:
        detail = _redact_git_output(result.stderr or result.stdout)
        raise CatalogPublishError(f"git {arguments[0]} failed: {detail or 'unknown error'}")
    return result


def _validate_checkout(checkout: Path, publication: PublicCatalogConfig) -> None:
    if not checkout.is_dir():
        raise CatalogPublishError(f"Public catalog checkout does not exist: {checkout}")
    top_level = _git(checkout, "rev-parse", "--show-toplevel").stdout.strip()
    if Path(top_level).resolve() != checkout.resolve():
        raise CatalogPublishError("Public catalog checkout_dir must be the Git repository root")
    if _GIT_REMOTE_NAME.fullmatch(publication.remote) is None:
        raise CatalogPublishError("Public catalog remote has an invalid Git remote name")
    _git(checkout, "check-ref-format", "--branch", publication.branch)
    _git(checkout, "remote", "get-url", publication.remote)


def _commit_message(published: list[dict[str, object]]) -> str:
    if len(published) == 1:
        return f"Publish {published[0]['common_name']}"
    if published:
        return f"Publish {len(published)} bird plates"
    return "Rebuild catalog index"


def run_catalog_publish(config: AppConfig, *, dry_run: bool = False) -> dict[str, object]:
    publication = config.public_catalog
    if not publication.enabled:
        raise CatalogPublishError("Public catalog publishing is disabled")
    checkout = publication.checkout_dir
    if checkout is None:
        raise CatalogPublishError("Public catalog checkout_dir is not configured")

    with exclusive_publish_lock(config.controller.state_dir):
        _validate_checkout(checkout, publication)
        remote_ref = f"refs/remotes/{publication.remote}/{publication.branch}"
        _git(
            checkout,
            "fetch",
            "--prune",
            publication.remote,
            f"+refs/heads/{publication.branch}:{remote_ref}",
        )
        _git(checkout, "worktree", "prune")
        work_parent = config.controller.state_dir / "catalog-publish-work"
        work_parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="publish-", dir=work_parent) as temporary:
            source_snapshot = Path(temporary) / "source-catalog"
            with catalog_state_lock(config.controller.state_dir):
                try:
                    shutil.copytree(config.controller.catalog_dir, source_snapshot)
                except FileNotFoundError as exc:
                    raise CatalogPublishError("Approved local catalog does not exist") from exc
            worktree = Path(temporary) / "checkout"
            _git(checkout, "worktree", "add", "--detach", str(worktree), remote_ref)
            try:
                sync = sync_public_catalog(
                    source_snapshot,
                    worktree / "catalog",
                )
                status = _git(
                    worktree,
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                    "--",
                    "catalog",
                ).stdout
                changed = bool(status.strip())
                result = {
                    **sync,
                    "changed": changed,
                    "dry_run": dry_run,
                    "pushed": False,
                    "commit": None,
                }
                if dry_run or not changed:
                    return result

                published = sync["published"]
                if not isinstance(published, list):
                    raise CatalogPublishError("Publisher produced an invalid change summary")
                _git(worktree, "add", "--", "catalog")
                _git(worktree, "diff", "--cached", "--check")
                _git(
                    worktree,
                    "-c",
                    f"user.name={publication.commit_name}",
                    "-c",
                    f"user.email={publication.commit_email}",
                    "commit",
                    "-m",
                    _commit_message(cast(list[dict[str, object]], published)),
                )
                commit = _git(worktree, "rev-parse", "HEAD").stdout.strip()
                _git(
                    worktree,
                    "push",
                    publication.remote,
                    f"HEAD:refs/heads/{publication.branch}",
                )
                result["pushed"] = True
                result["commit"] = commit
                return result
            finally:
                _git(checkout, "worktree", "remove", "--force", str(worktree), check=False)
                _git(checkout, "worktree", "prune", check=False)
