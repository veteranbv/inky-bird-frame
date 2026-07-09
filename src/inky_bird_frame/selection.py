"""Species and display rotation selection policies."""

from __future__ import annotations

import random

from .birds import BirdSpecies
from .catalog import CatalogEntry
from .config import RotationMode


def select_rotating_species(species: list[BirdSpecies], step: int = 0) -> BirdSpecies:
    if not species:
        raise ValueError("species list must not be empty")
    if step < 0:
        raise ValueError("step must be zero or greater")
    return species[step % len(species)]


def select_catalog_entry(
    entries: list[CatalogEntry],
    mode: RotationMode,
    *,
    next_index: int,
    last_taxon_id: int | None,
    shuffle_remaining: list[int],
    rng: random.Random | random.SystemRandom | None = None,
) -> tuple[CatalogEntry, int, list[int]]:
    if not entries:
        raise ValueError("catalog entries must not be empty")
    if next_index < 0:
        raise ValueError("next_index must be zero or greater")

    if mode is RotationMode.SEQUENTIAL:
        selected = entries[next_index % len(entries)]
        return selected, (next_index + 1) % len(entries), []

    random_source = rng or random.SystemRandom()
    by_taxon = {entry.taxon_id: entry for entry in entries}
    candidates = [entry for entry in entries if entry.taxon_id != last_taxon_id] or entries

    if mode is RotationMode.WEIGHTED:
        selected = random_source.choices(
            candidates,
            weights=[max(entry.observation_count or 1, 1) for entry in candidates],
            k=1,
        )[0]
        return selected, next_index, []

    remaining = [taxon_id for taxon_id in shuffle_remaining if taxon_id in by_taxon]
    if not remaining:
        remaining = list(by_taxon)
        random_source.shuffle(remaining)
        if len(remaining) > 1 and remaining[0] == last_taxon_id:
            remaining[0], remaining[1] = remaining[1], remaining[0]
    selected = by_taxon[remaining.pop(0)]
    return selected, next_index, remaining
