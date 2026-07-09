from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.catalog import candidate_directory, write_candidate_manifest
from inky_bird_frame.config import load_config
from inky_bird_frame.controller import generate_candidate, run_controller_cycle
from inky_bird_frame.errors import DataSourceError, GenerationError, InsufficientReferencesError
from inky_bird_frame.geo import ZipLocation
from inky_bird_frame.models import QualityReview, SpeciesProfileData
from inky_bird_frame.prompts import PROMPT_VERSION

PROFILE = SpeciesProfileData(
    taxon_id=9083,
    common_name="Northern Cardinal",
    scientific_name="Cardinalis cardinalis",
    family="Cardinalidae",
    measurements={"length": "8.3 in", "wingspan": "10 in", "weight": "1.5 oz"},
    field_marks=["crest", "red plumage", "black mask", "orange bill"],
    habitat="Woodland edges",
    behavior="Forages near cover",
    palette=["red", "black", "orange"],
    sources=[
        {"title": "Source one", "url": "https://example.test/one"},
        {"title": "Source two", "url": "https://example.test/two"},
    ],
)

CONFIG = """
[discovery]
zip_code = "12345"
radius_km = 8
species_limit = 12
window = "last-week"

[controller]
workspace_dir = "."
catalog_dir = "catalog"
state_dir = "state"
codex_path = "/usr/bin/false"
bind_host = "127.0.0.1"
port = 8793
references_per_species = 4
generations_per_cycle = 1
max_generation_attempts = 3

[display_node]
controller_url = "http://controller.test:8793"
state_dir = "display"
"""


