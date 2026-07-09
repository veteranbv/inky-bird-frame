from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inky_bird_frame.birds import ObservationWindow
from inky_bird_frame.config import RotationMode, load_config
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
max_generation_attempts = 3

[display_node]
controller_url = "http://controller.test:8793/"
state_dir = "var/display"
rotation_mode = "weighted"

[schedule]
refresh_minutes = 15
generation_minutes = 5
rotation_minutes = 3
rotation_jitter_seconds = 7
display_startup_delay_seconds = 30
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
        self.assertEqual(config.controller.max_generation_attempts, 3)
        self.assertEqual(config.display_node.controller_url, "http://controller.test:8793")
        self.assertEqual(config.display_node.rotation_mode, RotationMode.WEIGHTED)
        self.assertEqual(config.schedule.refresh_minutes, 15)
        self.assertEqual(config.schedule.generation_minutes, 5)
        self.assertEqual(config.schedule.rotation_minutes, 3)
        self.assertEqual(config.schedule.rotation_jitter_seconds, 7)
        self.assertEqual(config.schedule.display_startup_delay_seconds, 30)

    def test_rejects_invalid_zip(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(CONFIG.replace('zip_code = "12345"', 'zip_code = "local"'))

            with self.assertRaises(ConfigurationError):
                load_config(path)

    def test_resolves_bare_codex_name_from_path(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                CONFIG.replace(
                    'codex_path = "/Applications/Codex.app/Contents/Resources/codex"',
                    'codex_path = "codex"',
                )
            )
            with patch("inky_bird_frame.config.which", return_value="/opt/local/bin/codex"):
                config = load_config(path)

        self.assertEqual(config.controller.codex_path, Path("/opt/local/bin/codex"))

    def test_resolves_explicit_relative_codex_path_from_config_directory(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                CONFIG.replace(
                    'codex_path = "/Applications/Codex.app/Contents/Resources/codex"',
                    'codex_path = "./codex"',
                )
            )

            config = load_config(path)

        self.assertEqual(config.controller.codex_path, (Path(temporary) / "codex").resolve())

    def test_uses_backward_compatible_schedule_defaults(self) -> None:
        legacy = CONFIG.split("\n[schedule]\n", maxsplit=1)[0].replace(
            'rotation_mode = "weighted"\n', ""
        )
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(legacy)

            config = load_config(path)

        self.assertEqual(config.display_node.rotation_mode, RotationMode.SEQUENTIAL)
        self.assertEqual(config.schedule.refresh_minutes, 15)
        self.assertEqual(config.schedule.generation_minutes, 360)
        self.assertEqual(config.schedule.rotation_minutes, 30)

    def test_rejects_invalid_rotation_policy(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(CONFIG.replace('rotation_mode = "weighted"', 'rotation_mode = "chaos"'))

            with self.assertRaises(ConfigurationError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
