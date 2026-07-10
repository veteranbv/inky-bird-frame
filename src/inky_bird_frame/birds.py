"""Bird observation data sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from urllib.parse import urlencode

from .errors import DataSourceError
from .http import get_json


@dataclass(frozen=True)
class BirdSpecies:
    taxon_id: int
    common_name: str
    scientific_name: str
    observation_count: int
    source: str


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


@dataclass(frozen=True)
class DateRange:
    start: date | None
    end: date | None

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
        "verifiable": "true",
        "photos": "true",
        "per_page": str(limit),
    }
    query_params.update(date_range_for_window(window, today).as_query_params())
    params = urlencode(query_params)
    payload = get_json(
        f"https://api.inaturalist.org/v1/observations/species_counts?{params}",
        timeout_seconds,
    )
    return parse_inaturalist_species_counts(payload)


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
    summary = taxon.get("wikipedia_summary")
    source_url = taxon.get("wikipedia_url")
    ancestors = taxon.get("ancestors")
    if (
        not isinstance(taxon_id, int)
        or not isinstance(common_name, str)
        or not isinstance(scientific_name, str)
        or not isinstance(summary, str)
        or not isinstance(source_url, str)
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
        summary=summary,
        source_url=source_url,
    )


def fetch_taxon_context(taxon_id: int, timeout_seconds: float = 10.0) -> TaxonContext:
    if taxon_id <= 0:
        raise ValueError("taxon_id must be greater than zero")
    payload = get_json(f"https://api.inaturalist.org/v1/taxa/{taxon_id}", timeout_seconds)
    return parse_inaturalist_taxon(payload)
