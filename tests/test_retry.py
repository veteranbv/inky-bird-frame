from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.errors import CatalogError
from inky_bird_frame.models import ProfileConflict
from inky_bird_frame.retry import RetryGuidance, RetryStore, parse_retry_profile_conflicts


class RetryStoreTests(unittest.TestCase):
    def test_exponential_backoff_is_durable_and_capped(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "retries.json"
            store = RetryStore(path)
            species = BirdSpecies(42, "Example Bird", "Avis exemplum", 1, "eBird")
            first = store.record_failure(
                42,
                RuntimeError("temporary"),
                now=now,
                initial_minutes=30,
                maximum_minutes=60,
                species=species,
            )
            second = store.record_failure(
                42,
                RuntimeError("temporary"),
                now=now + timedelta(minutes=30),
                initial_minutes=30,
                maximum_minutes=60,
            )
            reloaded = RetryStore(path).get(42)

        self.assertEqual(first.next_attempt_at, now + timedelta(minutes=30))
        self.assertEqual(second.next_attempt_at, now + timedelta(minutes=90))
        self.assertEqual(reloaded, second)
        self.assertIsNotNone(reloaded)
        if reloaded is not None:
            self.assertEqual(reloaded.common_name, "Example Bird")
            self.assertEqual(reloaded.scientific_name, "Avis exemplum")

    def test_fixed_delay_and_clear(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            store = RetryStore(Path(temporary) / "retries.json")
            record = store.record_failure(
                42,
                RuntimeError("references"),
                now=now,
                initial_minutes=30,
                maximum_minutes=60,
                fixed_minutes=10080,
            )
            store.clear(42)

        self.assertEqual(record.next_attempt_at, now + timedelta(days=7))
        self.assertIsNone(store.get(42))

    def test_explicit_retry_time_is_durable_and_exclusive(self) -> None:
        now = datetime(2026, 7, 10, 23, 30, tzinfo=UTC)
        retry_at = datetime(2026, 7, 11, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "retries.json"
            store = RetryStore(path)
            record = store.record_failure(
                42,
                RuntimeError("research limit"),
                now=now,
                initial_minutes=30,
                maximum_minutes=60,
                retry_at=retry_at,
            )

            with self.assertRaisesRegex(ValueError, "mutually exclusive"):
                store.record_failure(
                    43,
                    RuntimeError("invalid"),
                    now=now,
                    initial_minutes=30,
                    maximum_minutes=60,
                    fixed_minutes=60,
                    retry_at=retry_at,
                )

            reloaded = RetryStore(path).get(42)

        self.assertEqual(record.next_attempt_at, retry_at)
        self.assertEqual(reloaded, record)

    def test_identity_can_be_added_without_changing_backoff(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "retries.json"
            store = RetryStore(path)
            original = store.record_failure(
                42,
                RuntimeError("temporary"),
                now=now,
                initial_minutes=30,
                maximum_minutes=60,
            )

            updated = store.set_identity(42, "Example Bird", "Avis exemplum")
            reloaded = RetryStore(path).get(42)

        self.assertEqual(updated.attempts, original.attempts)
        self.assertEqual(updated.next_attempt_at, original.next_attempt_at)
        self.assertEqual(updated.common_name, "Example Bird")
        self.assertEqual(updated.scientific_name, "Avis exemplum")
        self.assertEqual(reloaded, updated)

    def test_quality_guidance_is_durable_and_independent_from_backoff(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "retries.json"
            store = RetryStore(path)
            store.record_failure(
                42,
                RuntimeError("temporary"),
                now=now,
                initial_minutes=30,
                maximum_minutes=60,
            )
            guidance = store.set_quality_guidance(42, ("Correct the scale",))
            store.clear(42)
            reloaded = RetryStore(path)

            self.assertEqual(
                guidance,
                RetryGuidance(taxon_id=42, findings=("Correct the scale",)),
            )
            self.assertIsNone(reloaded.get(42))
            self.assertEqual(reloaded.quality_guidance(42), guidance)

            reloaded.clear_quality_guidance(42)

            self.assertIsNone(RetryStore(path).quality_guidance(42))

    def test_invalid_quality_guidance_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "retries.json"
            path.write_text(
                '{"schema_version":1,"records":[],"quality_guidance":'
                '[{"taxon_id":42,"findings":[]}]}'
            )

            with self.assertRaisesRegex(CatalogError, "Invalid retry quality guidance"):
                RetryStore(path)

    def test_correction_source_is_durable(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "retries.json"
            guidance = RetryStore(path).set_quality_guidance(
                42,
                ("Correct the scale",),
                source_plate="archive/42-bird/attempt-03/portrait.png",
            )

            reloaded = RetryStore(path).quality_guidance(42)

        self.assertEqual(guidance, reloaded)
        self.assertEqual(guidance.source_plate, "archive/42-bird/attempt-03/portrait.png")

    def test_invariant_findings_are_durable_and_merged_into_guidance(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "retries.json"
            guidance = RetryStore(path).set_quality_guidance(
                42,
                ("Correct the wing.",),
                invariant_findings=("Render clearly visible natural eyes.",),
            )

            reloaded = RetryStore(path).quality_guidance(42)

        self.assertEqual(
            guidance.findings,
            ("Render clearly visible natural eyes.", "Correct the wing."),
        )
        self.assertEqual(
            guidance.invariant_findings,
            ("Render clearly visible natural eyes.",),
        )
        self.assertEqual(reloaded, guidance)

    def test_profile_conflicts_are_durable_without_image_findings(self) -> None:
        conflict = ProfileConflict(
            field="measurements.length",
            profile_value="10-20 cm",
            observed_value="12-15 cm",
            sources=[
                {"title": "Birds", "url": "https://birds.example/length"},
                {"title": "Field", "url": "https://field.example/length"},
            ],
        )
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "retries.json"
            guidance = RetryStore(path).set_quality_guidance(
                42,
                (),
                profile_conflicts=(conflict,),
            )

            reloaded = RetryStore(path).quality_guidance(42)

        self.assertEqual(guidance.findings, ())
        self.assertEqual(guidance.profile_conflicts, (conflict,))
        self.assertEqual(reloaded, guidance)

    def test_retry_profile_conflicts_must_use_allowed_independent_sources(self) -> None:
        conflict = {
            "field": "measurements.length",
            "profile_value": "10-20 cm",
            "observed_value": "12-15 cm",
            "sources": [
                {"title": "One", "url": "https://outside.example/one"},
                {"title": "Two", "url": "https://other.example/two"},
            ],
        }

        with self.assertRaisesRegex(CatalogError, "Invalid retry quality guidance"):
            parse_retry_profile_conflicts(
                [conflict],
                Path("retry.json"),
                allowed_domains=("birds.example", "field.example"),
            )

        conflict["sources"] = [
            {"title": "One", "url": "https://one.birds.example/one"},
            {"title": "Two", "url": "https://two.birds.example/two"},
        ]
        with self.assertRaisesRegex(CatalogError, "Invalid retry quality guidance"):
            parse_retry_profile_conflicts(
                [conflict],
                Path("retry.json"),
                allowed_domains=("birds.example", "field.example"),
            )

    def test_retry_profile_conflicts_reject_malformed_state(self) -> None:
        valid = {
            "field": "measurements.length",
            "profile_value": "10-20 cm",
            "observed_value": "12-15 cm",
            "sources": [
                {"title": "Birds", "url": "https://birds.example/length"},
                {"title": "Field", "url": "https://field.example/length"},
            ],
        }
        cases: tuple[object, ...] = (
            [valid, valid],
            [{**valid, "observed_value": "10-20 cm"}],
            [
                {
                    **valid,
                    "sources": [
                        {"title": "Birds", "url": "http://birds.example/length"},
                        {"title": "Field", "url": "https://field.example/length"},
                    ],
                }
            ],
            [{**valid, "field": "scientific_name"}],
            [
                {
                    **valid,
                    "sources": [
                        {"url": "https://birds.example/length"},
                        {"title": "Field", "url": "https://field.example/length"},
                    ],
                }
            ],
        )

        for raw in cases:
            with (
                self.subTest(raw=raw),
                self.assertRaisesRegex(
                    CatalogError,
                    "Invalid retry quality guidance",
                ),
            ):
                parse_retry_profile_conflicts(raw, Path("retry.json"))

    def test_correction_source_must_remain_in_archive(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "retries.json"

            with self.assertRaisesRegex(CatalogError, "Invalid retry quality guidance"):
                RetryStore(path).set_quality_guidance(
                    42,
                    ("Correct the scale",),
                    source_plate="../private/portrait.png",
                )


if __name__ == "__main__":
    unittest.main()
