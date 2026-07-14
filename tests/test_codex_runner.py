from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inky_bird_frame.birds import BirdSpecies, TaxonContext
from inky_bird_frame.codex_runner import CodexRunner, _parse_review, parse_species_profile
from inky_bird_frame.errors import GenerationError
from inky_bird_frame.models import SpeciesProfileData

_CAPTURE_OUTPUT_ARGUMENT = """\
out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then
    out="$arg"
  fi
  prev="$arg"
done
"""


def _stub_executable(root: Path, body: str) -> Path:
    stub = root / "codex"
    stub.write_text(f"#!/bin/sh\n{body}\n")
    stub.chmod(0o755)
    return stub


def _species() -> BirdSpecies:
    return BirdSpecies(1, "Test Bird", "Avis test", 1, "test")


def _context() -> TaxonContext:
    return TaxonContext(
        taxon_id=1,
        common_name="Test Bird",
        scientific_name="Avis test",
        family="Testidae",
        summary="A test bird.",
        source_url="https://birds.example/taxa/1",
    )


def _profile() -> SpeciesProfileData:
    return SpeciesProfileData(
        taxon_id=1,
        common_name="Test Bird",
        scientific_name="Avis test",
        family="Testidae",
        measurements={"length": "1 in", "wingspan": "2 in", "weight": "3 oz"},
        field_marks=["one", "two", "three", "four"],
        habitat="Woods",
        behavior="Perches",
        palette=["red", "green", "blue"],
        sources=[
            {"title": "One", "url": "https://birds.example/one"},
            {"title": "Two", "url": "https://field.example/two"},
        ],
    )


class CodexRunnerSubprocessTests(unittest.TestCase):
    def test_nonzero_exit_raises_and_still_writes_log(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub = _stub_executable(
                root,
                'echo "stub stdout marker"\necho "stub stderr marker" >&2\nexit 3',
            )
            runner = CodexRunner(stub, root)
            log_path = root / "logs" / "plate.log"

            with self.assertRaisesRegex(GenerationError, "Codex exited with status 3; see "):
                runner.generate_plate(_species(), _profile(), [], [], root / "plate.png", log_path)

            self.assertTrue(log_path.is_file())
            log = log_path.read_text()
            self.assertIn("stub stdout marker", log)
            self.assertIn("stub stderr marker", log)

    def test_timeout_raises_generation_error(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub = _stub_executable(root, "sleep 5")
            runner = CodexRunner(stub, root, timeout_seconds=1)

            with self.assertRaisesRegex(GenerationError, "Codex timed out after 1 second"):
                runner.generate_plate(
                    _species(), _profile(), [], [], root / "plate.png", root / "plate.log"
                )

    def test_structured_output_missing_file_raises(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub = _stub_executable(root, "exit 0")
            runner = CodexRunner(stub, root)
            plate_path = root / "plate.png"
            plate_path.write_bytes(b"png")

            with self.assertRaisesRegex(
                GenerationError, "Codex did not write valid structured output"
            ):
                runner.review_plate(
                    _species(),
                    _profile(),
                    [],
                    plate_path,
                    [],
                    root / "review.json",
                    root / "review.log",
                    allowed_domains=("birds.example", "field.example"),
                )

    def test_structured_output_invalid_json_raises(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub = _stub_executable(root, f'{_CAPTURE_OUTPUT_ARGUMENT}printf "not json" > "$out"')
            runner = CodexRunner(stub, root)

            with self.assertRaisesRegex(
                GenerationError, "Codex did not write valid structured output"
            ):
                runner.create_profile(
                    _species(),
                    _context(),
                    [],
                    [],
                    root / "profile.json",
                    root / "profile.log",
                    allowed_domains=("birds.example", "field.example"),
                )

    def test_generate_plate_missing_image_raises(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub = _stub_executable(root, "exit 0")
            runner = CodexRunner(stub, root)

            with self.assertRaisesRegex(
                GenerationError, "Codex did not create the requested plate"
            ):
                runner.generate_plate(
                    _species(), _profile(), [], [], root / "plate.png", root / "plate.log"
                )

    def test_generate_plate_zero_byte_image_raises(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stub = _stub_executable(root, "exit 0")
            runner = CodexRunner(stub, root)
            plate_path = root / "plate.png"
            plate_path.touch()

            with self.assertRaisesRegex(
                GenerationError, "Codex did not create the requested plate"
            ):
                runner.generate_plate(
                    _species(), _profile(), [], [], plate_path, root / "plate.log"
                )

    def test_create_profile_rejects_mismatched_taxon_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload: dict[str, object] = dict(_profile())
            payload["taxon_id"] = 999
            payload_path = root / "payload.json"
            payload_path.write_text(json.dumps(payload))
            stub = _stub_executable(root, f'{_CAPTURE_OUTPUT_ARGUMENT}cp "{payload_path}" "$out"')
            runner = CodexRunner(stub, root)

            with self.assertRaisesRegex(
                GenerationError, "Codex profile identity does not match the discovered taxon"
            ):
                runner.create_profile(
                    _species(),
                    _context(),
                    [],
                    [],
                    root / "profile.json",
                    root / "profile.log",
                    allowed_domains=("birds.example", "field.example"),
                )


class CodexRunnerTests(unittest.TestCase):
    def test_review_uses_bounded_live_source_verification(self) -> None:
        species = BirdSpecies(1, "Test Bird", "Avis test", 1, "test")
        profile = SpeciesProfileData(
            taxon_id=1,
            common_name="Test Bird",
            scientific_name="Avis test",
            family="Testidae",
            measurements={"length": "1 in", "wingspan": "2 in", "weight": "3 oz"},
            field_marks=["one", "two", "three", "four"],
            habitat="Woods",
            behavior="Perches",
            palette=["red", "green", "blue"],
            sources=[
                {"title": "One", "url": "https://birds.example/one"},
                {"title": "Two", "url": "https://field.example/two"},
            ],
        )
        raw_review = {
            "passed": True,
            "species_accuracy": 5,
            "anatomy_accuracy": 5,
            "text_accuracy": 5,
            "composition_quality": 5,
            "location_free": True,
            "findings": [],
            "verification_sources": profile["sources"],
        }
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "codex"
            executable.touch()
            runner = CodexRunner(executable, root)
            with patch.object(runner, "_structured", return_value=raw_review) as structured:
                runner.review_plate(
                    species,
                    profile,
                    [],
                    Path("plate.png"),
                    [],
                    Path("review.json"),
                    Path("review.log"),
                    allowed_domains=("birds.example", "field.example"),
                )

        self.assertTrue(structured.call_args.kwargs["search"])
        prompt = structured.call_args.args[0]
        self.assertIn("birds.example, field.example", prompt)

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
