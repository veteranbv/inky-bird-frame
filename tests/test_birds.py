from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from inky_bird_frame.birds import (
    ObservationWindow,
    date_range_for_window,
    fetch_taxon_context,
    parse_birdnet_taxon,
    parse_inaturalist_species_counts,
    parse_inaturalist_taxon,
    parse_observation_window,
)
from inky_bird_frame.errors import DataSourceError


class InaturalistParsingTests(unittest.TestCase):
    def test_parse_species_counts_keeps_usable_species(self) -> None:
        payload = {
            "results": [
                {
                    "count": 26,
                    "taxon": {
                        "id": 12942,
                        "preferred_common_name": "Eastern Bluebird",
                        "name": "Sialia sialis",
                    },
                },
                {"count": 2, "taxon": {"name": "No common name"}},
                {"count": "bad", "taxon": {"preferred_common_name": "Bad", "name": "Bad bad"}},
            ]
        }

        species = parse_inaturalist_species_counts(payload)

        self.assertEqual(len(species), 1)
        self.assertEqual(species[0].taxon_id, 12942)
        self.assertEqual(species[0].common_name, "Eastern Bluebird")
        self.assertEqual(species[0].scientific_name, "Sialia sialis")
        self.assertEqual(species[0].observation_count, 26)
        self.assertEqual(species[0].source, "iNaturalist")

    def test_parse_species_counts_rejects_empty_usable_results(self) -> None:
        with self.assertRaises(DataSourceError):
            parse_inaturalist_species_counts({"results": [{"taxon": {}}]})

    def test_parse_species_counts_accepts_an_empty_result_set(self) -> None:
        self.assertEqual(parse_inaturalist_species_counts({"results": []}), [])

    def test_date_range_for_window(self) -> None:
        today = date(2026, 7, 9)

        self.assertEqual(
            date_range_for_window(ObservationWindow.LAST_DAY, today).as_query_params(),
            {"d1": "2026-07-08", "d2": "2026-07-09"},
        )
        self.assertEqual(
            date_range_for_window(ObservationWindow.LAST_WEEK, today).as_query_params(),
            {"d1": "2026-07-02", "d2": "2026-07-09"},
        )
        self.assertEqual(
            date_range_for_window(ObservationWindow.LAST_30_DAYS, today).as_query_params(),
            {"d1": "2026-06-09", "d2": "2026-07-09"},
        )
        self.assertEqual(
            date_range_for_window(ObservationWindow.LAST_YEAR, today).as_query_params(),
            {"d1": "2025-07-09", "d2": "2026-07-09"},
        )
        self.assertEqual(
            date_range_for_window(ObservationWindow.ALL_TIME, today).as_query_params(),
            {},
        )

    def test_parse_observation_window(self) -> None:
        self.assertIs(parse_observation_window("last-week"), ObservationWindow.LAST_WEEK)
        with self.assertRaises(ValueError):
            parse_observation_window("yesterday-ish")

    def test_parse_taxon_context_extracts_family(self) -> None:
        context = parse_inaturalist_taxon(
            {
                "results": [
                    {
                        "id": 12942,
                        "preferred_common_name": "Eastern Bluebird",
                        "name": "Sialia sialis",
                        "wikipedia_summary": "A small thrush.",
                        "wikipedia_url": "https://example.test/eastern-bluebird",
                        "ancestors": [
                            {"rank": "family", "name": "Turdidae"},
                        ],
                    }
                ]
            }
        )

        self.assertEqual(context.family, "Turdidae")
        self.assertEqual(context.taxon_id, 12942)

    def test_missing_inaturalist_summary_uses_validated_birdnet_context(self) -> None:
        inaturalist = {
            "results": [
                {
                    "id": 5020,
                    "preferred_common_name": "Green Heron",
                    "name": "Butorides virescens",
                    "wikipedia_summary": None,
                    "wikipedia_url": None,
                    "ancestors": [{"rank": "family", "name": "Ardeidae"}],
                }
            ]
        }
        birdnet = {
            "inat_id": 5020,
            "scientific_name": "Butorides virescens",
            "common_name": "Green Heron",
            "descriptions": {"en": "A small North American heron."},
            "wikipedia_urls": {"en": "https://en.wikipedia.org/wiki/Green_heron"},
        }
        with patch("inky_bird_frame.birds.get_json", side_effect=[inaturalist, birdnet]):
            context = fetch_taxon_context(5020)

        self.assertEqual(context.family, "Ardeidae")
        self.assertEqual(context.summary, "A small North American heron.")

    def test_birdnet_fallback_rejects_identity_mismatch(self) -> None:
        expected = parse_inaturalist_taxon(
            {
                "results": [
                    {
                        "id": 5020,
                        "preferred_common_name": "Green Heron",
                        "name": "Butorides virescens",
                        "ancestors": [{"rank": "family", "name": "Ardeidae"}],
                    }
                ]
            }
        )
        with self.assertRaisesRegex(DataSourceError, "identity"):
            parse_birdnet_taxon(
                {
                    "inat_id": 5020,
                    "scientific_name": "Different species",
                    "common_name": "Green Heron",
                    "descriptions": {"en": "Wrong"},
                    "wikipedia_urls": {"en": "https://example.test"},
                },
                expected,
            )


if __name__ == "__main__":
    unittest.main()
