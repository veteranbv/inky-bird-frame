from __future__ import annotations

import csv
import json
import stat
import unittest
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inky_bird_frame.birds import DateRange, EbirdArchiveSpecies, ObservationWindow
from inky_bird_frame.ebird_archive import (
    CSV_FILENAME,
    REQUIRED_COLUMNS,
    import_ebird_archive,
    read_ebird_archive_history,
)
from inky_bird_frame.errors import DataSourceError


def _row(
    submission_id: str,
    common_name: str,
    scientific_name: str,
    observed_on: str,
    *,
    location: str = "Private Place",
) -> dict[str, str]:
    row = dict.fromkeys(REQUIRED_COLUMNS, "")
    row.update(
        {
            "Submission ID": submission_id,
            "Common Name": common_name,
            "Scientific Name": scientific_name,
            "Taxonomic Order": "1",
            "Count": "1",
            "Location ID": "L-private",
            "Location": location,
            "Latitude": "1.25",
            "Longitude": "-2.5",
            "Date": observed_on,
            "Protocol": "Traveling",
            "All Obs Reported": "1",
        }
    )
    return row


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REQUIRED_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _write_zip(path: Path, rows: list[dict[str, str]]) -> None:
    csv_path = path.with_suffix(".csv")
    _write_csv(csv_path, rows)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(csv_path, CSV_FILENAME)


