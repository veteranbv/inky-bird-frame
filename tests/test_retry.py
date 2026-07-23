from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from inky_bird_frame.errors import CatalogError
from inky_bird_frame.retry import RetryGuidance, RetryStore


class RetryStoreTests(unittest.TestCase):
    def test_exponential_backoff_is_durable_and_capped(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "retries.json"
            store = RetryStore(path)
            first = store.record_failure(
                42,
                RuntimeError("temporary"),
                now=now,
                initial_minutes=30,
                maximum_minutes=60,
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


if __name__ == "__main__":
    unittest.main()
