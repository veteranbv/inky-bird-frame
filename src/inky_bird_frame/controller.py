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
    DateRange,
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
    CatalogEntry,
    CollectionEntry,
    CollectionOrigin,
    CollectionState,
    add_collection_taxa,
    approve_candidate,
    approved_taxon_ids,
    archive_invalid_approved_candidate,
    candidate_directory,
    catalog_state_lock,
    clear_catalog_staging,
    find_taxon_directory,
    has_passing_sourced_review,
    has_valid_approved_candidate,
    is_bounded_generation,
    read_catalog_entries,
    read_collection,
    read_collection_state,
    read_json,
    rebuild_catalog_index,
    remove_collection_taxa,
    sha256_file,
    utc_now,
    withdraw_approved_candidate,
    write_candidate_manifest,
    write_collection,
)
from .codex_runner import CodexRunner, parse_species_profile
from .config import AppConfig, DiscoveryProvider, discovery_source_label
from .errors import (
    CatalogError,
    DataSourceError,
    GenerationError,
    InkyBirdFrameError,
    InsufficientReferencesError,
    MissingDependencyError,
    QualityReviewError,
    SpeciesStateError,
)
from .geo import DiscoveryLocation, resolve_discovery_location
from .http import write_json_atomic
from .images import prepare_generated_plate
from .models import ReferencePhoto, SpeciesProfileData
from .prompts import PROMPT_VERSION
from .references import download_references, fetch_reference_candidates
from .research import ResearchBudget
from .retry import RetryStore
from .timeutil import parse_utc_timestamp

REVIEW_FAILURE_FALLBACK = "The previous attempt did not meet every automated review threshold."
HUMAN_REVIEW_SOURCE = "human-review"


def _merge_correction_findings(
    invariant_findings: tuple[str, ...],
    current_findings: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*invariant_findings, *current_findings)))


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
    location: DiscoveryLocation | None
    species: list[BirdSpecies]
    providers: list[ProviderStatus]
    unresolved: list[EbirdSpecies | BirdWeatherSpecies]


@dataclass(frozen=True)
class TerminalQueueEntry:
    species: BirdSpecies
    state: str
    paths: tuple[Path, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            **_species_payload(self.species),
            "terminal_state": self.state,
            "paths": [str(path) for path in self.paths],
        }


@dataclass(frozen=True)
class GenerationQueuePartition:
    actionable: list[BirdSpecies]
    terminal_blocked: list[TerminalQueueEntry]


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
    sources: tuple[DiscoveryProvider, ...] | None = None,
    window: ObservationWindow | None = None,
    date_range: DateRange | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_km: int | None = None,
    species_limit: int | None = None,
    persist_taxonomy_cache: bool = True,
) -> DiscoveryResult:
    selected_sources = sources or config.discovery.sources
    selected_window = window or config.discovery.observation_window
    selected_radius = radius_km if radius_km is not None else config.discovery.radius_km
    selected_limit = species_limit if species_limit is not None else config.discovery.species_limit
    if (latitude is None) != (longitude is None):
        raise ValueError("latitude and longitude must be provided together")
    if date_range is not None and selected_sources != (DiscoveryProvider.INATURALIST,):
        raise ValueError("explicit date ranges require --source inaturalist")
    if DiscoveryProvider.EBIRD in selected_sources:
        if selected_window in {ObservationWindow.LAST_YEAR, ObservationWindow.ALL_TIME}:
            raise ValueError("eBird discovery supports observation windows up to 30 days")
        if not 0 < selected_radius <= 50:
            raise ValueError("eBird discovery radius_km must be between 1 and 50")
        if not 0 < selected_limit <= 10_000:
            raise ValueError("eBird species_limit must be between 1 and 10000")
    if DiscoveryProvider.BIRDWEATHER in selected_sources and not 0 < selected_limit <= 100:
        raise ValueError("BirdWeather species_limit must be between 1 and 100")
    providers: list[ProviderStatus] = []
    provider_species: list[list[BirdSpecies]] = []
    unresolved: list[EbirdSpecies | BirdWeatherSpecies] = []
    location: DiscoveryLocation | None = None

    location_provider_names: list[str] = []
    if DiscoveryProvider.INATURALIST in selected_sources:
        location_provider_names.append("inaturalist")
    if DiscoveryProvider.EBIRD in selected_sources:
        location_provider_names.append("ebird")
    if location_provider_names:
        try:
            if latitude is None and longitude is None:
                location = resolve_discovery_location(config.discovery)
            else:
                location = resolve_discovery_location(
                    config.discovery,
                    latitude=latitude,
                    longitude=longitude,
                )
        except DataSourceError as exc:
            providers.extend(
                ProviderStatus(name, "error", 0, error=f"Location lookup failed: {exc}")
                for name in location_provider_names
            )

    if location is not None and DiscoveryProvider.INATURALIST in selected_sources:
        try:
            inaturalist = fetch_inaturalist_birds(
                latitude=location.latitude,
                longitude=location.longitude,
                radius_km=selected_radius,
                limit=selected_limit,
                window=selected_window,
                date_range=date_range,
            )
        except DataSourceError as exc:
            providers.append(ProviderStatus("inaturalist", "error", 0, error=str(exc)))
        else:
            provider_species.append(inaturalist)
            providers.append(ProviderStatus("inaturalist", "ok", len(inaturalist)))

    if location is not None and DiscoveryProvider.EBIRD in selected_sources:
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

    if DiscoveryProvider.BIRDWEATHER in selected_sources:
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
    if len(selected_sources) > 1:
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
                latest_detection_at=(existing.latest_detection_at or species.latest_detection_at),
            )
    return [merged[taxon_id] for taxon_id in order]


