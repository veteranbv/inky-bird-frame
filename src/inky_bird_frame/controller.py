"""Controller cycle: discover species, acquire references, generate, and stage."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import cast

from .birdbuddy import sync_birdbuddy_detections
from .birdnet_analyzer import read_birdnet_analyzer_history
from .birds import (
    BirdBuddySpecies,
    BirdNetAnalyzerSpecies,
    BirdNetGoSpecies,
    BirdSpecies,
    BirdWeatherSpecies,
    DateRange,
    EbirdArchiveSpecies,
    EbirdSpecies,
    ObservationWindow,
    fetch_birdnet_go_species,
    fetch_birdweather_species,
    fetch_ebird_observations,
    fetch_inaturalist_birds,
    fetch_taxon_context,
    resolve_birdbuddy_species,
    resolve_birdnet_analyzer_species,
    resolve_birdnet_go_species,
    resolve_birdweather_species,
    resolve_ebird_archive_species,
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
from .ebird_archive import read_ebird_archive_history
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
from .models import ProfileConflict, ReferencePhoto, SpeciesProfileData
from .prompts import PROMPT_VERSION
from .references import download_references, fetch_reference_candidates
from .research import ResearchBudget
from .retry import RetryRecord, RetryStore, parse_retry_profile_conflicts
from .timeutil import parse_utc_timestamp

REVIEW_FAILURE_FALLBACK = "The previous attempt did not meet every automated review threshold."
PROFILE_REFRESH_CORRECTION = (
    "Update the primary bird, every supplementary study, and every visible factual note to "
    "match the refreshed source-backed profile exactly, including its field marks, palette, "
    "measurements, and anatomy."
)
HUMAN_REVIEW_SOURCE = "human-review"


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
    details: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "name": self.name,
            "status": self.status,
            "species_count": self.species_count,
            "unresolved_count": self.unresolved_count,
        }
        if self.error is not None:
            value["error"] = self.error
        if self.details is not None:
            value["details"] = self.details
        return value


@dataclass(frozen=True)
class DiscoveryResult:
    location: DiscoveryLocation | None
    species: list[BirdSpecies]
    providers: list[ProviderStatus]
    unresolved: list[
        EbirdSpecies
        | EbirdArchiveSpecies
        | BirdWeatherSpecies
        | BirdBuddySpecies
        | BirdNetAnalyzerSpecies
        | BirdNetGoSpecies
    ]


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


@dataclass(frozen=True)
class GenerationWork:
    complete: bool
    discovery_status: str
    discovery_refreshed_at: datetime | None
    eligible: list[BirdSpecies]
    actionable: list[BirdSpecies]
    deferred: list[RetryRecord]
    terminal_blocked: list[TerminalQueueEntry]

    def as_dict(self) -> dict[str, object]:
        discovery: dict[str, object] = {"status": self.discovery_status}
        if self.discovery_refreshed_at is not None:
            discovery["refreshed_at"] = self.discovery_refreshed_at.isoformat()
        return {
            "complete": self.complete,
            "discovery": discovery,
            "eligible_count": len(self.eligible),
            "eligible": [_species_payload(species) for species in self.eligible],
            "actionable_count": len(self.actionable),
            "actionable": [_species_payload(species) for species in self.actionable],
            "deferred_count": len(self.deferred),
            "deferred": [record.as_dict() for record in self.deferred],
            "terminal_blocked_count": len(self.terminal_blocked),
            "terminal_blocked": [entry.as_dict() for entry in self.terminal_blocked],
        }


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
    date_range_sources = {
        (DiscoveryProvider.INATURALIST,),
        (DiscoveryProvider.EBIRD_ARCHIVE,),
    }
    if date_range is not None and selected_sources not in date_range_sources:
        raise ValueError(
            "explicit date ranges require --source inaturalist or --source ebird-archive"
        )
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
    unresolved: list[
        EbirdSpecies
        | EbirdArchiveSpecies
        | BirdWeatherSpecies
        | BirdBuddySpecies
        | BirdNetAnalyzerSpecies
        | BirdNetGoSpecies
    ] = []
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

    if DiscoveryProvider.EBIRD_ARCHIVE in selected_sources:
        try:
            ebird_archive_history = read_ebird_archive_history(
                config.controller.state_dir,
                window=selected_window,
                limit=selected_limit,
                date_range=date_range,
            )
            resolved, ebird_archive_unresolved = resolve_ebird_archive_species(
                ebird_archive_history.species,
                config.controller.state_dir / "ebird-archive-taxonomy-crosswalk.json",
                persist_cache=persist_taxonomy_cache,
            )
        except (DataSourceError, ValueError) as exc:
            providers.append(ProviderStatus("ebird-archive", "error", 0, error=str(exc)))
        else:
            unresolved.extend(ebird_archive_unresolved)
            if ebird_archive_history.species and not resolved:
                providers.append(
                    ProviderStatus(
                        "ebird-archive",
                        "error",
                        0,
                        unresolved_count=len(ebird_archive_unresolved),
                        error=(
                            "No eBird archive observations had an exact iNaturalist species match"
                        ),
                        details=ebird_archive_history.details(),
                    )
                )
            else:
                provider_species.append(resolved)
                providers.append(
                    ProviderStatus(
                        "ebird-archive",
                        "ok",
                        len(resolved),
                        unresolved_count=len(ebird_archive_unresolved),
                        details=ebird_archive_history.details(),
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

    if DiscoveryProvider.BIRDBUDDY in selected_sources:
        try:
            sync = sync_birdbuddy_detections(
                config.controller.state_dir,
                window=selected_window,
                limit=selected_limit,
                persist_history=persist_taxonomy_cache,
                include_manual_sightings=(config.discovery.birdbuddy_include_manual_sightings),
            )
            resolved, birdbuddy_unresolved = resolve_birdbuddy_species(
                sync.species,
                config.controller.state_dir / "birdbuddy-taxonomy-crosswalk.json",
                persist_cache=persist_taxonomy_cache,
            )
        except (DataSourceError, ValueError) as exc:
            providers.append(ProviderStatus("birdbuddy", "error", 0, error=str(exc)))
        else:
            unresolved.extend(birdbuddy_unresolved)
            if sync.species and not resolved:
                providers.append(
                    ProviderStatus(
                        "birdbuddy",
                        "error",
                        0,
                        unresolved_count=len(birdbuddy_unresolved),
                        error="No Bird Buddy detections had an exact iNaturalist species match",
                        details=sync.stats.as_dict(),
                    )
                )
            else:
                provider_species.append(resolved)
                providers.append(
                    ProviderStatus(
                        "birdbuddy",
                        "ok",
                        len(resolved),
                        unresolved_count=len(birdbuddy_unresolved),
                        details=sync.stats.as_dict(),
                    )
                )

    if DiscoveryProvider.BIRDNET_GO in selected_sources:
        try:
            base_url = config.discovery.birdnet_go_url
            if base_url is None:
                raise DataSourceError("BirdNET-Go base URL is not configured")
            birdnet_go_detections = fetch_birdnet_go_species(
                base_url=base_url,
                limit=selected_limit,
                window=selected_window,
            )
            resolved, birdnet_go_unresolved = resolve_birdnet_go_species(
                birdnet_go_detections,
                config.controller.state_dir / "birdnet-go-taxonomy-crosswalk.json",
                persist_cache=persist_taxonomy_cache,
            )
        except (DataSourceError, ValueError) as exc:
            providers.append(ProviderStatus("birdnet-go", "error", 0, error=str(exc)))
        else:
            unresolved.extend(birdnet_go_unresolved)
            if birdnet_go_detections and not resolved:
                providers.append(
                    ProviderStatus(
                        "birdnet-go",
                        "error",
                        0,
                        unresolved_count=len(birdnet_go_unresolved),
                        error="No BirdNET-Go detections had an exact iNaturalist species match",
                    )
                )
            else:
                provider_species.append(resolved)
                providers.append(
                    ProviderStatus(
                        "birdnet-go",
                        "ok",
                        len(resolved),
                        unresolved_count=len(birdnet_go_unresolved),
                    )
                )

    if DiscoveryProvider.BIRDNET_ANALYZER in selected_sources:
        try:
            birdnet_analyzer_history = read_birdnet_analyzer_history(
                config.controller.state_dir,
                window=selected_window,
                limit=selected_limit,
            )
            resolved, birdnet_analyzer_unresolved = resolve_birdnet_analyzer_species(
                birdnet_analyzer_history.species,
                config.controller.state_dir / "birdnet-analyzer-taxonomy-crosswalk.json",
                persist_cache=persist_taxonomy_cache,
            )
        except (DataSourceError, ValueError) as exc:
            providers.append(ProviderStatus("birdnet-analyzer", "error", 0, error=str(exc)))
        else:
            unresolved.extend(birdnet_analyzer_unresolved)
            if birdnet_analyzer_history.species and not resolved:
                providers.append(
                    ProviderStatus(
                        "birdnet-analyzer",
                        "error",
                        0,
                        unresolved_count=len(birdnet_analyzer_unresolved),
                        error=(
                            "No BirdNET Analyzer detections had an exact iNaturalist species match"
                        ),
                        details=birdnet_analyzer_history.details(),
                    )
                )
            else:
                provider_species.append(resolved)
                providers.append(
                    ProviderStatus(
                        "birdnet-analyzer",
                        "ok",
                        len(resolved),
                        unresolved_count=len(birdnet_analyzer_unresolved),
                        details=birdnet_analyzer_history.details(),
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
                latest_detection_at=_latest_detection_at(
                    existing.latest_detection_at,
                    species.latest_detection_at,
                ),
            )
    return [merged[taxon_id] for taxon_id in order]


def _latest_detection_at(first: str | None, second: str | None) -> str | None:
    candidates = [
        (parsed, value)
        for value in (first, second)
        if value is not None
        if (parsed := parse_utc_timestamp(value)) is not None
    ]
    return max(candidates, default=(None, None), key=lambda item: item[0])[1]


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
    species: EbirdSpecies
    | EbirdArchiveSpecies
    | BirdWeatherSpecies
    | BirdBuddySpecies
    | BirdNetAnalyzerSpecies
    | BirdNetGoSpecies,
) -> dict[str, object]:
    if isinstance(species, EbirdSpecies):
        provider = "ebird"
        provider_species_id = species.species_code
    elif isinstance(species, EbirdArchiveSpecies):
        provider = "ebird-archive"
        provider_species_id = species.scientific_name
    elif isinstance(species, BirdWeatherSpecies):
        provider = "birdweather"
        provider_species_id = str(species.species_id)
    elif isinstance(species, BirdBuddySpecies):
        provider = "birdbuddy"
        provider_species_id = species.species_id
    elif isinstance(species, BirdNetGoSpecies):
        provider = "birdnet-go"
        provider_species_id = species.scientific_name
    else:
        provider = "birdnet-analyzer"
        provider_species_id = species.scientific_name
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


def ensure_generation_retry(
    config: AppConfig,
    taxon_id: int,
    common_name: object,
    scientific_name: object,
    source: Path,
) -> None:
    """Keep an operator-requested retry eligible after its observation expires."""
    species = _replacement_species(
        taxon_id,
        common_name,
        scientific_name,
        source,
    )
    with catalog_state_lock(config.controller.state_dir):
        queued_species = read_generation_queue(config)
        _migrate_legacy_queue_before_human_review(config, queued_species, taxon_id)
        queued_index = next(
            (index for index, item in enumerate(queued_species) if item.taxon_id == taxon_id),
            None,
        )
        if queued_index is not None:
            queued = queued_species[queued_index]
            if (
                HUMAN_REVIEW_SOURCE not in queued.sources
                or queued.common_name != species.common_name
                or queued.scientific_name != species.scientific_name
            ):
                queued_species[queued_index] = species
                _write_generation_queue(config, queued_species)
            return
        queued_species.append(species)
        _write_generation_queue(config, queued_species)


def synchronize_generation_retry_identity(
    config: AppConfig, queued_species: list[BirdSpecies], species: BirdSpecies
) -> None:
    """Refresh durable retry state from the latest observed taxonomy."""
    retry_store = RetryStore(config.controller.state_dir / "generation-retries.json")
    retry = retry_store.get(species.taxon_id)
    retry_identity_changed = retry is not None and (
        retry.common_name != species.common_name or retry.scientific_name != species.scientific_name
    )
    queued_index = next(
        (
            index
            for index, queued in enumerate(queued_species)
            if queued.taxon_id == species.taxon_id
        ),
        None,
    )
    queued = queued_species[queued_index] if queued_index is not None else None
    has_human_review_queue = queued is not None and HUMAN_REVIEW_SOURCE in queued.sources
    queue_identity_changed = (
        queued is not None
        and HUMAN_REVIEW_SOURCE in queued.sources
        and (
            queued.common_name != species.common_name
            or queued.scientific_name != species.scientific_name
        )
    )
    if queue_identity_changed:
        assert queued_index is not None
        replacement = BirdSpecies(
            taxon_id=species.taxon_id,
            common_name=species.common_name,
            scientific_name=species.scientific_name,
            observation_count=0,
            source=HUMAN_REVIEW_SOURCE,
        )
        with catalog_state_lock(config.controller.state_dir):
            persisted_species = read_generation_queue(config)
            persisted_index = next(
                (
                    index
                    for index, persisted in enumerate(persisted_species)
                    if persisted.taxon_id == species.taxon_id
                    and HUMAN_REVIEW_SOURCE in persisted.sources
                ),
                None,
            )
            if persisted_index is None:
                raise SpeciesStateError(
                    f"Human-review queue entry disappeared for taxon {species.taxon_id}"
                )
            persisted_species[persisted_index] = replacement
            _write_generation_queue(config, persisted_species)
        queued_species[queued_index] = replacement
    if retry_identity_changed and not queue_identity_changed:
        retry_store.set_identity(species.taxon_id, species.common_name, species.scientific_name)
    if retry is not None or has_human_review_queue:
        _archive_incompatible_retry_profile(config.controller.state_dir, species)
    if retry_identity_changed and queue_identity_changed:
        retry_store.set_identity(species.taxon_id, species.common_name, species.scientific_name)


def _archive_incompatible_retry_profile(state_dir: Path, species: BirdSpecies) -> None:
    """Preserve a valid cached profile whose taxonomy no longer matches discovery."""
    profile_cache = state_dir / "profiles" / str(species.taxon_id)
    profile_path = profile_cache / "profile.json"
    if not profile_path.is_file():
        return
    try:
        raw = read_json(profile_path)
    except CatalogError:
        return
    if not isinstance(raw, dict):
        return
    cached_taxon_id = raw.get("taxon_id")
    cached_common_name = raw.get("common_name")
    cached_scientific_name = raw.get("scientific_name")
    if not (
        isinstance(cached_taxon_id, int)
        and isinstance(cached_common_name, str)
        and isinstance(cached_scientific_name, str)
    ):
        return
    if (
        cached_taxon_id != species.taxon_id
        or cached_common_name != species.common_name
        or cached_scientific_name != species.scientific_name
    ):
        _archive_controller_paths(state_dir, [profile_cache])


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
                _write_active_catalog(config, current_discovery_species(config))

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


def current_discovery_species(config: AppConfig) -> list[BirdSpecies]:
    if not _snapshot_path(config).exists():
        return []
    return _read_discovery_snapshot(config).species


def archive_invalid_approved_catalog_state(
    config: AppConfig,
    taxon_id: int,
) -> Path | None:
    with catalog_state_lock(config.controller.state_dir):
        observed = current_discovery_species(config)
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


def _migrate_legacy_queue_before_human_review(
    config: AppConfig,
    queued_species: list[BirdSpecies],
    taxon_id: int,
) -> None:
    pre_replacement_queue = [
        species
        for species in queued_species
        if species.taxon_id != taxon_id or HUMAN_REVIEW_SOURCE not in species.sources
    ]
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
    *,
    refresh_research: bool = False,
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
            observed = current_discovery_species(config)
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
            profile_cache = config.controller.state_dir / "profiles" / str(taxon_id)
            reference_cache = config.controller.state_dir / "references" / str(taxon_id)
            cleared_cached_profile = refresh_research and profile_cache.exists()
            cleared_cached_references = refresh_research and reference_cache.exists()
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
                _migrate_legacy_queue_before_human_review(config, queued_species, taxon_id)
                assert guidance is not None
                cache_sources = [
                    path
                    for path, should_clear in (
                        (profile_cache, cleared_cached_profile),
                        (reference_cache, cleared_cached_references),
                    )
                    if should_clear
                ]
                moved = _archive_controller_paths(
                    config.controller.state_dir,
                    cache_sources,
                )
                active_count = _write_active_catalog(
                    config,
                    observed,
                    approved=approved_entries,
                )
                return {
                    "taxon_id": taxon_id,
                    "status": "eligible",
                    "archived": moved,
                    "cleared_deferred_retry": False,
                    "cleared_cached_profile": cleared_cached_profile,
                    "cleared_cached_references": cleared_cached_references,
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
            _migrate_legacy_queue_before_human_review(config, queued_species, taxon_id)

            rejected_paths = sorted(
                (config.controller.state_dir / "rejected").glob(f"{taxon_id}-*")
            )
            old_sources = [pending] if pending is not None else []
            old_sources.extend(
                sorted((config.controller.state_dir / "failed").glob(f"{taxon_id}-*"))
            )
            old_sources.extend(path for path in rejected_paths if path != withdrawn)
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
        current_discovery_species(config),
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
            observed = current_discovery_species(config)
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


def _refresh_profile_after_conflict(
    config: AppConfig,
    species: BirdSpecies,
    references: list[ReferencePhoto],
    reference_paths: list[Path],
    runner: CodexRunner,
    prior_profile: SpeciesProfileData,
    profile_conflicts: tuple[ProfileConflict, ...],
    output_path: Path,
    log_path: Path,
) -> SpeciesProfileData:
    if not config.research.enabled:
        raise GenerationError("Profile conflict requires research, but research is disabled")
    context = fetch_taxon_context(species.taxon_id)
    ResearchBudget(
        config.controller.state_dir / "research-budget.json",
        daily_limit=config.research.max_searches_per_day,
        species_limit=config.research.max_searches_per_species,
    ).consume(species.taxon_id)
    write_json_atomic(log_path.parent / "profile-before-refresh.json", prior_profile)
    researched_profile = runner.create_profile(
        species,
        context,
        references,
        reference_paths,
        output_path,
        log_path,
        allowed_domains=config.research.allowed_domains,
        prior_profile=prior_profile,
        profile_conflicts=profile_conflicts,
    )
    profile = _merge_refreshed_profile(prior_profile, researched_profile, profile_conflicts)
    cache_path = config.controller.state_dir / "profiles" / str(species.taxon_id) / "profile.json"
    write_json_atomic(output_path, profile)
    write_json_atomic(cache_path, profile)
    write_json_atomic(log_path.parent / "profile-after-refresh.json", profile)
    return profile


def _merge_refreshed_profile(
    prior_profile: SpeciesProfileData,
    researched_profile: SpeciesProfileData,
    profile_conflicts: tuple[ProfileConflict, ...],
) -> SpeciesProfileData:
    merged = deepcopy(prior_profile)
    for conflict in profile_conflicts:
        field = conflict["field"]
        if field.startswith("measurements."):
            measurement = field.removeprefix("measurements.")
            if measurement == "length":
                merged["measurements"]["length"] = researched_profile["measurements"]["length"]
            elif measurement == "wingspan":
                merged["measurements"]["wingspan"] = researched_profile["measurements"]["wingspan"]
            else:
                merged["measurements"]["weight"] = researched_profile["measurements"]["weight"]
        elif field == "family":
            merged["family"] = researched_profile["family"]
        elif field == "field_marks":
            merged["field_marks"] = researched_profile["field_marks"]
            merged["palette"] = researched_profile["palette"]
        elif field == "habitat":
            merged["habitat"] = researched_profile["habitat"]
        elif field == "behavior":
            merged["behavior"] = researched_profile["behavior"]
        else:
            raise GenerationError(f"Unsupported profile conflict field: {field}")
    merged["sources"] = researched_profile["sources"]
    # The research pass returns a complete profile, so its citations cover both
    # refreshed and preserved fields without growing the cached source list on retries.
    return merged


def _deduplicate_findings(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(finding for group in groups for finding in group))


def _finding_key(finding: str) -> str:
    return " ".join(finding.split()).casefold()


def _write_attempt_history(
    logs: Path,
    work: Path,
    history: list[dict[str, object]],
) -> None:
    payload = {"schema_version": 1, "attempts": history}
    write_json_atomic(logs / "attempt-history.json", payload)
    write_json_atomic(work / "attempt-history.json", payload)


def generate_candidate(
    config: AppConfig,
    species: BirdSpecies,
    workspace: Path,
    *,
    initial_correction_findings: tuple[str, ...] = (),
    initial_correction_source: Path | None = None,
    invariant_correction_findings: tuple[str, ...] = (),
    initial_profile_conflicts: tuple[ProfileConflict, ...] = (),
) -> Path:
    state_dir = config.controller.state_dir
    if species.taxon_id in approved_taxon_ids(config.controller.catalog_dir):
        raise CatalogError(f"Taxon {species.taxon_id} is already approved")
    if find_taxon_directory(state_dir / "pending", species.taxon_id) is not None:
        raise CatalogError(f"Taxon {species.taxon_id} already has a pending candidate")

    references = load_or_fetch_references(config, species)
    reference_root = state_dir / "references" / str(species.taxon_id)
    reference_paths = [reference_root / reference.filename for reference in references]
    runner = (
        CodexRunner(config.controller.codex_path, workspace)
        if config.controller.codex_model is None
        else CodexRunner(
            config.controller.codex_path,
            workspace,
            model=config.controller.codex_model,
        )
    )
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
        correction_findings = initial_correction_findings
        correction_source = initial_correction_source
        resolved_corrections: tuple[str, ...] = ()
        prior_review_corrections: tuple[str, ...] = ()
        prior_profile_conflicts = initial_profile_conflicts
        previous_scores: dict[str, int] | None = None
        profile_refresh_used = False
        terminal_detail: str | None = None
        history: list[dict[str, object]] = []
        for attempt in range(1, config.controller.max_generation_attempts + 1):
            attempt_dir = work / f"attempt-{attempt:02d}"
            attempt_dir.mkdir()
            portrait_path = attempt_dir / "portrait.png"
            display_path = attempt_dir / "display.png"
            carried_invariants = _deduplicate_findings(
                invariant_correction_findings,
                resolved_corrections,
            )
            history_entry: dict[str, object] = {
                "attempt": attempt,
                "started_at": utc_now(),
                "prompt_version": PROMPT_VERSION,
                "generator": "Codex subscription / built-in gpt-image-2",
                "requested_model": config.controller.codex_model,
                "has_correction_source": correction_source is not None,
                "carried_invariant_count": len(carried_invariants),
            }
            generation_started = monotonic()
            try:
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
                        invariant_findings=carried_invariants,
                    )
                    prepare_generated_plate(generated_path, portrait_path, display_path)
            except Exception as exc:
                history_entry["generation_seconds"] = round(monotonic() - generation_started, 3)
                history_entry["outcome"] = "generation_error"
                history_entry["error_type"] = type(exc).__name__
                history.append(history_entry)
                _write_attempt_history(logs, work, history)
                raise
            history_entry["generation_seconds"] = round(monotonic() - generation_started, 3)

            review_started = monotonic()
            reviewed_corrections = _deduplicate_findings(
                prior_review_corrections,
                tuple(
                    finding
                    for finding in correction_findings
                    if finding not in {PROFILE_REFRESH_CORRECTION, REVIEW_FAILURE_FALLBACK}
                ),
            )
            try:
                review = runner.review_plate(
                    species,
                    profile,
                    references,
                    portrait_path,
                    reference_paths,
                    attempt_dir / "quality-review.json",
                    logs / f"03-quality-review-attempt-{attempt:02d}.log",
                    allowed_domains=config.research.allowed_domains,
                    prior_corrections=reviewed_corrections,
                    prior_profile_conflicts=prior_profile_conflicts,
                )
            except Exception as exc:
                history_entry["review_seconds"] = round(monotonic() - review_started, 3)
                history_entry["outcome"] = "review_error"
                history_entry["error_type"] = type(exc).__name__
                history.append(history_entry)
                _write_attempt_history(logs, work, history)
                raise
            history_entry["review_seconds"] = round(monotonic() - review_started, 3)
            write_json_atomic(attempt_dir / "quality-review.json", review.as_dict())
            history_entry["quality_review"] = review.as_dict()
            history_entry["outcome"] = (
                "passed"
                if review.passed
                else "profile_conflict"
                if review.profile_conflicts
                else "correction_required"
            )
            if correction_source_sha256 is not None:
                history_entry["correction_source_sha256"] = correction_source_sha256
            current_scores = {
                "species_accuracy": review.species_accuracy,
                "anatomy_accuracy": review.anatomy_accuracy,
                "text_accuracy": review.text_accuracy,
                "composition_quality": review.composition_quality,
            }
            history_entry["failed_axes"] = [
                axis for axis, score in current_scores.items() if score < 4
            ] + ([] if review.location_free else ["location_free"])
            new_correction_keys = {_finding_key(finding) for finding in review.correction_findings}
            resolved_candidates = _deduplicate_findings(
                resolved_corrections,
                review.resolved_corrections,
            )
            resolved_keys = {_finding_key(finding) for finding in resolved_candidates}
            history_entry["newly_resolved_findings"] = list(review.resolved_corrections)
            history_entry["regressed_findings"] = [
                finding
                for finding in review.correction_findings
                if _finding_key(finding) in resolved_keys
            ]
            history_entry["regressed_axes"] = (
                [
                    axis
                    for axis, score in current_scores.items()
                    if previous_scores is not None and previous_scores[axis] >= 4 and score < 4
                ]
                if previous_scores is not None
                else []
            )
            prior_conflict_values = {
                conflict["field"]: conflict["observed_value"]
                for conflict in prior_profile_conflicts
            }
            history_entry["profile_reversals"] = [
                conflict["field"]
                for conflict in review.profile_conflicts
                if conflict["field"] in prior_conflict_values
                and prior_conflict_values[conflict["field"]] != conflict["observed_value"]
            ]
            history.append(history_entry)
            _write_attempt_history(logs, work, history)
            if review.passed:
                shutil.copy2(profile_path, attempt_dir / "profile.json")
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

            resolved_corrections = tuple(
                finding
                for finding in resolved_candidates
                if _finding_key(finding) not in new_correction_keys
            )
            prior_review_corrections = _deduplicate_findings(
                reviewed_corrections,
                review.correction_findings,
            )
            prior_profile_conflicts = review.profile_conflicts
            previous_scores = current_scores
            if review.profile_conflicts:
                if not config.research.enabled:
                    terminal_detail = (
                        "a profile conflict requires research, but research is disabled"
                    )
                    history_entry["profile_refresh"] = "disabled"
                    _write_attempt_history(logs, work, history)
                    break
                if profile_refresh_used:
                    terminal_detail = "a profile conflict remained after one source-backed refresh"
                    history_entry["profile_refresh"] = "conflict_remained"
                    _write_attempt_history(logs, work, history)
                    break
                if attempt == config.controller.max_generation_attempts:
                    terminal_detail = "a profile conflict was found on the final attempt"
                    history_entry["profile_refresh"] = "not_run_no_attempt_remaining"
                    _write_attempt_history(logs, work, history)
                    break
                before_refresh = profile
                try:
                    profile = _refresh_profile_after_conflict(
                        config,
                        species,
                        references,
                        reference_paths,
                        runner,
                        profile,
                        review.profile_conflicts,
                        profile_output_path,
                        logs / f"04-profile-refresh-attempt-{attempt:02d}.log",
                    )
                except Exception as exc:
                    history_entry["profile_refresh"] = "failed"
                    history_entry["profile_refresh_error_type"] = type(exc).__name__
                    _write_attempt_history(logs, work, history)
                    raise
                profile_refresh_used = True
                refreshed_fields = {conflict["field"] for conflict in review.profile_conflicts}
                prior_profile_conflicts = tuple(
                    conflict
                    for conflict in prior_profile_conflicts
                    if conflict["field"] not in refreshed_fields
                )
                history_entry["profile_refresh"] = "completed"
                history_entry["profile_changed"] = before_refresh != profile
                _write_attempt_history(logs, work, history)
            correction_findings = _deduplicate_findings(
                review.correction_findings,
                (PROFILE_REFRESH_CORRECTION,) if review.profile_conflicts else (),
            ) or (REVIEW_FAILURE_FALLBACK,)
            correction_source = portrait_path

        failed = state_dir / "failed" / f"{species.taxon_id}-{_timestamp()}"
        failed.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(work, failed)
        reason = terminal_detail or (
            "the configured maximum of "
            f"{config.controller.max_generation_attempts} attempts was exhausted"
        )
        raise QualityReviewError(
            f"Generated plate failed automated quality review because {reason}; "
            f"artifacts retained at {failed}",
            profile_conflicts=prior_profile_conflicts,
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


def calculate_generation_work(
    *,
    snapshot: DiscoverySnapshot | None,
    queued_species: list[BirdSpecies],
    approved: set[int],
    terminal_states: dict[int, tuple[str, tuple[Path, ...]]],
    retry_records: list[RetryRecord],
    now: datetime,
    maximum_age: timedelta,
) -> GenerationWork:
    """Classify generation work from an explicit, side-effect-free state snapshot."""
    if snapshot is None:
        discovery_status = "missing"
        complete = False
        observed_species: list[BirdSpecies] = []
        refreshed_at = None
    else:
        refreshed_at = snapshot.refreshed_at.astimezone(UTC)
        complete = now.astimezone(UTC) - refreshed_at <= maximum_age
        discovery_status = "fresh" if complete else "stale"
        observed_species = snapshot.species

    retry_by_taxon = {record.taxon_id: record for record in retry_records}
    generation_species = list(observed_species)
    observed_taxa = {species.taxon_id for species in observed_species}
    for queued in queued_species:
        if queued.taxon_id in observed_taxa:
            continue
        retry = retry_by_taxon.get(queued.taxon_id)
        if (
            HUMAN_REVIEW_SOURCE not in queued.sources
            and retry is not None
            and retry.common_name is not None
            and retry.scientific_name is not None
        ):
            queued = BirdSpecies(
                taxon_id=queued.taxon_id,
                common_name=retry.common_name,
                scientific_name=retry.scientific_name,
                observation_count=queued.observation_count,
                source=queued.source,
                sources=queued.sources,
                latest_detection_at=queued.latest_detection_at,
            )
        generation_species.append(queued)

    eligible: list[BirdSpecies] = []
    terminal_blocked: list[TerminalQueueEntry] = []
    for species in generation_species:
        if species.taxon_id in approved:
            continue
        terminal = terminal_states.get(species.taxon_id)
        if terminal is None:
            eligible.append(species)
            continue
        state, paths = terminal
        if state != "pending":
            terminal_blocked.append(TerminalQueueEntry(species, state, paths))

    eligible_taxa = {species.taxon_id for species in eligible}
    deferred = sorted(
        (
            record
            for record in retry_records
            if record.taxon_id in eligible_taxa and record.next_attempt_at > now
        ),
        key=lambda record: record.next_attempt_at,
    )
    deferred_taxa = {record.taxon_id for record in deferred}
    actionable = (
        [species for species in eligible if species.taxon_id not in deferred_taxa]
        if complete
        else []
    )
    return GenerationWork(
        complete,
        discovery_status,
        refreshed_at,
        eligible,
        actionable,
        deferred,
        terminal_blocked,
    )


def _generation_terminal_states(
    state_dir: Path,
    observed_species: list[BirdSpecies],
    queued_species: list[BirdSpecies],
) -> dict[int, tuple[str, tuple[Path, ...]]]:
    taxon_ids = {species.taxon_id for species in (*observed_species, *queued_species)}
    return {
        taxon_id: terminal
        for taxon_id in taxon_ids
        if (terminal := _terminal_state(state_dir, taxon_id)) is not None
    }


def read_generation_work(
    config: AppConfig,
    *,
    approved: set[int] | None = None,
    now: datetime | None = None,
) -> GenerationWork:
    """Read local state and return the same generation classification used by a cycle."""
    snapshot = _read_discovery_snapshot(config) if _snapshot_path(config).exists() else None
    queued_species = read_generation_queue(config)
    retry_records = RetryStore(config.controller.state_dir / "generation-retries.json").records()
    return calculate_generation_work(
        snapshot=snapshot,
        queued_species=queued_species,
        approved=(
            approved if approved is not None else approved_taxon_ids(config.controller.catalog_dir)
        ),
        terminal_states=_generation_terminal_states(
            config.controller.state_dir,
            snapshot.species if snapshot is not None else [],
            queued_species,
        ),
        retry_records=retry_records,
        now=now or datetime.now(UTC),
        maximum_age=timedelta(minutes=config.schedule.refresh_minutes * 2),
    )


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
        species_list = snapshot.species
        retry_store = RetryStore(config.controller.state_dir / "generation-retries.json")
        work = calculate_generation_work(
            snapshot=snapshot,
            queued_species=queued_species,
            approved=approved_taxon_ids(config.controller.catalog_dir),
            terminal_states=_generation_terminal_states(
                config.controller.state_dir,
                species_list,
                queued_species,
            ),
            retry_records=retry_store.records(),
            now=datetime.now(UTC),
            maximum_age=timedelta(minutes=config.schedule.refresh_minutes * 2),
        )
        if not work.complete:
            raise DataSourceError(
                "Discovery state is stale; a successful refresh is required before generation"
            )
        for species in species_list:
            synchronize_generation_retry_identity(config, queued_species, species)
        observed_taxa = {species.taxon_id for species in species_list}
        for queued in queued_species:
            if queued.taxon_id in observed_taxa:
                continue
            retry = retry_store.get(queued.taxon_id)
            synchronized = queued
            if (
                HUMAN_REVIEW_SOURCE not in queued.sources
                and retry is not None
                and retry.common_name is not None
                and retry.scientific_name is not None
            ):
                synchronized = BirdSpecies(
                    taxon_id=queued.taxon_id,
                    common_name=retry.common_name,
                    scientific_name=retry.scientific_name,
                    observation_count=queued.observation_count,
                    source=queued.source,
                    sources=queued.sources,
                    latest_detection_at=queued.latest_detection_at,
                )
            synchronize_generation_retry_identity(config, queued_species, synchronized)
        retry_store = RetryStore(config.controller.state_dir / "generation-retries.json")
        eligible = work.eligible
        generated: list[dict[str, object]] = []
        failures: list[dict[str, object]] = []
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
                        profile_conflicts = parse_retry_profile_conflicts(
                            list(guidance.profile_conflicts),
                            retry_store.path,
                            allowed_domains=config.research.allowed_domains,
                        )
                    except CatalogError as exc:
                        raise DataSourceError(
                            "Stored profile conflict sources are outside the current "
                            "research allowlist"
                        ) from exc
                    try:
                        correction_source = _retry_source_plate(
                            config.controller.state_dir,
                            guidance.source_plate,
                        )
                    except SpeciesStateError:
                        if guidance.invariant_findings or guidance.profile_conflicts:
                            retry_store.set_quality_guidance(
                                species.taxon_id,
                                guidance.invariant_findings,
                                invariant_findings=guidance.invariant_findings,
                                profile_conflicts=guidance.profile_conflicts,
                            )
                        else:
                            retry_store.clear_quality_guidance(species.taxon_id)
                        raise
                    invariant_findings = set(guidance.invariant_findings)
                    current_findings = tuple(
                        finding
                        for finding in guidance.findings
                        if finding not in invariant_findings
                    )
                    generate_candidate(
                        config,
                        species,
                        config.controller.workspace_dir,
                        initial_correction_findings=current_findings,
                        initial_correction_source=correction_source,
                        invariant_correction_findings=guidance.invariant_findings,
                        initial_profile_conflicts=profile_conflicts,
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
                    species=species,
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
                    species=species,
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
                remaining_profile_conflicts = (
                    guidance.profile_conflicts
                    if guidance is not None and exc.profile_conflicts is None
                    else exc.profile_conflicts or ()
                )
                if guidance is not None and (
                    guidance.invariant_findings or remaining_profile_conflicts
                ):
                    retry_store.set_quality_guidance(
                        species.taxon_id,
                        guidance.invariant_findings,
                        invariant_findings=guidance.invariant_findings,
                        profile_conflicts=remaining_profile_conflicts,
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
                    species=species,
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
