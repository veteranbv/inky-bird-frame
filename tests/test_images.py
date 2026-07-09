from __future__ import annotations

import unittest

from inky_bird_frame.images import HARDWARE_SIZE, PORTRAIT_SIZE, ROTATION_DEGREES, slugify


class ImageHelperTests(unittest.TestCase):
    def test_slugify(self) -> None:
        self.assertEqual(slugify("Eastern Bluebird"), "eastern-bluebird")
        self.assertEqual(slugify("Sialia sialis!"), "sialia-sialis")

    def test_canonical_assets_match_inky_panel(self) -> None:
        self.assertEqual(PORTRAIT_SIZE, (1200, 1600))
        self.assertEqual(HARDWARE_SIZE, (1600, 1200))
        self.assertEqual(ROTATION_DEGREES, 90)


if __name__ == "__main__":
    unittest.main()