def _snapshot_path(config: AppConfig) -> Path:
    return config.controller.state_dir / "discovery.json"


def _active_catalog_path(config: AppConfig) -> Path:
    return config.controller.state_dir / "active-catalog.json"


def _generation_queue_path(config: AppConfig) -> Path:
    return config.controller.state_dir / "generation-queue.json"


def _species_payload(species: BirdSpecies) -> dict[str, object]:
    payload: dict[str, object] = {
        "taxon_id": species.taxon_id,
        "common_name": species.common_name,
        "scientific_name": species.scientific_name,
        "observation_count": species.observation_count,
        "source": species.source,
        "sources": list(species.sources),
    }
    if species.latest_detection_at is not None:
        payload["latest_detection_at"] = species.latest_detection_at
    return payload


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
        latest_detection_at = item.get("latest_detection_at")
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
            or (
                latest_detection_at is not None
                and (not isinstance(latest_detection_at, str) or not latest_detection_at)
            )
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
                latest_detection_at=latest_detection_at,
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
            "updated_at": utc_now(),
            "species": [_species_payload(item) for item in species],
        },
    )


def _migrate_legacy_seed_queue(
    state: CollectionState,
    queued_species: list[BirdSpecies],
) -> tuple[list[CollectionEntry], list[CollectionEntry], str, bool]:
    if state.legacy_seed_queue_migrated_at is not None:
        return state.entries, [], state.legacy_seed_queue_migrated_at, False
    updated, added = add_collection_taxa(
        state.entries,
        {species.taxon_id for species in queued_species},
        CollectionOrigin.HISTORICAL_SEED,
    )
    return updated, added, utc_now(), True


