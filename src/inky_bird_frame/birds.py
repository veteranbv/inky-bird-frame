"""Bird observation data sources."""

from __future__ import annotations

import fcntl
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final
from urllib.parse import quote, urlencode

from .errors import DataSourceError, TaxonomyMatchError
from .http import get_json, write_json_atomic
from .timeutil import parse_utc_timestamp


@dataclass(frozen=True)
class BirdSpecies:
    taxon_id: int
    common_name: str
    scientific_name: str
    observation_count: int
    source: str
    sources: tuple[str, ...] = ()
    latest_detection_at: str | None = None

    def __post_init__(self) -> None:
        if not self.sources:
            object.__setattr__(self, "sources", (self.source,))


@dataclass(frozen=True)
class EbirdSpecies:
    species_code: str
    common_name: str
    scientific_name: str
    observed_at: str


@dataclass(frozen=True)
class BirdWeatherSpecies:
    species_id: int
    common_name: str
    scientific_name: str
    detection_count: int
    latest_detection_at: str


@dataclass(frozen=True)
class EbirdResolution:
    species: list[BirdSpecies]
    unresolved: list[EbirdSpecies]


@dataclass(frozen=True)
class TaxonContext:
    taxon_id: int
    common_name: str
    scientific_name: str
    family: str
    summary: str
    source_url: str


class ObservationWindow(StrEnum):
    LAST_DAY = "last-day"
    LAST_WEEK = "last-week"
    LAST_30_DAYS = "last-30-days"
    LAST_YEAR = "last-year"
    ALL_TIME = "all-time"


EBIRD_BACK_DAYS: Final = {
    ObservationWindow.LAST_DAY: 1,
    ObservationWindow.LAST_WEEK: 7,
    ObservationWindow.LAST_30_DAYS: 30,
}
EBIRD_MAX_RADIUS_KM: Final = 50
EBIRD_UNRESOLVED_RETRY_DAYS: Final = 7
BIRDWEATHER_MAX_SPECIES: Final = 100


@dataclass(frozen=True)
class DateRange:
    start: date | None
    end: date | None

    def __post_init__(self) -> None:
        if (self.start is None) != (self.end is None):
            raise ValueError("date range requires both start and end dates")
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError("start date must be on or before end date")

    def as_query_params(self) -> dict[str, str]:
        params: dict[str, str] = {}
        if self.start is not None:
            params["d1"] = self.start.isoformat()
        if self.end is not None:
            params["d2"] = self.end.isoformat()
        return params


def date_range_for_window(window: ObservationWindow, today: date | None = None) -> DateRange:
    current = today or date.today()
    if window is ObservationWindow.ALL_TIME:
        return DateRange(start=None, end=None)
    if window is ObservationWindow.LAST_DAY:
        return DateRange(start=current - timedelta(days=1), end=current)
    if window is ObservationWindow.LAST_WEEK:
        return DateRange(start=current - timedelta(days=7), end=current)
    if window is ObservationWindow.LAST_30_DAYS:
        return DateRange(start=current - timedelta(days=30), end=current)
    if window is ObservationWindow.LAST_YEAR:
        return DateRange(start=current - timedelta(days=365), end=current)
    raise ValueError(f"Unsupported observation window: {window}")


def parse_observation_window(value: str) -> ObservationWindow:
    try:
        return ObservationWindow(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in ObservationWindow)
        raise ValueError(f"window must be one of: {allowed}") from exc


def parse_inaturalist_species_counts(payload: object) -> list[BirdSpecies]:
    if not isinstance(payload, dict):
        raise DataSourceError("iNaturalist response was not an object")

    results = payload.get("results")
    if not isinstance(results, list):
        raise DataSourceError("iNaturalist response did not include a results list")
    if not results:
        return []

    species: list[BirdSpecies] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        taxon = item.get("taxon")
        if not isinstance(taxon, dict):
            continue
        if taxon.get("rank") != "species":
            continue
        common = taxon.get("preferred_common_name")
        scientific = taxon.get("name")
        taxon_id = taxon.get("id")
        count = item.get("count")
        if not isinstance(common, str) or not isinstance(scientific, str):
            continue
        if not isinstance(taxon_id, int) or not isinstance(count, int):
            continue
        species.append(
            BirdSpecies(
                taxon_id=taxon_id,
                common_name=common,
                scientific_name=scientific,
                observation_count=count,
                source="iNaturalist",
            )
        )

    if not species:
        raise DataSourceError("No usable bird species were returned by iNaturalist")
    return species


