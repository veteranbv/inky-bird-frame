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
from tempfile import TemporaryDirectory
from typing import cast

from PIL import Image

from .catalog import (
    CatalogEntry,
    catalog_index_data,
    catalog_state_lock,
    has_passing_sourced_review,
    is_bounded_generation,
    read_catalog_entries,
    read_json,
    rebuild_catalog_index,
    sha256_file,
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
    index = read_json(catalog_dir / "index.json")
    _check_json_privacy(index, catalog_dir / "index.json")
    if index != catalog_index_data(entries):
        raise CatalogPublishError(f"Catalog index does not match species manifests: {catalog_dir}")
    return entries


def _trees_match(left: Path, right: Path) -> bool:
    left_files = sorted(path.relative_to(left) for path in left.rglob("*") if path.is_file())
    right_files = sorted(path.relative_to(right) for path in right.rglob("*") if path.is_file())
    return left_files == right_files and all(
        (left / relative).read_bytes() == (right / relative).read_bytes() for relative in left_files
    )


def sync_public_catalog(source_catalog: Path, destination_catalog: Path) -> dict[str, object]:
    source_entries = validate_public_catalog(source_catalog)
    _validate_catalog_root(destination_catalog, allow_create=True)
    if (destination_catalog / "index.json").exists():
        validate_public_catalog(destination_catalog)
    elif any((destination_catalog / "species").iterdir()):
        raise CatalogPublishError("Catalog with species entries must include index.json")

    source_by_taxon = {entry.taxon_id: entry for entry in source_entries}
    destination_species = destination_catalog / "species"
    published: list[dict[str, object]] = []
    existing: list[int] = []

    for taxon_id, entry in sorted(source_by_taxon.items()):
        source = source_catalog / "species" / f"{taxon_id}-{entry.slug}"
        matches = sorted(destination_species.glob(f"{taxon_id}-*"))
        if len(matches) > 1:
            raise CatalogPublishError(f"Catalog contains multiple directories for taxon {taxon_id}")
        if matches:
            if matches[0].name != source.name or not _trees_match(source, matches[0]):
                raise CatalogPublishError(
                    f"Catalog taxon {taxon_id} conflicts with immutable local approval"
                )
            existing.append(taxon_id)
            continue
        shutil.copytree(source, destination_species / source.name)
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
    return {"published": published, "already_present": existing}


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
) -> subprocess.CompletedProcess[str]:
    return _run([str(publication.gh_path), *arguments], input_text=input_text)


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
    authenticated = _gh(publication, "api", "user", "--jq", ".login").stdout.strip()
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
