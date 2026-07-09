"""Controller cycle: discover species, acquire references, generate, and stage."""

from __future__ import annotations

import fcntl
import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from .birds import BirdSpecies, fetch_inaturalist_birds, fetch_taxon_context
from .catalog import (
    approve_candidate,
    approved_taxon_ids,
    candidate_directory,
    find_taxon_directory,
    write_candidate_manifest,
    write_json_atomic,
)
from .codex_runner import CodexRunner
from .config import AppConfig
from .errors import (
    CatalogError,
    DataSourceError,
    GenerationError,
    InkyBirdFrameError,
    InsufficientReferencesError,
)
from .geo import ZipLocation, lookup_us_zip
from .images import prepare_generated_plate
from .models import ReferencePhoto
from .prompts import PROMPT_VERSION
from .references import download_references, fetch_reference_candidates


@contextmanager
def exclusive_cycle_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "controller.lock"
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise GenerationError("Another controller cycle is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def discover_species(config: AppConfig) -> tuple[ZipLocation, list[BirdSpecies]]:
    location = lookup_us_zip(config.discovery.zip_code)
    species = fetch_inaturalist_birds(
        latitude=location.latitude,
        longitude=location.longitude,
        radius_km=config.discovery.radius_km,
        limit=config.discovery.species_limit,
        window=config.discovery.observation_window,
    )
    return location, species


def _reference_from_dict(raw: object) -> ReferencePhoto:
    if not isinstance(raw, dict):
        raise CatalogError("Reference manifest entry must be an object")
    integer_fields = ("photo_id", "observation_id", "width", "height")
    string_fields = (
        "observer",
        "attribution",
        "license_code",
        "source_url",
        "image_url",
        "filename",
        "sha256",
    )
    if any(not isinstance(raw.get(field), int) for field in integer_fields) or any(
        not isinstance(raw.get(field), str) for field in string_fields
    ):
        raise CatalogError("Reference manifest entry has invalid fields")
    return ReferencePhoto(
        photo_id=cast(int, raw["photo_id"]),
        observation_id=cast(int, raw["observation_id"]),
        observer=cast(str, raw["observer"]),
        attribution=cast(str, raw["attribution"]),
        license_code=cast(str, raw["license_code"]),
        source_url=cast(str, raw["source_url"]),
        image_url=cast(str, raw["image_url"]),
        width=cast(int, raw["width"]),
        height=cast(int, raw["height"]),
        filename=cast(str, raw["filename"]),
        sha256=cast(str, raw["sha256"]),
    )


def load_or_fetch_references(config: AppConfig, species: BirdSpecies) -> list[ReferencePhoto]:
    directory = config.controller.state_dir / "references" / str(species.taxon_id)
    manifest_path = directory / "references.json"
    if manifest_path.is_file():
        try:
            raw = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as exc:
            raise CatalogError(f"Invalid reference manifest: {manifest_path}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("references"), list):
            raise CatalogError(f"Invalid reference manifest: {manifest_path}")
        references = [_reference_from_dict(item) for item in raw["references"]]
        missing = [
            item.filename for item in references if not (directory / item.filename).is_file()
        ]
        if missing:
            raise CatalogError(f"Reference files are missing: {', '.join(missing)}")
        return references

    candidates = fetch_reference_candidates(
        species.taxon_id,
        config.controller.references_per_species,
    )
    references = download_references(candidates, directory)
    write_json_atomic(
        manifest_path,
        {
            "schema_version": 1,
            "taxon_id": species.taxon_id,
            "common_name": species.common_name,
            "scientific_name": species.scientific_name,
            "references": [reference.as_dict() for reference in references],
        },
    )
    return references


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def generate_candidate(config: AppConfig, species: BirdSpecies, workspace: Path) -> Path:
    state_dir = config.controller.state_dir
    if species.taxon_id in approved_taxon_ids(config.controller.catalog_dir):
        raise CatalogError(f"Taxon {species.taxon_id} is already approved")
    if find_taxon_directory(state_dir / "pending", species.taxon_id) is not None:
        raise CatalogError(f"Taxon {species.taxon_id} already has a pending candidate")

    references = load_or_fetch_references(config, species)
    reference_root = state_dir / "references" / str(species.taxon_id)
    reference_paths = [reference_root / reference.filename for reference in references]
    context = fetch_taxon_context(species.taxon_id)
    runner = CodexRunner(config.controller.codex_path, workspace)
    work_parent = state_dir / "work"
    work_parent.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(prefix=f"{species.taxon_id}-", dir=work_parent) as temporary:
        work = Path(temporary)
        logs = state_dir / "runs" / f"{species.taxon_id}-{_timestamp()}"
        profile_path = work / "profile.json"
        profile = runner.create_profile(
            species,
            context,
            references,
            reference_paths,
            profile_path,
            logs / "01-profile.log",
        )
        correction_findings: tuple[str, ...] = ()
        history: list[dict[str, object]] = []
        for attempt in range(1, config.controller.max_generation_attempts + 1):
            attempt_dir = work / f"attempt-{attempt:02d}"
            attempt_dir.mkdir()
            generated_path = attempt_dir / "generated.png"
            runner.generate_plate(
                species,
                profile,
                references,
                reference_paths,
                generated_path,
                logs / f"02-generation-attempt-{attempt:02d}.log",
                correction_findings,
            )
            portrait_path = attempt_dir / "portrait.png"
            display_path = attempt_dir / "display.png"
            prepare_generated_plate(generated_path, portrait_path, display_path)
            generated_path.unlink()

            review = runner.review_plate(
                species,
                profile,
                references,
                portrait_path,
                reference_paths,
                attempt_dir / "quality-review.json",
                logs / f"03-quality-review-attempt-{attempt:02d}.log",
            )
            write_json_atomic(attempt_dir / "quality-review.json", review.as_dict())
            history.append({"attempt": attempt, "quality_review": review.as_dict()})
            if review.passed:
                shutil.copy2(profile_path, attempt_dir / "profile.json")
                write_json_atomic(attempt_dir / "attempt-history.json", history)
                write_candidate_manifest(
                    attempt_dir,
                    species,
                    profile,
                    references,
                    review,
                    generator="Codex subscription / built-in gpt-image-2",
                    prompt_version=PROMPT_VERSION,
                    attempt=attempt,
                    max_attempts=config.controller.max_generation_attempts,
                )
                destination = candidate_directory(state_dir, species)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise CatalogError(f"Pending destination already exists: {destination}")
                shutil.copytree(attempt_dir, destination)
                return destination
            correction_findings = review.findings or (
                "The previous attempt did not meet every automated review threshold.",
            )

        failed = state_dir / "failed" / f"{species.taxon_id}-{_timestamp()}"
        failed.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(work, failed)
        raise GenerationError(
            "Generated plate failed automated quality review after "
            f"{config.controller.max_generation_attempts} attempts; artifacts retained at {failed}"
        )


def _has_terminal_state(state_dir: Path, taxon_id: int) -> bool:
    return any(
        find_taxon_directory(state_dir / category, taxon_id) is not None
        for category in ("pending", "rejected")
    ) or bool(list((state_dir / "failed").glob(f"{taxon_id}-*")))


def record_failure(state_dir: Path, species: BirdSpecies, error: InkyBirdFrameError) -> Path:
    existing = sorted((state_dir / "failed").glob(f"{species.taxon_id}-*"))
    if existing:
        return existing[-1]
    destination = state_dir / "failed" / f"{species.taxon_id}-{_timestamp()}"
    write_json_atomic(
        destination / "failure.json",
        {
            "schema_version": 1,
            "status": "failed",
            "failed_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "taxon_id": species.taxon_id,
            "common_name": species.common_name,
            "scientific_name": species.scientific_name,
            "error_type": type(error).__name__,
            "error": str(error),
        },
    )
    return destination


def approve_passing_candidates(config: AppConfig) -> list[dict[str, object]]:
    published: list[dict[str, object]] = []
    pending_root = config.controller.state_dir / "pending"
    for manifest_path in sorted(pending_root.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as exc:
            raise CatalogError(f"Invalid pending manifest: {manifest_path}") from exc
        if not isinstance(manifest, dict) or not isinstance(manifest.get("taxon_id"), int):
            raise CatalogError(f"Pending manifest has no taxon ID: {manifest_path}")
        review = manifest.get("quality_review")
        generation = manifest.get("generation")
        if (
            not isinstance(review, dict)
            or not _has_passing_sourced_review(review)
            or not _is_bounded_generation(generation)
        ):
            continue
        entry = approve_candidate(
            config.controller.state_dir,
            config.controller.catalog_dir,
            cast(int, manifest["taxon_id"]),
        )
        published.append(entry.as_dict())
    return published


def _has_passing_sourced_review(review: dict[str, object]) -> bool:
    score_fields = (
        "species_accuracy",
        "anatomy_accuracy",
        "text_accuracy",
        "composition_quality",
    )
    if (
        review.get("passed") is not True
        or review.get("location_free") is not True
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


def _is_bounded_generation(generation: object) -> bool:
    if not isinstance(generation, dict):
        return False
    attempt = generation.get("attempt")
    max_attempts = generation.get("max_attempts")
    return (
        isinstance(attempt, int)
        and not isinstance(attempt, bool)
        and isinstance(max_attempts, int)
        and not isinstance(max_attempts, bool)
        and 1 <= attempt <= max_attempts
    )


def run_controller_cycle(config: AppConfig) -> dict[str, object]:
    with exclusive_cycle_lock(config.controller.state_dir):
        published = approve_passing_candidates(config)
        location, species_list = discover_species(config)
        approved = approved_taxon_ids(config.controller.catalog_dir)
        eligible = [
            species
            for species in species_list
            if species.taxon_id not in approved
            and not _has_terminal_state(config.controller.state_dir, species.taxon_id)
        ]
        generated: list[dict[str, object]] = []
        failures: list[dict[str, object]] = []
        for species in eligible[: config.controller.generations_per_cycle]:
            try:
                generate_candidate(config, species, config.controller.workspace_dir)
                entry = approve_candidate(
                    config.controller.state_dir,
                    config.controller.catalog_dir,
                    species.taxon_id,
                )
                generated.append(
                    {
                        "taxon_id": species.taxon_id,
                        "common_name": species.common_name,
                        "published": entry.as_dict(),
                    }
                )
            except InsufficientReferencesError as exc:
                failure_path = record_failure(config.controller.state_dir, species, exc)
                failures.append(
                    {
                        "taxon_id": species.taxon_id,
                        "common_name": species.common_name,
                        "error": str(exc),
                        "failure": str(failure_path),
                        "terminal": True,
                    }
                )
            except DataSourceError as exc:
                failures.append(
                    {
                        "taxon_id": species.taxon_id,
                        "common_name": species.common_name,
                        "error": str(exc),
                        "terminal": False,
                    }
                )
            except InkyBirdFrameError as exc:
                failure_path = record_failure(config.controller.state_dir, species, exc)
                failures.append(
                    {
                        "taxon_id": species.taxon_id,
                        "common_name": species.common_name,
                        "error": str(exc),
                        "failure": str(failure_path),
                        "terminal": True,
                    }
                )

        return {
            "discovery": {
                "place_name": location.place_name,
                "state": location.state,
                "window": config.discovery.observation_window.value,
                "radius_km": config.discovery.radius_km,
                "species_count": len(species_list),
            },
            "approved_count": len(approved_taxon_ids(config.controller.catalog_dir)),
            "published_pending": published,
            "eligible_count": len(eligible),
            "generated": generated,
            "failures": failures,
        }