def fetch_inaturalist_birds(
    *,
    latitude: float,
    longitude: float,
    radius_km: int,
    limit: int,
    window: ObservationWindow = ObservationWindow.ALL_TIME,
    date_range: DateRange | None = None,
    today: date | None = None,
    timeout_seconds: float = 10.0,
) -> list[BirdSpecies]:
    if radius_km <= 0:
        raise ValueError("radius_km must be greater than zero")
    if limit <= 0:
        raise ValueError("limit must be greater than zero")

    query_params = {
        "taxon_id": 3,
        "lat": f"{latitude:.6f}",
        "lng": f"{longitude:.6f}",
        "radius": str(radius_km),
        "rank": "species",
        "verifiable": "true",
        "photos": "true",
        "per_page": str(limit),
    }
    selected_dates = date_range or date_range_for_window(window, today)
    query_params.update(selected_dates.as_query_params())
    params = urlencode(query_params)
    payload = get_json(
        f"https://api.inaturalist.org/v1/observations/species_counts?{params}",
        timeout_seconds,
        error_label="iNaturalist API",
    )
    return parse_inaturalist_species_counts(payload)


def parse_ebird_observations(payload: object) -> list[EbirdSpecies]:
    if not isinstance(payload, list):
        raise DataSourceError("eBird response was not a list")

    species: list[EbirdSpecies] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        species_code = item.get("speciesCode")
        common_name = item.get("comName")
        scientific_name = item.get("sciName")
        observed_at = item.get("obsDt")
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (species_code, common_name, scientific_name, observed_at)
        ):
            continue
        assert isinstance(species_code, str)
        if species_code in seen:
            continue
        seen.add(species_code)
        species.append(
            EbirdSpecies(
                species_code=species_code,
                common_name=str(common_name),
                scientific_name=str(scientific_name),
                observed_at=str(observed_at),
            )
        )
    if payload and not species:
        raise DataSourceError("eBird response did not include usable species observations")
    return species


def fetch_ebird_observations(
    *,
    latitude: float,
    longitude: float,
    radius_km: int,
    limit: int,
    window: ObservationWindow,
    api_key: str,
    timeout_seconds: float = 10.0,
) -> list[EbirdSpecies]:
    if window not in EBIRD_BACK_DAYS:
        raise ValueError("eBird supports observation windows of 30 days or less")
    if not 0 < radius_km <= EBIRD_MAX_RADIUS_KM:
        raise ValueError(f"eBird radius_km must be between 1 and {EBIRD_MAX_RADIUS_KM}")
    if not 0 < limit <= 10_000:
        raise ValueError("eBird species_limit must be between 1 and 10000")
    if not api_key.strip():
        raise ValueError("eBird API key must not be empty")

    params = urlencode(
        {
            "lat": f"{latitude:.6f}",
            "lng": f"{longitude:.6f}",
            "dist": str(radius_km),
            "back": str(EBIRD_BACK_DAYS[window]),
            "cat": "species",
            "includeProvisional": "false",
            "maxResults": str(limit),
            "sort": "date",
            "sppLocale": "en",
        }
    )
    payload = get_json(
        f"https://api.ebird.org/v2/data/obs/geo/recent?{params}",
        timeout_seconds,
        headers={"X-eBirdApiToken": api_key},
    )
    return parse_ebird_observations(payload)


def parse_birdweather_species(payload: object) -> list[BirdWeatherSpecies]:
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise DataSourceError("BirdWeather response was not successful")
    results = payload.get("species")
    if not isinstance(results, list):
        raise DataSourceError("BirdWeather response did not include a species list")

    species: list[BirdWeatherSpecies] = []
    seen: set[int] = set()
    for item in results:
        if not isinstance(item, dict) or item.get("classification") != "avian":
            continue
        species_id = item.get("id")
        common_name = item.get("commonName")
        scientific_name = item.get("scientificName")
        latest_detection_at = item.get("latestDetectionAt")
        detections = item.get("detections")
        count = detections.get("total") if isinstance(detections, dict) else None
        if (
            not isinstance(species_id, int)
            or isinstance(species_id, bool)
            or species_id <= 0
            or species_id in seen
            or not isinstance(common_name, str)
            or not common_name.strip()
            or not isinstance(scientific_name, str)
            or not scientific_name.strip()
            or not isinstance(latest_detection_at, str)
            or not latest_detection_at.strip()
            or _parse_cache_datetime(latest_detection_at) is None
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count <= 0
        ):
            continue
        seen.add(species_id)
        species.append(
            BirdWeatherSpecies(
                species_id=species_id,
                common_name=common_name.strip(),
                scientific_name=scientific_name.strip(),
                detection_count=count,
                latest_detection_at=latest_detection_at.strip(),
            )
        )
    if results and not species:
        raise DataSourceError("BirdWeather response did not include usable avian species")
    return species


