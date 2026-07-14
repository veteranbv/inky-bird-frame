from __future__ import annotations

import json
import shutil
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.catalog import (
    approve_candidate,
    candidate_directory,
    read_catalog_entries,
    read_json,
    rebuild_catalog_index,
    write_candidate_manifest,
)
from inky_bird_frame.errors import CatalogError
from inky_bird_frame.http import write_json_atomic
from inky_bird_frame.models import QualityReview, SpeciesProfileData

PROFILE = SpeciesProfileData(
    taxon_id=7513,
    common_name="Carolina Wren",
    scientific_name="Thryothorus ludovicianus",
    family="Troglodytidae",
    measurements={"length": "5.5 in", "wingspan": "11 in", "weight": "0.7 oz"},
    field_marks=["white eyebrow", "rufous back", "barred wings", "upright tail"],
    habitat="Brushy woodland",
    behavior="Forages low in cover",
    palette=["rufous", "cream", "umber"],
    sources=[
        {"title": "Source one", "url": "https://example.test/one"},
        {"title": "Source two", "url": "https://example.test/two"},
    ],
)


def make_candidate(state: Path, species: BirdSpecies, review: QualityReview) -> Path:
    candidate = candidate_directory(state, species)
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
        prompt_version="test-v1",
    )
    return candidate


