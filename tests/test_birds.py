from __future__ import annotations

import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from inky_bird_frame.birds import (
    BirdSpecies,
    BirdWeatherSpecies,
    DateRange,
    EbirdSpecies,
    ObservationWindow,
    date_range_for_window,
    fetch_birdweather_species,
    fetch_ebird_observations,
    fetch_inaturalist_birds,
    fetch_taxon_context,
    parse_birdnet_taxon,
    parse_birdweather_species,
    parse_ebird_observations,
    parse_inaturalist_species_counts,
    parse_inaturalist_taxon,
    parse_inaturalist_taxon_match,
    parse_observation_window,
    resolve_birdweather_species,
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

    @patch("inky_bird_frame.birds.get_json", return_value={"results": []})
    def test_fetch_species_counts_uses_explicit_inclusive_dates(self, get_json: MagicMock) -> None:
        fetch_inaturalist_birds(
            latitude=33.6407,
            longitude=-84.4277,
            radius_km=11,
            limit=500,
            date_range=DateRange(date(2026, 7, 13), date(2026, 7, 16)),
        )

        params = parse_qs(urlsplit(get_json.call_args.args[0]).query)
        self.assertEqual(params["d1"], ["2026-07-13"])
        self.assertEqual(params["d2"], ["2026-07-16"])
        self.assertEqual(params["radius"], ["11"])
        self.assertEqual(get_json.call_args.kwargs["error_label"], "iNaturalist API")

    def test_date_range_rejects_partial_or_reversed_dates(self) -> None:
        with self.assertRaisesRegex(ValueError, "both start and end"):
            DateRange(date(2026, 7, 13), None)
        with self.assertRaisesRegex(ValueError, "on or before"):
            DateRange(date(2026, 7, 16), date(2026, 7, 13))

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

    def test_non_persistent_resolution_does_not_create_cache_state(self) -> None:
        observation = EbirdSpecies("easblu", "Eastern Bluebird", "Sialia sialis", "2026-07-12")
        species = BirdSpecies(12942, "Eastern Bluebird", "Sialia sialis", 1, "eBird")
        with TemporaryDirectory() as temporary:
            cache = Path(temporary) / "state/crosswalk.json"
            with patch(
                "inky_bird_frame.birds.fetch_inaturalist_taxon_match",
                return_value=species,
            ):
                result = resolve_ebird_species([observation], cache, persist_cache=False)

            state_exists = cache.parent.exists()

        self.assertEqual(result.species, [species])
        self.assertFalse(state_exists)


class BirdWeatherTests(unittest.TestCase):
    def test_parse_species_keeps_complete_unique_avian_detections(self) -> None:
        payload = {
            "success": True,
            "species": [
                {
                    "id": 42,
                    "commonName": "Eastern Bluebird",
                    "scientificName": "Sialia sialis",
                    "classification": "avian",
                    "detections": {"total": 7},
                    "latestDetectionAt": "2026-07-12T08:15:00-04:00",
                },
                {
                    "id": 42,
                    "commonName": "Eastern Bluebird",
                    "scientificName": "Sialia sialis",
                    "classification": "avian",
                    "detections": {"total": 6},
                    "latestDetectionAt": "2026-07-11T08:15:00-04:00",
                },
                {
                    "id": 9,
                    "commonName": "Little Brown Bat",
                    "scientificName": "Myotis lucifugus",
                    "classification": "bat",
                    "detections": {"total": 2},
                    "latestDetectionAt": "2026-07-12T01:00:00-04:00",
                },
            ],
        }

        species = parse_birdweather_species(payload)

        self.assertEqual(
            species,
            [
                BirdWeatherSpecies(
                    42,
                    "Eastern Bluebird",
                    "Sialia sialis",
                    7,
                    "2026-07-12T08:15:00-04:00",
                )
            ],
        )

    def test_parse_species_rejects_unsuccessful_response(self) -> None:
        with self.assertRaisesRegex(DataSourceError, "not successful"):
            parse_birdweather_species({"success": False})

    def test_parse_species_rejects_invalid_detection_timestamp(self) -> None:
        payload = {
            "success": True,
            "species": [
                {
                    "id": 42,
                    "commonName": "Eastern Bluebird",
                    "scientificName": "Sialia sialis",
                    "classification": "avian",
                    "detections": {"total": 7},
                    "latestDetectionAt": "not-a-timestamp",
                }
            ],
        }

        with self.assertRaisesRegex(DataSourceError, "usable avian species"):
            parse_birdweather_species(payload)

    @patch("inky_bird_frame.birds.get_json", return_value={"success": True, "species": []})
    def test_fetch_species_bounds_window_and_redacts_token(self, get_json: MagicMock) -> None:
        fetch_birdweather_species(
            token="station/token",
            limit=50,
            window=ObservationWindow.LAST_YEAR,
            today=date(2026, 7, 12),
        )

        url = get_json.call_args.args[0]
        params = parse_qs(urlsplit(url).query)
        self.assertIn("station%2Ftoken", url)
        self.assertEqual(params["from"], ["2025-07-12"])
        self.assertEqual(params["to"], ["2026-07-12"])
        self.assertEqual(params["classification"], ["avian"])
        self.assertEqual(get_json.call_args.kwargs["error_label"], "BirdWeather API")

    def test_resolution_preserves_detection_count_and_source(self) -> None:
        detection = BirdWeatherSpecies(
            42,
            "Eastern Bluebird",
            "Sialia sialis",
            7,
            "2026-07-12T08:15:00-04:00",
        )
        match = BirdSpecies(12942, "Eastern Bluebird", "Sialia canonicalis", 1, "eBird")
        with TemporaryDirectory() as temporary:
            cache = Path(temporary) / "crosswalk.json"
            with patch("inky_bird_frame.birds.fetch_inaturalist_taxon_match", return_value=match):
                species, unresolved = resolve_birdweather_species([detection], cache)

        self.assertEqual(unresolved, [])
        self.assertEqual(species[0].taxon_id, 12942)
        self.assertEqual(species[0].scientific_name, "Sialia canonicalis")
        self.assertEqual(species[0].observation_count, 7)
        self.assertEqual(species[0].sources, ("BirdWeather",))
        self.assertEqual(species[0].latest_detection_at, detection.latest_detection_at)


if __name__ == "__main__":
    unittest.main()
