"""Location lookup helpers."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import DataSourceError
from .http import get_json


@dataclass(frozen=True)
class ZipLocation:
    zip_code: str
    place_name: str
    state: str
    latitude: float
    longitude: float

    @property
    def label(self) -> str:
        return f"{self.place_name}, {self.state} | ZIP {self.zip_code}"


def parse_zippopotam_response(zip_code: str, payload: object) -> ZipLocation:
    if not isinstance(payload, dict):
        raise DataSourceError("ZIP lookup response was not an object")

    places = payload.get("places")
    if not isinstance(places, list) or not places:
        raise DataSourceError(f"No places returned for ZIP {zip_code}")

    first = places[0]
    if not isinstance(first, dict):
        raise DataSourceError(f"Malformed place returned for ZIP {zip_code}")

    try:
        return ZipLocation(
            zip_code=zip_code,
            place_name=str(first["place name"]),
            state=str(first["state abbreviation"]),
            latitude=float(first["latitude"]),
            longitude=float(first["longitude"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DataSourceError(f"Malformed ZIP lookup data for {zip_code}") from exc


def lookup_us_zip(zip_code: str, timeout_seconds: float = 10.0) -> ZipLocation:
    if not zip_code.isdigit() or len(zip_code) != 5:
        raise ValueError("zip_code must be a five digit US ZIP code")

    payload = get_json(f"https://api.zippopotam.us/us/{zip_code}", timeout_seconds)
    return parse_zippopotam_response(zip_code, payload)
