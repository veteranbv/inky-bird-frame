from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from inky_bird_frame.birds import ObservationWindow
from inky_bird_frame.config import load_config
from inky_bird_frame.errors import ConfigurationError

CONFIG = """
[discovery]
zip_code = "12345"
radius_km = 16
species_limit = 20
window = "last-30-days"

[controller]
workspace_dir = "."
catalog_dir = "catalog"
state_dir = "var/controller"
codex_path = "/Applications/Codex.app/Contents/Resources/codex"
bind_host = "0.0.0.0"
port = 8793
references_per_species = 4
generations_per_cycle = 1

[display_node]
controller_url = "http://controller.test:8793/"
state_dir = "var/display"
"""


class ConfigTests(unittest.TestCase):
    def test_loads_typed_config_and_resolves_relative_paths(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(CONFIG)

            config = load_config(path)

        self.assertEqual(config.discovery.observation_window, ObservationWindow.LAST_30_DAYS)
        self.assertEqual(config.discovery.radius_km, 16)
        self.assertEqual(config.controller.catalog_dir, (Path(temporary) / "catalog").resolve())
        self.assertEqual(config.display_node.controller_url, "http://controller.test:8793")

    def test_rejects_invalid_zip(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(CONFIG.replace('zip_code = "12345"', 'zip_code = "local"'))

            with self.assertRaises(ConfigurationError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
