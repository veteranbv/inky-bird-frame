from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inky_bird_frame.birds import BirdSpecies, TaxonContext
from inky_bird_frame.codex_runner import (
    CodexRunner,
    parse_species_profile,
)
from inky_bird_frame.codex_runner import (
    _parse_review as _parse_review_impl,
)
from inky_bird_frame.errors import GenerationError
from inky_bird_frame.models import QualityReview, SpeciesProfileData

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


def _parse_review(
    raw: object,
    allowed_domains: tuple[str, ...] | None = None,
    *,
    prior_corrections: tuple[str, ...] = (),
) -> QualityReview:
    return _parse_review_impl(
        raw,
        _profile(),
        allowed_domains,
        prior_corrections=prior_corrections,
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

    def test_generate_plate_attaches_correction_source_before_references(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "codex"
            executable.touch()
            source = root / "source.png"
            reference = root / "reference.png"
            source.write_bytes(b"source")
            reference.write_bytes(b"reference")
            output = root / "plate.png"
            runner = CodexRunner(executable, root)

            def run(_command: list[str], _prompt: str, _log_path: Path) -> None:
                output.write_bytes(b"generated")

            with patch.object(runner, "_run", side_effect=run) as execute:
                runner.generate_plate(
                    _species(),
                    _profile(),
                    [],
                    [reference],
                    output,
                    root / "plate.log",
                    ("Shorten the tail",),
                    correction_source_path=source,
                    invariant_findings=("Keep the correct wing pattern",),
                )

        command = execute.call_args.args[0]
        images = [command[index + 1] for index, value in enumerate(command) if value == "--image"]
        self.assertEqual(images, [str(source.resolve()), str(reference.resolve())])
        self.assertIn(
            "Image 1 is the previous plate and the edit target", execute.call_args.args[1]
        )
        self.assertIn("Shorten the tail", execute.call_args.args[1])
        self.assertIn("Keep the correct wing pattern", execute.call_args.args[1])
        self.assertIn("Non-regression constraints", execute.call_args.args[1])

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
    def test_configured_model_is_forwarded_to_exec(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "codex"
            executable.touch()
            command = CodexRunner(executable, root, model="tested-model")._base_command(
                writable=False
            )

        self.assertEqual(command[command.index("--model") + 1], "tested-model")

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
            "correction_findings": [],
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
        with self.assertRaisesRegex(GenerationError, "at least two verification sources"):
            _parse_review(
                {
                    "passed": True,
                    "species_accuracy": 5,
                    "anatomy_accuracy": 5,
                    "text_accuracy": 5,
                    "composition_quality": 5,
                    "location_free": True,
                    "findings": [],
                    "correction_findings": [],
                    "verification_sources": [
                        {"title": "Cornell", "url": "https://www.allaboutbirds.org/example"}
                    ],
                }
            )

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
                "correction_findings": [],
                "verification_sources": [
                    {"title": "Cornell", "url": "https://www.allaboutbirds.org/example"},
                    {"title": "Audubon", "url": "https://www.audubon.org/example"},
                ],
            }
        )

        self.assertTrue(review.passed)
        self.assertEqual(len(review.verification_sources), 2)

    def test_review_requires_two_distinct_verification_urls(self) -> None:
        with self.assertRaisesRegex(GenerationError, "at least two verification sources"):
            _parse_review(
                {
                    "passed": True,
                    "species_accuracy": 5,
                    "anatomy_accuracy": 5,
                    "text_accuracy": 5,
                    "composition_quality": 5,
                    "location_free": True,
                    "findings": [],
                    "correction_findings": [],
                    "verification_sources": [
                        {
                            "title": "Cornell identification",
                            "url": "https://example.test/bird",
                        },
                        {
                            "title": "Cornell life history",
                            "url": "https://example.test/bird",
                        },
                    ],
                }
            )

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
        with self.assertRaisesRegex(GenerationError, "independent verification source domains"):
            _parse_review(
                {
                    "passed": True,
                    "species_accuracy": 5,
                    "anatomy_accuracy": 5,
                    "text_accuracy": 5,
                    "composition_quality": 5,
                    "location_free": True,
                    "findings": [],
                    "correction_findings": [],
                    "verification_sources": [
                        {"title": "One", "url": "https://birds.example/one"},
                        {"title": "Two", "url": "https://birds.example/two"},
                    ],
                }
            )

    def test_failed_review_requires_actionable_corrections(self) -> None:
        with self.assertRaisesRegex(GenerationError, "must include correction_findings"):
            _parse_review(
                {
                    "passed": False,
                    "species_accuracy": 3,
                    "anatomy_accuracy": 4,
                    "text_accuracy": 5,
                    "composition_quality": 4,
                    "location_free": True,
                    "findings": ["The bill is too long"],
                    "correction_findings": [],
                    "verification_sources": [
                        {"title": "Cornell", "url": "https://www.allaboutbirds.org/example"},
                        {"title": "Audubon", "url": "https://www.audubon.org/example"},
                    ],
                }
            )

    def test_passing_review_rejects_actionable_corrections(self) -> None:
        with self.assertRaisesRegex(GenerationError, "must not include correction_findings"):
            _parse_review(
                {
                    "passed": True,
                    "species_accuracy": 5,
                    "anatomy_accuracy": 4,
                    "text_accuracy": 5,
                    "composition_quality": 4,
                    "location_free": True,
                    "findings": ["No material issue"],
                    "correction_findings": ["Change the bill"],
                    "verification_sources": [
                        {"title": "Cornell", "url": "https://www.allaboutbirds.org/example"},
                        {"title": "Audubon", "url": "https://www.audubon.org/example"},
                    ],
                }
            )

    def test_review_parses_source_backed_profile_conflict(self) -> None:
        review = _parse_review(
            {
                "passed": False,
                "species_accuracy": 5,
                "anatomy_accuracy": 5,
                "text_accuracy": 3,
                "composition_quality": 5,
                "location_free": True,
                "findings": ["The cached length conflicts with both sources"],
                "correction_findings": [],
                "profile_conflicts": [
                    {
                        "field": "measurements.length",
                        "profile_value": "1 in",
                        "observed_value": "12-15 cm",
                        "sources": [
                            {"title": "Birds", "url": "https://birds.example/length"},
                            {"title": "Field", "url": "https://field.example/length"},
                        ],
                    }
                ],
                "verification_sources": [
                    {"title": "Birds", "url": "https://birds.example/length"},
                    {"title": "Field", "url": "https://field.example/length"},
                ],
            },
            ("birds.example", "field.example"),
        )

        self.assertFalse(review.passed)
        self.assertEqual(review.profile_conflicts[0]["field"], "measurements.length")

    def test_profile_conflict_rejects_identity_field(self) -> None:
        with self.assertRaisesRegex(GenerationError, "field is not supported"):
            _parse_review(
                {
                    "passed": False,
                    "species_accuracy": 3,
                    "anatomy_accuracy": 5,
                    "text_accuracy": 5,
                    "composition_quality": 5,
                    "location_free": True,
                    "findings": ["Identity conflict"],
                    "correction_findings": [],
                    "profile_conflicts": [
                        {
                            "field": "scientific_name",
                            "profile_value": "Avis one",
                            "observed_value": "Avis two",
                            "sources": [
                                {"title": "Birds", "url": "https://birds.example/name"},
                                {"title": "Field", "url": "https://field.example/name"},
                            ],
                        }
                    ],
                    "verification_sources": [
                        {"title": "Birds", "url": "https://birds.example/name"},
                        {"title": "Field", "url": "https://field.example/name"},
                    ],
                },
                ("birds.example", "field.example"),
            )

    def test_profile_conflict_requires_independent_sources(self) -> None:
        with self.assertRaisesRegex(GenerationError, "two independent verification sources"):
            _parse_review(
                {
                    "passed": False,
                    "species_accuracy": 5,
                    "anatomy_accuracy": 5,
                    "text_accuracy": 3,
                    "composition_quality": 5,
                    "location_free": True,
                    "findings": ["Length conflict"],
                    "correction_findings": [],
                    "profile_conflicts": [
                        {
                            "field": "measurements.length",
                            "profile_value": "1 in",
                            "observed_value": "12-15 cm",
                            "sources": [
                                {"title": "One", "url": "https://birds.example/one"},
                                {"title": "Two", "url": "https://birds.example/two"},
                            ],
                        }
                    ],
                    "verification_sources": [
                        {"title": "Birds", "url": "https://birds.example/length"},
                        {"title": "Field", "url": "https://field.example/length"},
                    ],
                },
                ("birds.example", "field.example"),
            )

    def test_profile_conflict_must_quote_the_current_profile(self) -> None:
        with self.assertRaisesRegex(GenerationError, "does not match the current profile"):
            _parse_review(
                {
                    "passed": False,
                    "species_accuracy": 5,
                    "anatomy_accuracy": 5,
                    "text_accuracy": 3,
                    "composition_quality": 5,
                    "location_free": True,
                    "findings": ["A stale length conflict"],
                    "correction_findings": [],
                    "profile_conflicts": [
                        {
                            "field": "measurements.length",
                            "profile_value": "10-20 cm",
                            "observed_value": "12-15 cm",
                            "sources": [
                                {"title": "Birds", "url": "https://birds.example/length"},
                                {"title": "Field", "url": "https://field.example/length"},
                            ],
                        }
                    ],
                    "verification_sources": [
                        {"title": "Birds", "url": "https://birds.example/length"},
                        {"title": "Field", "url": "https://field.example/length"},
                    ],
                },
                ("birds.example", "field.example"),
            )

    def test_field_mark_conflict_accepts_equivalent_json_and_canonicalizes_it(self) -> None:
        review = _parse_review(
            {
                "passed": False,
                "species_accuracy": 3,
                "anatomy_accuracy": 5,
                "text_accuracy": 5,
                "composition_quality": 5,
                "location_free": True,
                "findings": ["The proposed field marks conflict with direct sources"],
                "correction_findings": [],
                "profile_conflicts": [
                    {
                        "field": "field_marks",
                        "profile_value": json.dumps(_profile()["field_marks"]),
                        "observed_value": '["one","two","three","different"]',
                        "sources": [
                            {"title": "Birds", "url": "https://birds.example/marks"},
                            {"title": "Field", "url": "https://field.example/marks"},
                        ],
                    }
                ],
                "verification_sources": [
                    {"title": "Birds", "url": "https://birds.example/marks"},
                    {"title": "Field", "url": "https://field.example/marks"},
                ],
            },
            ("birds.example", "field.example"),
        )

        self.assertEqual(
            review.profile_conflicts[0]["profile_value"],
            '["one","two","three","four"]',
        )

    def test_review_accepts_only_explicit_resolutions_from_history(self) -> None:
        review = _parse_review(
            {
                "passed": True,
                "species_accuracy": 5,
                "anatomy_accuracy": 5,
                "text_accuracy": 5,
                "composition_quality": 5,
                "location_free": True,
                "findings": ["The bill correction is now satisfied"],
                "correction_findings": [],
                "resolved_corrections": ["Shorten the bill"],
                "verification_sources": [
                    {"title": "Birds", "url": "https://birds.example/bill"},
                    {"title": "Field", "url": "https://field.example/bill"},
                ],
            },
            ("birds.example", "field.example"),
            prior_corrections=("Shorten the bill",),
        )

        self.assertEqual(review.resolved_corrections, ("Shorten the bill",))

        with self.assertRaisesRegex(GenerationError, "was not present in review history"):
            _parse_review(
                {
                    "passed": True,
                    "species_accuracy": 5,
                    "anatomy_accuracy": 5,
                    "text_accuracy": 5,
                    "composition_quality": 5,
                    "location_free": True,
                    "findings": [],
                    "correction_findings": [],
                    "resolved_corrections": ["Shorten the bill"],
                    "verification_sources": [
                        {"title": "Birds", "url": "https://birds.example/bill"},
                        {"title": "Field", "url": "https://field.example/bill"},
                    ],
                },
                ("birds.example", "field.example"),
            )

    def test_review_rejects_a_correction_as_resolved_and_actionable(self) -> None:
        with self.assertRaisesRegex(GenerationError, "both resolved and actionable"):
            _parse_review(
                {
                    "passed": False,
                    "species_accuracy": 3,
                    "anatomy_accuracy": 5,
                    "text_accuracy": 5,
                    "composition_quality": 5,
                    "location_free": True,
                    "findings": ["The bill remains too long"],
                    "correction_findings": ["Shorten the bill"],
                    "resolved_corrections": ["Shorten the bill"],
                    "verification_sources": [
                        {"title": "Birds", "url": "https://birds.example/bill"},
                        {"title": "Field", "url": "https://field.example/bill"},
                    ],
                },
                ("birds.example", "field.example"),
                prior_corrections=("Shorten the bill",),
            )

    def test_passing_review_shape_omits_empty_profile_conflicts(self) -> None:
        review = QualityReview(True, 5, 5, 5, 5, True, ())

        self.assertNotIn("profile_conflicts", review.as_dict())


if __name__ == "__main__":
    unittest.main()
