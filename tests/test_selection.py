from __future__ import annotations

import unittest

from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.selection import select_rotating_species


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


if __name__ == "__main__":
    unittest.main()
