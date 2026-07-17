from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from inky_bird_frame.display import detect_inky_display, show_on_inky
from inky_bird_frame.images import HARDWARE_SIZE, PAPER_COLOR


class FakeDisplay:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.image: Image.Image | None = None
        self.show_count = 0

    def set_image(self, image: object) -> None:
        if not isinstance(image, Image.Image):
            raise TypeError("expected a Pillow image")
        self.image = image

    def show(self) -> None:
        self.show_count += 1


class DisplayTests(unittest.TestCase):
    def test_auto_detection_rejects_unknown_geometry(self) -> None:
        module = SimpleNamespace(auto=lambda: FakeDisplay(600, 400))
        with (
            patch("inky_bird_frame.display.import_module", return_value=module),
            self.assertRaisesRegex(ValueError, "unsupported Inky display size"),
        ):
            detect_inky_display()

    def test_canonical_image_is_unchanged_for_13_inch_display(self) -> None:
        display = FakeDisplay(*HARDWARE_SIZE)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "display.png"
            Image.new("RGB", HARDWARE_SIZE, "navy").save(path)

            size = show_on_inky(path, display=display)

        self.assertEqual(size, HARDWARE_SIZE)
        self.assertIsNotNone(display.image)
        assert display.image is not None
        self.assertEqual(display.image.size, HARDWARE_SIZE)
        self.assertEqual(display.image.getpixel((0, 0)), (0, 0, 128))
        self.assertEqual(display.show_count, 1)

    def test_canonical_image_is_contained_without_cropping_for_7_inch_display(self) -> None:
        display = FakeDisplay(800, 480)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "display.png"
            Image.new("RGB", HARDWARE_SIZE, "black").save(path)

            size = show_on_inky(path, display=display)

        self.assertEqual(size, (800, 480))
        self.assertIsNotNone(display.image)
        assert display.image is not None
        self.assertEqual(display.image.size, (800, 480))
        self.assertEqual(display.image.getpixel((79, 240)), PAPER_COLOR)
        self.assertEqual(display.image.getpixel((80, 240)), (0, 0, 0))
        self.assertEqual(display.image.getpixel((719, 240)), (0, 0, 0))
        self.assertEqual(display.image.getpixel((720, 240)), PAPER_COLOR)
        self.assertEqual(display.show_count, 1)

    def test_rejects_noncanonical_image_for_7_inch_display(self) -> None:
        display = FakeDisplay(800, 480)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "wrong.png"
            Image.new("RGB", (640, 480), "black").save(path)

            with self.assertRaisesRegex(ValueError, "canonical size"):
                show_on_inky(path, display=display)

        self.assertEqual(display.show_count, 0)


if __name__ == "__main__":
    unittest.main()