class ControllerTests(unittest.TestCase):
    def test_transient_source_failure_remains_eligible(self) -> None:
        species = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        location = ZipLocation("12345", "Exampleville", "XY", 1.0, 2.0)
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            with (
                patch(
                    "inky_bird_frame.controller.discover_species",
                    return_value=(location, [species]),
                ),
                patch("inky_bird_frame.controller.generate_candidate") as generate,
            ):
                generate.side_effect = DataSourceError("iNaturalist timed out")
                result = run_controller_cycle(config)
            terminal_failures = list((config.controller.state_dir / "failed").glob("9083-*"))

        failures = result["failures"]
        self.assertEqual(terminal_failures, [])
        self.assertIsInstance(failures, list)
        if isinstance(failures, list):
            self.assertFalse(failures[0]["terminal"])

    def test_cycle_publishes_a_previously_reviewed_pending_candidate(self) -> None:
        species = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        location = ZipLocation("12345", "Exampleville", "XY", 1.0, 2.0)
        review = QualityReview(
            True,
            5,
            4,
            5,
            5,
            True,
            (),
            (
                {"title": "Cornell", "url": "https://example.test/cornell"},
                {"title": "ADW", "url": "https://example.test/adw"},
            ),
        )
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            candidate = candidate_directory(config.controller.state_dir, species)
            candidate.mkdir(parents=True)
            (candidate / "portrait.png").write_bytes(b"portrait")
            (candidate / "display.png").write_bytes(b"display")
            write_candidate_manifest(
                candidate,
                species,
                PROFILE,
                [],
                review,
                generator="test",
                prompt_version=PROMPT_VERSION,
                attempt=2,
                max_attempts=3,
            )
            with patch(
                "inky_bird_frame.controller.discover_species",
                return_value=(location, []),
            ):
                result = run_controller_cycle(config)

            published = (config.controller.catalog_dir / "species/9083-northern-cardinal").is_dir()

        self.assertTrue(published)
        self.assertEqual(result["approved_count"], 1)
        published_pending = result["published_pending"]
        self.assertIsInstance(published_pending, list)
        if isinstance(published_pending, list):
            self.assertEqual(len(published_pending), 1)

    def test_cycle_does_not_auto_publish_a_legacy_pending_candidate(self) -> None:
        species = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        location = ZipLocation("12345", "Exampleville", "XY", 1.0, 2.0)
        review = QualityReview(True, 5, 4, 5, 5, True, ())
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            candidate = candidate_directory(config.controller.state_dir, species)
            candidate.mkdir(parents=True)
            (candidate / "portrait.png").write_bytes(b"portrait")
            (candidate / "display.png").write_bytes(b"display")
            write_candidate_manifest(
                candidate,
                species,
                PROFILE,
                [],
                review,
                generator="legacy",
                prompt_version="field-journal-v1",
            )
            with patch(
                "inky_bird_frame.controller.discover_species",
                return_value=(location, []),
            ):
                result = run_controller_cycle(config)

            published = (config.controller.catalog_dir / "species/9083-northern-cardinal").is_dir()

        self.assertFalse(published)
        self.assertEqual(result["approved_count"], 0)
        self.assertEqual(result["published_pending"], [])

    def test_insufficient_references_become_terminal(self) -> None:
        species = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        location = ZipLocation("12345", "Exampleville", "XY", 1.0, 2.0)
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            with (
                patch(
                    "inky_bird_frame.controller.discover_species",
                    return_value=(location, [species]),
                ),
                patch("inky_bird_frame.controller.generate_candidate") as generate,
            ):
                generate.side_effect = InsufficientReferencesError("only 1 of 4 references")
                result = run_controller_cycle(config)
            terminal_failures = list((config.controller.state_dir / "failed").glob("9083-*"))

        self.assertEqual(len(terminal_failures), 1)
        failures = result["failures"]
        self.assertIsInstance(failures, list)
        if isinstance(failures, list):
            self.assertTrue(failures[0]["terminal"])

    def test_failed_review_is_corrected_and_passing_attempt_is_staged(self) -> None:
        species = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        failed_review = QualityReview(False, 3, 4, 5, 5, True, ("Crest is too short",))
        passed_review = QualityReview(
            True,
            5,
            4,
            5,
            5,
            True,
            (),
            (
                {"title": "Cornell", "url": "https://www.allaboutbirds.org/example"},
                {"title": "Audubon", "url": "https://www.audubon.org/example"},
            ),
        )

        class FakeRunner:
            corrections: list[tuple[str, ...]] = []
            reviews = iter((failed_review, passed_review))

            def __init__(self, _executable: Path, _workspace: Path) -> None:
                pass

            def create_profile(self, *_args: object) -> SpeciesProfileData:
                output_path = _args[-2]
                assert isinstance(output_path, Path)
                output_path.write_text(json.dumps(PROFILE))
                return PROFILE

            def generate_plate(self, *_args: object) -> Path:
                output_path = _args[-3]
                correction = _args[-1]
                assert isinstance(output_path, Path)
                assert isinstance(correction, tuple)
                self.corrections.append(correction)
                output_path.write_bytes(b"generated")
                return output_path

            def review_plate(self, *_args: object) -> QualityReview:
                return next(self.reviews)

        def prepare(_source: Path, portrait: Path, display: Path) -> None:
            portrait.write_bytes(b"portrait")
            display.write_bytes(b"display")

        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            with (
                patch("inky_bird_frame.controller.load_or_fetch_references", return_value=[]),
                patch("inky_bird_frame.controller.fetch_taxon_context"),
                patch("inky_bird_frame.controller.CodexRunner", FakeRunner),
                patch("inky_bird_frame.controller.prepare_generated_plate", side_effect=prepare),
            ):
                candidate = generate_candidate(config, species, config.controller.workspace_dir)
            manifest = json.loads((candidate / "manifest.json").read_text())

        self.assertEqual(FakeRunner.corrections, [(), ("Crest is too short",)])
        self.assertEqual(manifest["generation"]["attempt"], 2)
        self.assertEqual(manifest["status"], "pending")

    def test_expected_generation_failure_becomes_terminal(self) -> None:
        species = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        location = ZipLocation("12345", "Exampleville", "XY", 1.0, 2.0)
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            with (
                patch(
                    "inky_bird_frame.controller.discover_species",
                    return_value=(location, [species]),
                ),
                patch("inky_bird_frame.controller.generate_candidate") as generate,
            ):
                generate.side_effect = GenerationError("profile failed")
                result = run_controller_cycle(config)

            failures = list((config.controller.state_dir / "failed").glob("9083-*"))

        self.assertEqual(len(failures), 1)
        self.assertEqual(result["eligible_count"], 1)
        failure_results = result["failures"]
        self.assertIsInstance(failure_results, list)
        first_failure = failure_results[0] if isinstance(failure_results, list) else None
        self.assertIsInstance(first_failure, dict)
        if isinstance(first_failure, dict):
            self.assertEqual(first_failure["error"], "profile failed")


if __name__ == "__main__":
    unittest.main()
