from __future__ import annotations

import unittest
from pathlib import Path

from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.models import ReferencePhoto, SpeciesProfileData
from inky_bird_frame.prompts import PROMPT_VERSION, plate_prompt, review_prompt


class PromptTests(unittest.TestCase):
    def test_prompt_contract_version(self) -> None:
        self.assertEqual(PROMPT_VERSION, "field-journal-v4")

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

    def test_plate_prompt_includes_review_corrections(self) -> None:
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

        prompt = plate_prompt(
            species,
            profile,
            [],
            Path("candidate.png"),
            ("Correct the wing bars",),
        )

        self.assertIn("Correct the wing bars", prompt)
        self.assertIn("Create a new image", prompt)

    def test_plate_and_review_prompts_enforce_schematic_ruler(self) -> None:
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

        prompt = plate_prompt(
            species,
            profile,
            [],
            Path("candidate.png"),
            ("Shorten the tail",),
            invariant_findings=("Keep the accepted body proportions",),
            has_correction_source=True,
        )
        normalized_prompt = " ".join(prompt.split())

        self.assertIn("Use case: precise-object-edit", prompt)
        self.assertIn("Image 1 is the previous plate and the edit target", prompt)
        self.assertIn("Current actionable corrections", prompt)
        self.assertIn("Non-regression constraints", prompt)
        self.assertIn("Keep the accepted body proportions", prompt)
        self.assertIn("not a request to redraw a feature", normalized_prompt)
        self.assertIn("Do not shrink the bird", prompt)
        self.assertIn("BODY LENGTH: 7 in", prompt)
        self.assertIn("SCHEMATIC — NOT TO SCALE", prompt)
        self.assertIn("minimum at the bottom and maximum at the top", prompt)
        self.assertIn("exactly four evenly spaced unlabeled interior ticks", normalized_prompt)
        self.assertIn("ticks are subdivisions, not one-unit increments", normalized_prompt)
        self.assertIn("Do not draw a zero-based or wider full-range axis", prompt)
        self.assertIn("do not add a separate range bracket", normalized_prompt)
        self.assertIn("Match both the direction and degree", normalized_prompt)
        self.assertIn("the lower mandible is the jaw below the gape line", normalized_prompt)
        self.assertIn("its tip must be farther from the shared bill base", normalized_prompt)
        self.assertIn("Preserve the near-full length of the shorter structure", prompt)
        self.assertIn("do not substitute a wider gape, lengthen the wrong jaw", normalized_prompt)
        self.assertIn("the lower tip must also reach farther forward", normalized_prompt)
        self.assertIn("do not rely on a steeper gape angle", normalized_prompt)
        self.assertIn("same plausible proportional relationship", normalized_prompt)
        self.assertIn("keep the iris dark enough to blend", normalized_prompt)
        self.assertIn("render the supplied pupil shape precisely", normalized_prompt)
        self.assertIn("When the supplied shape is vertical", normalized_prompt)
        self.assertIn(
            "thin black vertical slit rather than a round or oval pupil", normalized_prompt
        )
        self.assertIn("Never invent a vertical slit", normalized_prompt)
        self.assertIn("compare every visible text line character-for-character", normalized_prompt)
        self.assertIn("Undo any incidental spelling, number, punctuation", normalized_prompt)
        self.assertNotIn("Do not copy or lightly edit", prompt)

        review = review_prompt(species, profile, [], ("example.test", "example.org"))
        normalized_review = " ".join(review.split())

        self.assertIn("values must increase from bottom to top", review)
        self.assertIn("no separate marker may disagree with it", review)
        self.assertIn(
            "range ruler must contain exactly four evenly spaced unlabeled interior ticks",
            normalized_review,
        )
        self.assertIn("five proportional segments, not one-unit increments", normalized_review)
        self.assertIn("Treat any mismatch as a material text error", normalized_review)
        self.assertIn("Read every visible text line in full", normalized_review)
        self.assertIn("duplicated, omitted, substituted, or nonsensical words", normalized_review)
        self.assertIn(
            "trace each one independently from its shared bill base to its tip", normalized_review
        )
        self.assertIn("ambiguous, co-terminal, or reversed rendering", normalized_review)
        self.assertIn("do not confuse the open-gape angle", normalized_review)
        self.assertIn("Compare both the direction and degree", normalized_review)
        self.assertIn("truncated-looking upper mandible", normalized_review)
        self.assertIn("inconsistent proportions between heads", normalized_review)
        self.assertIn("Do not infer a seasonal-plumage correction", normalized_review)
        self.assertIn(
            "explicit agreement from at least two direct allowed source pages", normalized_review
        )
        self.assertIn("exactly one complete primary specimen", normalized_review)
        self.assertIn(
            "detached wing and bill/head studies are intentional supplementary anatomy",
            normalized_review,
        )
        self.assertIn("validate each study independently", normalized_review)


if __name__ == "__main__":
    unittest.main()
