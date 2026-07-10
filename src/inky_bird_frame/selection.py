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
    by_taxon = _entries_by_taxon(entries)
    candidates = [entry for entry in entries if entry.taxon_id != last_taxon_id] or entries

    if mode is RotationMode.WEIGHTED:
        selected = random_source.choices(
            candidates,
            weights=[max(entry.observation_count or 1, 1) for entry in candidates],
            k=1,
        )[0]
        return selected, next_index, []

    if mode is not RotationMode.SHUFFLE:
        raise ValueError(f"Unsupported rotation mode: {mode}")

    remaining = [taxon_id for taxon_id in shuffle_remaining if taxon_id in by_taxon]
    if not remaining:
        remaining = list(by_taxon)
        _shuffle_without_immediate_repeat(remaining, last_taxon_id, random_source)
    selected = by_taxon[remaining.pop(0)]
    return selected, next_index, remaining


def select_shuffle_bag_entry(
    entries: list[CatalogEntry],
    *,
    last_taxon_id: int | None,
    shuffle_bag_remaining: list[int],
    shuffle_bag_seen: list[int],
    rng: random.Random | random.SystemRandom | None = None,
) -> tuple[CatalogEntry, list[int], list[int]]:
    """Select from a durable bag while admitting newly active entries immediately."""
    if not entries:
        raise ValueError("catalog entries must not be empty")

    random_source = rng or random.SystemRandom()
    by_taxon = _entries_by_taxon(entries)
    active_taxon_ids = list(by_taxon)
    _validate_unique_taxon_ids(shuffle_bag_remaining, "shuffle_bag_remaining")
    _validate_unique_taxon_ids(shuffle_bag_seen, "shuffle_bag_seen")
    if set(shuffle_bag_remaining) & set(shuffle_bag_seen):
        raise ValueError("shuffle_bag_remaining and shuffle_bag_seen must not overlap")

    remaining = [taxon_id for taxon_id in shuffle_bag_remaining if taxon_id in by_taxon]
    seen = [taxon_id for taxon_id in shuffle_bag_seen if taxon_id in by_taxon]
    known_taxon_ids = set(remaining) | set(seen)
    additions = [taxon_id for taxon_id in active_taxon_ids if taxon_id not in known_taxon_ids]
    if additions:
        remaining.extend(additions)
        _shuffle_without_immediate_repeat(remaining, last_taxon_id, random_source)
    elif not remaining:
        remaining = active_taxon_ids
        seen = []
        _shuffle_without_immediate_repeat(remaining, last_taxon_id, random_source)

    selected_taxon_id = remaining.pop(0)
    seen.append(selected_taxon_id)
    return by_taxon[selected_taxon_id], remaining, seen


def _entries_by_taxon(entries: list[CatalogEntry]) -> dict[int, CatalogEntry]:
    by_taxon: dict[int, CatalogEntry] = {}
    for entry in entries:
        if entry.taxon_id in by_taxon:
            raise ValueError(f"Duplicate catalog taxon ID: {entry.taxon_id}")
        by_taxon[entry.taxon_id] = entry
    return by_taxon


def _validate_unique_taxon_ids(taxon_ids: list[int], name: str) -> None:
    if len(taxon_ids) != len(set(taxon_ids)):
        raise ValueError(f"{name} must not contain duplicate taxon IDs")


def _shuffle_without_immediate_repeat(
    taxon_ids: list[int],
    last_taxon_id: int | None,
    rng: random.Random | random.SystemRandom,
) -> None:
    rng.shuffle(taxon_ids)
    if len(taxon_ids) > 1 and taxon_ids[0] == last_taxon_id:
        taxon_ids[0], taxon_ids[1] = taxon_ids[1], taxon_ids[0]
