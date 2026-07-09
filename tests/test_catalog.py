from __future__ import annotations

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.catalog import (
    approve_candidate,
    candidate_directory,
    rebuild_catalog_index,
    write_candidate_manifest,
    write_json_atomic,
)
from inky_bird_frame.errors import CatalogError
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

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].taxon_id, 12942)
        first_index = (catalog / "index.json").read_bytes()

        rebuild_catalog_index(catalog)

        self.assertEqual((catalog / "index.json").read_bytes(), first_index)
        payload = json.loads(first_index)
        self.assertEqual(payload["generated_at"], entries[0].approved_at)

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


if __name__ == "__main__":
    unittest.main()
