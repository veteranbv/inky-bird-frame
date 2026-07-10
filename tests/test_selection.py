from __future__ import annotations

import random
import unittest

from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.catalog import CatalogEntry
from inky_bird_frame.config import RotationMode
from inky_bird_frame.selection import (
    select_catalog_entry,
    select_rotating_species,
    select_shuffle_bag_entry,
)


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

    def test_shuffle_bag_visits_each_entry_without_replacement_before_refill(self) -> None:
        entries = [catalog_entry(1, 10), catalog_entry(2, 5), catalog_entry(3, 1)]
        rng = random.Random(7)
        remaining: list[int] = []
        seen: list[int] = []
        last_taxon_id: int | None = None
        selected_ids: list[int] = []

        for _ in range(len(entries) * 2):
            selected, remaining, seen = select_shuffle_bag_entry(
                entries,
                last_taxon_id=last_taxon_id,
                shuffle_bag_remaining=remaining,
                shuffle_bag_seen=seen,
                rng=rng,
            )
            selected_ids.append(selected.taxon_id)
            last_taxon_id = selected.taxon_id

        self.assertEqual(set(selected_ids[:3]), {1, 2, 3})
        self.assertEqual(set(selected_ids[3:]), {1, 2, 3})
        self.assertTrue(
            all(left != right for left, right in zip(selected_ids, selected_ids[1:], strict=False))
        )

    def test_shuffle_bag_adds_new_entries_and_prunes_removed_entries_mid_bag(self) -> None:
        entries = [catalog_entry(1, 10), catalog_entry(2, 5), catalog_entry(3, 1)]
        rng = random.Random(7)
        first, remaining, seen = select_shuffle_bag_entry(
            entries,
            last_taxon_id=None,
            shuffle_bag_remaining=[],
            shuffle_bag_seen=[],
            rng=rng,
        )
        removed_taxon_id = remaining[0]
        updated_entries = [entry for entry in entries if entry.taxon_id != removed_taxon_id] + [
            catalog_entry(4, 2)
        ]
        selected_ids = [first.taxon_id]

        while remaining:
            selected, remaining, seen = select_shuffle_bag_entry(
                updated_entries,
                last_taxon_id=selected_ids[-1],
                shuffle_bag_remaining=remaining,
                shuffle_bag_seen=seen,
                rng=rng,
            )
            selected_ids.append(selected.taxon_id)

        self.assertNotIn(removed_taxon_id, selected_ids)
        self.assertEqual(len(selected_ids), len(set(selected_ids)))
        self.assertIn(4, selected_ids)

    def test_shuffle_bag_selects_an_addition_before_refilling_shown_entries(self) -> None:
        entries = [catalog_entry(1, 10), catalog_entry(3, 1)]

        selected, remaining, seen = select_shuffle_bag_entry(
            entries,
            last_taxon_id=1,
            shuffle_bag_remaining=[2],
            shuffle_bag_seen=[1],
            rng=random.Random(7),
        )

        self.assertEqual(selected.taxon_id, 3)
        self.assertEqual(remaining, [])
        self.assertEqual(seen, [1, 3])

    def test_shuffle_bag_allows_a_singleton_catalog(self) -> None:
        entries = [catalog_entry(1, 1)]
        first, remaining, seen = select_shuffle_bag_entry(
            entries,
            last_taxon_id=None,
            shuffle_bag_remaining=[],
            shuffle_bag_seen=[],
            rng=random.Random(1),
        )
        second, _, _ = select_shuffle_bag_entry(
            entries,
            last_taxon_id=first.taxon_id,
            shuffle_bag_remaining=remaining,
            shuffle_bag_seen=seen,
            rng=random.Random(2),
        )

        self.assertEqual((first.taxon_id, second.taxon_id), (1, 1))


if __name__ == "__main__":
    unittest.main()
