"""Subscription-backed Codex execution for research, image generation, and QA."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final, cast

from .birds import BirdSpecies, TaxonContext
from .errors import GenerationError
from .models import QualityReview, ReferencePhoto, SourceLink, SpeciesProfileData
from .prompts import plate_prompt, profile_prompt, review_prompt

PROFILE_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {
        "taxon_id": {"type": "integer"},
        "common_name": {"type": "string"},
        "scientific_name": {"type": "string"},
        "family": {"type": "string"},
        "measurements": {
            "type": "object",
            "properties": {
                "length": {"type": "string"},
                "wingspan": {"type": "string"},
                "weight": {"type": "string"},
            },
            "required": ["length", "wingspan", "weight"],
            "additionalProperties": False,
        },
        "field_marks": {"type": "array", "items": {"type": "string"}},
        "habitat": {"type": "string"},
        "behavior": {"type": "string"},
        "palette": {"type": "array", "items": {"type": "string"}},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
                "required": ["title", "url"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "taxon_id",
        "common_name",
        "scientific_name",
        "family",
        "measurements",
        "field_marks",
        "habitat",
        "behavior",
        "palette",
        "sources",
    ],
    "additionalProperties": False,
}

REVIEW_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "species_accuracy": {"type": "integer", "minimum": 1, "maximum": 5},
        "anatomy_accuracy": {"type": "integer", "minimum": 1, "maximum": 5},
        "text_accuracy": {"type": "integer", "minimum": 1, "maximum": 5},
        "composition_quality": {"type": "integer", "minimum": 1, "maximum": 5},
        "location_free": {"type": "boolean"},
        "findings": {"type": "array", "items": {"type": "string"}},
        "verification_sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
                "required": ["title", "url"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "passed",
        "species_accuracy",
        "anatomy_accuracy",
        "text_accuracy",
        "composition_quality",
        "location_free",
        "findings",
        "verification_sources",
    ],
    "additionalProperties": False,
}


class CodexRunner:
    def __init__(self, executable: Path, workspace: Path, timeout_seconds: int = 1200) -> None:
        self.executable = executable
        self.workspace = workspace.resolve()
        self.timeout_seconds = timeout_seconds
        if not self.executable.is_file():
            raise GenerationError(f"Codex executable not found: {self.executable}")

    def _base_command(self, *, writable: bool, search: bool = False) -> list[str]:
        command = [str(self.executable)]
        if search:
            command.append("--search")
        command.extend(
            [
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "workspace-write" if writable else "read-only",
            ]
        )
        return command

    def _run(
        self,
        command: list[str],
        prompt: str,
        log_path: Path,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                command,
                cwd=self.workspace,
                input=prompt,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise GenerationError(f"Codex timed out after {self.timeout_seconds} seconds") from exc
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"COMMAND: {' '.join(command)}\n\nSTDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
        )
        if result.returncode != 0:
            raise GenerationError(f"Codex exited with status {result.returncode}; see {log_path}")
        return result

    def _structured(
        self,
        prompt: str,
        schema: dict[str, object],
        images: list[Path],
        output_path: Path,
        log_path: Path,
        *,
        search: bool,
    ) -> object:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=output_path.parent) as temporary:
            schema_path = Path(temporary) / "schema.json"
            schema_path.write_text(json.dumps(schema, indent=2, sort_keys=True))
            command = self._base_command(writable=True, search=search)
            for image in images:
                command.extend(["--image", str(image.resolve())])
            command.extend(["--output-schema", str(schema_path), "-o", str(output_path), "-"])
            self._run(command, prompt, log_path)
        try:
            return cast(object, json.loads(output_path.read_text()))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise GenerationError(
                f"Codex did not write valid structured output: {output_path}"
            ) from exc

    def create_profile(
        self,
        species: BirdSpecies,
        context: TaxonContext,
        references: list[ReferencePhoto],
        reference_paths: list[Path],
        output_path: Path,
        log_path: Path,
    ) -> SpeciesProfileData:
        raw = self._structured(
            profile_prompt(species, context, references),
            PROFILE_SCHEMA,
            reference_paths,
            output_path,
            log_path,
            search=True,
        )
        profile = _parse_profile(raw)
        if (
            profile["taxon_id"] != species.taxon_id
            or profile["common_name"] != species.common_name
            or profile["scientific_name"] != species.scientific_name
        ):
            raise GenerationError("Codex profile identity does not match the discovered taxon")
        return profile

    def generate_plate(
        self,
        species: BirdSpecies,
        profile: SpeciesProfileData,
        references: list[ReferencePhoto],
        reference_paths: list[Path],
        output_path: Path,
        log_path: Path,
        correction_findings: tuple[str, ...] = (),
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = self._base_command(writable=True)
        for image in reference_paths:
            command.extend(["--image", str(image.resolve())])
        command.extend(["-"])
        self._run(
            command,
            plate_prompt(species, profile, references, output_path, correction_findings),
            log_path,
        )
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise GenerationError(f"Codex did not create the requested plate: {output_path}")
        return output_path

    def review_plate(
        self,
        species: BirdSpecies,
        profile: SpeciesProfileData,
        references: list[ReferencePhoto],
        plate_path: Path,
        reference_paths: list[Path],
        output_path: Path,
        log_path: Path,
    ) -> QualityReview:
        raw = self._structured(
            review_prompt(species, profile, references),
            REVIEW_SCHEMA,
            [plate_path, *reference_paths],
            output_path,
            log_path,
            search=True,
        )
        return _parse_review(raw)


def _non_empty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GenerationError(f"Codex output field {field} must be a non-empty string")
    return value.strip()


def _string_list(value: object, field: str, minimum: int) -> list[str]:
    if not isinstance(value, list):
        raise GenerationError(f"Codex output field {field} must be a list")
    items = [_non_empty_string(item, field) for item in value]
    if len(items) < minimum:
        raise GenerationError(f"Codex output field {field} must contain at least {minimum} items")
    return items


def _parse_profile(raw: object) -> SpeciesProfileData:
    if not isinstance(raw, dict):
        raise GenerationError("Codex profile output must be an object")
    taxon_id = raw.get("taxon_id")
    measurements = raw.get("measurements")
    sources = raw.get("sources")
    if not isinstance(taxon_id, int) or not isinstance(measurements, dict):
        raise GenerationError("Codex profile has invalid identity or measurements")
    if not isinstance(sources, list):
        raise GenerationError("Codex profile sources must be a list")
    parsed_sources: list[SourceLink] = []
    for source in sources:
        if not isinstance(source, dict):
            raise GenerationError("Codex profile source must be an object")
        url = _non_empty_string(source.get("url"), "sources.url")
        if not url.startswith("https://"):
            raise GenerationError("Codex profile source URLs must use HTTPS")
        parsed_sources.append(
            SourceLink(title=_non_empty_string(source.get("title"), "sources.title"), url=url)
        )
    if len(parsed_sources) < 2:
        raise GenerationError("Codex profile must cite at least two sources")
    return SpeciesProfileData(
        taxon_id=taxon_id,
        common_name=_non_empty_string(raw.get("common_name"), "common_name"),
        scientific_name=_non_empty_string(raw.get("scientific_name"), "scientific_name"),
        family=_non_empty_string(raw.get("family"), "family"),
        measurements={
            "length": _non_empty_string(measurements.get("length"), "measurements.length"),
            "wingspan": _non_empty_string(measurements.get("wingspan"), "measurements.wingspan"),
            "weight": _non_empty_string(measurements.get("weight"), "measurements.weight"),
        },
        field_marks=_string_list(raw.get("field_marks"), "field_marks", 4),
        habitat=_non_empty_string(raw.get("habitat"), "habitat"),
        behavior=_non_empty_string(raw.get("behavior"), "behavior"),
        palette=_string_list(raw.get("palette"), "palette", 3),
        sources=parsed_sources,
    )


def _score(raw: dict[str, object], field: str) -> int:
    value = raw.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5:
        raise GenerationError(f"Codex review field {field} must be an integer from 1 to 5")
    return value


def _parse_review(raw: object) -> QualityReview:
    if not isinstance(raw, dict):
        raise GenerationError("Codex review output must be an object")
    reported_pass = raw.get("passed") is True
    location_free = raw.get("location_free") is True
    species_accuracy = _score(raw, "species_accuracy")
    anatomy_accuracy = _score(raw, "anatomy_accuracy")
    text_accuracy = _score(raw, "text_accuracy")
    composition_quality = _score(raw, "composition_quality")
    findings = tuple(_string_list(raw.get("findings"), "findings", 0))
    sources = raw.get("verification_sources")
    if not isinstance(sources, list):
        raise GenerationError("Codex review verification_sources must be a list")
    verification_sources: list[SourceLink] = []
    for source in sources:
        if not isinstance(source, dict):
            raise GenerationError("Codex review verification source must be an object")
        url = _non_empty_string(source.get("url"), "verification_sources.url")
        if not url.startswith("https://"):
            raise GenerationError("Codex review source URLs must use HTTPS")
        verification_sources.append(
            SourceLink(
                title=_non_empty_string(source.get("title"), "verification_sources.title"),
                url=url,
            )
        )
    passed = (
        reported_pass
        and location_free
        and len(verification_sources) >= 2
        and min(
            species_accuracy,
            anatomy_accuracy,
            text_accuracy,
            composition_quality,
        )
        >= 4
    )
    return QualityReview(
        passed=passed,
        species_accuracy=species_accuracy,
        anatomy_accuracy=anatomy_accuracy,
        text_accuracy=text_accuracy,
        composition_quality=composition_quality,
        location_free=location_free,
        findings=findings,
        verification_sources=tuple(verification_sources),
    )