class CatalogTests(unittest.TestCase):
    def test_atomic_json_writes_support_concurrent_health_requests(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "index.json"
            with ThreadPoolExecutor(max_workers=8) as executor:
                list(
                    executor.map(lambda value: write_json_atomic(path, {"value": value}), range(32))
                )
            payload = json.loads(path.read_text())

        self.assertIn(payload["value"], range(32))

    def test_approved_seed_has_valid_checksums(self) -> None:
        catalog = Path(__file__).parents[1] / "catalog"

        entries = rebuild_catalog_index(catalog)

        taxon_ids = {entry.taxon_id for entry in entries}
        self.assertTrue({9083, 12942}.issubset(taxon_ids))
        first_index = (catalog / "index.json").read_bytes()

        rebuild_catalog_index(catalog)

        self.assertEqual((catalog / "index.json").read_bytes(), first_index)
        payload = json.loads(first_index)
        self.assertEqual(payload["generated_at"], max(entry.approved_at for entry in entries))

    def test_approval_is_explicit_and_cannot_overwrite(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            candidate = candidate_directory(state, species)
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
                prompt_version="test-v1",
            )

            entry = approve_candidate(state, catalog, species.taxon_id)

            self.assertEqual(entry.taxon_id, species.taxon_id)
            self.assertFalse(candidate.exists())
            with self.assertRaises(CatalogError):
                approve_candidate(state, catalog, species.taxon_id)

    def test_interrupted_approval_with_leftover_pending_candidate_completes(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            candidate = make_candidate(state, species, review)
            backup = root / "backup"
            shutil.copytree(candidate, backup)
            approve_candidate(state, catalog, species.taxon_id)
            shutil.copytree(backup, candidate)

            entry = approve_candidate(state, catalog, species.taxon_id)

            self.assertEqual(entry.taxon_id, species.taxon_id)
            self.assertFalse(candidate.exists())
            self.assertEqual(
                [item.taxon_id for item in read_catalog_entries(catalog)],
                [species.taxon_id],
            )

    def test_stale_staging_is_cleared_and_invisible_to_catalog_reads(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            make_candidate(state, species, review)
            stale = catalog / ".staging" / "7513-carolina-wren"
            stale.mkdir(parents=True)
            (stale / "manifest.json").write_text("{not json")

            self.assertEqual(read_catalog_entries(catalog), [])

            entry = approve_candidate(state, catalog, species.taxon_id)

            self.assertFalse(stale.exists())
            self.assertEqual(entry.taxon_id, species.taxon_id)
            self.assertEqual(
                [item.taxon_id for item in read_catalog_entries(catalog)],
                [species.taxon_id],
            )

    def test_successful_approval_leaves_no_staging_directory(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            make_candidate(state, species, review)

            approve_candidate(state, catalog, species.taxon_id)

            self.assertFalse((catalog / ".staging").exists())
            self.assertEqual(
                sorted(path.name for path in catalog.iterdir()),
                ["index.json", "species"],
            )

    def test_partial_destination_is_discarded_and_reapproved_from_pending(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            candidate = make_candidate(state, species, review)
            manifest = json.loads((candidate / "manifest.json").read_text())
            manifest["status"] = "approved"
            manifest["approved_at"] = "2026-07-10T00:00:00+00:00"

            # An old-flow crash mid-copy: manifest landed, display.png did not.
            destination = catalog / "species" / "7513-carolina-wren"
            destination.mkdir(parents=True)
            (destination / "manifest.json").write_text(json.dumps(manifest))
            (destination / "portrait.png").write_bytes(b"portrait")

            entry = approve_candidate(state, catalog, species.taxon_id)

            self.assertEqual(entry.taxon_id, species.taxon_id)
            self.assertFalse(candidate.exists())
            self.assertEqual((destination / "display.png").read_bytes(), b"display")
            self.assertEqual(
                [item.taxon_id for item in read_catalog_entries(catalog)],
                [species.taxon_id],
            )

    def test_destination_without_readable_manifest_is_discarded_and_reapproved(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            candidate = make_candidate(state, species, review)

            # An old-flow crash before the manifest landed in the destination.
            destination = catalog / "species" / "7513-carolina-wren"
            destination.mkdir(parents=True)
            (destination / "portrait.png").write_bytes(b"partial")

            entry = approve_candidate(state, catalog, species.taxon_id)

            self.assertEqual(entry.taxon_id, species.taxon_id)
            self.assertFalse(candidate.exists())
            self.assertEqual((destination / "display.png").read_bytes(), b"display")

    def test_read_json_reports_non_utf8_corruption_as_catalog_error(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "torn.json"
            path.write_bytes(b'{"schema_version": 1, "species": [\xff\xfe')
            with self.assertRaisesRegex(CatalogError, "Invalid JSON"):
                read_json(path)

    def test_destination_with_differing_manifest_preserves_pending_and_raises(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            candidate = make_candidate(state, species, review)
            manifest = json.loads((candidate / "manifest.json").read_text())
            manifest["status"] = "approved"
            manifest["approved_at"] = "2026-07-10T00:00:00+00:00"
            manifest["common_name"] = "Corrected Wren"

            destination = catalog / "species" / "7513-carolina-wren"
            destination.mkdir(parents=True)
            (destination / "manifest.json").write_text(json.dumps(manifest))
            shutil.copy(candidate / "portrait.png", destination / "portrait.png")
            shutil.copy(candidate / "display.png", destination / "display.png")

            with self.assertRaisesRegex(CatalogError, "explicit replacement workflow"):
                approve_candidate(state, catalog, species.taxon_id)

            self.assertTrue(candidate.exists())
            self.assertTrue((destination / "manifest.json").exists())

    def test_conflicting_destination_still_requires_explicit_replacement(self) -> None:
        species = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 5, "test")
        review = QualityReview(True, 4, 4, 4, 4, True, ())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            catalog = root / "catalog"
            make_candidate(state, species, review)
            approve_candidate(state, catalog, species.taxon_id)
            candidate = candidate_directory(state, species)
            candidate.mkdir(parents=True)
            (candidate / "portrait.png").write_bytes(b"different portrait")
            (candidate / "display.png").write_bytes(b"different display")
            write_candidate_manifest(
                candidate,
                species,
                PROFILE,
                [],
                review,
                generator="test",
                prompt_version="test-v1",
            )

            with self.assertRaisesRegex(CatalogError, "already approved"):
                approve_candidate(state, catalog, species.taxon_id)

            self.assertTrue(candidate.exists())


if __name__ == "__main__":
    unittest.main()
