from __future__ import annotations

import unittest

from inky_bird_frame.config import DisplayConfig
from inky_bird_frame.images import slugify


class ImageHelperTests(unittest.TestCase):
    def test_slugify(self) -> None:
        self.assertEqual(slugify("Eastern Bluebird"), "eastern-bluebird")
        self.assertEqual(slugify("Sialia sialis!"), "sialia-sialis")

    def test_display_config_matches_inky_panel(self) -> None:
        config = DisplayConfig()

        self.assertEqual(config.portrait_size, (1200, 1600))
        self.assertEqual(config.hardware_size, (1600, 1200))
        self.assertEqual(config.rotation_degrees, 90)


if __name__ == "__main__":
    unittest.main()
