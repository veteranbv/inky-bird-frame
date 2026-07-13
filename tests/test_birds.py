from __future__ import annotations

import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from inky_bird_frame.birds import (
    EbirdSpecies,
    ObservationWindow,
    date_range_for_window,
    fetch_ebird_observations,
    fetch_inaturalist_birds,
    fetch_taxon_context,
    parse_birdnet_taxon,
    parse_ebird_observations,
    parse_inaturalist_species_counts,
    parse_inaturalist_taxon,
    parse_inaturalist_taxon_match,
    parse_observation_window,
    resolve_ebird_species,
)
from inky_bird_frame.errors import DataSourceError, TaxonomyMatchError


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
                        "rank": "species",
                    },
                },
                {
                    "count": 4,
                    "taxon": {
                        "id": 7846,
                        "preferred_common_name": "Flycatcher-shrikes",
                        "name": "Malaconotidae",
                        "rank": "family",
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

    @patch("inky_bird_frame.birds.get_json", return_value={"results": []})
    def test_fetch_species_counts_requests_species_rank(self, get_json: MagicMock) -> None:
        species = fetch_inaturalist_birds(
            latitude=38.0,
            longitude=-77.0,
            radius_km=8,
            limit=50,
            window=ObservationWindow.LAST_30_DAYS,
            today=date(2026, 7, 9),
        )

        self.assertEqual(species, [])
        url = get_json.call_args.args[0]
        params = parse_qs(urlsplit(url).query)
        self.assertEqual(params["rank"], ["species"])
        self.assertEqual(params["per_page"], ["50"])

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
        with patch(
            "inky_bird_frame.birds.get_json", side_effect=[inaturalist, birdnet]
        ) as get_json:
            context = fetch_taxon_context(5020)

        self.assertEqual(context.family, "Ardeidae")
        self.assertEqual(context.summary, "A small North American heron.")
        self.assertEqual(
            get_json.call_args_list[1].args[0],
            "https://birdnet.cornell.edu/taxonomy/api/species/Butorides%20virescens",
        )

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


class EbirdTests(unittest.TestCase):
    def test_parse_observations_keeps_complete_unique_species(self) -> None:
        payload = [
            {
                "speciesCode": "easblu",
                "comName": "Eastern Bluebird",
                "sciName": "Sialia sialis",
                "obsDt": "2026-07-12 08:15",
            },
            {
                "speciesCode": "easblu",
                "comName": "Eastern Bluebird",
                "sciName": "Sialia sialis",
                "obsDt": "2026-07-11 10:00",
            },
            {"speciesCode": "bad"},
        ]

        species = parse_ebird_observations(payload)

        self.assertEqual(len(species), 1)
        self.assertEqual(species[0].species_code, "easblu")

    @patch("inky_bird_frame.birds.get_json", return_value=[])
    def test_fetch_observations_uses_bounded_query_and_secret_header(
        self, get_json: MagicMock
    ) -> None:
        fetch_ebird_observations(
            latitude=38.12345,
            longitude=-77.98765,
            radius_km=11,
            limit=50,
            window=ObservationWindow.LAST_30_DAYS,
            api_key="secret-token",
        )

        url = get_json.call_args.args[0]
        params = parse_qs(urlsplit(url).query)
        self.assertEqual(params["lat"], ["38.123450"])
        self.assertEqual(params["lng"], ["-77.987650"])
        self.assertEqual(params["dist"], ["11"])
        self.assertEqual(params["back"], ["30"])
        self.assertEqual(params["cat"], ["species"])
        self.assertEqual(get_json.call_args.kwargs["headers"], {"X-eBirdApiToken": "secret-token"})
        self.assertNotIn("secret-token", url)

    def test_taxon_match_requires_one_exact_active_bird_species(self) -> None:
        payload = {
            "results": [
                {
                    "id": 12942,
                    "preferred_common_name": "Eastern Bluebird",
                    "name": "Sialia sialis",
                    "rank": "species",
                    "is_active": True,
                    "iconic_taxon_name": "Aves",
                },
                {
                    "id": 1,
                    "preferred_common_name": "Other bird",
                    "name": "Sialia currucoides",
                    "rank": "species",
                    "is_active": True,
                    "iconic_taxon_name": "Aves",
                },
            ]
        }

        species = parse_inaturalist_taxon_match(payload, "Sialia sialis")

        self.assertEqual(species.taxon_id, 12942)
        self.assertEqual(species.sources, ("eBird",))

    def test_resolution_uses_cached_exact_mapping(self) -> None:
        observation = EbirdSpecies(
            "easblu", "Eastern Bluebird", "Sialia sialis", "2026-07-12 08:15"
        )
        with TemporaryDirectory() as temporary:
            cache = Path(temporary) / "crosswalk.json"
            cache.write_text(
                '{"schema_version":1,"entries":{"easblu":'
                '{"scientific_name":"Sialia sialis","common_name":"Eastern Bluebird",'
                '"taxon_id":12942}}}'
            )
            with patch("inky_bird_frame.birds.fetch_inaturalist_taxon_match") as fetch:
                result = resolve_ebird_species([observation], cache)

        fetch.assert_not_called()
        self.assertEqual(result.species[0].taxon_id, 12942)
        self.assertEqual(result.unresolved, [])

    def test_unresolved_mapping_is_deferred_and_cached(self) -> None:
        observation = EbirdSpecies("split", "Split Bird", "Avis split", "2026-07-12")
        now = datetime(2026, 7, 12, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            cache = Path(temporary) / "crosswalk.json"
            with patch(
                "inky_bird_frame.birds.fetch_inaturalist_taxon_match",
                side_effect=TaxonomyMatchError("no exact match"),
            ) as fetch:
                first = resolve_ebird_species([observation], cache, now=now)
                second = resolve_ebird_species([observation], cache, now=now)

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(first.unresolved, [observation])
        self.assertEqual(second.unresolved, [observation])


if __name__ == "__main__":
    unittest.main()
