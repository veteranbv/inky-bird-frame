"""Typed generation recovery shared by the CLI and owner operations."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
from typing import cast

from .catalog import (
    approve_candidate,
    catalog_state_lock,
    find_taxon_directory,
    has_valid_approved_candidate,
    read_json,
    reject_candidate,
    sha256_file,
)
from .codex_runner import parse_species_profile
from .config import AppConfig
from .controller import (
    HUMAN_REVIEW_SOURCE,
    REVIEW_FAILURE_FALLBACK,
    archive_invalid_approved_catalog_state,
    current_discovery_species,
    ensure_generation_retry,
    exclusive_cycle_lock,
    read_generation_queue,
    retry_approved_candidate,
)
from .errors import CatalogError, CatalogPublishError, GenerationError, SpeciesStateError
from .http import MAX_ASSET_BYTES
from .journal import MAX_JOURNAL_BYTES, JournalConflictError, JournalError
from .models import ProfileConflict, SpeciesProfileData
from .publisher import validate_pending_candidate
from .retry import RetryStore, parse_retry_profile_conflicts


def _candidate_bytes(path: Path, maximum: int) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                raise ValueError("file")
            body = handle.read(maximum + 1)
        if len(body) > maximum:
            raise ValueError("size")
        return body
    except (OSError, ValueError) as exc:
        raise JournalError(
            "This candidate asset is unavailable or unsafe. Use operator recovery."
        ) from exc


def _pending_manifest(config: AppConfig, taxon_id: int) -> tuple[Path, dict[str, object], bytes]:
    pending_root = config.controller.state_dir / "pending"
    if pending_root.is_symlink():
        raise JournalError("Pending candidate storage is unsafe. Use operator recovery.")
    root = pending_root.resolve()
    path = find_taxon_directory(root, taxon_id)
    if path is None:
        raise JournalConflictError("This candidate is no longer pending. Reload Plate work.")
    if path.resolve() != path or not path.is_dir():
        raise JournalError("Pending candidate storage is unsafe. Use operator recovery.")
    manifest_bytes = _candidate_bytes(path / "manifest.json", MAX_JOURNAL_BYTES)
    try:
        manifest = json.loads(manifest_bytes)
        if (
            not isinstance(manifest, dict)
            or type(manifest.get("taxon_id")) is not int
            or manifest["taxon_id"] != taxon_id
            or manifest.get("status") != "pending"
            or not all(
                isinstance(manifest.get(key), str) and manifest[key].strip()
                for key in ("common_name", "scientific_name", "slug")
            )
            or path.name != f"{taxon_id}-{manifest['slug']}"
        ):
            raise ValueError("identity")
    except (ValueError, UnicodeDecodeError) as exc:
        raise JournalError("This pending manifest is invalid. Use operator recovery.") from exc
    return path, cast(dict[str, object], manifest), manifest_bytes


def _pending_candidate(
    config: AppConfig, taxon_id: int
) -> tuple[Path, dict[str, object], bytes, str]:
    path, manifest, manifest_bytes = _pending_manifest(config, taxon_id)
    portrait = _candidate_bytes(path / "portrait.png", MAX_ASSET_BYTES)
    display = _candidate_bytes(path / "display.png", MAX_ASSET_BYTES)
    digests = {
        "manifest.json": hashlib.sha256(manifest_bytes).hexdigest(),
        "portrait.png": hashlib.sha256(portrait).hexdigest(),
        "display.png": hashlib.sha256(display).hexdigest(),
    }
    for filename in ("profile.json", "quality-review.json"):
        if (path / filename).exists():
            digests[filename] = hashlib.sha256(
                _candidate_bytes(path / filename, MAX_JOURNAL_BYTES)
            ).hexdigest()
    revision = hashlib.sha256(json.dumps(digests, sort_keys=True).encode()).hexdigest()
    return path, manifest, portrait, revision


def pending_inventory(config: AppConfig) -> dict[str, object]:
    """Inventory manifests only; status polling must not re-hash full images."""
    root = config.controller.state_dir / "pending"
    if root.is_symlink() or root.exists() and not root.is_dir():
        return {"status": "unavailable", "candidates": []}
    candidates: list[dict[str, object]] = []
    ids = set()
    unknown = False
    for directory in sorted(root.iterdir()) if root.exists() else []:
        prefix, separator, _ = directory.name.partition("-")
        if not separator or not prefix.isascii() or not prefix.isdigit() or int(prefix) <= 0:
            unknown = True
            continue
        taxon_id = int(prefix)
        if taxon_id in ids:
            unknown = True
            continue
        ids.add(taxon_id)
        try:
            _, manifest, _ = _pending_manifest(config, taxon_id)
            if any(
                not (directory / filename).is_file() or (directory / filename).is_symlink()
                for filename in ("portrait.png", "display.png")
            ):
                raise JournalError("Candidate assets need operator recovery")
            candidates.append(
                {
                    "taxon_id": taxon_id,
                    "common_name": manifest["common_name"],
                    "scientific_name": manifest["scientific_name"],
                    "inspect_available": True,
                }
            )
        except (JournalError, CatalogError, OSError):
            candidates.append(
                {
                    "taxon_id": taxon_id,
                    "common_name": f"Taxon {taxon_id}",
                    "scientific_name": "",
                    "inspect_available": False,
                }
            )
    return {"status": "incomplete" if unknown else "ok", "candidates": candidates}


def pending_candidate(config: AppConfig, taxon_id: int) -> dict[str, object]:
    """Owner-only allowlist; generated candidates are never approved/public art."""
    with catalog_state_lock(config.controller.state_dir):
        path, manifest, _, revision = _pending_candidate(config, taxon_id)
        ready = True
        try:
            validate_pending_candidate(path)
        except (CatalogPublishError, CatalogError, OSError, ValueError):
            ready = False
        profile: SpeciesProfileData | None
        try:
            profile = parse_species_profile(manifest.get("profile"))
            if (
                profile["taxon_id"] != taxon_id
                or profile["common_name"] != manifest["common_name"]
                or profile["scientific_name"] != manifest["scientific_name"]
            ):
                profile = None
                ready = False
        except GenerationError:
            profile = None
            ready = False
        review = manifest.get("quality_review")
        if not isinstance(review, dict):
            review = {}
        sources = review.get("verification_sources", [])
        return {
            "taxon_id": taxon_id,
            "common_name": manifest["common_name"],
            "scientific_name": manifest["scientific_name"],
            "candidate_revision": revision,
            "portrait_url": f"/v1/owner/candidates/{taxon_id}/portrait?revision={revision}",
            "can_approve": ready,
            "profile": profile,
            "quality_review": {
                key: review[key]
                for key in (
                    "passed",
                    "species_accuracy",
                    "anatomy_accuracy",
                    "text_accuracy",
                    "composition_quality",
                    "location_free",
                )
                if type(review.get(key)) in {int, bool}
            },
            "verification_sources": [
                {"title": item["title"], "url": item["url"]}
                for item in sources
                if isinstance(item, dict)
                and isinstance(item.get("title"), str)
                and isinstance(item.get("url"), str)
                and item["url"].startswith("https://")
            ]
            if isinstance(sources, list)
            else [],
            "notice": "Not approved artwork. Approve only after checking the full plate "
            "and sourced review. Approval makes it available locally and to your "
            "existing publication policy."
            if ready
            else "This candidate does not meet ordinary publication checks. "
            "It cannot be approved here. Reject it or use explicit operator recovery; "
            "do not bypass the gate.",
        }


def pending_portrait(config: AppConfig, taxon_id: int, *, revision: str) -> bytes:
    with catalog_state_lock(config.controller.state_dir):
        _, _, body, current = _pending_candidate(config, taxon_id)
        if current != revision:
            raise JournalConflictError("This candidate changed. Reload its review before viewing.")
        return body


def decide_pending_candidate(
    config: AppConfig, taxon_id: int, *, revision: str, approve: bool, reason: str = ""
) -> dict[str, object]:
    with catalog_state_lock(config.controller.state_dir):
        path, _, _, current = _pending_candidate(config, taxon_id)
        if current != revision:
            raise JournalConflictError(
                "This candidate changed. Inspect the current plate before deciding."
            )
        if has_valid_approved_candidate(config.controller.catalog_dir, taxon_id):
            raise JournalError(
                "Approved art already exists. Replacement requires the explicit "
                "operator migration workflow."
            )
        if approve:
            try:
                validate_pending_candidate(path)
            except (CatalogPublishError, CatalogError, OSError, ValueError) as exc:
                raise JournalError(
                    "This candidate cannot pass ordinary approval. "
                    "Use operator recovery without bypassing review."
                ) from exc
            approve_candidate(config.controller.state_dir, config.controller.catalog_dir, taxon_id)
        else:
            if not reason.strip() or len(reason) > 2000:
                raise JournalError("Give a rejection reason of 1–2000 characters")
            reject_candidate(config.controller.state_dir, taxon_id, reason.strip())
    return {
        "taxon_id": taxon_id,
        "status": "approved" if approve else "rejected",
        "message": "Candidate approved locally. "
        "Existing catalog publication and display schedules still apply."
        if approve
        else "Candidate rejected. It remains held until an explicit reviewed retry; "
        "no model was started.",
    }


def _retry_quality_guidance(
    failed_directories: list[Path],
    source_attempt: int | None,
    allowed_domains: tuple[str, ...] | None,
) -> tuple[tuple[str, ...], Path | None, tuple[ProfileConflict, ...]]:
    if not failed_directories:
        if source_attempt is not None:
            raise ValueError("--source-attempt requires retained failed generation attempts")
        return (), None, ()
    attempts: list[tuple[int, Path]] = []
    for attempt_path in failed_directories[-1].glob("attempt-*"):
        if attempt_path.name == "attempt-history.json":
            continue
        try:
            attempt_number = int(attempt_path.name.removeprefix("attempt-"))
        except ValueError as exc:
            raise SpeciesStateError(f"Invalid generation attempt: {attempt_path}") from exc
        if attempt_number <= 0 or not attempt_path.is_dir():
            raise SpeciesStateError(f"Invalid generation attempt: {attempt_path}")
        attempts.append((attempt_number, attempt_path))
    if not attempts:
        if source_attempt is not None:
            raise ValueError("--source-attempt requires retained failed generation attempts")
        return (), None, ()
    attempts_by_number = dict(attempts)
    if source_attempt is None:
        selected_attempt, attempt_path = max(attempts, key=lambda item: item[0])
    else:
        if source_attempt <= 0 or source_attempt not in attempts_by_number:
            available = ", ".join(str(number) for number in sorted(attempts_by_number))
            raise ValueError(
                f"Generation attempt {source_attempt} is unavailable; choose one of: {available}"
            )
        selected_attempt = source_attempt
        attempt_path = attempts_by_number[source_attempt]
    review_path = attempt_path / "quality-review.json"
    if not review_path.is_file():
        raise SpeciesStateError(
            f"Generation attempt {selected_attempt} has no quality review: {attempt_path}"
        )
    try:
        review = json.loads(review_path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SpeciesStateError(f"Invalid quality review: {review_path}") from exc
    if not isinstance(review, dict) or review.get("passed") is not False:
        raise SpeciesStateError(f"Invalid failed quality review: {review_path}")
    findings = review.get("correction_findings")
    if findings is None:
        findings = review.get("findings")
    if not isinstance(findings, list) or any(
        not isinstance(finding, str) or not finding.strip() for finding in findings
    ):
        raise SpeciesStateError(f"Invalid quality review findings: {review_path}")
    try:
        profile_conflicts = parse_retry_profile_conflicts(
            review.get("profile_conflicts", []),
            review_path,
        )
    except CatalogError as exc:
        raise SpeciesStateError(f"Invalid quality review profile conflicts: {review_path}") from exc
    if allowed_domains is not None:
        try:
            profile_conflicts = parse_retry_profile_conflicts(
                list(profile_conflicts),
                review_path,
                allowed_domains=allowed_domains,
            )
        except CatalogError as exc:
            raise SpeciesStateError(
                "Retained profile conflict sources are outside the current research "
                "allowlist; restore the authorized domains or retry with --refresh-research"
            ) from exc
    source_plate = None
    if source_attempt is not None:
        source_plate = attempt_path / "portrait.png"
        if not source_plate.is_file():
            raise SpeciesStateError(
                f"Generation attempt {selected_attempt} has no portrait: {attempt_path}"
            )
    quality_findings = tuple(findings)
    if not quality_findings and not profile_conflicts:
        quality_findings = (REVIEW_FAILURE_FALLBACK,)
    return quality_findings, source_plate, profile_conflicts


def _retry_archived_quality_guidance(
    state_dir: Path,
    taxon_id: int,
    source_run: str,
    source_attempt: int,
    allowed_domains: tuple[str, ...] | None,
) -> tuple[tuple[str, ...], Path, tuple[ProfileConflict, ...]]:
    run_name = source_run.strip()
    if (
        not run_name
        or Path(run_name).is_absolute()
        or Path(run_name).parts != (run_name,)
        or not run_name.startswith(f"{taxon_id}-")
    ):
        raise ValueError("--source-run must be an archive directory name for the requested taxon")
    archive = (state_dir / "archive").resolve()
    run = state_dir / "archive" / run_name
    profile_path = run / "profile.json"
    if (
        run.is_symlink()
        or not run.is_dir()
        or run.resolve().parent != archive
        or profile_path.is_symlink()
        or not profile_path.is_file()
        or profile_path.resolve().parent != run.resolve()
    ):
        raise ValueError(f"Retained generation run is unavailable: {run_name}")
    profile = read_json(profile_path)
    if not isinstance(profile, dict) or profile.get("taxon_id") != taxon_id:
        raise SpeciesStateError(
            f"Retained generation profile identity does not match taxon {taxon_id}: {profile_path}"
        )
    for attempt_path in run.glob("attempt-*"):
        if attempt_path.name == "attempt-history.json":
            continue
        if (
            attempt_path.is_symlink()
            or not attempt_path.is_dir()
            or attempt_path.resolve().parent != run.resolve()
        ):
            raise SpeciesStateError(
                f"Retained generation attempt escapes its archive run: {attempt_path}"
            )
        for filename in ("quality-review.json", "portrait.png"):
            artifact_path = attempt_path / filename
            if artifact_path.is_symlink() or (
                artifact_path.exists() and artifact_path.resolve().parent != attempt_path.resolve()
            ):
                raise SpeciesStateError(
                    f"Retained generation artifact escapes its attempt: {artifact_path}"
                )
    findings, source_plate, profile_conflicts = _retry_quality_guidance(
        [run],
        source_attempt,
        allowed_domains,
    )
    if source_plate is None:
        raise SpeciesStateError(f"Retained generation attempt is unavailable: {run_name}")
    return findings, source_plate, profile_conflicts


def _retry_candidate_guidance(
    state_dir: Path,
    taxon_id: int,
    source_candidate: str,
) -> tuple[tuple[str, ...], Path, tuple[ProfileConflict, ...]]:
    candidate_name = source_candidate.strip()
    if (
        not candidate_name
        or Path(candidate_name).is_absolute()
        or Path(candidate_name).parts != (candidate_name,)
        or not candidate_name.startswith(f"{taxon_id}-")
    ):
        raise ValueError(
            "--source-candidate must be the archive directory name for the requested taxon"
        )
    archive = (state_dir / "archive").resolve()
    candidate = state_dir / "archive" / candidate_name
    if candidate.is_symlink() or not candidate.is_dir() or candidate.resolve().parent != archive:
        raise ValueError(f"Retained candidate is unavailable: {candidate_name}")
    manifest_path = candidate / "manifest.json"
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or manifest_path.resolve().parent != candidate.resolve()
    ):
        raise SpeciesStateError(f"Retained candidate has no valid manifest: {candidate}")
    manifest = read_json(manifest_path)
    rejection_reason = manifest.get("rejection_reason") if isinstance(manifest, dict) else None
    assets = manifest.get("assets") if isinstance(manifest, dict) else None
    portrait_asset = assets.get("portrait") if isinstance(assets, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("status") != "rejected"
        or manifest.get("taxon_id") != taxon_id
        or not isinstance(rejection_reason, str)
        or not rejection_reason.strip()
        or not isinstance(portrait_asset, dict)
        or portrait_asset.get("filename") != "portrait.png"
        or not isinstance(portrait_asset.get("sha256"), str)
    ):
        raise SpeciesStateError(f"Invalid retained candidate manifest: {manifest_path}")
    portrait = candidate / "portrait.png"
    if (
        portrait.is_symlink()
        or not portrait.is_file()
        or portrait.resolve().parent != candidate.resolve()
    ):
        raise SpeciesStateError(f"Retained candidate has no portrait: {candidate}")
    if sha256_file(portrait) != portrait_asset["sha256"]:
        raise SpeciesStateError(f"Retained candidate portrait checksum mismatch: {portrait}")
    return (rejection_reason.strip(),), portrait, ()


def _retry_species_identity(
    taxon_id: int,
    sources: list[Path],
    *,
    deferred_malformed_errors: list[CatalogError | SpeciesStateError] | None = None,
) -> tuple[str, str, Path] | None:
    identity: tuple[str, str, Path] | None = None
    for source in dict.fromkeys(sources):
        for filename in ("profile.json", "references.json", "manifest.json", "failure.json"):
            identity_path = source / filename
            if not identity_path.is_file():
                continue
            try:
                payload = read_json(identity_path)
            except CatalogError as exc:
                if deferred_malformed_errors is None:
                    raise
                deferred_malformed_errors.append(exc)
                continue
            if not isinstance(payload, dict):
                error = SpeciesStateError(f"Invalid retained candidate identity: {identity_path}")
                if deferred_malformed_errors is None:
                    raise error
                deferred_malformed_errors.append(error)
                continue
            retained_taxon_id = payload.get("taxon_id")
            if retained_taxon_id is None:
                continue
            if retained_taxon_id != taxon_id:
                raise SpeciesStateError(
                    f"Retained candidate identity does not match taxon {taxon_id}: {identity_path}"
                )
            common_name = payload.get("common_name")
            scientific_name = payload.get("scientific_name")
            if not isinstance(common_name, str) or not isinstance(scientific_name, str):
                continue
            candidate_identity = (common_name, scientific_name, identity_path)
            if identity is not None and identity[:2] != candidate_identity[:2]:
                raise SpeciesStateError(
                    f"Retained candidate identities conflict for taxon {taxon_id}"
                )
            identity = candidate_identity
    return identity


def _retry_archive_plan(
    state_dir: Path,
    sources: list[Path],
    correction_owner: Path | None,
    correction_source: Path | None,
) -> tuple[list[tuple[Path, Path]], Path | None]:
    archive = state_dir / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    reserved: set[Path] = set()
    moves: list[tuple[Path, Path]] = []
    archived_correction_source: Path | None = None
    for source in dict.fromkeys(sources):
        destination = archive / source.name
        counter = 1
        while destination.exists() or destination in reserved:
            destination = archive / f"{source.name}-{counter}"
            counter += 1
        reserved.add(destination)
        moves.append((source, destination))
        if source == correction_owner and correction_source is not None:
            archived_correction_source = destination / correction_source.relative_to(source)
    return moves, archived_correction_source


def retry_species(
    config: AppConfig,
    taxon_id: int,
    *,
    replace_approved: bool = False,
    refresh_research: bool = False,
    reason: str | None = None,
    source_attempt: int | None = None,
    source_candidate: str | None = None,
    source_run: str | None = None,
    corrections: list[str] | None = None,
) -> dict[str, object]:
    raw_reason = reason
    reason = raw_reason.strip() if isinstance(raw_reason, str) else None
    raw_corrections = corrections
    correction_override: tuple[str, ...] = ()
    if raw_corrections is not None:
        if not isinstance(raw_corrections, list) or any(
            not isinstance(correction, str) or not correction.strip()
            for correction in raw_corrections
        ):
            raise ValueError("--correction values must be non-empty")
        correction_override = tuple(correction.strip() for correction in raw_corrections)
        if len(set(correction_override)) != len(correction_override):
            raise ValueError("--correction values must not repeat")
    if source_attempt is not None and source_candidate is not None:
        raise ValueError("--source-attempt cannot be combined with --source-candidate")
    if source_run is not None and source_candidate is not None:
        raise ValueError("--source-run cannot be combined with --source-candidate")
    if source_run is not None and source_attempt is None:
        raise ValueError("--source-run requires --source-attempt")
    if correction_override and source_attempt is None and source_candidate is None:
        raise ValueError("--correction requires --source-attempt or --source-candidate")
    if replace_approved:
        if (
            source_attempt is not None
            or source_candidate is not None
            or source_run is not None
            or correction_override
        ):
            raise ValueError(
                "--replace-approved cannot be combined with a correction source selector"
            )
        if not reason:
            raise ValueError("--replace-approved requires a non-empty --reason")
        return retry_approved_candidate(
            config,
            taxon_id,
            reason,
            refresh_research=refresh_research,
        )
    if raw_reason is not None:
        raise ValueError("--reason requires --replace-approved")

    with exclusive_cycle_lock(config.controller.state_dir):
        pending = find_taxon_directory(config.controller.state_dir / "pending", taxon_id)
        if pending is not None and (pending / "manifest.json").is_file():
            raise ValueError("Pending candidates must be approved or rejected before retrying")
        with catalog_state_lock(config.controller.state_dir):
            if has_valid_approved_candidate(config.controller.catalog_dir, taxon_id):
                raise ValueError(
                    f"Taxon {taxon_id} is already approved; use --replace-approved "
                    "with --reason after human review"
                )
        failed_directories = sorted((config.controller.state_dir / "failed").glob(f"{taxon_id}-*"))
        quality_findings: tuple[str, ...]
        correction_source: Path | None
        profile_conflicts: tuple[ProfileConflict, ...]
        if source_run is not None and source_attempt is not None:
            quality_findings, correction_source, profile_conflicts = (
                _retry_archived_quality_guidance(
                    config.controller.state_dir,
                    taxon_id,
                    source_run,
                    source_attempt,
                    None if refresh_research else config.research.allowed_domains,
                )
            )
        elif source_candidate is None:
            quality_findings, correction_source, profile_conflicts = _retry_quality_guidance(
                failed_directories,
                source_attempt,
                None if refresh_research else config.research.allowed_domains,
            )
        else:
            quality_findings, correction_source, profile_conflicts = _retry_candidate_guidance(
                config.controller.state_dir,
                taxon_id,
                source_candidate,
            )
        if correction_override:
            quality_findings = correction_override
        sources = [pending] if pending is not None else []
        sources.extend(failed_directories)
        sources.extend(sorted((config.controller.state_dir / "rejected").glob(f"{taxon_id}-*")))
        terminal_sources = list(sources)
        retry_store = RetryStore(config.controller.state_dir / "generation-retries.json")
        existing_guidance = retry_store.quality_guidance(taxon_id)
        if existing_guidance is not None and not refresh_research:
            try:
                existing_profile_conflicts = parse_retry_profile_conflicts(
                    list(existing_guidance.profile_conflicts),
                    retry_store.path,
                    allowed_domains=config.research.allowed_domains,
                )
            except CatalogError as exc:
                raise SpeciesStateError(
                    "Stored profile conflict sources are outside the current research "
                    "allowlist; restore the authorized domains or retry with --refresh-research"
                ) from exc
        else:
            existing_profile_conflicts = ()
        if refresh_research:
            profile_conflicts = ()
            if existing_guidance is not None and not quality_findings:
                quality_findings = existing_guidance.findings
        selected_profile_conflicts = profile_conflicts
        reuse_existing_guidance = (
            not refresh_research
            and not quality_findings
            and not selected_profile_conflicts
            and existing_guidance is not None
        )
        if reuse_existing_guidance:
            assert existing_guidance is not None
            quality_findings = existing_guidance.findings
        conflicts_by_field = {
            conflict["field"]: conflict
            for conflicts in (
                existing_profile_conflicts,
                profile_conflicts,
            )
            for conflict in conflicts
        }
        profile_conflicts = tuple(conflicts_by_field.values())
        retry_record = retry_store.get(taxon_id)
        deferred = retry_record is not None
        if not sources and not deferred and source_candidate is None and source_run is None:
            raise ValueError(
                f"No failed, rejected, or deferred candidate exists for taxon {taxon_id}"
            )
        observed = next(
            (
                species
                for species in current_discovery_species(config)
                if species.taxon_id == taxon_id
            ),
            None,
        )
        deferred_identity_errors: list[CatalogError | SpeciesStateError] = []
        terminal_identity = _retry_species_identity(
            taxon_id,
            terminal_sources,
            deferred_malformed_errors=deferred_identity_errors,
        )
        identity = (
            (
                observed.common_name,
                observed.scientific_name,
                config.controller.state_dir / "discovery.json",
            )
            if observed is not None
            else None
        )
        queued = next(
            (species for species in read_generation_queue(config) if species.taxon_id == taxon_id),
            None,
        )
        if identity is None and queued is not None and HUMAN_REVIEW_SOURCE in queued.sources:
            identity = (
                queued.common_name,
                queued.scientific_name,
                config.controller.state_dir / "generation-queue.json",
            )
        if (
            identity is None
            and retry_record is not None
            and retry_record.common_name is not None
            and retry_record.scientific_name is not None
        ):
            identity = (
                retry_record.common_name,
                retry_record.scientific_name,
                retry_store.path,
            )
        if identity is None:
            identity = terminal_identity
        if identity is None and queued is not None:
            identity = (
                queued.common_name,
                queued.scientific_name,
                config.controller.state_dir / "generation-queue.json",
            )
        profile_cache = config.controller.state_dir / "profiles" / str(taxon_id)
        cached_profile_identity = (
            _retry_species_identity(
                taxon_id,
                [profile_cache],
                deferred_malformed_errors=(deferred_identity_errors if refresh_research else None),
            )
            if profile_cache.exists() and (not refresh_research or identity is None)
            else None
        )
        if identity is None:
            identity = cached_profile_identity
        incompatible_cached_profile = (
            identity is not None
            and cached_profile_identity is not None
            and cached_profile_identity[:2] != identity[:2]
        )
        cleared_cached_profile = (
            refresh_research or incompatible_cached_profile
        ) and profile_cache.exists()
        if cleared_cached_profile:
            sources.append(profile_cache)
        reference_cache = config.controller.state_dir / "references" / str(taxon_id)
        if identity is None:
            identity = _retry_species_identity(taxon_id, [reference_cache])
        cleared_cached_references = refresh_research and reference_cache.exists()
        if cleared_cached_references:
            sources.append(reference_cache)
        correction_owner = (
            next(
                (source for source in sources if correction_source.is_relative_to(source)),
                None,
            )
            if correction_source is not None
            else None
        )
        retained_correction_source = (
            correction_source if source_candidate is not None or source_run is not None else None
        )
        if (
            correction_source is not None
            and correction_owner is None
            and retained_correction_source is None
        ):
            raise SpeciesStateError("The selected correction source is outside retained state")
        if identity is None and correction_source is not None:
            identity = _retry_species_identity(
                taxon_id,
                [correction_source.parent, correction_source.parent.parent],
            )
        if identity is None and deferred_identity_errors:
            raise deferred_identity_errors[0]
        if identity is None:
            raise SpeciesStateError(
                f"Taxon {taxon_id} has no recoverable species identity; "
                "wait for rediscovery or restore its retained profile or references"
            )
        if identity is not None and retry_record is not None:
            retry_record = retry_store.set_identity(
                taxon_id,
                identity[0],
                identity[1],
            )
        invalid_approved_archive = archive_invalid_approved_catalog_state(
            config,
            taxon_id,
        )
        moved = [str(invalid_approved_archive)] if invalid_approved_archive is not None else []
        archive_plan, planned_correction_source = _retry_archive_plan(
            config.controller.state_dir,
            sources,
            correction_owner,
            correction_source,
        )
        archived_correction_source = retained_correction_source or planned_correction_source
        guidance_source_plate = (
            archived_correction_source.relative_to(config.controller.state_dir).as_posix()
            if archived_correction_source is not None
            else existing_guidance.source_plate
            if existing_guidance is not None
            else None
        )
        final_guidance = None if refresh_research else existing_guidance
        if refresh_research and existing_guidance is not None:
            retry_store.clear_quality_guidance(taxon_id)
        if (quality_findings or profile_conflicts) and not reuse_existing_guidance:
            final_guidance = retry_store.set_quality_guidance(
                taxon_id,
                quality_findings,
                source_plate=guidance_source_plate,
                invariant_findings=(
                    existing_guidance.invariant_findings if existing_guidance is not None else ()
                ),
                profile_conflicts=profile_conflicts,
            )
        terminal_source_set = set(terminal_sources)
        cache_moves = [move for move in archive_plan if move[0] not in terminal_source_set]
        terminal_moves = [move for move in archive_plan if move[0] in terminal_source_set]
        if identity is not None and terminal_moves:
            ensure_generation_retry(
                config,
                taxon_id,
                identity[0],
                identity[1],
                identity[2],
            )
        for source, destination in cache_moves:
            shutil.move(str(source), destination)
            moved.append(str(destination))
        if identity is not None and not terminal_moves:
            ensure_generation_retry(
                config,
                taxon_id,
                identity[0],
                identity[1],
                identity[2],
            )
        if terminal_moves:
            with catalog_state_lock(config.controller.state_dir):
                for source, destination in terminal_moves:
                    shutil.move(str(source), destination)
                    moved.append(str(destination))
        retry_store.clear(taxon_id)
        queued_for_generation = any(
            species.taxon_id == taxon_id for species in read_generation_queue(config)
        )
    return {
        "taxon_id": taxon_id,
        "status": "eligible",
        "archived": moved,
        "cleared_deferred_retry": deferred,
        "cleared_cached_profile": cleared_cached_profile,
        "cleared_cached_references": cleared_cached_references,
        "preserved_quality_findings_count": (
            len(final_guidance.findings) if final_guidance is not None else 0
        ),
        "preserved_profile_conflicts_count": (
            len(final_guidance.profile_conflicts) if final_guidance is not None else 0
        ),
        "replaced_approved": False,
        "source_attempt": source_attempt,
        "source_candidate": source_candidate,
        "source_run": source_run,
        "correction_override_count": len(correction_override),
        "queued_for_generation": queued_for_generation,
        "preserved_correction_source": (
            final_guidance is not None and final_guidance.source_plate is not None
        ),
    }
