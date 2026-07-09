from __future__ import annotations

import unittest

from inky_bird_frame.errors import DataSourceError
from inky_bird_frame.geo import parse_zippopotam_response


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

        self.assertEqual(location.zip_code, "12345")
        self.assertEqual(location.place_name, "Exampleville")
        self.assertEqual(location.state, "XY")
        self.assertEqual(location.latitude, 38.25)
        self.assertEqual(location.longitude, -77.50)
        self.assertEqual(location.label, "Exampleville, XY | ZIP 12345")

    def test_parse_zip_response_rejects_missing_places(self) -> None:
        with self.assertRaises(DataSourceError):
            parse_zippopotam_response("12345", {"places": []})


if __name__ == "__main__":
    unittest.main()
