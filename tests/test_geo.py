from __future__ import annotations

import unittest
from unittest.mock import patch

from inky_bird_frame.birds import ObservationWindow
from inky_bird_frame.config import DiscoveryConfig
from inky_bird_frame.errors import DataSourceError
from inky_bird_frame.geo import (
    lookup_geoapify_postcode,
    lookup_us_zip,
    parse_geoapify_postcode_response,
    parse_zippopotam_response,
    resolve_discovery_location,
)


def discovery_config(
    *,
    zip_code: str | None = None,
    postal_code: str | None = None,
    country_code: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    geoapify_api_key: str | None = None,
) -> DiscoveryConfig:
    return DiscoveryConfig(
        zip_code=zip_code,
        postal_code=postal_code,
        country_code=country_code,
        latitude=latitude,
        longitude=longitude,
        radius_km=8,
        species_limit=50,
        observation_window=ObservationWindow.LAST_30_DAYS,
        geoapify_api_key=geoapify_api_key,
    )


class ZipParsingTests(unittest.TestCase):
    def test_parse_zip_response(self) -> None:
        location = parse_zippopotam_response(
            "12345",
            {
                "places": [
                    {
                        "place name": "Exampleville",
                        "state abbreviation": "XY",
                        "latitude": "38.25",
                        "longitude": "-77.50",
                    }
                ]
            },
        )

        self.assertEqual(location.postal_code, "12345")
        self.assertEqual(location.place_name, "Exampleville")
        self.assertEqual(location.state, "XY")
        self.assertEqual(location.latitude, 38.25)
        self.assertEqual(location.longitude, -77.50)
        self.assertEqual(location.country_code, "us")
        self.assertEqual(location.geocoder, "zippopotam")
        self.assertEqual(location.label, "Exampleville, XY | postal code 12345")

    def test_parse_zip_response_rejects_missing_places(self) -> None:
        with self.assertRaises(DataSourceError):
            parse_zippopotam_response("12345", {"places": []})

    def test_zip_lookup_redacts_postal_code_from_transport_errors(self) -> None:
        with (
            patch("inky_bird_frame.geo.get_json", side_effect=DataSourceError("failure")) as get,
            self.assertRaises(DataSourceError),
        ):
            lookup_us_zip("12345")

        self.assertEqual(get.call_args.kwargs["error_label"], "Zippopotam US ZIP API")


class GeoapifyParsingTests(unittest.TestCase):
    def test_parses_exact_international_postcode(self) -> None:
        location = parse_geoapify_postcode_response(
            "SW1A 1AA",
            "gb",
            {
                "features": [
                    {
                        "properties": {
                            "postcode": "SW1A 1AA",
                            "country_code": "gb",
                            "city": "London",
                            "state": "England",
                            "lat": 51.501009,
                            "lon": -0.141588,
                            "datasource": {"attribution": "© OpenStreetMap contributors"},
                        }
                    }
                ]
            },
        )

        self.assertEqual(location.postal_code, "SW1A 1AA")
        self.assertEqual(location.country_code, "gb")
        self.assertEqual(location.place_name, "London")
        self.assertEqual(location.state, "England")
        self.assertEqual(location.latitude, 51.501009)
        self.assertEqual(location.longitude, -0.141588)
        self.assertEqual(location.geocoder, "geoapify")
        self.assertEqual(
            location.geocoder_attribution,
            "Powered by Geoapify; © OpenStreetMap contributors",
        )

    def test_rejects_non_exact_postcode_result(self) -> None:
        payload = {
            "features": [
                {
                    "properties": {
                        "postcode": "SW1A 2AA",
                        "country_code": "gb",
                        "lat": 51.5,
                        "lon": -0.1,
                    }
                }
            ]
        }

        with self.assertRaisesRegex(DataSourceError, "no exact match"):
            parse_geoapify_postcode_response("SW1A 1AA", "gb", payload)

    def test_rejects_ambiguous_exact_postcode_results(self) -> None:
        payload = {
            "features": [
                {
                    "properties": {
                        "postcode": "100 00",
                        "country_code": "cz",
                        "lat": 50.1,
                        "lon": 14.4,
                    }
                },
                {
                    "properties": {
                        "postcode": "10000",
                        "country_code": "cz",
                        "lat": 50.2,
                        "lon": 14.5,
                    }
                },
            ]
        }

        with self.assertRaisesRegex(DataSourceError, "multiple locations"):
            parse_geoapify_postcode_response("100 00", "cz", payload)

    def test_lookup_encodes_query_and_redacts_key_from_errors(self) -> None:
        with (
            patch("inky_bird_frame.geo.get_json", return_value={"features": []}) as get,
            self.assertRaises(DataSourceError),
        ):
            lookup_geoapify_postcode("SW1A 1AA", "gb", "private-key")

        url = get.call_args.args[0]
        self.assertIn("postcode=SW1A+1AA", url)
        self.assertIn("countrycode=gb", url)
        self.assertIn("apiKey=private-key", url)
        self.assertEqual(get.call_args.kwargs["error_label"], "Geoapify Postcode API")


class DiscoveryLocationTests(unittest.TestCase):
    def test_resolves_configured_coordinates_without_network(self) -> None:
        config = discovery_config(
            latitude=51.5,
            longitude=-0.1,
        )
        with patch("inky_bird_frame.geo.get_json") as get:
            location = resolve_discovery_location(config)

        self.assertEqual(location.latitude, 51.5)
        self.assertEqual(location.longitude, -0.1)
        self.assertEqual(location.geocoder, "configured")
        get.assert_not_called()

    def test_resolves_geoapify_postcode_configuration(self) -> None:
        config = discovery_config(
            postal_code="SW1A 1AA",
            country_code="gb",
            geoapify_api_key="secret",
        )
        expected = object()
        with patch("inky_bird_frame.geo.lookup_geoapify_postcode", return_value=expected) as lookup:
            location = resolve_discovery_location(config)

        self.assertIs(location, expected)
        lookup.assert_called_once_with("SW1A 1AA", "gb", "secret")

    def test_resolves_legacy_us_zip_configuration(self) -> None:
        config = discovery_config(zip_code="12345")
        expected = object()
        with patch("inky_bird_frame.geo.lookup_us_zip", return_value=expected) as lookup:
            location = resolve_discovery_location(config)

        self.assertIs(location, expected)
        lookup.assert_called_once_with("12345")


if __name__ == "__main__":
    unittest.main()
