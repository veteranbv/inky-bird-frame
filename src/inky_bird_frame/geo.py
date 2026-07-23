"""Discovery location lookup helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from urllib.parse import urlencode

from .config import DiscoveryConfig
from .errors import DataSourceError
from .http import get_json

GEOAPIFY_POSTCODE_URL = "https://api.geoapify.com/v1/postcode/search"
GEOAPIFY_ATTRIBUTION = "Powered by Geoapify"


@dataclass(frozen=True)
class DiscoveryLocation:
    postal_code: str | None
    place_name: str
    state: str
    latitude: float
    longitude: float
    country_code: str | None = None
    geocoder: str = "configured"
    geocoder_attribution: str | None = None

    @property
    def label(self) -> str:
        place = ", ".join(part for part in (self.place_name, self.state) if part)
        if self.postal_code is not None:
            postal = f"postal code {self.postal_code}"
            return f"{place} | {postal}" if place else postal
        return place or "configured coordinates"


def _valid_coordinate(value: object, *, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and minimum <= parsed <= maximum else None


def _normalized_postal_code(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def parse_zippopotam_response(zip_code: str, payload: object) -> DiscoveryLocation:
    if not isinstance(payload, dict):
        raise DataSourceError("US ZIP lookup response was not an object")

    places = payload.get("places")
    if not isinstance(places, list) or not places:
        raise DataSourceError("US ZIP lookup returned no places")

    first = places[0]
    if not isinstance(first, dict):
        raise DataSourceError("US ZIP lookup returned a malformed place")

    try:
        latitude = float(first["latitude"])
        longitude = float(first["longitude"])
        place_name = str(first["place name"])
        state = str(first["state abbreviation"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DataSourceError("US ZIP lookup returned malformed data") from exc
    if (
        not math.isfinite(latitude)
        or not -90 <= latitude <= 90
        or not math.isfinite(longitude)
        or not -180 <= longitude <= 180
    ):
        raise DataSourceError("US ZIP lookup returned invalid coordinates")
    return DiscoveryLocation(
        postal_code=zip_code,
        place_name=place_name,
        state=state,
        latitude=latitude,
        longitude=longitude,
        country_code="us",
        geocoder="zippopotam",
    )


def lookup_us_zip(zip_code: str, timeout_seconds: float = 10.0) -> DiscoveryLocation:
    if not zip_code.isdigit() or len(zip_code) != 5:
        raise ValueError("zip_code must be a five digit US ZIP code")

    payload = get_json(
        f"https://api.zippopotam.us/us/{zip_code}",
        timeout_seconds,
        error_label="Zippopotam US ZIP API",
    )
    return parse_zippopotam_response(zip_code, payload)


def parse_geoapify_postcode_response(
    postal_code: str,
    country_code: str,
    payload: object,
) -> DiscoveryLocation:
    if not isinstance(payload, dict):
        raise DataSourceError("Geoapify Postcode API response was not an object")
    features = payload.get("features")
    if not isinstance(features, list):
        raise DataSourceError("Geoapify Postcode API response had no feature list")

    expected_postal = _normalized_postal_code(postal_code)
    candidates: list[DiscoveryLocation] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            continue
        result_postal = properties.get("postcode")
        result_country = properties.get("country_code")
        if (
            not isinstance(result_postal, str)
            or _normalized_postal_code(result_postal) != expected_postal
            or not isinstance(result_country, str)
            or result_country.casefold() != country_code.casefold()
        ):
            continue
        latitude = _valid_coordinate(properties.get("lat"), minimum=-90, maximum=90)
        longitude = _valid_coordinate(properties.get("lon"), minimum=-180, maximum=180)
        if latitude is None or longitude is None:
            continue
        place_name = next(
            (
                value
                for name in ("city", "municipality", "county", "country")
                if isinstance((value := properties.get(name)), str) and value.strip()
            ),
            "",
        )
        state = next(
            (
                value
                for name in ("state_code", "state")
                if isinstance((value := properties.get(name)), str) and value.strip()
            ),
            "",
        )
        datasource = properties.get("datasource")
        source_attribution = datasource.get("attribution") if isinstance(datasource, dict) else None
        source = (
            source_attribution.strip()
            if isinstance(source_attribution, str) and source_attribution.strip()
            else None
        )
        attribution = f"{GEOAPIFY_ATTRIBUTION}; {source}" if source else GEOAPIFY_ATTRIBUTION
        candidates.append(
            DiscoveryLocation(
                postal_code=postal_code,
                place_name=place_name,
                state=state,
                latitude=latitude,
                longitude=longitude,
                country_code=country_code.casefold(),
                geocoder="geoapify",
                geocoder_attribution=attribution,
            )
        )

    if not candidates:
        raise DataSourceError("Geoapify found no exact match for the configured postal code")
    coordinate_pairs = {(item.latitude, item.longitude) for item in candidates}
    if len(coordinate_pairs) > 1:
        raise DataSourceError(
            "Geoapify returned multiple locations for the configured postal code; "
            "configure latitude and longitude instead"
        )
    return candidates[0]


def lookup_geoapify_postcode(
    postal_code: str,
    country_code: str,
    api_key: str,
    timeout_seconds: float = 10.0,
) -> DiscoveryLocation:
    query = urlencode(
        {
            "postcode": postal_code,
            "countrycode": country_code,
            "geometry": "point",
            "apiKey": api_key,
        }
    )
    payload = get_json(
        f"{GEOAPIFY_POSTCODE_URL}?{query}",
        timeout_seconds,
        error_label="Geoapify Postcode API",
    )
    return parse_geoapify_postcode_response(postal_code, country_code, payload)


def resolve_discovery_location(config: DiscoveryConfig) -> DiscoveryLocation:
    if config.latitude is not None and config.longitude is not None:
        return DiscoveryLocation(
            postal_code=None,
            place_name="",
            state="",
            latitude=config.latitude,
            longitude=config.longitude,
        )
    if config.postal_code is not None and config.country_code is not None:
        if config.geoapify_api_key is None:
            raise DataSourceError("Geoapify API key is not configured")
        return lookup_geoapify_postcode(
            config.postal_code,
            config.country_code,
            config.geoapify_api_key,
        )
    if config.zip_code is not None:
        return lookup_us_zip(config.zip_code)
    raise DataSourceError("Discovery location is not configured")
