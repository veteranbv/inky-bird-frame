"""Durable limits for subscription-backed web research."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from .errors import CatalogError, GenerationError
from .http import write_json_atomic


class ResearchBudget:
    def __init__(self, path: Path, *, daily_limit: int, species_limit: int) -> None:
        self.path = path
        self.daily_limit = daily_limit
        self.species_limit = species_limit

    def consume(self, taxon_id: int, now: datetime | None = None) -> None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        day, total, species = self._read()
        if day != current.date():
            total = 0
            species = {}
        species_count = species.get(taxon_id, 0)
        if total >= self.daily_limit:
            raise GenerationError(
                f"Daily research limit of {self.daily_limit} searches has been reached"
            )
        if species_count >= self.species_limit:
            raise GenerationError(
                f"Taxon {taxon_id} reached its research limit of {self.species_limit} searches"
            )
        species[taxon_id] = species_count + 1
        write_json_atomic(
            self.path,
            {
                "schema_version": 1,
                "date": current.date().isoformat(),
                "total": total + 1,
                "species": {str(key): value for key, value in sorted(species.items())},
            },
        )

    def _read(self) -> tuple[date, int, dict[int, int]]:
        if not self.path.exists():
            return date.min, 0, {}
        try:
            raw = json.loads(self.path.read_text())
        except json.JSONDecodeError as exc:
            raise CatalogError(f"Invalid research budget: {self.path}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise CatalogError(f"Unsupported research budget: {self.path}")
        day = raw.get("date")
        total = raw.get("total")
        species = raw.get("species")
        if (
            not isinstance(day, str)
            or not isinstance(total, int)
            or isinstance(total, bool)
            or total < 0
            or not isinstance(species, dict)
        ):
            raise CatalogError(f"Invalid research budget: {self.path}")
        try:
            parsed_day = date.fromisoformat(day)
        except ValueError as exc:
            raise CatalogError(f"Invalid research budget date: {self.path}") from exc
        parsed_species: dict[int, int] = {}
        for key, value in species.items():
            try:
                taxon_id = int(key)
            except (TypeError, ValueError) as exc:
                raise CatalogError(f"Invalid research budget taxon: {self.path}") from exc
            if taxon_id <= 0 or not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise CatalogError(f"Invalid research budget count: {self.path}")
            parsed_species[taxon_id] = value
        return parsed_day, total, parsed_species
