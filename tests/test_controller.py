from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.config import load_config
from inky_bird_frame.controller import run_controller_cycle
from inky_bird_frame.errors import GenerationError
from inky_bird_frame.geo import ZipLocation

CONFIG = """
[discovery]
zip_code = "12345"
radius_km = 8
species_limit = 12
window = "last-week"

[controller]
workspace_dir = "."
catalog_dir = "catalog"
state_dir = "state"
codex_path = "/usr/bin/false"
bind_host = "127.0.0.1"
port = 8793
references_per_species = 4
generations_per_cycle = 1

[display_node]
controller_url = "http://controller.test:8793"
state_dir = "display"
"""


class ControllerTests(unittest.TestCase):
    def test_expected_generation_failure_becomes_terminal(self) -> None:
        species = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        location = ZipLocation("12345", "Exampleville", "XY", 1.0, 2.0)
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            with (
                patch(
                    "inky_bird_frame.controller.discover_species",
                    return_value=(location, [species]),
                ),
                patch("inky_bird_frame.controller.generate_candidate") as generate,
            ):
                generate.side_effect = GenerationError("profile failed")
                result = run_controller_cycle(config)

            failures = list((config.controller.state_dir / "failed").glob("9083-*"))

        self.assertEqual(len(failures), 1)
        self.assertEqual(result["eligible_count"], 1)
        failure_results = result["failures"]
        self.assertIsInstance(failure_results, list)
        first_failure = failure_results[0] if isinstance(failure_results, list) else None
        self.assertIsInstance(first_failure, dict)
        if isinstance(first_failure, dict):
            self.assertEqual(first_failure["error"], "profile failed")


if __name__ == "__main__":
    unittest.main()
