from __future__ import annotations

import unittest

from inky_bird_frame.errors import DataSourceError
from inky_bird_frame.references import parse_reference_candidates, photo_url_for_size


def observation(
    observation_id: int,
    observer: str,
    photo_id: int,
    license_code: str = "cc-by",
    width: int = 1600,
    height: int = 1200,
) -> dict[str, object]:
    return {
        "id": observation_id,
        "uri": f"https://www.inaturalist.org/observations/{observation_id}",
        "user": {"login": observer},
        "photos": [
            {
                "id": photo_id,
                "attribution": f"Photo by {observer}",
                "license_code": license_code,
                "url": f"https://static.example/photos/{photo_id}/square.jpg",
                "original_dimensions": {"width": width, "height": height},
            }
        ],
    }


class ReferenceTests(unittest.TestCase):
    def test_selects_permissive_photos_from_distinct_observers(self) -> None:
        payload = {
            "results": [
                observation(1, "alice", 11),
                observation(2, "alice", 12),
                observation(3, "bob", 13, "cc0"),
                observation(4, "carol", 14, "cc-by-nc"),
            ]
        }

        references = parse_reference_candidates(payload, 2)

        self.assertEqual([item.photo_id for item in references], [11, 13])
        self.assertEqual(references[0].image_url, "https://static.example/photos/11/large.jpg")

    def test_rejects_insufficient_licensed_references(self) -> None:
        with self.assertRaises(DataSourceError):
            parse_reference_candidates({"results": [observation(1, "alice", 11, "cc-by-nc")]}, 1)

    def test_photo_url_size_is_validated(self) -> None:
        with self.assertRaises(ValueError):
            photo_url_for_size("https://example.test/1/square.jpg", "enormous")


if __name__ == "__main__":
    unittest.main()
