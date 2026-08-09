from __future__ import annotations

import json
import stat
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from inky_bird_frame.birdnet_analyzer import (
    import_birdnet_analyzer_csv,
    read_birdnet_analyzer_history,
)
from inky_bird_frame.birds import BirdNetAnalyzerSpecies, ObservationWindow
from inky_bird_frame.errors import DataSourceError

HEADER = "Start (s),End (s),Scientific name,Common name,Confidence,File\n"


class BirdNetAnalyzerImportTests(unittest.TestCase):
    def test_import_is_private_idempotent_and_does_not_retain_file_metadata(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "results.csv"
            csv_path.write_text(
                HEADER
                + "0.0,3.0,Sialia sialis,Eastern Bluebird,0.91,/private/audio/one.wav\n"
                + "3.0,6.0,Cardinalis cardinalis,Northern Cardinal,0.88,second.wav\n"
            )
            first = import_birdnet_analyzer_csv(
                csv_path,
                root / "state",
                now=datetime(2026, 8, 9, 12, tzinfo=UTC),
            )
            second = import_birdnet_analyzer_csv(
                csv_path,
                root / "state",
                now=datetime(2026, 8, 9, 13, tzinfo=UTC),
            )
            state_path = root / "state/birdnet-analyzer-detections.json"
            serialized = state_path.read_text()
            mode = stat.S_IMODE(state_path.stat().st_mode)

        self.assertEqual(first.imported, 2)
        self.assertEqual(first.undated, 2)
        self.assertEqual(second.imported, 0)
        self.assertEqual(second.duplicates, 2)
        self.assertEqual(mode, 0o600)
        self.assertNotIn("/private/audio", serialized)
        self.assertNotIn("second.wav", serialized)
        self.assertNotIn("0.91", serialized)

    def test_reimport_reclassifies_a_segment_and_adds_an_explicit_date(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "results.csv"
            csv_path.write_text(HEADER + "0,3,Sialia sialis,Eastern Bluebird,0.91,one.wav\n")
            import_birdnet_analyzer_csv(csv_path, root / "state")
            csv_path.write_text(
                HEADER + "0,3,Cardinalis cardinalis,Northern Cardinal,0.95,one.wav\n"
            )

            result = import_birdnet_analyzer_csv(
                csv_path,
                root / "state",
                observed_on=date(2026, 8, 8),
            )
            history = read_birdnet_analyzer_history(
                root / "state",
                window=ObservationWindow.ALL_TIME,
                limit=50,
            )

        self.assertEqual(result.updated, 1)
        self.assertEqual(result.dated, 1)
        self.assertEqual(
            history.species,
            [BirdNetAnalyzerSpecies("Northern Cardinal", "Cardinalis cardinalis", 1)],
        )

    def test_conflicting_explicit_date_fails_without_replacing_state(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "results.csv"
            csv_path.write_text(HEADER + "0,3,Sialia sialis,Eastern Bluebird,0.91,one.wav\n")
            import_birdnet_analyzer_csv(
                csv_path,
                root / "state",
                observed_on=date(2026, 8, 8),
            )
            before = (root / "state/birdnet-analyzer-detections.json").read_bytes()

            with self.assertRaisesRegex(DataSourceError, "different date"):
                import_birdnet_analyzer_csv(
                    csv_path,
                    root / "state",
                    observed_on=date(2026, 8, 9),
                )
            after = (root / "state/birdnet-analyzer-detections.json").read_bytes()

        self.assertEqual(after, before)

    def test_missing_columns_and_invalid_rows_fail_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "results.csv"
            csv_path.write_text("Scientific name,Common name\nSialia sialis,Eastern Bluebird\n")
            with self.assertRaisesRegex(DataSourceError, "missing required columns"):
                import_birdnet_analyzer_csv(csv_path, root / "state")

            csv_path.write_text(HEADER + "3,0,Sialia sialis,Eastern Bluebird,1.5,one.wav\n")
            with self.assertRaisesRegex(DataSourceError, "CSV row 2"):
                import_birdnet_analyzer_csv(csv_path, root / "state")

        self.assertFalse((root / "state/birdnet-analyzer-detections.json").exists())

    def test_read_failure_does_not_expose_csv_path(self) -> None:
        private_path = Path("/private/recordings/results.csv")
        with TemporaryDirectory() as temporary, self.assertRaises(DataSourceError) as raised:
            import_birdnet_analyzer_csv(private_path, Path(temporary) / "state")

        self.assertNotIn(str(private_path), str(raised.exception))

    def test_dry_run_does_not_write_state(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "results.csv"
            csv_path.write_text(HEADER + "0,3,Sialia sialis,Eastern Bluebird,0.91,one.wav\n")
            result = import_birdnet_analyzer_csv(csv_path, root / "state", dry_run=True)

        self.assertTrue(result.dry_run)
        self.assertFalse((root / "state/birdnet-analyzer-detections.json").exists())


class BirdNetAnalyzerHistoryTests(unittest.TestCase):
    def test_windows_exclude_undated_and_out_of_range_detections(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "results.csv"
            csv_path.write_text(HEADER + "0,3,Sialia sialis,Eastern Bluebird,0.91,undated.wav\n")
            import_birdnet_analyzer_csv(csv_path, root / "state")
            csv_path.write_text(
                HEADER
                + "0,3,Sialia sialis,Eastern Bluebird,0.92,recent.wav\n"
                + "3,6,Sialia sialis,Eastern Bluebird,0.93,recent.wav\n"
            )
            import_birdnet_analyzer_csv(
                csv_path,
                root / "state",
                observed_on=date(2026, 8, 8),
            )
            all_time = read_birdnet_analyzer_history(
                root / "state",
                window=ObservationWindow.ALL_TIME,
                limit=50,
                today=date(2026, 8, 9),
            )
            last_day = read_birdnet_analyzer_history(
                root / "state",
                window=ObservationWindow.LAST_DAY,
                limit=50,
                today=date(2026, 8, 9),
            )

        self.assertEqual(all_time.species[0].detection_count, 3)
        self.assertEqual(last_day.species[0].detection_count, 2)
        self.assertEqual(last_day.excluded_undated, 1)

    def test_rejects_insecure_permissions(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "results.csv"
            csv_path.write_text(HEADER + "0,3,Sialia sialis,Eastern Bluebird,0.91,one.wav\n")
            import_birdnet_analyzer_csv(csv_path, root / "state")
            state_path = root / "state/birdnet-analyzer-detections.json"
            state_path.chmod(0o644)

            with self.assertRaisesRegex(DataSourceError, "mode 0600"):
                read_birdnet_analyzer_history(
                    root / "state",
                    window=ObservationWindow.ALL_TIME,
                    limit=50,
                )

    def test_rejects_tampered_state_without_leaking_detection_data(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            state_dir.mkdir()
            state_path = state_dir / "birdnet-analyzer-detections.json"
            state_path.write_text(json.dumps({"schema_version": 99}))
            state_path.chmod(0o600)

            with self.assertRaisesRegex(DataSourceError, "Unsupported BirdNET Analyzer state"):
                read_birdnet_analyzer_history(
                    state_dir,
                    window=ObservationWindow.ALL_TIME,
                    limit=50,
                )


if __name__ == "__main__":
    unittest.main()
