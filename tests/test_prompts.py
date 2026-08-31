from __future__ import annotations

import unittest
from pathlib import Path

from inky_bird_frame.birds import BirdSpecies, TaxonContext
from inky_bird_frame.models import ProfileConflict, ReferencePhoto, SpeciesProfileData
from inky_bird_frame.prompts import PROMPT_VERSION, plate_prompt, profile_prompt, review_prompt


class PromptTests(unittest.TestCase):
    def test_prompt_contract_version(self) -> None:
        self.assertEqual(PROMPT_VERSION, "field-journal-v6")

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

    def test_profile_and_review_prompts_include_bounded_conflict_history(self) -> None:
        species = BirdSpecies(1, "Test Bird", "Avis test", 1, "test")
        profile = SpeciesProfileData(
            taxon_id=1,
            common_name="Test Bird",
            scientific_name="Avis test",
            family="Testidae",
            measurements={"length": "10 cm", "wingspan": "20 cm", "weight": "30 g"},
            field_marks=["one", "two", "three", "four"],
            habitat="Woods",
            behavior="Perches",
            palette=["red", "green", "blue"],
            sources=[
                {"title": "One", "url": "https://birds.example/one"},
                {"title": "Two", "url": "https://field.example/two"},
            ],
        )
        conflict = ProfileConflict(
            **{
                "field": "measurements.length",
                "profile_value": "10 cm",
                "observed_value": "12 cm",
                "sources": [
                    {"title": "One", "url": "https://birds.example/length"},
                    {"title": "Two", "url": "https://field.example/length"},
                ],
            }
        )
        context = TaxonContext(
            taxon_id=1,
            common_name="Test Bird",
            scientific_name="Avis test",
            family="Testidae",
            summary="A test bird.",
            source_url="https://birds.example/taxon/1",
        )

        research = profile_prompt(
            species,
            context,
            [],
            ("birds.example", "field.example"),
            prior_profile=profile,
            profile_conflicts=(conflict,),
        )
        review = review_prompt(
            species,
            profile,
            [],
            ("birds.example", "field.example"),
            prior_corrections=("Repair the visible leg",),
            prior_profile_conflicts=(conflict,),
        )
        normalized_research = " ".join(research.split())
        normalized_review = " ".join(review.split())

        self.assertIn("one bounded re-adjudication", normalized_research)
        self.assertIn(
            "Do not assume either the prior profile or the review claim", normalized_research
        )
        self.assertIn('"field": "measurements.length"', research)
        self.assertIn("Earlier review history for convergence checking", normalized_review)
        self.assertIn("Repair the visible leg", normalized_review)
        self.assertIn("resolved_corrections only when", normalized_review)
        self.assertIn("must exactly match one earlier correction", normalized_review)
        self.assertIn("never repeat a stale profile_value", normalized_review)
        self.assertIn(
            "drop a conflict that the current direct sources no longer support", normalized_review
        )
        self.assertIn(
            "Do not report a profile conflict merely because another allowed source publishes a "
            "different supported set",
            normalized_review,
        )
        self.assertIn("required to use one compatible published measurement set", normalized_review)

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
        self.assertIn("roughly even, unlabeled interior ticks", normalized_prompt)
        self.assertIn("count and spacing do not encode measurement units", normalized_prompt)
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
        self.assertIn("no separate marker may disagree with it", normalized_review)
        self.assertIn(
            "exact count or minor spacing variation is not a factual error", normalized_review
        )
        self.assertIn("wrong values or units", normalized_review)
        self.assertIn("remains a material text error", normalized_review)
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
        self.assertIn("naturally occluded far-side wing or leg is acceptable", normalized_review)
        self.assertIn("never excuse a malformed", normalized_review)
        self.assertIn("at least two direct HTTPS sources", normalized_review)
        self.assertIn("Do not instruct the image generator", normalized_review)
        self.assertIn("Identity fields are not adjudicable", normalized_review)
        self.assertNotIn("exactly four evenly spaced", normalized_prompt)
        self.assertNotIn("exactly four evenly spaced", normalized_review)


if __name__ == "__main__":
    unittest.main()
