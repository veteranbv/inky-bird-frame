from __future__ import annotations

import io
import json
import stat
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.cli import (
    build_parser,
    catalog_sync_command,
    config_install_command,
    generate_command,
    main,
    refresh_command,
    retry_command,
    seed_command,
    species_to_dict,
)
from inky_bird_frame.config import DiscoveryProvider
from inky_bird_frame.controller import exclusive_cycle_lock
from inky_bird_frame.errors import ConfigurationError, DataSourceError, GenerationError


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

    def test_catalog_sync_uses_explicit_catalog_paths(self) -> None:
        args = build_parser().parse_args(
            [
                "catalog",
                "sync",
                "--source-catalog",
                "bundled-catalog",
                "--catalog",
                "managed-catalog",
                "--state-dir",
                "controller-state",
            ]
        )

        self.assertEqual(str(args.source_catalog), "bundled-catalog")
        self.assertEqual(str(args.catalog), "managed-catalog")
        self.assertEqual(str(args.state_dir), "controller-state")

    def test_catalog_sync_uses_controller_catalog_lock(self) -> None:
        args = Namespace(
            source_catalog=Path("bundled-catalog"),
            catalog=Path("managed-catalog"),
            state_dir=Path("controller-state"),
        )
        with (
            patch("inky_bird_frame.cli.catalog_state_lock") as catalog_lock,
            patch(
                "inky_bird_frame.cli.sync_public_catalog",
                return_value={"published": [], "already_present": []},
            ) as sync,
            redirect_stdout(io.StringIO()),
        ):
            catalog_sync_command(args)

        catalog_lock.assert_called_once_with(Path("controller-state"))
        sync.assert_called_once_with(Path("bundled-catalog"), Path("managed-catalog"))

    def test_scheduler_requires_explicit_config(self) -> None:
        args = build_parser().parse_args(["scheduler", "--config", "instance.toml"])

        self.assertEqual(str(args.config), "instance.toml")

    def test_seed_supports_year_window_and_overrides(self) -> None:
        args = build_parser().parse_args(
            [
                "seed",
                "--config",
                "instance.toml",
                "--window",
                "last-year",
                "--source",
                "inaturalist",
                "--source",
                "ebird",
                "--radius-km",
                "16",
                "--species-limit",
                "500",
                "--dry-run",
            ]
        )

        self.assertEqual(args.window, "last-year")
        self.assertEqual(args.source, ["inaturalist", "ebird"])
        self.assertEqual(args.radius_km, 16)
        self.assertEqual(args.species_limit, 500)
        self.assertTrue(args.dry_run)

    def test_species_output_preserves_legacy_source(self) -> None:
        species = BirdSpecies(12942, "Eastern Bluebird", "Sialia sialis", 3, "iNaturalist")

        payload = species_to_dict(species)

        self.assertEqual(payload["source"], "iNaturalist")
        self.assertEqual(payload["sources"], ["iNaturalist"])

    def test_seed_rejects_duplicate_source_overrides(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not repeat"):
            seed_command(Namespace(source=["ebird", "ebird"]))

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

    def test_config_install_validates_and_atomically_writes_private_file(self) -> None:
        with TemporaryDirectory() as temporary:
            destination = Path(temporary) / "config.toml"
            config = """
[discovery]
zip_code = "12345"
radius_km = 8
species_limit = 50
window = "last-30-days"

[controller]
workspace_dir = "workspace"
catalog_dir = "catalog"
state_dir = "state"
codex_path = "codex"
bind_host = "0.0.0.0"
port = 8793
references_per_species = 4
generations_per_cycle = 1

[display_node]
controller_url = "http://controller.test:8793"
state_dir = "display-state"
rotation_mode = "shuffle_bag"
"""
            with patch("sys.stdin", io.StringIO(config)), redirect_stdout(io.StringIO()):
                config_install_command(Namespace(destination=destination))

            installed = destination.read_text()
            mode = stat.S_IMODE(destination.stat().st_mode)

        self.assertEqual(installed, config)
        self.assertEqual(mode, 0o600)

    def test_config_install_does_not_replace_destination_with_invalid_toml(self) -> None:
        with TemporaryDirectory() as temporary:
            destination = Path(temporary) / "config.toml"
            destination.write_text("existing")
            with (
                patch("sys.stdin", io.StringIO("not = [valid")),
                redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(ConfigurationError, "Invalid TOML"),
            ):
                config_install_command(Namespace(destination=destination))

            installed = destination.read_text()

        self.assertEqual(installed, "existing")

    def test_setup_and_doctor_have_role_specific_commands(self) -> None:
        setup = build_parser().parse_args(
            [
                "setup",
                "display",
                "--config",
                "instance.toml",
                "--source-dir",
                "/srv/inky-bird-frame",
                "--venv",
                "/opt/inky",
                "--yes",
            ]
        )
        doctor = build_parser().parse_args(["doctor", "controller", "--config", "instance.toml"])

        self.assertEqual(setup.role, "display")
        self.assertEqual(str(setup.source_dir), "/srv/inky-bird-frame")
        self.assertEqual(str(setup.venv), "/opt/inky")
        self.assertTrue(setup.yes)
        self.assertEqual(doctor.role, "controller")

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

    def test_refresh_does_not_clear_taxonomy_alert_when_ebird_fails(self) -> None:
        config = SimpleNamespace(
            discovery=SimpleNamespace(
                sources=(DiscoveryProvider.INATURALIST, DiscoveryProvider.EBIRD)
            ),
        )
        result = {
            "providers": [
                {"name": "inaturalist", "status": "ok"},
                {"name": "ebird", "status": "error"},
            ],
            "unresolved_species": [],
            "new_species": [],
        }
        with (
            patch("inky_bird_frame.cli._config", return_value=config),
            patch("inky_bird_frame.cli.run_refresh_cycle", return_value=result),
            patch("inky_bird_frame.cli.safe_record_degradation"),
            patch("inky_bird_frame.cli.safe_record_recovery") as recover,
            redirect_stdout(io.StringIO()),
        ):
            refresh_command(Namespace())

        recovered_keys = [call.kwargs["key"] for call in recover.call_args_list]
        self.assertNotIn("ebird-taxonomy", recovered_keys)

    def test_refresh_tracks_taxonomy_alerts_by_provider(self) -> None:
        config = SimpleNamespace(discovery=SimpleNamespace(sources=tuple(DiscoveryProvider)))
        result = {
            "providers": [
                {"name": "inaturalist", "status": "ok"},
                {"name": "ebird", "status": "ok"},
                {"name": "birdweather", "status": "ok"},
            ],
            "unresolved_species": [
                {
                    "provider": "birdweather",
                    "species_code": "42",
                    "common_name": "Split Bird",
                    "scientific_name": "Avis split",
                }
            ],
            "new_species": [],
        }
        with (
            patch("inky_bird_frame.cli._config", return_value=config),
            patch("inky_bird_frame.cli.run_refresh_cycle", return_value=result),
            patch("inky_bird_frame.cli.safe_record_degradation") as degradation,
            patch("inky_bird_frame.cli.safe_record_recovery") as recovery,
            redirect_stdout(io.StringIO()),
        ):
            refresh_command(Namespace())

        degraded_keys = [call.kwargs["key"] for call in degradation.call_args_list]
        recovered_keys = [call.kwargs["key"] for call in recovery.call_args_list]
        self.assertIn("birdweather-taxonomy", degraded_keys)
        self.assertNotIn("ebird-taxonomy", degraded_keys)
        self.assertIn("ebird-taxonomy", recovered_keys)
        self.assertNotIn("birdweather-taxonomy", recovered_keys)


if __name__ == "__main__":
    unittest.main()