def enqueue_seed_species(
    config: AppConfig,
    *,
    window: ObservationWindow | None = None,
    date_range: DateRange | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    sources: tuple[DiscoveryProvider, ...] | None = None,
    radius_km: int | None = None,
    species_limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    if (window is None) == (date_range is None):
        raise ValueError("provide either an observation window or an explicit date range")
    if date_range is not None and (date_range.start is None or date_range.end is None):
        raise ValueError("explicit date range requires both start and end dates")
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
            sources=sources,
            window=window,
            date_range=date_range,
            latitude=latitude,
            longitude=longitude,
            radius_km=radius,
            species_limit=limit,
            persist_taxonomy_cache=not dry_run,
        )
        discovered = discovery.species
        state_lock = nullcontext() if dry_run else catalog_state_lock(config.controller.state_dir)
        with state_lock:
            approved = approved_taxon_ids(config.controller.catalog_dir)
            legacy_queue = read_generation_queue(config)
            existing = [species for species in legacy_queue if species.taxon_id not in approved]
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
            collection, migrated_legacy_queue, migrated_at, migration_applied = (
                _migrate_legacy_seed_queue(
                    read_collection_state(config.controller.state_dir),
                    legacy_queue,
                )
            )
            collection, collection_added = add_collection_taxa(
                collection,
                {species.taxon_id for species in discovered},
                CollectionOrigin.HISTORICAL_SEED,
            )
            if not dry_run:
                _write_generation_queue(config, queued)
                if migration_applied or collection_added:
                    write_collection(
                        config.controller.state_dir,
                        collection,
                        legacy_seed_queue_migrated_at=migrated_at,
                    )
                _write_active_catalog(config, _current_discovery_species(config))

    return {
        "window": window.value if window is not None else None,
        "start_date": (
            date_range.start.isoformat()
            if date_range is not None and date_range.start is not None
            else None
        ),
        "end_date": (
            date_range.end.isoformat()
            if date_range is not None and date_range.end is not None
            else None
        ),
        "source": discovery_source_label(sources or config.discovery.sources),
        "sources": [provider.value for provider in (sources or config.discovery.sources)],
        "radius_km": radius,
        "species_limit": limit,
        "discovered_count": len(discovered),
        "already_approved_count": sum(species.taxon_id in approved for species in discovered),
        "eligible_count": len(eligible),
        "added_count": len(added),
        "queued_count": len(queued),
        "migrated_legacy_queue_count": len(migrated_legacy_queue),
        "migrated_legacy_queue_taxon_ids": [entry.taxon_id for entry in migrated_legacy_queue],
        "collection_added_count": len(collection_added),
        "collection_count": len(collection),
        "dry_run": dry_run,
        "added": [_species_payload(species) for species in added],
        "collection_added_taxon_ids": [entry.taxon_id for entry in collection_added],
        "providers": [provider.as_dict() for provider in discovery.providers],
        "unresolved_count": len(discovery.unresolved),
    }


