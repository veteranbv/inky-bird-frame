from __future__ import annotations

import unittest

from inky_bird_frame.images import (
    COMPACT_HARDWARE_SIZE,
    HARDWARE_SIZE,
    PORTRAIT_SIZE,
    ROTATION_DEGREES,
    SUPPORTED_HARDWARE_SIZES,
    slugify,
)


class ImageHelperTests(unittest.TestCase):
    def test_slugify(self) -> None:
        self.assertEqual(slugify("Eastern Bluebird"), "eastern-bluebird")
        self.assertEqual(slugify("Sialia sialis!"), "sialia-sialis")
        self.assertEqual(slugify("Anna's Hummingbird"), "anna-s-hummingbird")
        self.assertEqual(slugify("Piopío"), "piopío")

    def test_canonical_assets_match_inky_panel(self) -> None:
        self.assertEqual(PORTRAIT_SIZE, (1200, 1600))
        self.assertEqual(HARDWARE_SIZE, (1600, 1200))
        self.assertEqual(COMPACT_HARDWARE_SIZE, (800, 480))
        self.assertEqual(SUPPORTED_HARDWARE_SIZES, {(1600, 1200), (800, 480)})
        self.assertEqual(ROTATION_DEGREES, 90)


if __name__ == "__main__":
    unittest.main()
