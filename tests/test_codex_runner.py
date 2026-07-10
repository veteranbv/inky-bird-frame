from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from inky_bird_frame.codex_runner import CodexRunner, _parse_review, parse_species_profile
from inky_bird_frame.errors import GenerationError


class CodexRunnerTests(unittest.TestCase):
    def test_review_requires_two_verification_sources(self) -> None:
        review = _parse_review(
            {
                "passed": True,
                "species_accuracy": 5,
                "anatomy_accuracy": 5,
                "text_accuracy": 5,
                "composition_quality": 5,
                "location_free": True,
                "findings": [],
                "verification_sources": [
                    {"title": "Cornell", "url": "https://www.allaboutbirds.org/example"}
                ],
            }
        )

        self.assertFalse(review.passed)

    def test_review_passes_with_scores_and_two_verification_sources(self) -> None:
        review = _parse_review(
            {
                "passed": True,
                "species_accuracy": 5,
                "anatomy_accuracy": 4,
                "text_accuracy": 5,
                "composition_quality": 4,
                "location_free": True,
                "findings": [],
                "verification_sources": [
                    {"title": "Cornell", "url": "https://www.allaboutbirds.org/example"},
                    {"title": "Audubon", "url": "https://www.audubon.org/example"},
                ],
            }
        )

        self.assertTrue(review.passed)
        self.assertEqual(len(review.verification_sources), 2)

    def test_review_requires_two_distinct_verification_urls(self) -> None:
        review = _parse_review(
            {
                "passed": True,
                "species_accuracy": 5,
                "anatomy_accuracy": 5,
                "text_accuracy": 5,
                "composition_quality": 5,
                "location_free": True,
                "findings": [],
                "verification_sources": [
                    {"title": "Cornell identification", "url": "https://example.test/bird"},
                    {"title": "Cornell life history", "url": "https://example.test/bird"},
                ],
            }
        )

        self.assertFalse(review.passed)
        self.assertEqual(len(review.verification_sources), 1)

    def test_noninteractive_command_allows_deployment_workspace(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "codex"
            executable.touch()
            runner = CodexRunner(executable, root)

            command = runner._base_command(writable=False)

        self.assertIn("--skip-git-repo-check", command)
        self.assertIn("read-only", command)

    def test_profile_rejects_sources_outside_allowlist(self) -> None:
        profile = {
            "taxon_id": 1,
            "common_name": "Test Bird",
            "scientific_name": "Avis test",
            "family": "Testidae",
            "measurements": {"length": "1 in", "wingspan": "2 in", "weight": "3 oz"},
            "field_marks": ["one", "two", "three", "four"],
            "habitat": "Woods",
            "behavior": "Perches",
            "palette": ["red", "green", "blue"],
            "sources": [
                {"title": "Allowed", "url": "https://birds.example/one"},
                {"title": "Not allowed", "url": "https://search.example/two"},
            ],
        }

        with self.assertRaisesRegex(GenerationError, "allowlist"):
            parse_species_profile(profile, ("birds.example",))

    def test_review_requires_independent_source_domains(self) -> None:
        review = _parse_review(
            {
                "passed": True,
                "species_accuracy": 5,
                "anatomy_accuracy": 5,
                "text_accuracy": 5,
                "composition_quality": 5,
                "location_free": True,
                "findings": [],
                "verification_sources": [
                    {"title": "One", "url": "https://birds.example/one"},
                    {"title": "Two", "url": "https://birds.example/two"},
                ],
            }
        )

        self.assertFalse(review.passed)


if __name__ == "__main__":
    unittest.main()
