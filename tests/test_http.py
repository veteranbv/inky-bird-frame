from __future__ import annotations

import unittest
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError

from inky_bird_frame.errors import DataSourceError
from inky_bird_frame.http import get_json


class JsonHttpTests(unittest.TestCase):
    def test_error_label_redacts_sensitive_url(self) -> None:
        url = "https://example.test/stations/private-token/species"
        error = HTTPError(url, 401, "Unauthorized", Message(), None)

        with (
            patch("inky_bird_frame.http.urlopen", side_effect=error),
            self.assertRaises(DataSourceError) as raised,
        ):
            get_json(url, error_label="BirdWeather API")

        self.assertEqual(str(raised.exception), "HTTP 401 from BirdWeather API")
        self.assertNotIn("private-token", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
