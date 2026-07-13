"""Controller cycle: discover species, acquire references, generate, and stage."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from .birds import (
    BirdSpecies,
    BirdWeatherSpecies,
    EbirdSpecies,
    ObservationWindow,
    fetch_birdweather_species,
    fetch_ebird_observations,
    fetch_inaturalist_birds,
    fetch_taxon_context,
    resolve_birdweather_species,
    resolve_ebird_species,
)
from .catalog import (
    approve_candidate,
    approved_taxon_ids,
    candidate_directory,
    catalog_state_lock,
    find_taxon_directory,
    has_passing_sourced_review,
    is_bounded_generation,
    rebuild_catalog_index,
    write_candidate_manifest,
    write_json_atomic,
)
from .codex_runner import CodexRunner, parse_species_profile
from .config import AppConfig, DiscoverySource
from .errors import (
    CatalogError,
    DataSourceError,
    GenerationError,
    InkyBirdFrameError,
    InsufficientReferencesError,
    MissingDependencyError,
    QualityReviewError,
)
from .geo import ZipLocation, lookup_us_zip
from .images import prepare_generated_plate
from .models import ReferencePhoto, SpeciesProfileData
from .prompts import PROMPT_VERSION
from .references import download_references, fetch_reference_candidates
from .research import ResearchBudget
from .retry import RetryStore


@dataclass(frozen=True)
class DiscoverySnapshot:
    refreshed_at: datetime
    place_name: str
    state: str
    species: list[BirdSpecies]


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    status: str
    species_count: int
    unresolved_count: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "name": self.name,
            "status": self.status,
            "species_count": self.species_count,
            "unresolved_count": self.unresolved_count,
        }
        if self.error is not None:
            value["error"] = self.error
        return value


@dataclass(frozen=True)
class DiscoveryResult:
    location: ZipLocation | None
    species: list[BirdSpecies]
    providers: list[ProviderStatus]
    unresolved: list[EbirdSpecies | BirdWeatherSpecies]


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


@contextmanager
def exclusive_refresh_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "refresh.lock").open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DataSourceError("Another observation refresh is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def discover_species(
    config: AppConfig,
    *,
    source: DiscoverySource | None = None,
    window: ObservationWindow | None = None,
    radius_km: int | None = None,
    species_limit: int | None = None,
    persist_taxonomy_cache: bool = True,
) -> DiscoveryResult:
    selected_source = source or config.discovery.source
    selected_window = window or config.discovery.observation_window
    selected_radius = radius_km if radius_km is not None else config.discovery.radius_km
    selected_limit = species_limit if species_limit is not None else config.discovery.species_limit
    if selected_source.uses_ebird:
        if selected_window in {ObservationWindow.LAST_YEAR, ObservationWindow.ALL_TIME}:
            raise ValueError("eBird discovery supports observation windows up to 30 days")
        if not 0 < selected_radius <= 50:
            raise ValueError("eBird discovery radius_km must be between 1 and 50")
        if not 0 < selected_limit <= 10_000:
            raise ValueError("eBird species_limit must be between 1 and 10000")
    if selected_source.uses_birdweather and not 0 < selected_limit <= 100:
        raise ValueError("BirdWeather species_limit must be between 1 and 100")
    providers: list[ProviderStatus] = []
    provider_species: list[list[BirdSpecies]] = []
    unresolved: list[EbirdSpecies | BirdWeatherSpecies] = []
    location: ZipLocation | None = None

    location_provider_names: list[str] = []
    if selected_source in {
        DiscoverySource.INATURALIST,
        DiscoverySource.COMBINED,
        DiscoverySource.ALL,
    }:
        location_provider_names.append("inaturalist")
    if selected_source in {DiscoverySource.EBIRD, DiscoverySource.COMBINED, DiscoverySource.ALL}:
        location_provider_names.append("ebird")
    if location_provider_names:
        try:
            location = lookup_us_zip(config.discovery.zip_code)
        except DataSourceError as exc:
            providers.extend(
                ProviderStatus(name, "error", 0, error=f"ZIP lookup failed: {exc}")
                for name in location_provider_names
            )

    if location is not None and selected_source in {
        DiscoverySource.INATURALIST,
        DiscoverySource.COMBINED,
        DiscoverySource.ALL,
    }:
        try:
            inaturalist = fetch_inaturalist_birds(
                latitude=location.latitude,
                longitude=location.longitude,
                radius_km=selected_radius,
                limit=selected_limit,
                window=selected_window,
            )
        except DataSourceError as exc:
            providers.append(ProviderStatus("inaturalist", "error", 0, error=str(exc)))
        else:
            provider_species.append(inaturalist)
            providers.append(ProviderStatus("inaturalist", "ok", len(inaturalist)))

    if location is not None and selected_source in {
        DiscoverySource.EBIRD,
        DiscoverySource.COMBINED,
        DiscoverySource.ALL,
    }:
        try:
            api_key = config.discovery.ebird_api_key
            if api_key is None and config.discovery.ebird_api_key_env is not None:
                environment_value = os.environ.get(config.discovery.ebird_api_key_env)
                api_key = environment_value.strip() if environment_value else None
            if api_key is None:
                raise DataSourceError("eBird API key is not configured")
            observations = fetch_ebird_observations(
                latitude=location.latitude,
                longitude=location.longitude,
                radius_km=selected_radius,
                limit=selected_limit,
                window=selected_window,
                api_key=api_key,
            )
            resolution = resolve_ebird_species(
                observations,
                config.controller.state_dir / "ebird-taxonomy-crosswalk.json",
                persist_cache=persist_taxonomy_cache,
            )
        except (DataSourceError, ValueError) as exc:
            providers.append(ProviderStatus("ebird", "error", 0, error=str(exc)))
        else:
            unresolved.extend(resolution.unresolved)
            if observations and not resolution.species:
                providers.append(
                    ProviderStatus(
                        "ebird",
                        "error",
                        0,
                        unresolved_count=len(resolution.unresolved),
                        error="No eBird observations had an exact iNaturalist species match",
                    )
                )
            else:
                provider_species.append(resolution.species)
                providers.append(
                    ProviderStatus(
                        "ebird",
                        "ok",
                        len(resolution.species),
                        unresolved_count=len(resolution.unresolved),
                    )
                )

    if selected_source in {DiscoverySource.BIRDWEATHER, DiscoverySource.ALL}:
        try:
            token = config.discovery.birdweather_token
            if token is None and config.discovery.birdweather_token_env is not None:
                environment_value = os.environ.get(config.discovery.birdweather_token_env)
                token = environment_value.strip() if environment_value else None
            if token is None:
                raise DataSourceError("BirdWeather station token is not configured")
            detections = fetch_birdweather_species(
                token=token,
                limit=selected_limit,
                window=selected_window,
            )
            resolved, birdweather_unresolved = resolve_birdweather_species(
                detections,
                config.controller.state_dir / "birdweather-taxonomy-crosswalk.json",
                persist_cache=persist_taxonomy_cache,
            )
        except (DataSourceError, ValueError) as exc:
            providers.append(ProviderStatus("birdweather", "error", 0, error=str(exc)))
        else:
            unresolved.extend(birdweather_unresolved)
            if detections and not resolved:
                providers.append(
                    ProviderStatus(
                        "birdweather",
                        "error",
                        0,
                        unresolved_count=len(birdweather_unresolved),
                        error="No BirdWeather detections had an exact iNaturalist species match",
                    )
                )
            else:
                provider_species.append(resolved)
                providers.append(
                    ProviderStatus(
                        "birdweather",
                        "ok",
                        len(resolved),
                        unresolved_count=len(birdweather_unresolved),
                    )
                )

    if not provider_species:
        failures = "; ".join(
            f"{provider.name}: {provider.error}" for provider in providers if provider.error
        )
        raise DataSourceError(f"All configured observation providers failed: {failures}")
    species = _merge_provider_species(provider_species)
    if selected_source in {DiscoverySource.COMBINED, DiscoverySource.ALL}:
        species.sort(key=lambda item: (item.common_name.casefold(), item.taxon_id))
    return DiscoveryResult(location, species, providers, unresolved)


def _merge_provider_species(provider_species: list[list[BirdSpecies]]) -> list[BirdSpecies]:
    merged: dict[int, BirdSpecies] = {}
    order: list[int] = []
    for result in provider_species:
        for species in result:
            existing = merged.get(species.taxon_id)
            if existing is None:
                merged[species.taxon_id] = species
                order.append(species.taxon_id)
                continue
            sources = tuple(dict.fromkeys((*existing.sources, *species.sources)))
            merged[species.taxon_id] = BirdSpecies(
                taxon_id=existing.taxon_id,
                common_name=existing.common_name,
                scientific_name=existing.scientific_name,
                observation_count=max(existing.observation_count, species.observation_count),
                source="+".join(sources),
                sources=sources,
            )
    return [merged[taxon_id] for taxon_id in order]


def _snapshot_path(config: AppConfig) -> Path:
    return config.controller.state_dir / "discovery.json"


def _active_catalog_path(config: AppConfig) -> Path:
    return config.controller.state_dir / "active-catalog.json"


def _generation_queue_path(config: AppConfig) -> Path:
    return config.controller.state_dir / "generation-queue.json"


def _species_payload(species: BirdSpecies) -> dict[str, object]:
    return {
        "taxon_id": species.taxon_id,
        "common_name": species.common_name,
        "scientific_name": species.scientific_name,
        "observation_count": species.observation_count,
        "source": species.source,
        "sources": list(species.sources),
    }


def _unresolved_species_payload(
    species: EbirdSpecies | BirdWeatherSpecies,
) -> dict[str, object]:
    if isinstance(species, EbirdSpecies):
        provider = "ebird"
        provider_species_id = species.species_code
    else:
        provider = "birdweather"
        provider_species_id = str(species.species_id)
    return {
        "provider": provider,
        "species_code": provider_species_id,
        "common_name": species.common_name,
        "scientific_name": species.scientific_name,
    }


def _parse_species_list(raw: object, source: Path) -> list[BirdSpecies]:
    if not isinstance(raw, list):
        raise CatalogError(f"Invalid species list in {source}")
    species: list[BirdSpecies] = []
    seen: set[int] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise CatalogError(f"Invalid species in {source}")
        taxon_id = item.get("taxon_id")
        observation_count = item.get("observation_count")
        strings = [item.get(name) for name in ("common_name", "scientific_name")]
        sources_value = item.get("sources")
        if sources_value is None and isinstance(item.get("source"), str):
            sources_value = [item["source"]]
        if (
            not isinstance(taxon_id, int)
            or isinstance(taxon_id, bool)
            or taxon_id <= 0
            or taxon_id in seen
            or not isinstance(observation_count, int)
            or isinstance(observation_count, bool)
            or observation_count < 0
            or any(not isinstance(value, str) or not value for value in strings)
            or not isinstance(sources_value, list)
            or not sources_value
            or any(not isinstance(value, str) or not value for value in sources_value)
        ):
            raise CatalogError(f"Invalid species in {source}")
        seen.add(taxon_id)
        species.append(
            BirdSpecies(
                taxon_id=taxon_id,
                common_name=cast(str, strings[0]),
                scientific_name=cast(str, strings[1]),
                observation_count=observation_count,
                source="+".join(cast(list[str], sources_value)),
                sources=tuple(cast(list[str], sources_value)),
            )
        )
    return species


def read_generation_queue(config: AppConfig) -> list[BirdSpecies]:
    path = _generation_queue_path(config)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise CatalogError(f"Invalid generation queue: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") not in {1, 2}:
        raise CatalogError(f"Unsupported generation queue: {path}")
    return _parse_species_list(raw.get("species"), path)


def _write_generation_queue(config: AppConfig, species: list[BirdSpecies]) -> None:
    write_json_atomic(
        _generation_queue_path(config),
        {
            "schema_version": 2,
            "updated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "species": [_species_payload(item) for item in species],
        },
    )


def enqueue_seed_species(
    config: AppConfig,
    *,
    window: ObservationWindow,
    source: DiscoverySource | None = None,
    radius_km: int | None = None,
    species_limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    radius = radius_km if radius_km is not None else config.discovery.radius_km
    limit = species_limit if species_limit is not None else config.discovery.species_limit
    if radius <= 0:
        raise ValueError("radius_km must be greater than zero")
    if limit <= 0:
        raise ValueError("species_limit must be greater than zero")

    cycle_lock = nullcontext() if dry_run else exclusive_cycle_lock(config.controller.state_dir)
    with cycle_lock:
        discovery = discover_species(
            config,
            source=source,
            window=window,
            radius_km=radius,
            species_limit=limit,
            persist_taxonomy_cache=not dry_run,
        )
        discovered = discovery.species
        approved = approved_taxon_ids(config.controller.catalog_dir)
        existing = [
            species for species in read_generation_queue(config) if species.taxon_id not in approved
        ]
        eligible = [
            species
            for species in discovered
            if species.taxon_id not in approved
            and not _has_terminal_state(config.controller.state_dir, species.taxon_id)
        ]
        queued_by_taxon = {species.taxon_id: species for species in existing}
        added: list[BirdSpecies] = []
        for species in eligible:
            if species.taxon_id in queued_by_taxon:
                continue
            queued_by_taxon[species.taxon_id] = species
            added.append(species)
        queued = list(queued_by_taxon.values())
        if not dry_run:
            _write_generation_queue(config, queued)

    return {
        "window": window.value,
        "source": (source or config.discovery.source).value,
        "radius_km": radius,
        "species_limit": limit,
        "discovered_count": len(discovered),
        "already_approved_count": sum(species.taxon_id in approved for species in discovered),
        "eligible_count": len(eligible),
        "added_count": len(added),
        "queued_count": len(queued),
        "dry_run": dry_run,
        "added": [_species_payload(species) for species in added],
        "providers": [provider.as_dict() for provider in discovery.providers],
        "unresolved_count": len(discovery.unresolved),
    }


def _write_active_catalog(config: AppConfig, species_list: list[BirdSpecies]) -> int:
    approved = rebuild_catalog_index(config.controller.catalog_dir)
    observed = {species.taxon_id: species for species in species_list}
    active: list[dict[str, object]] = []
    for entry in approved:
        species = observed.get(entry.taxon_id)
        if species is None:
            continue
        value = entry.as_dict()
        value["observation_count"] = species.observation_count
        active.append(value)
    write_json_atomic(
        _active_catalog_path(config),
        {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "species": active,
        },
    )
    return len(active)


def run_refresh_cycle(config: AppConfig) -> dict[str, object]:
    with exclusive_refresh_lock(config.controller.state_dir):
        previous_taxa: set[int] = set()
        place_name = ""
        state = ""
        if _snapshot_path(config).exists():
            previous = _read_discovery_snapshot(config)
            previous_taxa = {species.taxon_id for species in previous.species}
            if config.discovery.source is not DiscoverySource.BIRDWEATHER:
                place_name = previous.place_name
                state = previous.state
        discovery = discover_species(config)
        location = discovery.location
        if location is not None:
            place_name = location.place_name
            state = location.state
        species_list = discovery.species
        new_species = [species for species in species_list if species.taxon_id not in previous_taxa]
        with catalog_state_lock(config.controller.state_dir):
            refreshed_at = datetime.now(UTC).replace(microsecond=0)
            write_json_atomic(
                _snapshot_path(config),
                {
                    "schema_version": 2,
                    "refreshed_at": refreshed_at.isoformat(),
                    "place_name": place_name,
                    "state": state,
                    "providers": [provider.as_dict() for provider in discovery.providers],
                    "species": [_species_payload(species) for species in species_list],
                },
            )
            active_count = _write_active_catalog(config, species_list)
    return {
        "refreshed_at": refreshed_at.isoformat(),
        "place_name": place_name,
        "state": state,
        "window": config.discovery.observation_window.value,
        "radius_km": config.discovery.radius_km,
        "source": config.discovery.source.value,
        "providers": [provider.as_dict() for provider in discovery.providers],
        "unresolved_species": [
            _unresolved_species_payload(species) for species in discovery.unresolved
        ],
        "species_count": len(species_list),
        "new_species": [_species_payload(species) for species in new_species],
        "active_approved_count": active_count,
    }


def _read_discovery_snapshot(config: AppConfig) -> DiscoverySnapshot:
    path = _snapshot_path(config)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise DataSourceError("Discovery state is missing; run refresh before generation") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"Invalid discovery state: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") not in {1, 2}:
        raise CatalogError(f"Unsupported discovery state: {path}")
    refreshed_at = raw.get("refreshed_at")
    place_name = raw.get("place_name")
    state = raw.get("state")
    species_raw = raw.get("species")
    if (
        not isinstance(refreshed_at, str)
        or not isinstance(place_name, str)
        or not isinstance(state, str)
        or not isinstance(species_raw, list)
    ):
        raise CatalogError(f"Invalid discovery state: {path}")
    try:
        refreshed = datetime.fromisoformat(refreshed_at)
    except ValueError as exc:
        raise CatalogError(f"Invalid discovery timestamp: {path}") from exc
    if refreshed.tzinfo is None:
        raise CatalogError(f"Discovery timestamp has no timezone: {path}")

    species = _parse_species_list(species_raw, path)
    return DiscoverySnapshot(refreshed, place_name, state, species)


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


def load_or_create_profile(
    config: AppConfig,
    species: BirdSpecies,
    references: list[ReferencePhoto],
    reference_paths: list[Path],
    runner: CodexRunner,
    output_path: Path,
    log_path: Path,
) -> tuple[SpeciesProfileData, Path]:
    cache_path = config.controller.state_dir / "profiles" / str(species.taxon_id) / "profile.json"
    cached = cache_path.is_file()
    if cached:
        try:
            raw = json.loads(cache_path.read_text())
        except json.JSONDecodeError as exc:
            raise CatalogError(f"Invalid cached species profile: {cache_path}") from exc
        try:
            profile = parse_species_profile(raw, config.research.allowed_domains)
        except GenerationError as exc:
            raise CatalogError(f"Invalid cached species profile: {cache_path}") from exc
    else:
        if not config.research.enabled:
            raise GenerationError(
                f"Taxon {species.taxon_id} has no cached profile and research is disabled"
            )
        context = fetch_taxon_context(species.taxon_id)
        ResearchBudget(
            config.controller.state_dir / "research-budget.json",
            daily_limit=config.research.max_searches_per_day,
            species_limit=config.research.max_searches_per_species,
        ).consume(species.taxon_id)
        profile = runner.create_profile(
            species,
            context,
            references,
            reference_paths,
            output_path,
            log_path,
            allowed_domains=config.research.allowed_domains,
        )
    if (
        profile["taxon_id"] != species.taxon_id
        or profile["common_name"] != species.common_name
        or profile["scientific_name"] != species.scientific_name
    ):
        raise CatalogError(
            f"Cached species profile identity does not match taxon {species.taxon_id}"
        )
    write_json_atomic(output_path, profile)
    if not cached:
        write_json_atomic(cache_path, profile)
    return profile, output_path


def generate_candidate(config: AppConfig, species: BirdSpecies, workspace: Path) -> Path:
    state_dir = config.controller.state_dir
    if species.taxon_id in approved_taxon_ids(config.controller.catalog_dir):
        raise CatalogError(f"Taxon {species.taxon_id} is already approved")
    if find_taxon_directory(state_dir / "pending", species.taxon_id) is not None:
        raise CatalogError(f"Taxon {species.taxon_id} already has a pending candidate")

    references = load_or_fetch_references(config, species)
    reference_root = state_dir / "references" / str(species.taxon_id)
    reference_paths = [reference_root / reference.filename for reference in references]
    runner = CodexRunner(config.controller.codex_path, workspace)
    work_parent = state_dir / "work"
    work_parent.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(prefix=f"{species.taxon_id}-", dir=work_parent) as temporary:
        work = Path(temporary)
        logs = state_dir / "runs" / f"{species.taxon_id}-{_timestamp()}"
        profile_output_path = work / "profile.json"
        profile, profile_path = load_or_create_profile(
            config,
            species,
            references,
            reference_paths,
            runner,
            profile_output_path,
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
                allowed_domains=config.research.allowed_domains,
            )
            write_json_atomic(attempt_dir / "quality-review.json", review.as_dict())
            history.append({"attempt": attempt, "quality_review": review.as_dict()})
            if review.passed:
                shutil.copy2(profile_path, attempt_dir / "profile.json")
                write_json_atomic(logs / "attempt-history.json", history)
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
        raise QualityReviewError(
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
        if not has_passing_sourced_review(review) or not is_bounded_generation(generation):
            continue
        entry = approve_candidate(
            config.controller.state_dir,
            config.controller.catalog_dir,
            cast(int, manifest["taxon_id"]),
        )
        published.append(entry.as_dict())
    return published


def run_generation_cycle(config: AppConfig) -> dict[str, object]:
    with exclusive_cycle_lock(config.controller.state_dir):
        with catalog_state_lock(config.controller.state_dir):
            published = approve_passing_candidates(config)
        snapshot = _read_discovery_snapshot(config)
        maximum_age = timedelta(minutes=config.schedule.refresh_minutes * 2)
        if datetime.now(UTC) - snapshot.refreshed_at.astimezone(UTC) > maximum_age:
            raise DataSourceError(
                "Discovery state is stale; a successful refresh is required before generation"
            )
        species_list = snapshot.species
        queued_species = read_generation_queue(config)
        generation_species = list(species_list)
        observed_taxa = {species.taxon_id for species in species_list}
        generation_species.extend(
            species for species in queued_species if species.taxon_id not in observed_taxa
        )
        approved = approved_taxon_ids(config.controller.catalog_dir)
        eligible = [
            species
            for species in generation_species
            if species.taxon_id not in approved
            and not _has_terminal_state(config.controller.state_dir, species.taxon_id)
        ]
        generated: list[dict[str, object]] = []
        failures: list[dict[str, object]] = []
        retry_store = RetryStore(config.controller.state_dir / "generation-retries.json")
        attempted_count = 0
        for species in eligible:
            if len(generated) >= config.controller.generations_per_cycle:
                break
            if attempted_count >= config.controller.max_species_attempts_per_cycle:
                break
            if not retry_store.due(species.taxon_id, datetime.now(UTC)):
                continue
            attempted_count += 1
            try:
                generate_candidate(config, species, config.controller.workspace_dir)
                with catalog_state_lock(config.controller.state_dir):
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
                retry_store.clear(species.taxon_id)
            except InsufficientReferencesError as exc:
                retry = retry_store.record_failure(
                    species.taxon_id,
                    exc,
                    now=datetime.now(UTC),
                    initial_minutes=config.controller.retry_initial_minutes,
                    maximum_minutes=config.controller.retry_max_minutes,
                    fixed_minutes=config.controller.insufficient_references_retry_minutes,
                )
                failures.append(
                    {
                        "taxon_id": species.taxon_id,
                        "common_name": species.common_name,
                        "error": str(exc),
                        "retry_at": retry.next_attempt_at.isoformat(),
                        "terminal": False,
                    }
                )
            except DataSourceError as exc:
                retry = retry_store.record_failure(
                    species.taxon_id,
                    exc,
                    now=datetime.now(UTC),
                    initial_minutes=config.controller.retry_initial_minutes,
                    maximum_minutes=config.controller.retry_max_minutes,
                )
                failures.append(
                    {
                        "taxon_id": species.taxon_id,
                        "common_name": species.common_name,
                        "error": str(exc),
                        "retry_at": retry.next_attempt_at.isoformat(),
                        "terminal": False,
                    }
                )
            except QualityReviewError as exc:
                retry_store.clear(species.taxon_id)
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
            except (CatalogError, MissingDependencyError):
                raise
            except InkyBirdFrameError as exc:
                retry = retry_store.record_failure(
                    species.taxon_id,
                    exc,
                    now=datetime.now(UTC),
                    initial_minutes=config.controller.retry_initial_minutes,
                    maximum_minutes=config.controller.retry_max_minutes,
                )
                failures.append(
                    {
                        "taxon_id": species.taxon_id,
                        "common_name": species.common_name,
                        "error": str(exc),
                        "retry_at": retry.next_attempt_at.isoformat(),
                        "terminal": False,
                    }
                )

        with catalog_state_lock(config.controller.state_dir):
            latest_snapshot = _read_discovery_snapshot(config)
            active_count = _write_active_catalog(config, latest_snapshot.species)
            approved_after = approved_taxon_ids(config.controller.catalog_dir)
            remaining_queue = [
                species for species in queued_species if species.taxon_id not in approved_after
            ]
            _write_generation_queue(config, remaining_queue)
        eligible_taxa = {species.taxon_id for species in eligible}
        deferred = retry_store.deferred(eligible_taxa, datetime.now(UTC))
        outstanding_retries = retry_store.outstanding(eligible_taxa)
        return {
            "discovery": {
                "refreshed_at": snapshot.refreshed_at.isoformat(),
                "place_name": snapshot.place_name,
                "state": snapshot.state,
                "window": config.discovery.observation_window.value,
                "radius_km": config.discovery.radius_km,
                "species_count": len(species_list),
            },
            "approved_count": len(approved_taxon_ids(config.controller.catalog_dir)),
            "active_approved_count": active_count,
            "published_pending": published,
            "eligible_count": len(eligible),
            "attempted_count": attempted_count,
            "deferred_count": len(deferred),
            "deferred": [record.as_dict() for record in deferred],
            "outstanding_retry_count": len(outstanding_retries),
            "queued_count": len(remaining_queue),
            "generated": generated,
            "failures": failures,
        }


def run_controller_cycle(config: AppConfig) -> dict[str, object]:
    refresh = run_refresh_cycle(config)
    generation = run_generation_cycle(config)
    return {**generation, "refresh": refresh}
