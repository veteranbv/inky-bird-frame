from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inky_bird_frame.config import DisplayNodeConfig
from inky_bird_frame.display_node import run_display_cycle


class DisplayNodeTests(unittest.TestCase):
    def test_downloads_verifies_displays_then_skips_unchanged_single_plate(self) -> None:
        image = b"display-image"
        digest = hashlib.sha256(image).hexdigest()
        payload = {
            "schema_version": 1,
            "species": [
                {
                    "taxon_id": 12942,
                    "common_name": "Eastern Bluebird",
                    "scientific_name": "Sialia sialis",
                    "slug": "eastern-bluebird",
                    "portrait_path": "species/12942-eastern-bluebird/portrait.png",
                    "portrait_sha256": "b" * 64,
                    "display_path": "species/12942-eastern-bluebird/display.png",
                    "display_sha256": digest,
                    "approved_at": "2026-07-09T00:00:00+00:00",
                }
            ],
        }
        with TemporaryDirectory() as temporary:
            config = DisplayNodeConfig("http://controller.test", Path(temporary))
            with (
                patch("inky_bird_frame.display_node.get_json", return_value=payload),
                patch("inky_bird_frame.display_node.get_bytes", return_value=image) as get_bytes,
                patch("inky_bird_frame.display_node.show_on_inky", return_value=(1600, 1200)),
            ):
                first = run_display_cycle(config)
                second = run_display_cycle(config)

        self.assertEqual(first["display_update"], "sent")
        self.assertEqual(second["display_update"], "unchanged")
        get_bytes.assert_called_once()

    def test_unchanged_plate_advances_to_a_new_catalog_entry(self) -> None:
        first_image = b"first"
        second_image = b"second"
        first_digest = hashlib.sha256(first_image).hexdigest()
        second_digest = hashlib.sha256(second_image).hexdigest()
        entry = {
            "scientific_name": "Example bird",
            "portrait_path": "portrait.png",
            "portrait_sha256": "b" * 64,
            "approved_at": "2026-07-09T00:00:00+00:00",
        }
        payload = {
            "schema_version": 1,
            "species": [
                {
                    **entry,
                    "taxon_id": 1,
                    "common_name": "First bird",
                    "slug": "first-bird",
                    "display_path": "first.png",
                    "display_sha256": first_digest,
                },
                {
                    **entry,
                    "taxon_id": 2,
                    "common_name": "Second bird",
                    "slug": "second-bird",
                    "display_path": "second.png",
                    "display_sha256": second_digest,
                },
            ],
        }
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "state.json").write_text(
                json.dumps({"next_index": 0, "last_sha256": first_digest})
            )
            config = DisplayNodeConfig("http://controller.test", state_dir)
            with (
                patch("inky_bird_frame.display_node.get_json", return_value=payload),
                patch(
                    "inky_bird_frame.display_node.get_bytes", return_value=second_image
                ) as get_bytes,
                patch("inky_bird_frame.display_node.show_on_inky", return_value=(1600, 1200)),
            ):
                unchanged = run_display_cycle(config)
                updated = run_display_cycle(config)

        self.assertEqual(unchanged["display_update"], "unchanged")
        self.assertEqual(updated["taxon_id"], 2)
        get_bytes.assert_called_once()


if __name__ == "__main__":
    unittest.main()