def fetch_birdweather_species(
    *,
    token: str,
    limit: int,
    window: ObservationWindow,
    today: date | None = None,
    timeout_seconds: float = 10.0,
) -> list[BirdWeatherSpecies]:
    if not token.strip():
        raise ValueError("BirdWeather station token must not be empty")
    if not 0 < limit <= BIRDWEATHER_MAX_SPECIES:
        raise ValueError(
            f"BirdWeather species_limit must be between 1 and {BIRDWEATHER_MAX_SPECIES}"
        )
    date_range = date_range_for_window(window, today)
    query: dict[str, str] = {
        "limit": str(limit),
        "sort": "top",
        "order": "desc",
        "classification": "avian",
    }
    if date_range.start is None:
        query["period"] = "all"
    else:
        query["from"] = date_range.start.isoformat()
        if date_range.end is not None:
            query["to"] = date_range.end.isoformat()
    encoded_token = quote(token.strip(), safe="")
    payload = get_json(
        f"https://app.birdweather.com/api/v1/stations/{encoded_token}/species?{urlencode(query)}",
        timeout_seconds,
        error_label="BirdWeather API",
    )
    return parse_birdweather_species(payload)


def parse_inaturalist_taxon_match(payload: object, scientific_name: str) -> BirdSpecies:
    if not isinstance(payload, dict):
        raise DataSourceError("iNaturalist taxon search response was not an object")
    results = payload.get("results")
    if not isinstance(results, list):
        raise DataSourceError("iNaturalist taxon search response did not include results")

    matches: list[BirdSpecies] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        taxon_id = item.get("id")
        common_name = item.get("preferred_common_name")
        name = item.get("name")
        if (
            item.get("rank") == "species"
            and item.get("is_active") is not False
            and item.get("iconic_taxon_name") == "Aves"
            and name == scientific_name
            and isinstance(taxon_id, int)
            and not isinstance(taxon_id, bool)
            and isinstance(common_name, str)
            and common_name
        ):
            matches.append(
                BirdSpecies(
                    taxon_id=taxon_id,
                    common_name=common_name,
                    scientific_name=name,
                    observation_count=1,
                    source="eBird",
                )
            )
    if len(matches) != 1:
        raise TaxonomyMatchError(
            f"Expected one active iNaturalist bird species matching {scientific_name}; "
            f"found {len(matches)}"
        )
    return matches[0]


def fetch_inaturalist_taxon_match(
    scientific_name: str, timeout_seconds: float = 10.0
) -> BirdSpecies:
    params = urlencode(
        {
            "q": scientific_name,
            "rank": "species",
            "taxon_id": "3",
            "per_page": "20",
        }
    )
    payload = get_json(f"https://api.inaturalist.org/v1/taxa?{params}", timeout_seconds)
    return parse_inaturalist_taxon_match(payload, scientific_name)


def resolve_ebird_species(
    observations: list[EbirdSpecies],
    cache_path: Path,
    *,
    now: datetime | None = None,
    timeout_seconds: float = 10.0,
    persist_cache: bool = True,
) -> EbirdResolution:
    if not persist_cache:
        return _resolve_external_species_locked(
            observations,
            cache_path,
            now=now,
            timeout_seconds=timeout_seconds,
            persist_cache=False,
        )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with _taxonomy_crosswalk_lock(cache_path):
        return _resolve_external_species_locked(
            observations,
            cache_path,
            now=now,
            timeout_seconds=timeout_seconds,
            persist_cache=True,
        )


