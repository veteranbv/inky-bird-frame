from __future__ import annotations

import random
import unittest

from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.catalog import CatalogEntry
from inky_bird_frame.config import RotationMode
from inky_bird_frame.selection import select_catalog_entry, select_rotating_species


def catalog_entry(taxon_id: int, count: int) -> CatalogEntry:
    return CatalogEntry(
        taxon_id=taxon_id,
        common_name=f"Bird {taxon_id}",
        scientific_name=f"Species {taxon_id}",
        slug=f"bird-{taxon_id}",
        portrait_path=f"{taxon_id}/portrait.png",
        portrait_sha256="a" * 64,
        display_path=f"{taxon_id}/display.png",
        display_sha256="b" * 64,
        approved_at="2026-07-09T00:00:00+00:00",
        observation_count=count,
    )


class SelectionTests(unittest.TestCase):
    def test_select_rotating_species_wraps_by_step(self) -> None:
        species = [
            BirdSpecies(12942, "Eastern Bluebird", "Sialia sialis", 26, "iNaturalist"),
            BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 18, "iNaturalist"),
        ]

        self.assertEqual(select_rotating_species(species, 0).common_name, "Eastern Bluebird")
        self.assertEqual(select_rotating_species(species, 1).common_name, "Carolina Wren")
        self.assertEqual(select_rotating_species(species, 2).common_name, "Eastern Bluebird")

    def test_select_rotating_species_rejects_bad_input(self) -> None:
        with self.assertRaises(ValueError):
            select_rotating_species([])
        with self.assertRaises(ValueError):
            select_rotating_species([BirdSpecies(1, "A", "B", 1, "test")], -1)

    def test_shuffle_visits_each_entry_before_repeating(self) -> None:
        entries = [catalog_entry(1, 10), catalog_entry(2, 5), catalog_entry(3, 1)]
        rng = random.Random(7)
        remaining: list[int] = []
        selected_ids: list[int] = []
        last_taxon_id: int | None = None
        for _ in entries:
            selected, _, remaining = select_catalog_entry(
                entries,
                RotationMode.SHUFFLE,
                next_index=0,
                last_taxon_id=last_taxon_id,
                shuffle_remaining=remaining,
                rng=rng,
            )
            selected_ids.append(selected.taxon_id)
            last_taxon_id = selected.taxon_id

        self.assertEqual(set(selected_ids), {1, 2, 3})
        self.assertEqual(remaining, [])

    def test_shuffle_refill_includes_every_entry_without_immediate_repeat(self) -> None:
        entries = [catalog_entry(1, 10), catalog_entry(2, 5), catalog_entry(3, 1)]
        rng = random.Random(7)
        remaining: list[int] = []
        selected_ids: list[int] = []
        last_taxon_id: int | None = None
        for _ in range(len(entries) * 2):
            selected, _, remaining = select_catalog_entry(
                entries,
                RotationMode.SHUFFLE,
                next_index=0,
                last_taxon_id=last_taxon_id,
                shuffle_remaining=remaining,
                rng=rng,
            )
            selected_ids.append(selected.taxon_id)
            last_taxon_id = selected.taxon_id

        self.assertEqual(set(selected_ids[:3]), {1, 2, 3})
        self.assertEqual(set(selected_ids[3:]), {1, 2, 3})
        self.assertTrue(
            all(left != right for left, right in zip(selected_ids, selected_ids[1:], strict=False))
        )

    def test_weighted_selection_uses_observation_counts_and_avoids_repeat(self) -> None:
        entries = [catalog_entry(1, 10), catalog_entry(2, 3)]
        selected, _, _ = select_catalog_entry(
            entries,
            RotationMode.WEIGHTED,
            next_index=0,
            last_taxon_id=1,
            shuffle_remaining=[],
            rng=random.Random(2),
        )

        self.assertEqual(selected.taxon_id, 2)


if __name__ == "__main__":
    unittest.main()
