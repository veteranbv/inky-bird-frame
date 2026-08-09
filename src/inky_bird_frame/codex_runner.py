"""Subscription-backed Codex execution for research, image generation, and QA."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final, cast
from urllib.parse import urlsplit

from .birds import BirdSpecies, TaxonContext
from .errors import GenerationError
from .models import (
    PROFILE_CONFLICT_FIELDS,
    ProfileConflict,
    QualityReview,
    ReferencePhoto,
    SourceLink,
    SpeciesProfileData,
)
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
        "correction_findings": {"type": "array", "items": {"type": "string"}},
        "resolved_corrections": {"type": "array", "items": {"type": "string"}},
        "profile_conflicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": list(PROFILE_CONFLICT_FIELDS)},
                    "profile_value": {"type": "string"},
                    "observed_value": {"type": "string"},
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "url": {"type": "string"},
                            },
                            "required": ["title", "url"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["field", "profile_value", "observed_value", "sources"],
                "additionalProperties": False,
            },
        },
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
        "correction_findings",
        "resolved_corrections",
        "profile_conflicts",
        "verification_sources",
    ],
    "additionalProperties": False,
}


class CodexRunner:
    def __init__(
        self,
        executable: Path,
        workspace: Path,
        timeout_seconds: int = 1200,
        *,
        model: str | None = None,
    ) -> None:
        self.executable = executable
        self.workspace = workspace.resolve()
        self.timeout_seconds = timeout_seconds
        self.model = model
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
        if self.model is not None:
            command.extend(["--model", self.model])
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
        *,
        allowed_domains: tuple[str, ...],
        prior_profile: SpeciesProfileData | None = None,
        profile_conflicts: tuple[ProfileConflict, ...] = (),
    ) -> SpeciesProfileData:
        raw = self._structured(
            profile_prompt(
                species,
                context,
                references,
                allowed_domains,
                prior_profile=prior_profile,
                profile_conflicts=profile_conflicts,
            ),
            PROFILE_SCHEMA,
            reference_paths,
            output_path,
            log_path,
            search=True,
        )
        profile = parse_species_profile(raw, allowed_domains)
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
        *,
        correction_source_path: Path | None = None,
        invariant_findings: tuple[str, ...] = (),
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = self._base_command(writable=True)
        if correction_source_path is not None:
            command.extend(["--image", str(correction_source_path.resolve())])
        for image in reference_paths:
            command.extend(["--image", str(image.resolve())])
        command.extend(["-"])
        self._run(
            command,
            plate_prompt(
                species,
                profile,
                references,
                output_path,
                correction_findings,
                invariant_findings=invariant_findings,
                has_correction_source=correction_source_path is not None,
            ),
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
        *,
        allowed_domains: tuple[str, ...],
        prior_corrections: tuple[str, ...] = (),
        prior_profile_conflicts: tuple[ProfileConflict, ...] = (),
    ) -> QualityReview:
        raw = self._structured(
            review_prompt(
                species,
                profile,
                references,
                allowed_domains,
                prior_corrections=prior_corrections,
                prior_profile_conflicts=prior_profile_conflicts,
            ),
            REVIEW_SCHEMA,
            [plate_path, *reference_paths],
            output_path,
            log_path,
            search=True,
        )
        return _parse_review(
            raw,
            profile,
            allowed_domains,
            prior_corrections=prior_corrections,
        )


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


def _allowed_source(url: str, allowed_domains: tuple[str, ...] | None) -> bool:
    if allowed_domains is None:
        return True
    hostname = urlsplit(url).hostname
    return hostname is not None and any(
        hostname == domain or hostname.endswith(f".{domain}") for domain in allowed_domains
    )


def _source_identity(url: str, allowed_domains: tuple[str, ...] | None) -> str | None:
    hostname = urlsplit(url).hostname
    if hostname is None or allowed_domains is None:
        return hostname
    return next(
        (
            domain
            for domain in allowed_domains
            if hostname == domain or hostname.endswith(f".{domain}")
        ),
        None,
    )


def parse_species_profile(
    raw: object, allowed_domains: tuple[str, ...] | None = None
) -> SpeciesProfileData:
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
    source_hosts: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise GenerationError("Codex profile source must be an object")
        url = _non_empty_string(source.get("url"), "sources.url")
        if not url.startswith("https://"):
            raise GenerationError("Codex profile source URLs must use HTTPS")
        if not _allowed_source(url, allowed_domains):
            raise GenerationError("Codex profile cited a source outside the configured allowlist")
        source_identity = _source_identity(url, allowed_domains)
        if source_identity is not None:
            source_hosts.add(source_identity)
        parsed_sources.append(
            SourceLink(title=_non_empty_string(source.get("title"), "sources.title"), url=url)
        )
    if len(parsed_sources) < 2:
        raise GenerationError("Codex profile must cite at least two sources")
    if len(source_hosts) < 2:
        raise GenerationError("Codex profile must cite at least two independent source domains")
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


def _parse_profile_conflicts(
    value: object,
    current_profile: SpeciesProfileData,
    allowed_domains: tuple[str, ...] | None,
) -> tuple[ProfileConflict, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise GenerationError("Codex review field profile_conflicts must be a list")
    conflicts: list[ProfileConflict] = []
    fields: set[str] = set()
    for raw_conflict in value:
        if not isinstance(raw_conflict, dict):
            raise GenerationError("Codex profile conflict must be an object")
        field = _non_empty_string(raw_conflict.get("field"), "profile_conflicts.field")
        if field not in PROFILE_CONFLICT_FIELDS:
            raise GenerationError(f"Codex profile conflict field is not supported: {field}")
        if field in fields:
            raise GenerationError(f"Codex profile conflict field is duplicated: {field}")
        fields.add(field)
        profile_value = _non_empty_string(
            raw_conflict.get("profile_value"), "profile_conflicts.profile_value"
        )
        expected_profile_value: str
        if field == "measurements.length":
            expected_profile_value = current_profile["measurements"]["length"]
        elif field == "measurements.wingspan":
            expected_profile_value = current_profile["measurements"]["wingspan"]
        elif field == "measurements.weight":
            expected_profile_value = current_profile["measurements"]["weight"]
        elif field == "field_marks":
            expected_profile_value = json.dumps(
                current_profile["field_marks"],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        elif field == "family":
            expected_profile_value = current_profile["family"]
        elif field == "habitat":
            expected_profile_value = current_profile["habitat"]
        elif field == "behavior":
            expected_profile_value = current_profile["behavior"]
        else:
            raise GenerationError(f"Unsupported profile conflict field: {field}")
        matches_current_profile = profile_value == expected_profile_value
        if field == "field_marks":
            try:
                matches_current_profile = (
                    json.loads(profile_value) == current_profile["field_marks"]
                )
            except json.JSONDecodeError:
                matches_current_profile = False
        if not matches_current_profile:
            raise GenerationError(
                f"Codex profile conflict does not match the current profile field: {field}"
            )
        profile_value = expected_profile_value
        observed_value = _non_empty_string(
            raw_conflict.get("observed_value"), "profile_conflicts.observed_value"
        )
        if field == "field_marks":
            try:
                observed_field_marks = json.loads(observed_value)
            except json.JSONDecodeError as exc:
                raise GenerationError(
                    "Codex field_marks conflict observed_value must be a JSON array"
                ) from exc
            if not isinstance(observed_field_marks, list) or any(
                not isinstance(mark, str) or not mark.strip() for mark in observed_field_marks
            ):
                raise GenerationError(
                    "Codex field_marks conflict observed_value must be a JSON array of strings"
                )
            if observed_field_marks == current_profile["field_marks"]:
                raise GenerationError("Codex profile conflict values must disagree")
            observed_value = json.dumps(
                observed_field_marks,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        if profile_value == observed_value:
            raise GenerationError("Codex profile conflict values must disagree")
        raw_sources = raw_conflict.get("sources")
        if not isinstance(raw_sources, list):
            raise GenerationError("Codex profile conflict sources must be a list")
        sources: list[SourceLink] = []
        urls: set[str] = set()
        domains: set[str] = set()
        for raw_source in raw_sources:
            if not isinstance(raw_source, dict):
                raise GenerationError("Codex profile conflict source must be an object")
            url = _non_empty_string(raw_source.get("url"), "profile_conflicts.sources.url")
            if not url.startswith("https://"):
                raise GenerationError("Codex profile conflict source URLs must use HTTPS")
            if not _allowed_source(url, allowed_domains):
                raise GenerationError(
                    "Codex profile conflict cited a source outside the configured allowlist"
                )
            if url in urls:
                continue
            urls.add(url)
            identity = _source_identity(url, allowed_domains)
            if identity is not None:
                domains.add(identity)
            sources.append(
                SourceLink(
                    title=_non_empty_string(
                        raw_source.get("title"), "profile_conflicts.sources.title"
                    ),
                    url=url,
                )
            )
        if len(sources) < 2 or len(domains) < 2:
            raise GenerationError(
                "Codex profile conflicts must cite two independent verification sources"
            )
        conflicts.append(
            ProfileConflict(
                field=field,
                profile_value=profile_value,
                observed_value=observed_value,
                sources=sources,
            )
        )
    return tuple(conflicts)


def _parse_resolved_corrections(
    value: object,
    prior_corrections: tuple[str, ...],
    current_corrections: tuple[str, ...],
) -> tuple[str, ...]:
    if value is None:
        return ()
    items = _string_list(value, "resolved_corrections", 0)
    prior_by_key = {" ".join(item.split()).casefold(): item for item in prior_corrections}
    current_keys = {" ".join(item.split()).casefold() for item in current_corrections}
    resolved: list[str] = []
    resolved_keys: set[str] = set()
    for item in items:
        key = " ".join(item.split()).casefold()
        if key not in prior_by_key:
            raise GenerationError("Codex resolved correction was not present in review history")
        if key in current_keys:
            raise GenerationError("Codex correction cannot be both resolved and actionable")
        if key in resolved_keys:
            raise GenerationError("Codex resolved correction is duplicated")
        resolved_keys.add(key)
        resolved.append(prior_by_key[key])
    return tuple(resolved)


def _parse_review(
    raw: object,
    current_profile: SpeciesProfileData,
    allowed_domains: tuple[str, ...] | None = None,
    *,
    prior_corrections: tuple[str, ...] = (),
) -> QualityReview:
    if not isinstance(raw, dict):
        raise GenerationError("Codex review output must be an object")
    reported_pass = raw.get("passed") is True
    location_free = raw.get("location_free") is True
    species_accuracy = _score(raw, "species_accuracy")
    anatomy_accuracy = _score(raw, "anatomy_accuracy")
    text_accuracy = _score(raw, "text_accuracy")
    composition_quality = _score(raw, "composition_quality")
    findings = tuple(_string_list(raw.get("findings"), "findings", 0))
    correction_findings = tuple(
        _string_list(raw.get("correction_findings"), "correction_findings", 0)
    )
    resolved_corrections = _parse_resolved_corrections(
        raw.get("resolved_corrections"),
        prior_corrections,
        correction_findings,
    )
    profile_conflicts = _parse_profile_conflicts(
        raw.get("profile_conflicts"),
        current_profile,
        allowed_domains,
    )
    sources = raw.get("verification_sources")
    if not isinstance(sources, list):
        raise GenerationError("Codex review verification_sources must be a list")
    verification_sources: list[SourceLink] = []
    source_urls: set[str] = set()
    source_hosts: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise GenerationError("Codex review verification source must be an object")
        url = _non_empty_string(source.get("url"), "verification_sources.url")
        if not url.startswith("https://"):
            raise GenerationError("Codex review source URLs must use HTTPS")
        if not _allowed_source(url, allowed_domains):
            raise GenerationError("Codex review cited a source outside the configured allowlist")
        if url in source_urls:
            continue
        source_urls.add(url)
        source_identity = _source_identity(url, allowed_domains)
        if source_identity is not None:
            source_hosts.add(source_identity)
        verification_sources.append(
            SourceLink(
                title=_non_empty_string(source.get("title"), "verification_sources.title"),
                url=url,
            )
        )
    if len(verification_sources) < 2:
        raise GenerationError("Codex review must cite at least two verification sources")
    if len(source_hosts) < 2:
        raise GenerationError(
            "Codex review must cite at least two independent verification source domains"
        )
    requires_correction = not (
        reported_pass
        and location_free
        and min(
            species_accuracy,
            anatomy_accuracy,
            text_accuracy,
            composition_quality,
        )
        >= 4
    )
    if requires_correction and not (correction_findings or profile_conflicts):
        raise GenerationError(
            "Failed Codex reviews must include correction_findings or profile_conflicts"
        )
    if not requires_correction and (correction_findings or profile_conflicts):
        raise GenerationError(
            "Passing Codex reviews must not include correction_findings or profile_conflicts"
        )
    return QualityReview(
        passed=not requires_correction,
        species_accuracy=species_accuracy,
        anatomy_accuracy=anatomy_accuracy,
        text_accuracy=text_accuracy,
        composition_quality=composition_quality,
        location_free=location_free,
        findings=findings,
        verification_sources=tuple(verification_sources),
        correction_findings=correction_findings,
        profile_conflicts=profile_conflicts,
        resolved_corrections=resolved_corrections,
    )
