from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout

from inky_bird_frame.cli import build_parser, main


class CliTests(unittest.TestCase):
    def test_status_requires_explicit_config(self) -> None:
        args = build_parser().parse_args(["status", "--config", "instance.toml"])

        self.assertEqual(str(args.config), "instance.toml")

    def test_catalog_publish_supports_dry_run(self) -> None:
        args = build_parser().parse_args(
            ["catalog-publish", "--config", "instance.toml", "--dry-run"]
        )

        self.assertEqual(str(args.config), "instance.toml")
        self.assertTrue(args.dry_run)

    def test_seed_supports_year_window_and_overrides(self) -> None:
        args = build_parser().parse_args(
            [
                "seed",
                "--config",
                "instance.toml",
                "--window",
                "last-year",
                "--radius-km",
                "16",
                "--species-limit",
                "500",
                "--dry-run",
            ]
        )

        self.assertEqual(args.window, "last-year")
        self.assertEqual(args.radius_km, 16)
        self.assertEqual(args.species_limit, 500)
        self.assertTrue(args.dry_run)

    def test_config_validation_and_notification_commands_require_config(self) -> None:
        validate = build_parser().parse_args(["config", "validate", "--config", "instance.toml"])
        status = build_parser().parse_args(["notifications", "status", "--config", "instance.toml"])
        test = build_parser().parse_args(["notifications", "test", "--config", "instance.toml"])
        dispatch = build_parser().parse_args(
            ["notifications", "dispatch", "--config", "instance.toml"]
        )
        retry = build_parser().parse_args(["notifications", "retry", "--config", "instance.toml"])

        self.assertEqual(str(validate.config), "instance.toml")
        self.assertEqual(str(status.config), "instance.toml")
        self.assertEqual(str(test.config), "instance.toml")
        self.assertEqual(str(dispatch.config), "instance.toml")
        self.assertEqual(str(retry.config), "instance.toml")

    def test_expected_error_uses_json_envelope(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = main(["status", "--config", "missing.toml"])

        self.assertEqual(exit_code, 1)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["error"]["type"], "ConfigurationError")


if __name__ == "__main__":
    unittest.main()