def _write_active_catalog(
    config: AppConfig,
    species_list: list[BirdSpecies],
    *,
    approved: list[CatalogEntry] | None = None,
) -> int:
    approved_entries = (
        approved if approved is not None else rebuild_catalog_index(config.controller.catalog_dir)
    )
    observed = {species.taxon_id: species for species in species_list}
    collection_taxa = {entry.taxon_id for entry in read_collection(config.controller.state_dir)}
    active: list[dict[str, object]] = []
    for entry in approved_entries:
        species = observed.get(entry.taxon_id)
        if species is None and entry.taxon_id not in collection_taxa:
            continue
        value = entry.as_dict()
        if species is not None:
            value["observation_count"] = species.observation_count
            if species.latest_detection_at is not None:
                value["latest_detection_at"] = species.latest_detection_at
        active.append(value)
    write_json_atomic(
        _active_catalog_path(config),
        {
            "schema_version": 1,
            "generated_at": utc_now(),
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
        "source": discovery_source_label(config.discovery.sources),
        "sources": [provider.value for provider in config.discovery.sources],
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
    refreshed = parse_utc_timestamp(refreshed_at)
    if refreshed is None:
        raise CatalogError(f"Invalid discovery timestamp: {path}")

    species = _parse_species_list(species_raw, path)
    return DiscoverySnapshot(refreshed, place_name, state, species)


def _current_discovery_species(config: AppConfig) -> list[BirdSpecies]:
    if not _snapshot_path(config).exists():
        return []
    return _read_discovery_snapshot(config).species


def archive_invalid_approved_catalog_state(
    config: AppConfig,
    taxon_id: int,
) -> Path | None:
    with catalog_state_lock(config.controller.state_dir):
        observed = _current_discovery_species(config)
        approved_path = find_taxon_directory(
            config.controller.catalog_dir / "species",
            taxon_id,
        )
        if approved_path is None:
            _write_active_catalog(config, observed)
            return None
        if has_valid_approved_candidate(config.controller.catalog_dir, taxon_id):
            raise ValueError(
                f"Taxon {taxon_id} is already approved; use --replace-approved "
                "with --reason after human review"
            )
        archived = archive_invalid_approved_candidate(
            config.controller.state_dir,
            config.controller.catalog_dir,
            taxon_id,
        )
        _write_active_catalog(config, observed)
        return archived


def _replacement_species(
    taxon_id: int,
    common_name: object,
    scientific_name: object,
    source: Path,
) -> BirdSpecies:
    if (
        not isinstance(common_name, str)
        or not common_name.strip()
        or not isinstance(scientific_name, str)
        or not scientific_name.strip()
    ):
        raise CatalogError(f"Replacement candidate identity is invalid: {source}")
    return BirdSpecies(
        taxon_id=taxon_id,
        common_name=common_name,
        scientific_name=scientific_name,
        observation_count=0,
        source=HUMAN_REVIEW_SOURCE,
    )


def _resumable_approved_replacement(
    state_dir: Path,
    taxon_id: int,
    reason: str,
) -> tuple[Path, BirdSpecies] | None:
    matches: list[tuple[Path, BirdSpecies]] = []
    for path in sorted((state_dir / "rejected").glob(f"{taxon_id}-*")):
        manifest_path = path / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = read_json(manifest_path)
        if not isinstance(manifest, dict):
            raise CatalogError(f"Rejected candidate manifest is invalid: {manifest_path}")
        if (
            manifest.get("taxon_id") != taxon_id
            or manifest.get("status") != "rejected"
            or not isinstance(manifest.get("approved_at"), str)
            or manifest.get("rejection_reason") != reason
        ):
            continue
        matches.append(
            (
                path,
                _replacement_species(
                    taxon_id,
                    manifest.get("common_name"),
                    manifest.get("scientific_name"),
                    manifest_path,
                ),
            )
        )
    if len(matches) > 1:
        raise CatalogError(f"Multiple resumable replacements exist for taxon {taxon_id}")
    return matches[0] if matches else None


def _archive_controller_paths(state_dir: Path, sources: list[Path]) -> list[str]:
    archive = state_dir / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    seen: set[Path] = set()
    for source in sources:
        if not source.exists():
            continue
        resolved = source.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        destination = archive / source.name
        counter = 1
        while destination.exists():
            destination = archive / f"{source.name}-{counter}"
            counter += 1
        shutil.move(str(source), destination)
        moved.append(str(destination))
    return moved


def _migrate_legacy_queue_before_replacement(
    config: AppConfig,
    queued_species: list[BirdSpecies],
    taxon_id: int,
) -> None:
    pre_replacement_queue = [species for species in queued_species if species.taxon_id != taxon_id]
    collection, _, migrated_at, migration_applied = _migrate_legacy_seed_queue(
        read_collection_state(config.controller.state_dir),
        pre_replacement_queue,
    )
    if migration_applied:
        write_collection(
            config.controller.state_dir,
            collection,
            legacy_seed_queue_migrated_at=migrated_at,
        )


def retry_approved_candidate(
    config: AppConfig,
    taxon_id: int,
    reason: str,
) -> dict[str, object]:
    rejection_reason = reason.strip()
    if not rejection_reason:
        raise ValueError("Approved replacement requires a non-empty rejection reason")

    with exclusive_cycle_lock(config.controller.state_dir):
        pending = find_taxon_directory(config.controller.state_dir / "pending", taxon_id)
        if pending is not None and (pending / "manifest.json").is_file():
            raise ValueError("Pending candidates must be approved or rejected before retrying")
        with catalog_state_lock(config.controller.state_dir):
            approved_entries = read_catalog_entries(config.controller.catalog_dir)
            approved_entry = next(
                (entry for entry in approved_entries if entry.taxon_id == taxon_id),
                None,
            )
            approved_path = find_taxon_directory(
                config.controller.catalog_dir / "species",
                taxon_id,
            )
            if (approved_entry is None) != (approved_path is None):
                raise CatalogError(f"Approved catalog state is inconsistent for taxon {taxon_id}")

            retry_store = RetryStore(config.controller.state_dir / "generation-retries.json")
            queued_species = read_generation_queue(config)
            observed = _current_discovery_species(config)
            resumable = (
                _resumable_approved_replacement(
                    config.controller.state_dir,
                    taxon_id,
                    rejection_reason,
                )
                if approved_entry is None
                else None
            )
            queued = next(
                (species for species in queued_species if species.taxon_id == taxon_id),
                None,
            )
            guidance = retry_store.quality_guidance(taxon_id)
            if approved_entry is None and resumable is None:
                already_prepared = (
                    queued is not None
                    and guidance is not None
                    and rejection_reason in guidance.invariant_findings
                    and guidance.source_plate is None
                    and not _has_terminal_state(config.controller.state_dir, taxon_id)
                )
                if not already_prepared:
                    raise ValueError(f"No approved candidate exists for taxon {taxon_id}")
                _migrate_legacy_queue_before_replacement(config, queued_species, taxon_id)
                assert guidance is not None
                active_count = _write_active_catalog(
                    config,
                    observed,
                    approved=approved_entries,
                )
                return {
                    "taxon_id": taxon_id,
                    "status": "eligible",
                    "archived": [],
                    "cleared_deferred_retry": False,
                    "cleared_cached_profile": False,
                    "cleared_cached_references": False,
                    "preserved_quality_findings_count": len(guidance.findings),
                    "replaced_approved": True,
                    "resumed": True,
                    "source_attempt": None,
                    "preserved_correction_source": False,
                    "active_approved_count": active_count,
                    "queued_for_generation": True,
                }

            if approved_entry is not None and approved_path is not None:
                species = _replacement_species(
                    taxon_id,
                    approved_entry.common_name,
                    approved_entry.scientific_name,
                    approved_path / "manifest.json",
                )
                withdrawn: Path | None = None
            else:
                assert resumable is not None
                withdrawn, species = resumable

            if queued is not None and (
                queued.common_name != species.common_name
                or queued.scientific_name != species.scientific_name
            ):
                raise CatalogError(f"Generation queue identity differs for taxon {taxon_id}")
            _migrate_legacy_queue_before_replacement(config, queued_species, taxon_id)

            rejected_paths = sorted(
                (config.controller.state_dir / "rejected").glob(f"{taxon_id}-*")
            )
            old_sources = [pending] if pending is not None else []
            old_sources.extend(
                sorted((config.controller.state_dir / "failed").glob(f"{taxon_id}-*"))
            )
            old_sources.extend(path for path in rejected_paths if path != withdrawn)
            profile_cache = config.controller.state_dir / "profiles" / str(taxon_id)
            reference_cache = config.controller.state_dir / "references" / str(taxon_id)
            cleared_cached_profile = profile_cache.exists()
            cleared_cached_references = reference_cache.exists()
            if cleared_cached_profile:
                old_sources.append(profile_cache)
            if cleared_cached_references:
                old_sources.append(reference_cache)
            moved = _archive_controller_paths(config.controller.state_dir, old_sources)

            if queued is None:
                queued_species.append(species)
                _write_generation_queue(config, queued_species)
            deferred = retry_store.get(taxon_id) is not None
            retry_store.clear(taxon_id)
            guidance = retry_store.set_quality_guidance(
                taxon_id,
                (rejection_reason,),
                invariant_findings=(rejection_reason,),
            )

            if withdrawn is None:
                withdrawn = withdraw_approved_candidate(
                    config.controller.state_dir,
                    config.controller.catalog_dir,
                    taxon_id,
                    rejection_reason,
                )
            active_count = _write_active_catalog(config, observed)
            moved.extend(_archive_controller_paths(config.controller.state_dir, [withdrawn]))

    return {
        "taxon_id": taxon_id,
        "status": "eligible",
        "archived": moved,
        "cleared_deferred_retry": deferred,
        "cleared_cached_profile": cleared_cached_profile,
        "cleared_cached_references": cleared_cached_references,
        "preserved_quality_findings_count": len(guidance.findings),
        "replaced_approved": True,
        "resumed": resumable is not None,
        "source_attempt": None,
        "preserved_correction_source": False,
        "active_approved_count": active_count,
        "queued_for_generation": True,
    }


def _collection_summary(
    entries: list[CollectionEntry],
    approved: list[CatalogEntry],
    observed: list[BirdSpecies],
    *,
    legacy_seed_queue_migrated_at: str | None,
) -> dict[str, object]:
    approved_by_taxon = {entry.taxon_id: entry for entry in approved}
    observed_taxa = {species.taxon_id for species in observed}
    collection_taxa = {entry.taxon_id for entry in entries}
    active_taxa = set(approved_by_taxon) & (observed_taxa | collection_taxa)
    members: list[dict[str, object]] = []
    for entry in entries:
        approved_entry = approved_by_taxon.get(entry.taxon_id)
        member = {
            **entry.as_dict(),
            "approved": approved_entry is not None,
            "observed": entry.taxon_id in observed_taxa,
            "active": entry.taxon_id in active_taxa,
        }
        if approved_entry is not None:
            member["common_name"] = approved_entry.common_name
            member["scientific_name"] = approved_entry.scientific_name
        members.append(member)
    return {
        "collection_count": len(entries),
        "approved_member_count": sum(entry.taxon_id in approved_by_taxon for entry in entries),
        "active_approved_count": len(active_taxa),
        "legacy_seed_queue_migrated": legacy_seed_queue_migrated_at is not None,
        "legacy_seed_queue_migrated_at": legacy_seed_queue_migrated_at,
        "members": members,
    }


def collection_status(
    config: AppConfig, *, approved: list[CatalogEntry] | None = None
) -> dict[str, object]:
    state = read_collection_state(config.controller.state_dir)
    return _collection_summary(
        state.entries,
        approved if approved is not None else read_catalog_entries(config.controller.catalog_dir),
        _current_discovery_species(config),
        legacy_seed_queue_migrated_at=state.legacy_seed_queue_migrated_at,
    )


def _change_collection(
    config: AppConfig,
    *,
    taxon_ids: set[int] | None,
    origin: CollectionOrigin | None,
    remove: bool,
    dry_run: bool,
) -> dict[str, object]:
    if taxon_ids is not None and any(
        not isinstance(taxon_id, int) or isinstance(taxon_id, bool) or taxon_id <= 0
        for taxon_id in taxon_ids
    ):
        raise ValueError("collection taxon IDs must be positive integers")
    cycle_lock = nullcontext() if dry_run else exclusive_cycle_lock(config.controller.state_dir)
    with cycle_lock:
        state_lock = nullcontext() if dry_run else catalog_state_lock(config.controller.state_dir)
        with state_lock:
            approved = read_catalog_entries(config.controller.catalog_dir)
            requested_taxa = (
                {entry.taxon_id for entry in approved} if taxon_ids is None else taxon_ids
            )
            current, migrated_legacy_queue, migrated_at, migration_applied = (
                _migrate_legacy_seed_queue(
                    read_collection_state(config.controller.state_dir),
                    read_generation_queue(config),
                )
            )
            if remove:
                updated, removed = remove_collection_taxa(current, requested_taxa)
                added: list[CollectionEntry] = []
            else:
                if origin is None:
                    raise ValueError("collection additions require an origin")
                updated, added = add_collection_taxa(current, requested_taxa, origin)
                removed = []
            observed = _current_discovery_species(config)
            summary = _collection_summary(
                updated,
                approved,
                observed,
                legacy_seed_queue_migrated_at=migrated_at,
            )
            if not dry_run:
                if migration_applied or added or removed:
                    write_collection(
                        config.controller.state_dir,
                        updated,
                        legacy_seed_queue_migrated_at=migrated_at,
                    )
                summary["active_approved_count"] = _write_active_catalog(
                    config,
                    observed,
                    approved=approved,
                )
    return {
        **{key: value for key, value in summary.items() if key != "members"},
        "added_count": len(added),
        "removed_count": len(removed),
        "added_taxon_ids": [entry.taxon_id for entry in added],
        "removed_taxon_ids": [entry.taxon_id for entry in removed],
        "migrated_legacy_queue_count": len(migrated_legacy_queue),
        "migrated_legacy_queue_taxon_ids": [entry.taxon_id for entry in migrated_legacy_queue],
        "dry_run": dry_run,
    }


def import_approved_collection(config: AppConfig, *, dry_run: bool = False) -> dict[str, object]:
    return _change_collection(
        config,
        taxon_ids=None,
        origin=CollectionOrigin.CATALOG_IMPORT,
        remove=False,
        dry_run=dry_run,
    )


def add_collection_member(
    config: AppConfig, taxon_id: int, *, dry_run: bool = False
) -> dict[str, object]:
    return _change_collection(
        config,
        taxon_ids={taxon_id},
        origin=CollectionOrigin.MANUAL,
        remove=False,
        dry_run=dry_run,
    )


def remove_collection_member(
    config: AppConfig, taxon_id: int, *, dry_run: bool = False
) -> dict[str, object]:
    return _change_collection(
        config,
        taxon_ids={taxon_id},
        origin=None,
        remove=True,
        dry_run=dry_run,
    )


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
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SpeciesStateError(f"Invalid reference manifest: {manifest_path}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("references"), list):
            raise SpeciesStateError(f"Invalid reference manifest: {manifest_path}")
        try:
            references = [_reference_from_dict(item) for item in raw["references"]]
        except CatalogError as exc:
            raise SpeciesStateError(f"Invalid reference manifest: {manifest_path}") from exc
        missing = [
            item.filename for item in references if not (directory / item.filename).is_file()
        ]
        if missing:
            raise SpeciesStateError(f"Reference files are missing: {', '.join(missing)}")
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
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SpeciesStateError(f"Invalid cached species profile: {cache_path}") from exc
        try:
            profile = parse_species_profile(raw, config.research.allowed_domains)
        except GenerationError as exc:
            raise SpeciesStateError(f"Invalid cached species profile: {cache_path}") from exc
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
        raise SpeciesStateError(
            f"Cached species profile identity does not match taxon {species.taxon_id}"
        )
    write_json_atomic(output_path, profile)
    if not cached:
        write_json_atomic(cache_path, profile)
    return profile, output_path


def generate_candidate(
    config: AppConfig,
    species: BirdSpecies,
    workspace: Path,
    *,
    initial_correction_findings: tuple[str, ...] = (),
    initial_correction_source: Path | None = None,
    invariant_correction_findings: tuple[str, ...] = (),
) -> Path:
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
    # The image tool copies its bitmap from inside Codex, so that one output
    # must live below the workspace-write sandbox root. Keep profiles, review
    # inputs, and candidate staging in controller state where Codex cannot
    # mutate them, then let the trusted parent process prepare the bitmap.
    generation_parent = workspace.resolve() / ".inky-bird-frame-generation"
    generation_parent.mkdir(parents=True, exist_ok=True)

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
        correction_findings = _merge_correction_findings(
            invariant_correction_findings,
            initial_correction_findings,
        )
        correction_source = initial_correction_source
        history: list[dict[str, object]] = []
        for attempt in range(1, config.controller.max_generation_attempts + 1):
            attempt_dir = work / f"attempt-{attempt:02d}"
            attempt_dir.mkdir()
            portrait_path = attempt_dir / "portrait.png"
            display_path = attempt_dir / "display.png"
            with TemporaryDirectory(
                prefix=f"{species.taxon_id}-attempt-{attempt:02d}-",
                dir=generation_parent,
            ) as generation_temporary:
                generated_path = Path(generation_temporary) / "generated.png"
                correction_source_sha256 = (
                    sha256_file(correction_source) if correction_source is not None else None
                )
                runner.generate_plate(
                    species,
                    profile,
                    references,
                    reference_paths,
                    generated_path,
                    logs / f"02-generation-attempt-{attempt:02d}.log",
                    correction_findings,
                    correction_source_path=correction_source,
                )
                prepare_generated_plate(generated_path, portrait_path, display_path)

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
            history_entry: dict[str, object] = {
                "attempt": attempt,
                "quality_review": review.as_dict(),
            }
            if correction_source_sha256 is not None:
                history_entry["correction_source_sha256"] = correction_source_sha256
            history.append(history_entry)
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
                    correction_source_sha256=correction_source_sha256,
                )
                destination = candidate_directory(state_dir, species)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise CatalogError(f"Pending destination already exists: {destination}")
                shutil.copytree(attempt_dir, destination)
                return destination
            correction_findings = _merge_correction_findings(
                invariant_correction_findings,
                review.correction_findings or (REVIEW_FAILURE_FALLBACK,),
            )
            correction_source = portrait_path

        failed = state_dir / "failed" / f"{species.taxon_id}-{_timestamp()}"
        failed.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(work, failed)
        raise QualityReviewError(
            "Generated plate failed automated quality review after "
            f"{config.controller.max_generation_attempts} attempts; artifacts retained at {failed}"
        )


def _retry_source_plate(state_dir: Path, relative_path: str | None) -> Path | None:
    if relative_path is None:
        return None
    state_root = state_dir.resolve()
    source = (state_dir / relative_path).resolve()
    if not source.is_relative_to(state_root) or not source.is_file():
        raise SpeciesStateError(f"Invalid retained correction source: {relative_path}")
    return source


def _terminal_state(state_dir: Path, taxon_id: int) -> tuple[str, tuple[Path, ...]] | None:
    pending = find_taxon_directory(state_dir / "pending", taxon_id)
    if pending is not None:
        state = "pending" if (pending / "manifest.json").is_file() else "incomplete_pending"
        return state, (pending,)
    rejected = find_taxon_directory(state_dir / "rejected", taxon_id)
    if rejected is not None:
        return "rejected", (rejected,)
    failed = tuple(sorted((state_dir / "failed").glob(f"{taxon_id}-*")))
    if failed:
        return "failed", failed
    return None


def _has_terminal_state(state_dir: Path, taxon_id: int) -> bool:
    return _terminal_state(state_dir, taxon_id) is not None


def _partition_generation_queue(
    state_dir: Path,
    species_list: list[BirdSpecies],
    approved: set[int],
) -> GenerationQueuePartition:
    actionable: list[BirdSpecies] = []
    terminal_blocked: list[TerminalQueueEntry] = []
    for species in species_list:
        if species.taxon_id in approved:
            continue
        terminal = _terminal_state(state_dir, species.taxon_id)
        if terminal is None:
            actionable.append(species)
            continue
        state, paths = terminal
        if state == "pending":
            continue
        terminal_blocked.append(TerminalQueueEntry(species, state, paths))
    return GenerationQueuePartition(actionable, terminal_blocked)


def read_generation_queue_partition(
    config: AppConfig, *, approved: set[int] | None = None
) -> GenerationQueuePartition:
    return _partition_generation_queue(
        config.controller.state_dir,
        read_generation_queue(config),
        approved if approved is not None else approved_taxon_ids(config.controller.catalog_dir),
    )


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
            "failed_at": utc_now(),
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
    clear_catalog_staging(config.controller.catalog_dir)
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
        queued_species = read_generation_queue(config)
        with catalog_state_lock(config.controller.state_dir):
            collection, migrated_legacy_queue, migrated_at, migration_applied = (
                _migrate_legacy_seed_queue(
                    read_collection_state(config.controller.state_dir),
                    queued_species,
                )
            )
            if migration_applied:
                write_collection(
                    config.controller.state_dir,
                    collection,
                    legacy_seed_queue_migrated_at=migrated_at,
                )
            published = approve_passing_candidates(config)
        snapshot = _read_discovery_snapshot(config)
        maximum_age = timedelta(minutes=config.schedule.refresh_minutes * 2)
        if datetime.now(UTC) - snapshot.refreshed_at.astimezone(UTC) > maximum_age:
            raise DataSourceError(
                "Discovery state is stale; a successful refresh is required before generation"
            )
        species_list = snapshot.species
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
            guidance = retry_store.quality_guidance(species.taxon_id)
            try:
                if guidance is None:
                    generate_candidate(config, species, config.controller.workspace_dir)
                else:
                    try:
                        correction_source = _retry_source_plate(
                            config.controller.state_dir,
                            guidance.source_plate,
                        )
                    except SpeciesStateError:
                        if guidance.invariant_findings:
                            retry_store.set_quality_guidance(
                                species.taxon_id,
                                guidance.invariant_findings,
                                invariant_findings=guidance.invariant_findings,
                            )
                        else:
                            retry_store.clear_quality_guidance(species.taxon_id)
                        raise
                    generate_candidate(
                        config,
                        species,
                        config.controller.workspace_dir,
                        initial_correction_findings=guidance.findings,
                        initial_correction_source=correction_source,
                        invariant_correction_findings=guidance.invariant_findings,
                    )
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
                retry_store.clear_quality_guidance(species.taxon_id)
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
                if guidance is not None and guidance.invariant_findings:
                    retry_store.set_quality_guidance(
                        species.taxon_id,
                        guidance.invariant_findings,
                        invariant_findings=guidance.invariant_findings,
                    )
                else:
                    retry_store.clear_quality_guidance(species.taxon_id)
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
            except MissingDependencyError:
                raise
            except SpeciesStateError as exc:
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
            except CatalogError:
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
            queue_partition = _partition_generation_queue(
                config.controller.state_dir,
                remaining_queue,
                approved_after,
            )
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
            "migrated_legacy_queue_count": len(migrated_legacy_queue),
            "migrated_legacy_queue_taxon_ids": [entry.taxon_id for entry in migrated_legacy_queue],
            "queued_count": len(queue_partition.actionable),
            "terminal_blocked_count": len(queue_partition.terminal_blocked),
            "terminal_blocked": [entry.as_dict() for entry in queue_partition.terminal_blocked],
            "generated": generated,
            "failures": failures,
        }


def run_controller_cycle(config: AppConfig) -> dict[str, object]:
    refresh = run_refresh_cycle(config)
    generation = run_generation_cycle(config)
    return {**generation, "refresh": refresh}
