from __future__ import annotations

import unittest
from pathlib import Path

from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.models import ReferencePhoto, SpeciesProfileData
from inky_bird_frame.prompts import plate_prompt


class PromptTests(unittest.TestCase):
    def test_plate_prompt_contains_species_facts_and_excludes_location(self) -> None:
        species = BirdSpecies(12942, "Eastern Bluebird", "Sialia sialis", 26, "iNaturalist")
        profile = SpeciesProfileData(
            taxon_id=12942,
            common_name="Eastern Bluebird",
            scientific_name="Sialia sialis",
            family="Turdidae",
            measurements={"length": "7 in", "wingspan": "12 in", "weight": "1 oz"},
            field_marks=["blue head", "blue back", "rufous breast", "white belly"],
            habitat="Open woodland",
            behavior="Drops from perches to forage",
            palette=["blue", "rufous", "white"],
            sources=[
                {"title": "A", "url": "https://example.test/a"},
                {"title": "B", "url": "https://example.test/b"},
            ],
        )
        reference = ReferencePhoto(
            1,
            2,
            "observer",
            "Photo by observer",
            "cc-by",
            "https://example.test/observation",
            "https://example.test/image.jpg",
            1600,
            1200,
            "image.jpg",
            "a" * 64,
        )

        prompt = plate_prompt(species, profile, [reference], Path("candidate.png"))

        self.assertIn("$imagegen", prompt)
        self.assertIn("blue head", prompt)
        self.assertIn("Photo by observer", prompt)
        self.assertNotIn("12345", prompt)
        self.assertNotIn("Exampleville", prompt)


if __name__ == "__main__":
    unittest.main()
