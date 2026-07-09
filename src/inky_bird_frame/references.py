"""Licensed iNaturalist reference-photo acquisition."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlencode, urlsplit, urlunsplit

from .errors import DataSourceError, InsufficientReferencesError, MissingDependencyError
from .http import get_bytes, get_json, write_bytes_atomic
from .models import ReferencePhoto

ALLOWED_LICENSES: Final = ("cc0", "cc-by")
MIN_REFERENCE_EDGE: Final = 800


@dataclass(frozen=True)
class ReferenceCandidate:
    photo_id: int
    observation_id: int
    observer: str
    attribution: str
    license_code: str
    source_url: str
    image_url: str
    width: int
    height: int


def photo_url_for_size(url: str, size: str = "large") -> str:
    if size not in {"small", "medium", "large", "original"}:
        raise ValueError(f"Unsupported iNaturalist photo size: {size}")
    parsed = urlsplit(url)
    filename = Path(parsed.path).name
    if "." not in filename:
        raise DataSourceError(f"Unexpected iNaturalist photo URL: {url}")
    suffix = filename.rsplit(".", 1)[1]
    parent = str(Path(parsed.path).parent)
    return urlunsplit((parsed.scheme, parsed.netloc, f"{parent}/{size}.{suffix}", parsed.query, ""))


def _parse_candidate(observation: object) -> list[ReferenceCandidate]:
    if not isinstance(observation, dict):
        return []
    observation_id = observation.get("id")
    source_url = observation.get("uri")
    user = observation.get("user")
    photos = observation.get("photos")
    if (
        not isinstance(observation_id, int)
        or not isinstance(source_url, str)
        or not isinstance(user, dict)
        or not isinstance(photos, list)
    ):
        return []
    observer = user.get("login")
    if not isinstance(observer, str):
        return []

    candidates: list[ReferenceCandidate] = []
    for photo in photos:
        if not isinstance(photo, dict):
            continue
        photo_id = photo.get("id")
        attribution = photo.get("attribution")
        license_code = photo.get("license_code")
        image_url = photo.get("url")
        dimensions = photo.get("original_dimensions")
        if (
            not isinstance(photo_id, int)
            or not isinstance(attribution, str)
            or not isinstance(license_code, str)
            or license_code.lower() not in ALLOWED_LICENSES
            or not isinstance(image_url, str)
            or not isinstance(dimensions, dict)
        ):
            continue
        width = dimensions.get("width")
        height = dimensions.get("height")
        if not isinstance(width, int) or not isinstance(height, int):
            continue
        if min(width, height) < MIN_REFERENCE_EDGE:
            continue
        candidates.append(
            ReferenceCandidate(
                photo_id=photo_id,
                observation_id=observation_id,
                observer=observer,
                attribution=attribution,
                license_code=license_code.lower(),
                source_url=source_url,
                image_url=photo_url_for_size(image_url),
                width=width,
                height=height,
            )
        )
    return candidates


def parse_reference_candidates(payload: object, count: int) -> list[ReferenceCandidate]:
    if not isinstance(payload, dict):
        raise DataSourceError("iNaturalist observations response was not an object")
    results = payload.get("results")
    if not isinstance(results, list):
        raise DataSourceError("iNaturalist observations response did not include results")

    selected: list[ReferenceCandidate] = []
    observers: set[str] = set()
    for observation in results:
        candidates = _parse_candidate(observation)
        candidate = next((item for item in candidates if item.observer not in observers), None)
        if candidate is None:
            continue
        selected.append(candidate)
        observers.add(candidate.observer)
        if len(selected) == count:
            break

    if len(selected) < count:
        raise InsufficientReferencesError(
            f"Only {len(selected)} suitable licensed reference photos were found; {count} required"
        )
    return selected


def fetch_reference_candidates(
    taxon_id: int,
    count: int,
    timeout_seconds: float = 20.0,
) -> list[ReferenceCandidate]:
    if taxon_id <= 0:
        raise ValueError("taxon_id must be greater than zero")
    if count <= 0:
        raise ValueError("count must be greater than zero")
    params = urlencode(
        {
            "taxon_id": str(taxon_id),
            "photos": "true",
            "quality_grade": "research",
            "photo_licensed": "true",
            "photo_license": ",".join(ALLOWED_LICENSES),
            "order_by": "votes",
            "order": "desc",
            "per_page": str(max(30, count * 10)),
        }
    )
    payload = get_json(f"https://api.inaturalist.org/v1/observations?{params}", timeout_seconds)
    return parse_reference_candidates(payload, count)


def _validate_image(path: Path) -> None:
    try:
        from PIL import Image
    except ModuleNotFoundError as exc:
        raise MissingDependencyError("Pillow is required to validate reference images") from exc
    try:
        with Image.open(path) as image:
            image.verify()
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise DataSourceError(f"Downloaded reference is not a valid image: {path.name}") from exc


def download_references(
    candidates: list[ReferenceCandidate],
    destination: Path,
    timeout_seconds: float = 30.0,
) -> list[ReferencePhoto]:
    destination.mkdir(parents=True, exist_ok=True)
    references: list[ReferencePhoto] = []
    for index, candidate in enumerate(candidates, start=1):
        suffix = Path(urlsplit(candidate.image_url).path).suffix.lower() or ".jpg"
        filename = f"{index:02d}-{candidate.photo_id}{suffix}"
        path = destination / filename
        content = get_bytes(candidate.image_url, timeout_seconds)
        write_bytes_atomic(path, content)
        _validate_image(path)
        references.append(
            ReferencePhoto(
                photo_id=candidate.photo_id,
                observation_id=candidate.observation_id,
                observer=candidate.observer,
                attribution=candidate.attribution,
                license_code=candidate.license_code,
                source_url=candidate.source_url,
                image_url=candidate.image_url,
                width=candidate.width,
                height=candidate.height,
                filename=filename,
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    return references