class EbirdArchiveImportTests(unittest.TestCase):
    def test_zip_import_is_private_atomic_and_idempotent(self) -> None:
        rows = [
            _row("S1", "Eastern Bluebird", "Sialia sialis", "2026-08-08"),
            _row("S1", "Northern Cardinal", "Cardinalis cardinalis", "2026-08-08"),
            _row("S2", "Eastern Bluebird", "Sialia sialis", "2026-08-09"),
        ]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "download.zip"
            _write_zip(archive_path, rows)
            first = import_ebird_archive(
                archive_path,
                root / "state",
                now=datetime(2026, 8, 9, 12, tzinfo=UTC),
            )
            second = import_ebird_archive(
                archive_path,
                root / "state",
                now=datetime(2026, 8, 9, 13, tzinfo=UTC),
            )
            state_path = root / "state/ebird-archive-observations.json"
            serialized = state_path.read_text()
            mode = stat.S_IMODE(state_path.stat().st_mode)

        self.assertEqual(first.rows, 3)
        self.assertEqual(first.checklists, 2)
        self.assertEqual(first.species, 2)
        self.assertEqual(first.added_checklists, 2)
        self.assertEqual(second.unchanged_checklists, 2)
        self.assertEqual(mode, 0o600)
        self.assertNotIn("S1", serialized)
        self.assertNotIn("Private Place", serialized)
        self.assertNotIn("L-private", serialized)
        self.assertNotIn("1.25", serialized)

    def test_direct_csv_supports_all_time_and_finite_windows(self) -> None:
        rows = [
            _row("S1", "Eastern Bluebird", "Sialia sialis", "2025-01-01"),
            _row("S2", "Eastern Bluebird", "Sialia sialis", "2026-08-08"),
            _row("S2", "Northern Cardinal", "Cardinalis cardinalis", "2026-08-08"),
        ]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "renamed-export.csv"
            _write_csv(csv_path, rows)
            import_ebird_archive(csv_path, root / "state")
            all_time = read_ebird_archive_history(
                root / "state",
                window=ObservationWindow.ALL_TIME,
                limit=50,
                today=date(2026, 8, 9),
            )
            last_day = read_ebird_archive_history(
                root / "state",
                window=ObservationWindow.LAST_DAY,
                limit=50,
                today=date(2026, 8, 9),
            )
            exact_range = read_ebird_archive_history(
                root / "state",
                window=ObservationWindow.ALL_TIME,
                date_range=DateRange(date(2025, 1, 1), date(2025, 1, 1)),
                limit=50,
            )

        self.assertEqual(
            all_time.species,
            [
                EbirdArchiveSpecies("Eastern Bluebird", "Sialia sialis", 2),
                EbirdArchiveSpecies("Northern Cardinal", "Cardinalis cardinalis", 1),
            ],
        )
        self.assertEqual(last_day.selected_observations, 2)
        self.assertEqual(last_day.excluded_observations, 1)
        self.assertEqual(last_day.earliest_observed_on, "2025-01-01")
        self.assertEqual(
            exact_range.species, [EbirdArchiveSpecies("Eastern Bluebird", "Sialia sialis", 1)]
        )

    def test_complete_snapshot_updates_checklists_and_protects_against_reduction(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "MyEBirdData.csv"
            _write_csv(
                csv_path,
                [
                    _row("S1", "Eastern Bluebird", "Sialia sialis", "2026-08-08"),
                    _row("S2", "Northern Cardinal", "Cardinalis cardinalis", "2026-08-09"),
                ],
            )
            import_ebird_archive(csv_path, root / "state")
            before = (root / "state/ebird-archive-observations.json").read_bytes()
            _write_csv(
                csv_path,
                [_row("S1", "Bluebird", "Sialia sialis", "2026-08-08")],
            )
            with self.assertRaisesRegex(DataSourceError, "omits previously imported checklists"):
                import_ebird_archive(csv_path, root / "state")
            self.assertEqual((root / "state/ebird-archive-observations.json").read_bytes(), before)
            result = import_ebird_archive(
                csv_path,
                root / "state",
                allow_history_reduction=True,
            )
            history = read_ebird_archive_history(
                root / "state",
                window=ObservationWindow.ALL_TIME,
                limit=50,
            )

        self.assertEqual(result.removed_checklists, 1)
        self.assertEqual(result.updated_checklists, 1)
        self.assertEqual(history.species, [EbirdArchiveSpecies("Bluebird", "Sialia sialis", 1)])

    def test_duplicate_rows_are_counted_once(self) -> None:
        duplicated = _row("S1", "Eastern Bluebird", "Sialia sialis", "2026-08-08")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "MyEBirdData.csv"
            _write_csv(csv_path, [duplicated, duplicated])
            result = import_ebird_archive(csv_path, root / "state")
            history = read_ebird_archive_history(
                root / "state",
                window=ObservationWindow.ALL_TIME,
                limit=50,
            )

        self.assertEqual(result.rows, 2)
        self.assertEqual(result.duplicate_rows, 1)
        self.assertEqual(history.total_observations, 1)

    def test_dry_run_does_not_write_history(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "MyEBirdData.csv"
            _write_csv(
                csv_path,
                [_row("S1", "Eastern Bluebird", "Sialia sialis", "2026-08-08")],
            )
            result = import_ebird_archive(csv_path, root / "state", dry_run=True)

        self.assertTrue(result.dry_run)
        self.assertFalse((root / "state/ebird-archive-observations.json").exists())

    def test_malformed_and_unsafe_archives_fail_closed_without_leaking_path(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "private-download.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../MyEBirdData.csv", "not safe")
            with self.assertRaises(DataSourceError) as raised:
                import_ebird_archive(archive_path, root / "state")
            self.assertNotIn(str(archive_path), str(raised.exception))

            csv_path = root / "bad.csv"
            csv_path.write_text("Submission ID,Common Name\nS1,Bluebird\n")
            with self.assertRaisesRegex(DataSourceError, "missing required columns"):
                import_ebird_archive(csv_path, root / "state")

            _write_csv(
                csv_path,
                [_row("S1", "Eastern Bluebird", "Sialia sialis", "08/08/2026")],
            )
            with self.assertRaisesRegex(DataSourceError, "date on row 2"):
                import_ebird_archive(csv_path, root / "state")

        self.assertFalse((root / "state/ebird-archive-observations.json").exists())

    def test_rejects_insecure_or_oversized_state_and_input(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "MyEBirdData.csv"
            _write_csv(
                csv_path,
                [_row("S1", "Eastern Bluebird", "Sialia sialis", "2026-08-08")],
            )
            with (
                patch("inky_bird_frame.ebird_archive.MAX_UNCOMPRESSED_BYTES", 1),
                self.assertRaisesRegex(DataSourceError, "size limit"),
            ):
                import_ebird_archive(csv_path, root / "state")
            import_ebird_archive(csv_path, root / "state")
            state_path = root / "state/ebird-archive-observations.json"
            state_path.chmod(0o644)
            with self.assertRaisesRegex(DataSourceError, "mode 0600"):
                read_ebird_archive_history(
                    root / "state",
                    window=ObservationWindow.ALL_TIME,
                    limit=50,
                )

    def test_rejects_tampered_state(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            state_dir.mkdir()
            state_path = state_dir / "ebird-archive-observations.json"
            state_path.write_text(json.dumps({"schema_version": 99}))
            state_path.chmod(0o600)
            with self.assertRaisesRegex(DataSourceError, "Unsupported eBird archive state"):
                read_ebird_archive_history(
                    state_dir,
                    window=ObservationWindow.ALL_TIME,
                    limit=50,
                )

    def test_rejects_inconsistent_state_metadata_and_species(self) -> None:
        rows = [_row("S1", "Eastern Bluebird", "Sialia sialis", "2026-08-08")]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "MyEBirdData.csv"
            _write_csv(csv_path, rows)
            import_ebird_archive(csv_path, root / "state")
            state_path = root / "state/ebird-archive-observations.json"
            payload = json.loads(state_path.read_text())
            payload["earliest_observed_on"] = "2026-08-07"
            state_path.write_text(json.dumps(payload))
            state_path.chmod(0o600)
            with self.assertRaisesRegex(DataSourceError, "date range"):
                read_ebird_archive_history(
                    root / "state",
                    window=ObservationWindow.ALL_TIME,
                    limit=50,
                )

            payload["earliest_observed_on"] = "2026-08-08"
            checklist = next(iter(payload["checklists"].values()))
            checklist["species"].append(
                {
                    "common_name": "Bluebird",
                    "scientific_name": "Sialia sialis",
                }
            )
            state_path.write_text(json.dumps(payload))
            state_path.chmod(0o600)
            with self.assertRaisesRegex(DataSourceError, "Duplicate species"):
                read_ebird_archive_history(
                    root / "state",
                    window=ObservationWindow.ALL_TIME,
                    limit=50,
                )

    def test_rejects_csv_rows_with_extra_columns(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "MyEBirdData.csv"
            _write_csv(
                csv_path,
                [_row("S1", "Eastern Bluebird", "Sialia sialis", "2026-08-08")],
            )
            lines = csv_path.read_text().splitlines()
            csv_path.write_text("\n".join((lines[0], f"{lines[1]},unexpected")))
            with self.assertRaisesRegex(DataSourceError, "Malformed.*row 2"):
                import_ebird_archive(csv_path, root / "state")


if __name__ == "__main__":
    unittest.main()
