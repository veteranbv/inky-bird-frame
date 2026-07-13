from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inky_bird_frame.birds import ObservationWindow
from inky_bird_frame.config import DiscoverySource, NotificationEvent, RotationMode, load_config
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

[public_catalog]
enabled = true
checkout_dir = "var/public-catalog"
repository = "example/inky-bird-frame"
gh_path = "/opt/homebrew/bin/gh"
remote = "public"
base_branch = "main"
commit_name = "Catalog Publisher"
commit_email = "catalog@example.test"

[schedule]
refresh_minutes = 15
generation_minutes = 5
rotation_minutes = 3
rotation_jitter_seconds = 7
display_startup_delay_seconds = 30
catalog_publish_minutes = 4
"""


class ConfigTests(unittest.TestCase):
    def test_loads_typed_config_and_resolves_relative_paths(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(CONFIG)

            config = load_config(path)

        self.assertEqual(config.discovery.observation_window, ObservationWindow.LAST_30_DAYS)
        self.assertIs(config.discovery.source, DiscoverySource.INATURALIST)
        self.assertEqual(config.discovery.radius_km, 16)
        self.assertEqual(config.controller.catalog_dir, (Path(temporary) / "catalog").resolve())
        self.assertEqual(config.controller.max_generation_attempts, 3)
        self.assertEqual(config.display_node.controller_url, "http://controller.test:8793")
        self.assertEqual(config.display_node.rotation_mode, RotationMode.WEIGHTED)
        self.assertTrue(config.public_catalog.enabled)
        self.assertEqual(
            config.public_catalog.checkout_dir,
            (Path(temporary) / "var/public-catalog").resolve(),
        )
        self.assertEqual(config.public_catalog.remote, "public")
        self.assertEqual(config.public_catalog.repository, "example/inky-bird-frame")
        self.assertEqual(config.public_catalog.gh_path, Path("/opt/homebrew/bin/gh"))
        self.assertEqual(config.public_catalog.base_branch, "main")
        self.assertEqual(config.public_catalog.commit_name, "Catalog Publisher")
        self.assertEqual(config.public_catalog.commit_email, "catalog@example.test")
        self.assertEqual(config.schedule.refresh_minutes, 15)
        self.assertEqual(config.schedule.generation_minutes, 5)
        self.assertEqual(config.schedule.rotation_minutes, 3)
        self.assertEqual(config.schedule.rotation_jitter_seconds, 7)
        self.assertEqual(config.schedule.display_startup_delay_seconds, 30)
        self.assertEqual(config.schedule.catalog_publish_minutes, 4)

    def test_ebird_source_requires_a_key(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(CONFIG.replace("[discovery]\n", '[discovery]\nsource = "ebird"\n'))

            with self.assertRaisesRegex(ConfigurationError, "requires ebird_api_key"):
                load_config(path)

    def test_ebird_key_can_come_from_environment(self) -> None:
        configured = CONFIG.replace(
            "[discovery]\n",
            '[discovery]\nsource = "combined"\nebird_api_key_env = "TEST_EBIRD_KEY"\n',
        )
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(configured)
            with patch.dict("os.environ", {"TEST_EBIRD_KEY": "secret"}):
                config = load_config(path)

        self.assertIs(config.discovery.source, DiscoverySource.COMBINED)
        self.assertEqual(config.discovery.ebird_api_key, "secret")

    def test_inaturalist_default_resolves_key_for_source_override(self) -> None:
        configured = CONFIG.replace(
            "[discovery]\n", '[discovery]\nebird_api_key_env = "TEST_EBIRD_KEY"\n'
        )
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(configured)
            with patch.dict("os.environ", {"TEST_EBIRD_KEY": "secret"}):
                config = load_config(path)

        self.assertIs(config.discovery.source, DiscoverySource.INATURALIST)
        self.assertEqual(config.discovery.ebird_api_key, "secret")

    def test_ebird_rejects_long_observation_windows(self) -> None:
        configured = CONFIG.replace(
            "[discovery]\n", '[discovery]\nsource = "ebird"\nebird_api_key = "secret"\n'
        ).replace('window = "last-30-days"', 'window = "last-year"')
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(configured)

            with self.assertRaisesRegex(ConfigurationError, "up to 30 days"):
                load_config(path)

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
        legacy = CONFIG.split("\n[public_catalog]\n", maxsplit=1)[0].replace(
            'rotation_mode = "weighted"\n', ""
        )
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(legacy)

            config = load_config(path)

        self.assertEqual(config.display_node.rotation_mode, RotationMode.SEQUENTIAL)
        self.assertFalse(config.public_catalog.enabled)
        self.assertIsNone(config.public_catalog.checkout_dir)
        self.assertEqual(config.schedule.refresh_minutes, 15)
        self.assertEqual(config.schedule.generation_minutes, 360)
        self.assertEqual(config.schedule.rotation_minutes, 30)
        self.assertEqual(config.schedule.catalog_publish_minutes, 5)

    def test_enabled_public_catalog_requires_checkout(self) -> None:
        invalid = CONFIG.replace('checkout_dir = "var/public-catalog"\n', "")
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(invalid)

            with self.assertRaisesRegex(ConfigurationError, "checkout_dir is required"):
                load_config(path)

    def test_enabled_public_catalog_requires_repository(self) -> None:
        invalid = CONFIG.replace('repository = "example/inky-bird-frame"\n', "")
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(invalid)

            with self.assertRaisesRegex(ConfigurationError, "repository is required"):
                load_config(path)

    def test_rejects_invalid_rotation_policy(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(CONFIG.replace('rotation_mode = "weighted"', 'rotation_mode = "chaos"'))

            with self.assertRaises(ConfigurationError):
                load_config(path)

    def test_loads_shuffle_bag_rotation_policy(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                CONFIG.replace('rotation_mode = "weighted"', 'rotation_mode = "shuffle_bag"')
            )

            config = load_config(path)

        self.assertEqual(config.display_node.rotation_mode, RotationMode.SHUFFLE_BAG)

    def test_loads_research_queue_and_notification_settings(self) -> None:
        configured = (
            CONFIG
            + """