def _resolve_external_species_locked(
    observations: list[EbirdSpecies],
    cache_path: Path,
    *,
    now: datetime | None,
    timeout_seconds: float,
    persist_cache: bool,
    source: str = "eBird",
    counts: dict[str, int] | None = None,
) -> EbirdResolution:
    current = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    cache = _read_taxonomy_crosswalk(cache_path)
    resolved: list[BirdSpecies] = []
    unresolved: list[EbirdSpecies] = []
    changed = False

    for observation in observations:
        cached = cache.get(observation.species_code)
        if cached is not None and cached.get("scientific_name") == observation.scientific_name:
            taxon_id = cached.get("taxon_id")
            common_name = cached.get("common_name")
            if isinstance(taxon_id, int) and isinstance(common_name, str):
                resolved.append(
                    BirdSpecies(
                        taxon_id=taxon_id,
                        common_name=common_name,
                        scientific_name=observation.scientific_name,
                        observation_count=(counts or {}).get(observation.species_code, 1),
                        source=source,
                    )
                )
                continue
            retry_at = _parse_cache_datetime(cached.get("retry_at"))
            if retry_at is not None and current < retry_at:
                unresolved.append(observation)
                continue

        try:
            species = fetch_inaturalist_taxon_match(
                observation.scientific_name, timeout_seconds=timeout_seconds
            )
        except TaxonomyMatchError:
            cache[observation.species_code] = {
                "scientific_name": observation.scientific_name,
                "retry_at": (current + timedelta(days=EBIRD_UNRESOLVED_RETRY_DAYS)).isoformat(),
            }
            unresolved.append(observation)
            changed = True
            continue
        cache[observation.species_code] = {
            "scientific_name": species.scientific_name,
            "common_name": species.common_name,
            "taxon_id": species.taxon_id,
            "resolved_at": current.isoformat(),
        }
        resolved.append(
            BirdSpecies(
                taxon_id=species.taxon_id,
                common_name=species.common_name,
                scientific_name=species.scientific_name,
                observation_count=(counts or {}).get(observation.species_code, 1),
                source=source,
            )
        )
        changed = True

    if changed and persist_cache:
        _write_taxonomy_crosswalk(cache_path, cache)
    return EbirdResolution(species=resolved, unresolved=unresolved)


def resolve_birdweather_species(
    detections: list[BirdWeatherSpecies],
    cache_path: Path,
    *,
    now: datetime | None = None,
    timeout_seconds: float = 10.0,
    persist_cache: bool = True,
) -> tuple[list[BirdSpecies], list[BirdWeatherSpecies]]:
    observations = [
        EbirdSpecies(
            species_code=str(item.species_id),
            common_name=item.common_name,
            scientific_name=item.scientific_name,
            observed_at=item.latest_detection_at,
        )
        for item in detections
    ]
    counts = {str(item.species_id): item.detection_count for item in detections}
    resolution = _resolve_external_species(
        observations,
        cache_path,
        source="BirdWeather",
        counts=counts,
        now=now,
        timeout_seconds=timeout_seconds,
        persist_cache=persist_cache,
    )
    unresolved_codes = {item.species_code for item in resolution.unresolved}
    resolved_detections = [
        item for item in detections if str(item.species_id) not in unresolved_codes
    ]
    if len(resolution.species) != len(resolved_detections):
        raise DataSourceError("BirdWeather taxonomy resolution returned inconsistent results")
    resolved = [
        replace(species, latest_detection_at=detection.latest_detection_at)
        for species, detection in zip(resolution.species, resolved_detections, strict=True)
    ]
    return resolved, [item for item in detections if str(item.species_id) in unresolved_codes]


def _resolve_external_species(
    observations: list[EbirdSpecies],
    cache_path: Path,
    *,
    source: str,
    counts: dict[str, int],
    now: datetime | None,
    timeout_seconds: float,
    persist_cache: bool,
) -> EbirdResolution:
    if not persist_cache:
        return _resolve_external_species_locked(
            observations,
            cache_path,
            now=now,
            timeout_seconds=timeout_seconds,
            persist_cache=False,
            source=source,
            counts=counts,
        )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with _taxonomy_crosswalk_lock(cache_path):
        return _resolve_external_species_locked(
            observations,
            cache_path,
            now=now,
            timeout_seconds=timeout_seconds,
            persist_cache=True,
            source=source,
            counts=counts,
        )


