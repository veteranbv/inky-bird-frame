"""Domain models persisted by the generation and catalog pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict


class SourceLink(TypedDict):
    title: str
    url: str


class Measurements(TypedDict):
    length: str
    wingspan: str
    weight: str


class SpeciesProfileData(TypedDict):
    taxon_id: int
    common_name: str
    scientific_name: str
    family: str
    measurements: Measurements
    field_marks: list[str]
    habitat: str
    behavior: str
    palette: list[str]
    sources: list[SourceLink]


@dataclass(frozen=True)
class ReferencePhoto:
    photo_id: int
    observation_id: int
    observer: str
    attribution: str
    license_code: str
    source_url: str
    image_url: str
    width: int
    height: int
    filename: str
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "photo_id": self.photo_id,
            "observation_id": self.observation_id,
            "observer": self.observer,
            "attribution": self.attribution,
            "license_code": self.license_code,
            "source_url": self.source_url,
            "image_url": self.image_url,
            "width": self.width,
            "height": self.height,
            "filename": self.filename,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class QualityReview:
    passed: bool
    species_accuracy: int
    anatomy_accuracy: int
    text_accuracy: int
    composition_quality: int
    location_free: bool
    findings: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "species_accuracy": self.species_accuracy,
            "anatomy_accuracy": self.anatomy_accuracy,
            "text_accuracy": self.text_accuracy,
            "composition_quality": self.composition_quality,
            "location_free": self.location_free,
            "findings": list(self.findings),
        }