[research]
enabled = true
max_searches_per_day = 4
max_searches_per_species = 1
allowed_domains = ["allaboutbirds.org", "audubon.org"]

[notifications]
enabled = true
degradation_failure_threshold = 2
degradation_window_minutes = 20
cooldown_minutes = 120
delivery_retry_minutes = 7
max_delivery_attempts = 9

[[notifications.destinations]]
name = "pushover"
url = "pover://user@token"
events = ["generation_approved", "terminal_error"]
"""
        ).replace(
            "max_generation_attempts = 3",
            """max_generation_attempts = 3
max_species_attempts_per_cycle = 8
retry_initial_minutes = 10
retry_max_minutes = 120
insufficient_references_retry_minutes = 1440""",
        )
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(configured)
            config = load_config(path)

        self.assertEqual(config.controller.max_species_attempts_per_cycle, 8)
        self.assertEqual(config.controller.insufficient_references_retry_minutes, 1440)
        self.assertEqual(config.research.max_searches_per_day, 4)
        self.assertEqual(config.research.allowed_domains, ("allaboutbirds.org", "audubon.org"))
        self.assertTrue(config.notifications.enabled)
        self.assertEqual(config.notifications.destinations[0].name, "pushover")
        self.assertEqual(
            config.notifications.destinations[0].events,
            (NotificationEvent.GENERATION_APPROVED, NotificationEvent.TERMINAL_ERROR),
        )

    def test_research_requires_two_distinct_allowed_domains(self) -> None:
        configured = (
            CONFIG
            + """

[research]
allowed_domains = ["allaboutbirds.org", "ALLABOUTBIRDS.ORG"]
"""
        )
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(configured)

            with self.assertRaisesRegex(ConfigurationError, "two distinct domains"):
                load_config(path)

    def test_research_domains_are_normalized_for_dns_matching(self) -> None:
        configured = (
            CONFIG
            + """

[research]
allowed_domains = ["ALLABOUTBIRDS.ORG", "Audubon.org"]
"""
        )
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(configured)
            config = load_config(path)

        self.assertEqual(
            config.research.allowed_domains,
            ("allaboutbirds.org", "audubon.org"),
        )

    def test_enabled_notifications_require_a_destination(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(CONFIG + "\n[notifications]\nenabled = true\n")

            with self.assertRaisesRegex(ConfigurationError, "destination"):
                load_config(path)

    def test_disabled_notifications_do_not_require_url_environment(self) -> None:
        configured = (
            CONFIG
            + """

[notifications]
enabled = false

[[notifications.destinations]]
name = "pushover"
url_env = "MISSING_NOTIFICATION_URL"
events = ["terminal_error"]
"""
        )
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(configured)
            with patch.dict("os.environ", {}, clear=True):
                config = load_config(path)

        self.assertFalse(config.notifications.enabled)
        self.assertEqual(config.notifications.destinations[0].url, "env://MISSING_NOTIFICATION_URL")
        self.assertEqual(config.notifications.destinations[0].url_env, "MISSING_NOTIFICATION_URL")


if __name__ == "__main__":
    unittest.main()