@contextmanager
def _taxonomy_crosswalk_lock(cache_path: Path) -> Iterator[None]:
    with cache_path.with_suffix(f"{cache_path.suffix}.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _parse_cache_datetime(value: object) -> datetime | None:
    return parse_utc_timestamp(value)


def _read_taxonomy_crosswalk(path: Path) -> dict[str, dict[str, object]]:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise DataSourceError(f"Invalid taxonomy crosswalk: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise DataSourceError(f"Unsupported taxonomy crosswalk: {path}")
    entries = raw.get("entries")
    if not isinstance(entries, dict) or any(
        not isinstance(key, str) or not isinstance(value, dict) for key, value in entries.items()
    ):
        raise DataSourceError(f"Invalid taxonomy crosswalk: {path}")
    return {str(key): dict(value) for key, value in entries.items()}


def _write_taxonomy_crosswalk(path: Path, entries: dict[str, dict[str, object]]) -> None:
    write_json_atomic(path, {"schema_version": 1, "entries": entries})


def parse_inaturalist_taxon(payload: object) -> TaxonContext:
    if not isinstance(payload, dict):
        raise DataSourceError("iNaturalist taxon response was not an object")
    results = payload.get("results")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise DataSourceError("iNaturalist taxon response did not include a result")

    taxon = results[0]
    taxon_id = taxon.get("id")
    common_name = taxon.get("preferred_common_name")
    scientific_name = taxon.get("name")
    summary_value = taxon.get("wikipedia_summary")
    source_url_value = taxon.get("wikipedia_url")
    ancestors = taxon.get("ancestors")
    if (
        not isinstance(taxon_id, int)
        or not isinstance(common_name, str)
        or not isinstance(scientific_name, str)
        or not isinstance(ancestors, list)
    ):
        raise DataSourceError("iNaturalist taxon response was incomplete")

    family = ""
    for ancestor in ancestors:
        if isinstance(ancestor, dict) and ancestor.get("rank") == "family":
            name = ancestor.get("name")
            if isinstance(name, str):
                family = name
                break
    if not family:
        raise DataSourceError("iNaturalist taxon response did not include a family")

    return TaxonContext(
        taxon_id=taxon_id,
        common_name=common_name,
        scientific_name=scientific_name,
        family=family,
        summary=summary_value if isinstance(summary_value, str) else "",
        source_url=(
            source_url_value
            if isinstance(source_url_value, str)
            else f"https://www.inaturalist.org/taxa/{taxon_id}"
        ),
    )


def parse_birdnet_taxon(payload: object, expected: TaxonContext) -> TaxonContext:
    if not isinstance(payload, dict):
        raise DataSourceError("BirdNET Taxonomy response was not an object")
    taxon_id = payload.get("inat_id")
    scientific_name = payload.get("scientific_name")
    common_name = payload.get("common_name")
    descriptions = payload.get("descriptions")
    wikipedia_urls = payload.get("wikipedia_urls")
    if (
        taxon_id != expected.taxon_id
        or scientific_name != expected.scientific_name
        or not isinstance(common_name, str)
        or not isinstance(descriptions, dict)
        or not isinstance(wikipedia_urls, dict)
    ):
        raise DataSourceError("BirdNET Taxonomy identity did not match the iNaturalist taxon")
    summary = descriptions.get("en")
    source_url = wikipedia_urls.get("en")
    if not isinstance(summary, str) or not summary.strip() or not isinstance(source_url, str):
        raise DataSourceError("BirdNET Taxonomy response did not include English context")
    return TaxonContext(
        taxon_id=expected.taxon_id,
        common_name=expected.common_name,
        scientific_name=expected.scientific_name,
        family=expected.family,
        summary=summary.strip(),
        source_url=source_url,
    )


def fetch_taxon_context(taxon_id: int, timeout_seconds: float = 10.0) -> TaxonContext:
    if taxon_id <= 0:
        raise ValueError("taxon_id must be greater than zero")
    payload = get_json(f"https://api.inaturalist.org/v1/taxa/{taxon_id}", timeout_seconds)
    context = parse_inaturalist_taxon(payload)
    if context.summary.strip():
        return context
    species_key = quote(context.scientific_name, safe="")
    birdnet = get_json(
        f"https://birdnet.cornell.edu/taxonomy/api/species/{species_key}", timeout_seconds
    )
    return parse_birdnet_taxon(birdnet, context)
