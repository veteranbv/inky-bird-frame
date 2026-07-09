"""Species selection policies."""

from __future__ import annotations

from .birds import BirdSpecies


def select_rotating_species(species: list[BirdSpecies], step: int = 0) -> BirdSpecies:
    if not species:
        raise ValueError("species list must not be empty")
    if step < 0:
        raise ValueError("step must be zero or greater")
    return species[step % len(species)]
