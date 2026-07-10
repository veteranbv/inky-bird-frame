from __future__ import annotations

import io
import json
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from inky_bird_frame.cli import build_parser, generate_command, main, retry_command
from inky_bird_frame.controller import exclusive_cycle_lock
from inky_bird_frame.errors import DataSourceError, GenerationError


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

    def test_catalog_contribution_commands_use_explicit_catalog_paths(self) -> None:
        prepare = build_parser().parse_args(
            [
                "catalog",
                "prepare",
                "42",
                "--source-catalog",
                "approved",
                "--catalog",
                "catalog",
            ]
        )
        validate = build_parser().parse_args(
            [
                "catalog",
                "validate",
                "--catalog",
                "catalog",
                "--base-catalog",
                "base-catalog",
            ]
        )

        self.assertEqual(prepare.taxon_id, 42)
        self.assertEqual(str(prepare.source_catalog), "approved")
        self.assertEqual(str(prepare.catalog), "catalog")
        self.assertEqual(str(validate.catalog), "catalog")
        self.assertEqual(str(validate.base_catalog), "base-catalog")

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

    def test_generate_notifies_for_approved_pending_candidate(self) -> None:
        result = {
            "published_pending": [{"taxon_id": 1, "common_name": "Recovered Bird"}],
            "generated": [],
            "failures": [],
            "deferred_count": 0,
            "outstanding_retry_count": 0,
        }
        with (
            patch("inky_bird_frame.cli._config"),
            patch("inky_bird_frame.cli.run_generation_cycle", return_value=result),
            patch("inky_bird_frame.cli.safe_notify") as notify,
            patch("inky_bird_frame.cli.safe_record_recovery"),
            redirect_stdout(io.StringIO()),
        ):
            generate_command(Namespace())

        self.assertEqual(notify.call_count, 1)
        self.assertEqual(notify.call_args.kwargs["dedupe_key"], "1")

    def test_generate_does_not_recover_while_species_remain_deferred(self) -> None:
        result = {
            "published_pending": [],
            "generated": [],
            "failures": [],
            "deferred_count": 0,
            "outstanding_retry_count": 1,
        }
        with (
            patch("inky_bird_frame.cli._config"),
            patch("inky_bird_frame.cli.run_generation_cycle", return_value=result),
            patch("inky_bird_frame.cli.safe_record_recovery") as recover,
            redirect_stdout(io.StringIO()),
        ):
            generate_command(Namespace())

        recovered_keys = [call.kwargs["key"] for call in recover.call_args_list]
        self.assertEqual(recovered_keys, ["generation-cycle"])

    def test_retry_archives_cached_profile(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            failed = state_dir / "failed/42-example-bird"
            failed.mkdir(parents=True)
            profile = state_dir / "profiles/42/profile.json"
            profile.parent.mkdir(parents=True)
            profile.write_text("{}")
            config = SimpleNamespace(controller=SimpleNamespace(state_dir=state_dir))
            output = io.StringIO()

            with patch("inky_bird_frame.cli._config", return_value=config), redirect_stdout(output):
                retry_command(Namespace(taxon_id=42))

            result = json.loads(output.getvalue())["data"]
            profile_exists = profile.exists()
            archived_profile_exists = (state_dir / "archive/42/profile.json").exists()

        self.assertTrue(result["cleared_cached_profile"])
        self.assertFalse(profile_exists)
        self.assertTrue(archived_profile_exists)

    def test_retry_is_excluded_by_running_generation_cycle(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            config = SimpleNamespace(controller=SimpleNamespace(state_dir=state_dir))
            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                exclusive_cycle_lock(state_dir),
                self.assertRaisesRegex(GenerationError, "already running"),
            ):
                retry_command(Namespace(taxon_id=42))

    def test_refresh_failure_notification_redacts_exception_details(self) -> None:
        secret = "private ZIP and coordinates"
        with (
            patch("inky_bird_frame.cli._config"),
            patch(
                "inky_bird_frame.cli.run_refresh_cycle",
                side_effect=DataSourceError(secret),
            ),
            patch("inky_bird_frame.cli.safe_record_degradation") as degradation,
            self.assertRaises(DataSourceError),
        ):
            from inky_bird_frame.cli import refresh_command

            refresh_command(Namespace())

        body = degradation.call_args.kwargs["body"]
        self.assertNotIn(secret, body)
        self.assertIn("DataSourceError", body)


if __name__ == "__main__":
    unittest.main()
