from __future__ import annotations

import io
import json
import shutil
import stat
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import UTC, date, datetime
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import inky_bird_frame
from inky_bird_frame.birds import BirdSpecies, DateRange
from inky_bird_frame.catalog import sha256_file
from inky_bird_frame.cli import (
    _confirm_birdbuddy_authorization,
    birdbuddy_login_command,
    birdbuddy_logout_command,
    build_parser,
    catalog_sync_command,
    config_install_command,
    generate_command,
    main,
    notifications_dispatch_command,
    refresh_command,
    retry_command,
    seed_command,
    serve_command,
    species_to_dict,
    status_command,
)
from inky_bird_frame.config import AppConfig, DiscoveryProvider
from inky_bird_frame.controller import (
    HUMAN_REVIEW_SOURCE,
    REVIEW_FAILURE_FALLBACK,
    exclusive_cycle_lock,
    read_generation_queue,
)
from inky_bird_frame.errors import (
    ConfigurationError,
    DataSourceError,
    GenerationError,
    SpeciesStateError,
)
from inky_bird_frame.retry import RetryStore


def controller_config(state_dir: Path, catalog_dir: Path | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        controller=SimpleNamespace(
            state_dir=state_dir,
            catalog_dir=catalog_dir if catalog_dir is not None else state_dir / "catalog",
        )
    )


def write_test_species_identity(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "profile.json").write_text(
        json.dumps(
            {
                "taxon_id": 42,
                "common_name": "Example Bird",
                "scientific_name": "Avis exemplum",
            }
        )
    )


class CliTests(unittest.TestCase):
    def test_birdbuddy_commands_parse_explicit_authorization_and_logout(self) -> None:
        login = build_parser().parse_args(
            [
                "birdbuddy",
                "login",
                "--config",
                "instance.toml",
                "--feeder-id",
                "feeder-1",
                "--confirm-authorized-access",
            ]
        )
        status = build_parser().parse_args(["birdbuddy", "status", "--config", "instance.toml"])
        logout = build_parser().parse_args(
            ["birdbuddy", "logout", "--config", "instance.toml", "--yes"]
        )

        self.assertTrue(login.confirm_authorized_access)
        self.assertEqual(login.feeder_id, "feeder-1")
        self.assertEqual(str(status.config), "instance.toml")
        self.assertTrue(logout.yes)

    def test_noninteractive_birdbuddy_login_fails_before_credentials(self) -> None:
        stdin = SimpleNamespace(isatty=lambda: False)
        with (
            patch("inky_bird_frame.cli.sys.stdin", stdin),
            self.assertRaisesRegex(DataSourceError, "confirmation is required"),
        ):
            _confirm_birdbuddy_authorization(False)

    def test_birdbuddy_login_uses_environment_without_echoing_credentials(self) -> None:
        args = Namespace(
            config=Path("instance.toml"),
            confirm_authorized_access=True,
            feeder_id="feeder-1",
        )
        config = controller_config(Path("state"))
        with (
            patch("inky_bird_frame.cli._config", return_value=config),
            patch.dict(
                "os.environ",
                {
                    "INKY_BIRDBUDDY_EMAIL": "birder@example.test",
                    "INKY_BIRDBUDDY_PASSWORD": "private-password",
                },
                clear=True,
            ),
            patch(
                "inky_bird_frame.cli.login_birdbuddy",
                return_value={"authenticated": True},
            ) as login,
            redirect_stdout(io.StringIO()) as output,
        ):
            result = birdbuddy_login_command(args)

        self.assertEqual(result, 0)
        login.assert_called_once_with(
            Path("state"),
            email="birder@example.test",
            password="private-password",
            authorization_confirmed=True,
            feeder_id="feeder-1",
        )
        self.assertNotIn("private-password", output.getvalue())
        self.assertNotIn("birder@example.test", output.getvalue())

    def test_birdbuddy_logout_requires_yes_before_loading_config(self) -> None:
        with (
            patch("inky_bird_frame.cli._config") as load,
            self.assertRaisesRegex(DataSourceError, "requires --yes"),
        ):
            birdbuddy_logout_command(Namespace(yes=False, config=Path("instance.toml")))

        load.assert_not_called()

    def test_runtime_version_matches_package_metadata(self) -> None:
        self.assertEqual(inky_bird_frame.__version__, version("inky-bird-frame"))

    def test_status_requires_explicit_config(self) -> None:
        args = build_parser().parse_args(["status", "--config", "instance.toml"])

        self.assertEqual(str(args.config), "instance.toml")

    def test_catalog_publish_supports_dry_run(self) -> None:
        args = build_parser().parse_args(
            ["catalog-publish", "--config", "instance.toml", "--dry-run"]
        )

        self.assertEqual(str(args.config), "instance.toml")
        self.assertTrue(args.dry_run)

    def test_retry_parses_explicit_approved_replacement(self) -> None:
        args = build_parser().parse_args(
            [
                "retry",
                "42",
                "--config",
                "instance.toml",
                "--replace-approved",
                "--reason",
                "Human review rejected the plate.",
            ]
        )

        self.assertTrue(args.replace_approved)
        self.assertEqual(args.reason, "Human review rejected the plate.")
        self.assertFalse(args.refresh_research)

    def test_retry_parses_explicit_research_refresh(self) -> None:
        args = build_parser().parse_args(
            ["retry", "42", "--config", "instance.toml", "--refresh-research"]
        )

        self.assertTrue(args.refresh_research)

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

    def test_serve_loads_config_without_resolving_private_environment(self) -> None:
        controller = SimpleNamespace()
        args = Namespace(config=Path("instance.toml"))
        with (
            patch(
                "inky_bird_frame.cli.load_config",
                return_value=SimpleNamespace(controller=controller),
            ) as load,
            patch("inky_bird_frame.cli.serve_catalog") as serve,
        ):
            result = serve_command(args)

        self.assertEqual(result, 0)
        load.assert_called_once_with(Path("instance.toml"), load_secrets=False)
        serve.assert_called_once_with(controller)

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

    def test_collection_commands_support_explicit_preview_and_mutation(self) -> None:
        listed = build_parser().parse_args(["collection", "list", "--config", "instance.toml"])
        imported = build_parser().parse_args(
            [
                "collection",
                "import-approved",
                "--config",
                "instance.toml",
                "--dry-run",
            ]
        )
        added = build_parser().parse_args(["collection", "add", "42", "--config", "instance.toml"])
        removed = build_parser().parse_args(
            ["collection", "remove", "42", "--config", "instance.toml", "--dry-run"]
        )

        self.assertEqual(str(listed.config), "instance.toml")
        self.assertTrue(imported.dry_run)
        self.assertEqual(added.taxon_id, 42)
        self.assertFalse(added.dry_run)
        self.assertEqual(removed.taxon_id, 42)
        self.assertTrue(removed.dry_run)

    def test_status_separates_terminal_blocked_queue_entries(self) -> None:
        actionable = BirdSpecies(1, "Ready Bird", "Avis parata", 2, "iNaturalist")
        blocked = {
            "taxon_id": 2,
            "common_name": "Blocked Bird",
            "terminal_state": "failed",
            "paths": ["state/failed/2-blocked-bird"],
        }
        queue = SimpleNamespace(
            actionable=[actionable],
            terminal_blocked=[SimpleNamespace(as_dict=lambda: blocked)],
        )
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            config = SimpleNamespace(
                controller=SimpleNamespace(catalog_dir=state / "catalog", state_dir=state)
            )
            output = io.StringIO()
            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                patch("inky_bird_frame.cli.rebuild_catalog_index", return_value=[]),
                patch("inky_bird_frame.cli.read_generation_queue_partition", return_value=queue),
                patch(
                    "inky_bird_frame.cli.collection_status",
                    return_value={"collection_count": 0, "members": []},
                ),
                redirect_stdout(output),
            ):
                status_command(Namespace())

        payload = json.loads(output.getvalue())["data"]
        self.assertEqual([entry["taxon_id"] for entry in payload["queued"]], [1])
        self.assertEqual(payload["terminal_blocked"], [blocked])
        self.assertEqual(payload["collection"], {"collection_count": 0})

    def test_seed_supports_historical_dates_and_coordinates(self) -> None:
        args = build_parser().parse_args(
            [
                "seed",
                "--config",
                "instance.toml",
                "--start-date",
                "2026-07-13",
                "--end-date",
                "2026-07-16",
                "--source",
                "inaturalist",
                "--latitude",
                "33.6407",
                "--longitude",
                "-84.4277",
                "--radius-km",
                "11",
                "--dry-run",
            ]
        )
        with (
            patch("inky_bird_frame.cli._config", return_value=SimpleNamespace()),
            patch("inky_bird_frame.cli.enqueue_seed_species", return_value={}) as enqueue,
            redirect_stdout(io.StringIO()),
        ):
            seed_command(args)

        self.assertEqual(
            enqueue.call_args.kwargs["date_range"],
            DateRange(start=date(2026, 7, 13), end=date(2026, 7, 16)),
        )
        self.assertEqual(enqueue.call_args.kwargs["latitude"], 33.6407)
        self.assertEqual(enqueue.call_args.kwargs["longitude"], -84.4277)
        self.assertIsNone(enqueue.call_args.kwargs["window"])

    def test_seed_rejects_incomplete_date_or_coordinate_pairs(self) -> None:
        common = {
            "source": ["inaturalist"],
            "window": None,
            "start_date": "2026-07-13",
            "end_date": None,
            "latitude": 33.6407,
            "longitude": -84.4277,
        }
        with self.assertRaisesRegex(ValueError, "start-date and --end-date"):
            seed_command(Namespace(**common))

        common.update(end_date="2026-07-16", longitude=None)
        with self.assertRaisesRegex(ValueError, "latitude and --longitude"):
            seed_command(Namespace(**common))

    def test_species_output_preserves_legacy_source(self) -> None:
        species = BirdSpecies(12942, "Eastern Bluebird", "Sialia sialis", 3, "iNaturalist")

        payload = species_to_dict(species)

        self.assertEqual(payload["source"], "iNaturalist")
        self.assertEqual(payload["sources"], ["iNaturalist"])
        self.assertNotIn("latest_detection_at", payload)

    def test_species_output_includes_latest_detection_timestamp(self) -> None:
        timestamp = "2026-08-02T14:21:06-04:00"
        species = BirdSpecies(
            145310,
            "American Goldfinch",
            "Spinus tristis",
            3,
            "BirdWeather",
            latest_detection_at=timestamp,
        )

        payload = species_to_dict(species)

        self.assertEqual(payload["latest_detection_at"], timestamp)

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

    def test_retry_parser_accepts_selected_source_attempt(self) -> None:
        retry = build_parser().parse_args(
            ["retry", "42", "--source-attempt", "3", "--config", "instance.toml"]
        )

        self.assertEqual(retry.taxon_id, 42)
        self.assertEqual(retry.source_attempt, 3)
        self.assertEqual(str(retry.config), "instance.toml")

    def test_retry_parser_accepts_archived_source_run(self) -> None:
        retry = build_parser().parse_args(
            [
                "retry",
                "42",
                "--source-run",
                "42-20260803T060207Z",
                "--source-attempt",
                "3",
                "--config",
                "instance.toml",
            ]
        )

        self.assertEqual(retry.taxon_id, 42)
        self.assertEqual(retry.source_run, "42-20260803T060207Z")
        self.assertEqual(retry.source_attempt, 3)
        self.assertEqual(str(retry.config), "instance.toml")

    def test_retry_parser_accepts_operator_corrections(self) -> None:
        retry = build_parser().parse_args(
            [
                "retry",
                "42",
                "--source-attempt",
                "2",
                "--correction",
                "Correct only the ruler text.",
                "--correction",
                "Preserve the accepted bill.",
                "--config",
                "instance.toml",
            ]
        )

        self.assertEqual(
            retry.correction,
            ["Correct only the ruler text.", "Preserve the accepted bill."],
        )

    def test_retry_parser_accepts_selected_source_candidate(self) -> None:
        retry = build_parser().parse_args(
            [
                "retry",
                "42",
                "--source-candidate",
                "42-example-bird-2",
                "--config",
                "instance.toml",
            ]
        )

        self.assertEqual(retry.taxon_id, 42)
        self.assertEqual(retry.source_candidate, "42-example-bird-2")
        self.assertEqual(str(retry.config), "instance.toml")

    def test_notifications_dispatch_checks_display_heartbeat(self) -> None:
        output = io.StringIO()
        with (
            patch("inky_bird_frame.cli._config"),
            patch(
                "inky_bird_frame.cli.dispatch_notifications",
                return_value={"attempted": 0},
            ) as dispatch,
            patch(
                "inky_bird_frame.cli.check_display_heartbeat",
                return_value={"checked": False, "stale": None},
            ) as check,
            redirect_stdout(output),
        ):
            notifications_dispatch_command(Namespace())

        payload = json.loads(output.getvalue())["data"]
        self.assertEqual(check.call_count, 1)
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(payload["display_heartbeat"], {"checked": False, "stale": None})
        self.assertEqual(payload["attempted"], 0)

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

    def test_retry_preserves_cached_research_by_default(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            failed = state_dir / "failed/42-example-bird"
            review = failed / "attempt-01/quality-review.json"
            review.parent.mkdir(parents=True)
            review.write_text(json.dumps({"passed": False, "findings": ["Fix the feet"]}))
            write_test_species_identity(failed)
            profile = state_dir / "profiles/42/profile.json"
            profile.parent.mkdir(parents=True)
            profile.write_text("{}")
            references = state_dir / "references/42/references.json"
            references.parent.mkdir(parents=True)
            references.write_text("{}")
            config = controller_config(state_dir)
            output = io.StringIO()

            with patch("inky_bird_frame.cli._config", return_value=config), redirect_stdout(output):
                retry_command(Namespace(taxon_id=42))

            result = json.loads(output.getvalue())["data"]
            profile_exists = profile.exists()
            references_exist = references.exists()

        self.assertFalse(result["cleared_cached_profile"])
        self.assertFalse(result["cleared_cached_references"])
        self.assertTrue(profile_exists)
        self.assertTrue(references_exist)

    def test_retry_archives_cached_research_when_refresh_requested(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            failed = state_dir / "failed/42-example-bird"
            failed.mkdir(parents=True)
            write_test_species_identity(failed)
            old_review = failed / "attempt-99/quality-review.json"
            old_review.parent.mkdir()
            old_review.write_text(json.dumps({"passed": False, "findings": ["Outdated finding"]}))
            review = failed / "attempt-100/quality-review.json"
            review.parent.mkdir()
            review.write_text(
                json.dumps({"passed": False, "findings": ["Correct the ruler scale"]})
            )
            profile = state_dir / "profiles/42/profile.json"
            profile.parent.mkdir(parents=True)
            profile.write_text("{malformed")
            references = state_dir / "references/42/references.json"
            references.parent.mkdir(parents=True)
            references.write_text("{}")
            config = controller_config(state_dir)
            output = io.StringIO()

            with patch("inky_bird_frame.cli._config", return_value=config), redirect_stdout(output):
                retry_command(Namespace(taxon_id=42, refresh_research=True))

            result = json.loads(output.getvalue())["data"]
            guidance = RetryStore(state_dir / "generation-retries.json").quality_guidance(42)
            profile_exists = profile.exists() or references.exists()
            archived_profile_exists = (state_dir / "archive/42/profile.json").exists()

        self.assertTrue(result["cleared_cached_profile"])
        self.assertTrue(result["cleared_cached_references"])
        self.assertFalse(profile_exists)
        self.assertTrue(archived_profile_exists)
        self.assertEqual(result["preserved_quality_findings_count"], 1)
        self.assertIsNotNone(guidance)
        if guidance is not None:
            self.assertEqual(guidance.findings, ("Correct the ruler scale",))

    def test_retry_allows_recovery_past_incomplete_catalog_directory(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            failed = state_dir / "failed/42-example-bird"
            failed.mkdir(parents=True)
            write_test_species_identity(failed)
            catalog_dir = state_dir / "catalog"
            debris = catalog_dir / "species/42-example-bird"
            debris.mkdir(parents=True)
            (debris / "portrait.png").write_bytes(b"incomplete approval")
            (debris / "manifest.json").write_text(
                json.dumps({"taxon_id": 42, "status": "approved"})
            )
            config = controller_config(state_dir, catalog_dir)
            output = io.StringIO()

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                redirect_stdout(output),
            ):
                retry_command(Namespace(taxon_id=42))

            archived = (state_dir / "archive/42-example-bird").is_dir()
            invalid_archived = (state_dir / "archive/invalid-approved-42-example-bird").is_dir()
            debris_exists = debris.exists()
            result = json.loads(output.getvalue())["data"]

        self.assertTrue(archived)
        self.assertTrue(invalid_archived)
        self.assertFalse(debris_exists)
        self.assertEqual(len(result["archived"]), 2)

    def test_retry_preserves_fallback_for_empty_quality_findings(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            review = state_dir / "failed/42-example-bird/attempt-01/quality-review.json"
            review.parent.mkdir(parents=True)
            review.write_text(json.dumps({"passed": False, "findings": []}))
            write_test_species_identity(review.parent.parent)
            config = controller_config(state_dir)
            output = io.StringIO()

            with patch("inky_bird_frame.cli._config", return_value=config), redirect_stdout(output):
                retry_command(Namespace(taxon_id=42))

            result = json.loads(output.getvalue())["data"]
            guidance = RetryStore(state_dir / "generation-retries.json").quality_guidance(42)

        self.assertEqual(result["preserved_quality_findings_count"], 1)
        self.assertIsNotNone(guidance)
        if guidance is not None:
            self.assertEqual(guidance.findings, (REVIEW_FAILURE_FALLBACK,))

    def test_retry_preserves_guidance_when_clearing_only_deferred_backoff(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            state_dir.mkdir(parents=True, exist_ok=True)
            store = RetryStore(state_dir / "generation-retries.json")
            store.record_failure(
                42,
                GenerationError("transient failure"),
                now=datetime(2026, 8, 2, tzinfo=UTC),
                initial_minutes=5,
                maximum_minutes=60,
                species=BirdSpecies(42, "Deferred Bird", "Avis dilata", 1, "eBird"),
            )
            expected = store.set_quality_guidance(
                42,
                ("Keep the visible eye correction.",),
                source_plate="archive/42-source/portrait.png",
                invariant_findings=("Keep the visible eye correction.",),
            )
            config = controller_config(state_dir)
            output = io.StringIO()

            with patch("inky_bird_frame.cli._config", return_value=config), redirect_stdout(output):
                retry_command(Namespace(taxon_id=42))

            reloaded = RetryStore(state_dir / "generation-retries.json")
            guidance = reloaded.quality_guidance(42)
            retry = reloaded.get(42)
            result = json.loads(output.getvalue())["data"]

        self.assertIsNone(retry)
        self.assertEqual(guidance, expected)
        self.assertEqual(result["preserved_quality_findings_count"], 1)
        self.assertTrue(result["preserved_correction_source"])

    def test_retry_reads_findings_from_legacy_null_correction_field(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            review = state_dir / "failed/42-example-bird/attempt-01/quality-review.json"
            review.parent.mkdir(parents=True)
            review.write_text(
                json.dumps(
                    {
                        "passed": False,
                        "findings": ["Correct the measurement ranges"],
                        "correction_findings": None,
                    }
                )
            )
            write_test_species_identity(review.parent.parent)
            config = controller_config(state_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                redirect_stdout(io.StringIO()),
            ):
                retry_command(Namespace(taxon_id=42))

            guidance = RetryStore(state_dir / "generation-retries.json").quality_guidance(42)

        self.assertIsNotNone(guidance)
        if guidance is not None:
            self.assertEqual(guidance.findings, ("Correct the measurement ranges",))

    def test_retry_preserves_incomplete_pending_candidate_without_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            pending = state_dir / "pending/42-example-bird"
            pending.mkdir(parents=True)
            (pending / "portrait.png").write_bytes(b"partial")
            config = controller_config(state_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                self.assertRaisesRegex(SpeciesStateError, "no recoverable species identity"),
            ):
                retry_command(Namespace(taxon_id=42))

            archived = state_dir / "archive/42-example-bird"
            pending_exists = pending.exists()

        self.assertTrue(pending_exists)
        self.assertFalse(archived.exists())

    def test_retry_rejects_complete_pending_candidate(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            pending = state_dir / "pending/42-example-bird"
            pending.mkdir(parents=True)
            (pending / "manifest.json").write_text("{}")
            config = controller_config(state_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                self.assertRaisesRegex(ValueError, "must be approved or rejected"),
            ):
                retry_command(Namespace(taxon_id=42))
            pending_exists = pending.is_dir()

        self.assertTrue(pending_exists)

    def test_retry_requires_explicit_replacement_for_approved_candidate(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            catalog_dir = state_dir / "catalog"
            approved = catalog_dir / "species/42-example-bird"
            approved.mkdir(parents=True)
            config = controller_config(state_dir, catalog_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                patch(
                    "inky_bird_frame.cli.has_valid_approved_candidate",
                    return_value=True,
                ),
                self.assertRaisesRegex(ValueError, "use --replace-approved"),
            ):
                retry_command(Namespace(taxon_id=42))

            approved_exists = approved.is_dir()

        self.assertTrue(approved_exists)

    def test_retry_approved_replacement_requires_reason_and_fresh_source(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            config = controller_config(state_dir)
            with patch("inky_bird_frame.cli._config", return_value=config):
                with self.assertRaisesRegex(ValueError, "requires a non-empty --reason"):
                    retry_command(
                        Namespace(
                            taxon_id=42,
                            replace_approved=True,
                            reason=" ",
                            source_attempt=None,
                        )
                    )
                with self.assertRaisesRegex(ValueError, "cannot be combined"):
                    retry_command(
                        Namespace(
                            taxon_id=42,
                            replace_approved=True,
                            reason="Human review rejected the plate.",
                            source_attempt=1,
                        )
                    )

    def test_retry_withdraws_approved_candidate_and_starts_from_scratch(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            config = controller_config(state_dir)
            expected = {
                "taxon_id": 42,
                "status": "eligible",
                "replaced_approved": True,
                "queued_for_generation": True,
            }
            output = io.StringIO()

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                patch(
                    "inky_bird_frame.cli.retry_approved_candidate",
                    return_value=expected,
                ) as replace,
                redirect_stdout(output),
            ):
                retry_command(
                    Namespace(
                        taxon_id=42,
                        replace_approved=True,
                        reason="Human review rejected the eyes.",
                        source_attempt=None,
                    )
                )

            result = json.loads(output.getvalue())["data"]

        self.assertEqual(result, expected)
        replace.assert_called_once_with(
            config,
            42,
            "Human review rejected the eyes.",
            refresh_research=False,
        )

    def test_retry_preserves_selected_attempt_as_correction_source(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            failed = state_dir / "failed/42-example-bird"
            for attempt, correction in ((1, "Fix the tail"), (2, "Fix the wing")):
                attempt_dir = failed / f"attempt-{attempt:02d}"
                attempt_dir.mkdir(parents=True)
                (attempt_dir / "portrait.png").write_bytes(f"portrait-{attempt}".encode())
                (attempt_dir / "quality-review.json").write_text(
                    json.dumps(
                        {
                            "passed": False,
                            "findings": ["Verified correct trait", correction],
                            "correction_findings": [correction],
                        }
                    )
                )
            (failed / "profile.json").write_text(
                json.dumps(
                    {
                        "taxon_id": 42,
                        "common_name": "Example Bird",
                        "scientific_name": "Avis exemplum",
                    }
                )
            )
            config = controller_config(state_dir)
            output = io.StringIO()

            with patch("inky_bird_frame.cli._config", return_value=config), redirect_stdout(output):
                retry_command(Namespace(taxon_id=42, source_attempt=1))

            result = json.loads(output.getvalue())["data"]
            guidance = RetryStore(state_dir / "generation-retries.json").quality_guidance(42)
            source_bytes = (
                (state_dir / guidance.source_plate).read_bytes()
                if guidance is not None and guidance.source_plate is not None
                else None
            )
            queue = read_generation_queue(cast(AppConfig, config))
            collection = json.loads((state_dir / "collection.json").read_text())

        self.assertTrue(result["preserved_correction_source"])
        self.assertTrue(result["queued_for_generation"])
        self.assertEqual(result["source_attempt"], 1)
        self.assertIsNotNone(guidance)
        if guidance is not None:
            self.assertEqual(guidance.findings, ("Fix the tail",))
            self.assertIsNotNone(guidance.source_plate)
        self.assertEqual(source_bytes, b"portrait-1")
        self.assertEqual([item.taxon_id for item in queue], [42])
        self.assertEqual(queue[0].source, "human-review")
        self.assertEqual(collection["taxa"], [])
        self.assertIsNotNone(collection["legacy_seed_queue_migrated_at"])

    def test_retry_refreshes_stale_queued_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            failed = state_dir / "failed/42-example-bird"
            attempt = failed / "attempt-01"
            attempt.mkdir(parents=True)
            (attempt / "portrait.png").write_bytes(b"strong source")
            (attempt / "quality-review.json").write_text(
                json.dumps(
                    {
                        "passed": False,
                        "findings": ["Correct the ruler"],
                        "correction_findings": ["Correct the ruler"],
                    }
                )
            )
            (failed / "profile.json").write_text(
                json.dumps(
                    {
                        "taxon_id": 42,
                        "common_name": "Current Bird Name",
                        "scientific_name": "Avis current",
                    }
                )
            )
            (state_dir / "generation-queue.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "species": [
                            {
                                "taxon_id": 42,
                                "common_name": "Old Bird Name",
                                "scientific_name": "Avis old",
                                "observation_count": 1,
                                "source": "iNaturalist",
                                "sources": ["iNaturalist"],
                            }
                        ],
                    }
                )
            )
            config = controller_config(state_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                redirect_stdout(io.StringIO()),
            ):
                retry_command(Namespace(taxon_id=42, source_attempt=1))

            queue = read_generation_queue(cast(AppConfig, config))

        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0].common_name, "Current Bird Name")
        self.assertEqual(queue[0].scientific_name, "Avis current")
        self.assertEqual(queue[0].source, "human-review")

    def test_retry_preserves_current_observed_identity_after_observation_expires(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            failed = state_dir / "failed/42-old-bird-name"
            failed.mkdir(parents=True)
            (failed / "profile.json").write_text(
                json.dumps(
                    {
                        "taxon_id": 42,
                        "common_name": "Old Bird Name",
                        "scientific_name": "Avis old",
                    }
                )
            )
            cached_profile = state_dir / "profiles/42/profile.json"
            cached_profile.parent.mkdir(parents=True)
            cached_profile.write_text(
                json.dumps(
                    {
                        "taxon_id": 42,
                        "common_name": "Old Bird Name",
                        "scientific_name": "Avis old",
                    }
                )
            )
            discovery = state_dir / "discovery.json"
            discovery.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "refreshed_at": "2026-08-05T12:00:00+00:00",
                        "place_name": "Exampleville",
                        "state": "XY",
                        "species": [
                            {
                                "taxon_id": 42,
                                "common_name": "Current Bird Name",
                                "scientific_name": "Avis current",
                                "observation_count": 1,
                                "source": "eBird",
                                "sources": ["eBird"],
                            }
                        ],
                    }
                )
            )
            config = controller_config(state_dir)
            output = io.StringIO()

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                redirect_stdout(output),
            ):
                retry_command(Namespace(taxon_id=42))

            discovery.unlink()
            queue = read_generation_queue(cast(AppConfig, config))
            result = json.loads(output.getvalue())["data"]
            archived_profile = state_dir / "archive/42/profile.json"
            cached_profile_exists = cached_profile.exists()
            archived_profile_exists = archived_profile.is_file()

        self.assertEqual([item.taxon_id for item in queue], [42])
        self.assertEqual(queue[0].common_name, "Current Bird Name")
        self.assertEqual(queue[0].scientific_name, "Avis current")
        self.assertEqual(queue[0].source, "human-review")
        self.assertTrue(result["cleared_cached_profile"])
        self.assertFalse(cached_profile_exists)
        self.assertTrue(archived_profile_exists)

    def test_retry_prefers_refreshed_human_review_queue_over_terminal_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            failed = state_dir / "failed/42-old-bird-name"
            failed.mkdir(parents=True)
            (failed / "profile.json").write_text(
                json.dumps(
                    {
                        "taxon_id": 42,
                        "common_name": "Old Bird Name",
                        "scientific_name": "Avis old",
                    }
                )
            )
            (state_dir / "generation-queue.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "species": [
                            {
                                "taxon_id": 42,
                                "common_name": "Current Bird Name",
                                "scientific_name": "Avis current",
                                "observation_count": 0,
                                "source": HUMAN_REVIEW_SOURCE,
                                "sources": [HUMAN_REVIEW_SOURCE],
                            }
                        ],
                    }
                )
            )
            config = controller_config(state_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                redirect_stdout(io.StringIO()),
            ):
                retry_command(Namespace(taxon_id=42))

            queue = read_generation_queue(cast(AppConfig, config))

        self.assertEqual([item.taxon_id for item in queue], [42])
        self.assertEqual(queue[0].common_name, "Current Bird Name")
        self.assertEqual(queue[0].scientific_name, "Avis current")
        self.assertEqual(queue[0].source, HUMAN_REVIEW_SOURCE)

    def test_retry_uses_cached_profile_for_expired_deferred_observation(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            profile = state_dir / "profiles/42/profile.json"
            profile.parent.mkdir(parents=True)
            profile.write_text(
                json.dumps(
                    {
                        "taxon_id": 42,
                        "common_name": "Deferred Bird",
                        "scientific_name": "Avis dilata",
                    }
                )
            )
            store = RetryStore(state_dir / "generation-retries.json")
            store.record_failure(
                42,
                GenerationError("transient failure"),
                now=datetime(2026, 8, 2, tzinfo=UTC),
                initial_minutes=5,
                maximum_minutes=60,
            )
            config = controller_config(state_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                redirect_stdout(io.StringIO()),
            ):
                retry_command(Namespace(taxon_id=42))

            queue = read_generation_queue(cast(AppConfig, config))
            profile_exists = profile.is_file()

        self.assertEqual([item.taxon_id for item in queue], [42])
        self.assertEqual(queue[0].common_name, "Deferred Bird")
        self.assertTrue(profile_exists)

    def test_retry_uses_reference_manifest_for_legacy_deferred_observation(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            references = state_dir / "references/42/references.json"
            references.parent.mkdir(parents=True)
            references.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "taxon_id": 42,
                        "common_name": "Referenced Bird",
                        "scientific_name": "Avis relata",
                        "references": [],
                    }
                )
            )
            RetryStore(state_dir / "generation-retries.json").record_failure(
                42,
                GenerationError("legacy transient failure"),
                now=datetime(2026, 8, 2, tzinfo=UTC),
                initial_minutes=5,
                maximum_minutes=60,
            )
            config = controller_config(state_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                redirect_stdout(io.StringIO()),
            ):
                retry_command(Namespace(taxon_id=42))

            queue = read_generation_queue(cast(AppConfig, config))

        self.assertEqual([item.taxon_id for item in queue], [42])
        self.assertEqual(queue[0].common_name, "Referenced Bird")
        self.assertEqual(queue[0].scientific_name, "Avis relata")

    def test_retry_refreshes_references_after_recovering_legacy_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            reference_cache = state_dir / "references/42"
            reference_cache.mkdir(parents=True)
            (reference_cache / "references.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "taxon_id": 42,
                        "common_name": "Referenced Bird",
                        "scientific_name": "Avis relata",
                        "references": [],
                    }
                )
            )
            profile_cache = state_dir / "profiles/42"
            profile_cache.mkdir(parents=True)
            (profile_cache / "profile.json").write_text("[]")
            RetryStore(state_dir / "generation-retries.json").record_failure(
                42,
                GenerationError("legacy transient failure"),
                now=datetime(2026, 8, 2, tzinfo=UTC),
                initial_minutes=5,
                maximum_minutes=60,
            )
            config = controller_config(state_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                redirect_stdout(io.StringIO()),
            ):
                retry_command(Namespace(taxon_id=42, refresh_research=True))

            queue = read_generation_queue(cast(AppConfig, config))
            archived_references = state_dir / "archive/42-1/references.json"
            archived_references_exists = archived_references.is_file()
            archived_profile_exists = (state_dir / "archive/42/profile.json").is_file()
            reference_cache_exists = reference_cache.exists()
            profile_cache_exists = profile_cache.exists()

        self.assertEqual([item.taxon_id for item in queue], [42])
        self.assertEqual(queue[0].common_name, "Referenced Bird")
        self.assertEqual(queue[0].scientific_name, "Avis relata")
        self.assertTrue(archived_references_exists)
        self.assertTrue(archived_profile_exists)
        self.assertFalse(reference_cache_exists)
        self.assertFalse(profile_cache_exists)

    def test_retry_uses_deferred_record_identity_when_profile_was_not_created(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            species = BirdSpecies(42, "Early Failure Bird", "Avis immatura", 1, "eBird")
            store = RetryStore(state_dir / "generation-retries.json")
            store.record_failure(
                42,
                GenerationError("reference lookup failed"),
                now=datetime(2026, 8, 2, tzinfo=UTC),
                initial_minutes=5,
                maximum_minutes=60,
                species=species,
            )
            config = controller_config(state_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                redirect_stdout(io.StringIO()),
            ):
                retry_command(Namespace(taxon_id=42))

            queue = read_generation_queue(cast(AppConfig, config))
            retry_record = RetryStore(state_dir / "generation-retries.json").get(42)

        self.assertEqual([item.taxon_id for item in queue], [42])
        self.assertEqual(queue[0].common_name, "Early Failure Bird")
        self.assertEqual(queue[0].scientific_name, "Avis immatura")
        self.assertIsNone(retry_record)

    def test_retry_retains_legacy_deferred_record_without_recoverable_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            store = RetryStore(state_dir / "generation-retries.json")
            expected = store.record_failure(
                42,
                GenerationError("reference lookup failed"),
                now=datetime(2026, 8, 2, tzinfo=UTC),
                initial_minutes=5,
                maximum_minutes=60,
            )
            config = controller_config(state_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                self.assertRaisesRegex(SpeciesStateError, "no recoverable species identity"),
            ):
                retry_command(Namespace(taxon_id=42))

            retained = RetryStore(state_dir / "generation-retries.json").get(42)
            queue = read_generation_queue(cast(AppConfig, config))

        self.assertEqual(retained, expected)
        self.assertEqual(queue, [])

    def test_retry_prefers_deferred_record_identity_over_stale_caches(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            profile = state_dir / "profiles/42/profile.json"
            profile.parent.mkdir(parents=True)
            profile.write_text(
                json.dumps(
                    {
                        "taxon_id": 42,
                        "common_name": "Old Profile Name",
                        "scientific_name": "Avis profile",
                    }
                )
            )
            references = state_dir / "references/42/references.json"
            references.parent.mkdir(parents=True)
            references.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "taxon_id": 42,
                        "common_name": "Old Reference Name",
                        "scientific_name": "Avis reference",
                        "references": [],
                    }
                )
            )
            species = BirdSpecies(42, "Current Bird Name", "Avis current", 1, "eBird")
            RetryStore(state_dir / "generation-retries.json").record_failure(
                42,
                GenerationError("reference lookup failed"),
                now=datetime(2026, 8, 2, tzinfo=UTC),
                initial_minutes=5,
                maximum_minutes=60,
                species=species,
            )
            config = controller_config(state_dir)
            output = io.StringIO()

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                redirect_stdout(output),
            ):
                retry_command(Namespace(taxon_id=42))

            queue = read_generation_queue(cast(AppConfig, config))
            result = json.loads(output.getvalue())["data"]
            profile_exists = profile.exists()
            archived_profile_exists = (state_dir / "archive/42/profile.json").is_file()

        self.assertEqual([item.taxon_id for item in queue], [42])
        self.assertEqual(queue[0].common_name, "Current Bird Name")
        self.assertEqual(queue[0].scientific_name, "Avis current")
        self.assertTrue(result["cleared_cached_profile"])
        self.assertFalse(profile_exists)
        self.assertTrue(archived_profile_exists)

    def test_retry_prefers_deferred_identity_over_non_human_queue(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            (state_dir / "generation-queue.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "species": [
                            {
                                "taxon_id": 42,
                                "common_name": "Old Seed Name",
                                "scientific_name": "Avis old",
                                "observation_count": 1,
                                "source": "eBird",
                                "sources": ["eBird"],
                            }
                        ],
                    }
                )
            )
            current_species = BirdSpecies(42, "Current Bird Name", "Avis current", 1, "eBird")
            RetryStore(state_dir / "generation-retries.json").record_failure(
                42,
                GenerationError("temporary failure"),
                now=datetime(2026, 8, 2, tzinfo=UTC),
                initial_minutes=5,
                maximum_minutes=60,
                species=current_species,
            )
            profile = state_dir / "profiles/42/profile.json"
            profile.parent.mkdir(parents=True)
            profile.write_text(
                json.dumps(
                    {
                        "taxon_id": 42,
                        "common_name": current_species.common_name,
                        "scientific_name": current_species.scientific_name,
                    }
                )
            )
            config = controller_config(state_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                redirect_stdout(io.StringIO()),
            ):
                retry_command(Namespace(taxon_id=42))

            queue = read_generation_queue(cast(AppConfig, config))
            profile_exists = profile.is_file()

        self.assertEqual([item.taxon_id for item in queue], [42])
        self.assertEqual(queue[0].common_name, current_species.common_name)
        self.assertEqual(queue[0].scientific_name, current_species.scientific_name)
        self.assertEqual(queue[0].source, HUMAN_REVIEW_SOURCE)
        self.assertTrue(profile_exists)

    def test_retry_persists_guidance_before_archiving_terminal_state(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            failed = state_dir / "failed/42-example-bird"
            attempt = failed / "attempt-01"
            attempt.mkdir(parents=True)
            (attempt / "portrait.png").write_bytes(b"strong source")
            (attempt / "quality-review.json").write_text(
                json.dumps(
                    {
                        "passed": False,
                        "findings": ["Correct the ruler"],
                        "correction_findings": ["Correct the ruler"],
                    }
                )
            )
            (failed / "profile.json").write_text(
                json.dumps(
                    {
                        "taxon_id": 42,
                        "common_name": "Example Bird",
                        "scientific_name": "Avis exemplum",
                    }
                )
            )
            config = controller_config(state_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                patch("inky_bird_frame.cli.shutil.move", side_effect=OSError("interrupted")),
                self.assertRaisesRegex(OSError, "interrupted"),
            ):
                retry_command(Namespace(taxon_id=42, source_attempt=1))

            guidance = RetryStore(state_dir / "generation-retries.json").quality_guidance(42)
            queue = read_generation_queue(cast(AppConfig, config))
            failed_exists = failed.is_dir()

        self.assertIsNotNone(guidance)
        if guidance is not None:
            self.assertEqual(guidance.findings, ("Correct the ruler",))
            self.assertEqual(
                guidance.source_plate,
                "archive/42-example-bird/attempt-01/portrait.png",
            )
        self.assertEqual([item.taxon_id for item in queue], [42])
        self.assertTrue(failed_exists)

    def test_retry_queues_cache_identity_while_terminal_state_blocks_generation(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            pending = state_dir / "pending/42-example-bird"
            pending.mkdir(parents=True)
            (pending / "portrait.png").write_bytes(b"incomplete candidate")
            profile_cache = state_dir / "profiles/42"
            profile_cache.mkdir(parents=True)
            (profile_cache / "profile.json").write_text(
                json.dumps(
                    {
                        "taxon_id": 42,
                        "common_name": "Example Bird",
                        "scientific_name": "Avis exemplum",
                    }
                )
            )
            reference_cache = state_dir / "references/42"
            reference_cache.mkdir(parents=True)
            (reference_cache / "references.json").write_text("{}")
            moved_sources: list[Path] = []

            def interrupt_reference_move(source: str, _destination: Path) -> None:
                moved_sources.append(Path(source))
                if Path(source) == reference_cache:
                    raise OSError("cache archival interrupted")

            config = controller_config(state_dir)
            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                patch("inky_bird_frame.cli.shutil.move", side_effect=interrupt_reference_move),
                self.assertRaisesRegex(OSError, "cache archival interrupted"),
            ):
                retry_command(
                    Namespace(
                        taxon_id=42,
                        refresh_research=True,
                    )
                )

            queue = read_generation_queue(cast(AppConfig, config))
            pending_exists = pending.is_dir()

        self.assertEqual(moved_sources, [profile_cache, reference_cache])
        self.assertEqual([item.taxon_id for item in queue], [42])
        self.assertEqual(queue[0].common_name, "Example Bird")
        self.assertTrue(pending_exists)

    def test_retry_preserves_identity_across_partial_cache_archival(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            profile_cache = state_dir / "profiles/42"
            profile_cache.mkdir(parents=True)
            (profile_cache / "profile.json").write_text(
                json.dumps(
                    {
                        "taxon_id": 42,
                        "common_name": "Legacy Deferred Bird",
                        "scientific_name": "Avis dilata",
                    }
                )
            )
            reference_cache = state_dir / "references/42"
            reference_cache.mkdir(parents=True)
            (reference_cache / "references.json").write_text("{}")
            RetryStore(state_dir / "generation-retries.json").record_failure(
                42,
                GenerationError("legacy transient failure"),
                now=datetime(2026, 8, 2, tzinfo=UTC),
                initial_minutes=5,
                maximum_minutes=60,
            )
            real_move = shutil.move

            def interrupt_reference_move(source: str, destination: Path) -> None:
                if Path(source) == reference_cache:
                    raise OSError("cache archival interrupted")
                real_move(source, destination)

            config = controller_config(state_dir)
            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                patch("inky_bird_frame.cli.shutil.move", side_effect=interrupt_reference_move),
                self.assertRaisesRegex(OSError, "cache archival interrupted"),
            ):
                retry_command(Namespace(taxon_id=42, refresh_research=True))

            interrupted_record = RetryStore(state_dir / "generation-retries.json").get(42)
            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                redirect_stdout(io.StringIO()),
            ):
                retry_command(Namespace(taxon_id=42, refresh_research=True))

            queue = read_generation_queue(cast(AppConfig, config))
            final_record = RetryStore(state_dir / "generation-retries.json").get(42)

        self.assertIsNotNone(interrupted_record)
        if interrupted_record is not None:
            self.assertEqual(interrupted_record.common_name, "Legacy Deferred Bird")
            self.assertEqual(interrupted_record.scientific_name, "Avis dilata")
        self.assertEqual([item.taxon_id for item in queue], [42])
        self.assertEqual(queue[0].common_name, "Legacy Deferred Bird")
        self.assertIsNone(final_record)

    def test_retry_reports_existing_queue_membership_without_recoverable_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            pending = state_dir / "pending/42-example-bird"
            pending.mkdir(parents=True)
            (pending / "portrait.png").write_bytes(b"incomplete candidate")
            (state_dir / "generation-queue.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "species": [
                            {
                                "taxon_id": 42,
                                "common_name": "Queued Bird",
                                "scientific_name": "Avis ordinata",
                                "observation_count": 0,
                                "source": "human-review",
                                "sources": ["human-review"],
                            }
                        ],
                    }
                )
            )
            RetryStore(state_dir / "generation-retries.json").record_failure(
                42,
                GenerationError("temporary failure"),
                now=datetime(2026, 8, 2, tzinfo=UTC),
                initial_minutes=5,
                maximum_minutes=60,
                species=BirdSpecies(42, "Old Bird Name", "Avis old", 0, "human-review"),
            )
            config = controller_config(state_dir)
            output = io.StringIO()

            with patch("inky_bird_frame.cli._config", return_value=config), redirect_stdout(output):
                retry_command(Namespace(taxon_id=42))

            result = json.loads(output.getvalue())["data"]
            queue = read_generation_queue(cast(AppConfig, config))

        self.assertTrue(result["queued_for_generation"])
        self.assertEqual(queue[0].common_name, "Queued Bird")
        self.assertEqual(queue[0].scientific_name, "Avis ordinata")

    def test_retry_prefers_current_identity_over_older_edit_source(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            failed = state_dir / "failed/42-current-run"
            failed.mkdir(parents=True)
            (failed / "profile.json").write_text(
                json.dumps(
                    {
                        "taxon_id": 42,
                        "common_name": "Current Bird Name",
                        "scientific_name": "Avis current",
                    }
                )
            )
            run = state_dir / "archive/42-older-run"
            attempt = run / "attempt-01"
            attempt.mkdir(parents=True)
            (run / "profile.json").write_text(
                json.dumps(
                    {
                        "taxon_id": 42,
                        "common_name": "Old Bird Name",
                        "scientific_name": "Avis old",
                    }
                )
            )
            (attempt / "portrait.png").write_bytes(b"older strong source")
            (attempt / "quality-review.json").write_text(
                json.dumps(
                    {
                        "passed": False,
                        "findings": ["Correct the ruler"],
                        "correction_findings": ["Correct the ruler"],
                    }
                )
            )
            config = controller_config(state_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                redirect_stdout(io.StringIO()),
            ):
                retry_command(
                    Namespace(
                        taxon_id=42,
                        source_run=run.name,
                        source_attempt=1,
                    )
                )

            queue = read_generation_queue(cast(AppConfig, config))

        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0].common_name, "Current Bird Name")
        self.assertEqual(queue[0].scientific_name, "Avis current")

    def test_retry_preserves_selected_attempt_from_archived_run(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            run = state_dir / "archive/42-20260803T060207Z"
            attempt = run / "attempt-03"
            attempt.mkdir(parents=True)
            (run / "profile.json").write_text(
                json.dumps(
                    {
                        "taxon_id": 42,
                        "common_name": "Example Bird",
                        "scientific_name": "Avis exemplum",
                    }
                )
            )
            (attempt / "portrait.png").write_bytes(b"best earlier portrait")
            (attempt / "quality-review.json").write_text(
                json.dumps(
                    {
                        "passed": False,
                        "findings": ["Verified bill", "Darken both irises"],
                        "correction_findings": ["Darken both irises"],
                    }
                )
            )
            config = controller_config(state_dir)
            output = io.StringIO()

            with patch("inky_bird_frame.cli._config", return_value=config), redirect_stdout(output):
                retry_command(
                    Namespace(
                        taxon_id=42,
                        source_run=run.name,
                        source_attempt=3,
                    )
                )

            result = json.loads(output.getvalue())["data"]
            guidance = RetryStore(state_dir / "generation-retries.json").quality_guidance(42)
            source_bytes = (
                (state_dir / guidance.source_plate).read_bytes()
                if guidance is not None and guidance.source_plate is not None
                else None
            )
            queue = read_generation_queue(cast(AppConfig, config))

        self.assertTrue(result["preserved_correction_source"])
        self.assertEqual(result["source_run"], run.name)
        self.assertEqual(result["source_attempt"], 3)
        self.assertEqual(result["archived"], [])
        self.assertIsNotNone(guidance)
        if guidance is not None:
            self.assertEqual(guidance.findings, ("Darken both irises",))
        self.assertEqual(source_bytes, b"best earlier portrait")
        self.assertTrue(result["queued_for_generation"])
        self.assertEqual([item.taxon_id for item in queue], [42])

    def test_retry_rejects_symlinked_attempt_from_archived_run(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            run = state_dir / "archive/42-20260803T060207Z"
            run.mkdir(parents=True)
            (run / "profile.json").write_text(json.dumps({"taxon_id": 42}))
            external_attempt = state_dir / "external-attempt"
            external_attempt.mkdir()
            (external_attempt / "portrait.png").write_bytes(b"external portrait")
            (external_attempt / "quality-review.json").write_text(
                json.dumps(
                    {
                        "passed": False,
                        "findings": ["Correct the ruler"],
                        "correction_findings": ["Correct the ruler"],
                    }
                )
            )
            (run / "attempt-01").symlink_to(external_attempt, target_is_directory=True)
            config = controller_config(state_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                self.assertRaisesRegex(SpeciesStateError, "escapes its archive run"),
            ):
                retry_command(
                    Namespace(
                        taxon_id=42,
                        source_run=run.name,
                        source_attempt=1,
                    )
                )

    def test_retry_rejects_mismatched_archived_run_profile(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            run = state_dir / "archive/42-20260803T060207Z"
            attempt = run / "attempt-01"
            attempt.mkdir(parents=True)
            (run / "profile.json").write_text(json.dumps({"taxon_id": 43}))
            (attempt / "portrait.png").write_bytes(b"wrong species portrait")
            (attempt / "quality-review.json").write_text(
                json.dumps(
                    {
                        "passed": False,
                        "findings": ["Correct the ruler"],
                        "correction_findings": ["Correct the ruler"],
                    }
                )
            )
            config = controller_config(state_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                self.assertRaisesRegex(SpeciesStateError, "identity does not match taxon 42"),
            ):
                retry_command(
                    Namespace(
                        taxon_id=42,
                        source_run=run.name,
                        source_attempt=1,
                    )
                )

    def test_retry_rejects_invalid_source_run_before_archiving(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            failed = state_dir / "failed/42-example-bird/attempt-01"
            failed.mkdir(parents=True)
            (failed / "quality-review.json").write_text(
                json.dumps({"passed": False, "findings": ["Fix the tail"]})
            )
            config = controller_config(state_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                self.assertRaisesRegex(ValueError, "archive directory name"),
            ):
                retry_command(
                    Namespace(
                        taxon_id=42,
                        source_run="../42-20260803T060207Z",
                        source_attempt=1,
                    )
                )

            self.assertTrue((state_dir / "failed/42-example-bird").is_dir())
            self.assertFalse((state_dir / "archive").exists())

    def test_retry_operator_correction_overrides_review_and_preserves_invariant(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            failed = state_dir / "failed/42-example-bird/attempt-02"
            failed.mkdir(parents=True)
            (failed / "portrait.png").write_bytes(b"accepted anatomy")
            (failed / "quality-review.json").write_text(
                json.dumps(
                    {
                        "passed": False,
                        "findings": ["Redraw the accepted bill", "Correct the ruler"],
                        "correction_findings": [
                            "Redraw the accepted bill",
                            "Correct the ruler",
                        ],
                    }
                )
            )
            write_test_species_identity(failed.parent)
            invariant = "Keep the source-matched bill proportion."
            RetryStore(state_dir / "generation-retries.json").set_quality_guidance(
                42,
                (invariant,),
                invariant_findings=(invariant,),
            )
            config = controller_config(state_dir)
            output = io.StringIO()

            with patch("inky_bird_frame.cli._config", return_value=config), redirect_stdout(output):
                retry_command(
                    Namespace(
                        taxon_id=42,
                        source_attempt=2,
                        correction=["Correct only the ruler text."],
                    )
                )

            result = json.loads(output.getvalue())["data"]
            guidance = RetryStore(state_dir / "generation-retries.json").quality_guidance(42)

        self.assertEqual(result["correction_override_count"], 1)
        self.assertIsNotNone(guidance)
        if guidance is not None:
            self.assertEqual(
                guidance.findings,
                (invariant, "Correct only the ruler text."),
            )
            self.assertEqual(guidance.invariant_findings, (invariant,))

    def test_retry_operator_correction_requires_source_and_unique_values(self) -> None:
        with TemporaryDirectory() as temporary:
            config = controller_config(Path(temporary))
            with patch("inky_bird_frame.cli._config", return_value=config):
                with self.assertRaisesRegex(ValueError, "requires --source-attempt"):
                    retry_command(
                        Namespace(
                            taxon_id=42,
                            correction=["Correct only the ruler text."],
                        )
                    )
                with self.assertRaisesRegex(ValueError, "must not repeat"):
                    retry_command(
                        Namespace(
                            taxon_id=42,
                            source_attempt=2,
                            correction=["Fix the ruler", "Fix the ruler"],
                        )
                    )

    def test_retry_source_run_requires_attempt(self) -> None:
        with TemporaryDirectory() as temporary:
            config = controller_config(Path(temporary))
            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                self.assertRaisesRegex(ValueError, "requires --source-attempt"),
            ):
                retry_command(Namespace(taxon_id=42, source_run="42-retained-run"))

    def test_retry_preserves_human_rejected_candidate_as_correction_source(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            candidate = state_dir / "archive/42-example-bird-2"
            candidate.mkdir(parents=True)
            portrait = candidate / "portrait.png"
            portrait.write_bytes(b"strong source")
            rejection_reason = "Correct the bill while preserving the legs and composition."
            (candidate / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "rejected",
                        "taxon_id": 42,
                        "common_name": "Example Bird",
                        "scientific_name": "Avis exemplum",
                        "rejection_reason": rejection_reason,
                        "assets": {
                            "portrait": {
                                "filename": "portrait.png",
                                "sha256": sha256_file(portrait),
                            }
                        },
                    }
                )
            )
            failed = state_dir / "failed/42-example-bird/attempt-01"
            failed.mkdir(parents=True)
            (failed / "quality-review.json").write_text(
                json.dumps({"passed": False, "findings": ["Ignore this other attempt"]})
            )
            config = controller_config(state_dir)
            output = io.StringIO()

            with patch("inky_bird_frame.cli._config", return_value=config), redirect_stdout(output):
                retry_command(Namespace(taxon_id=42, source_candidate="42-example-bird-2"))

            result = json.loads(output.getvalue())["data"]
            guidance = RetryStore(state_dir / "generation-retries.json").quality_guidance(42)

        self.assertEqual(result["source_candidate"], "42-example-bird-2")
        self.assertTrue(result["preserved_correction_source"])
        self.assertIsNotNone(guidance)
        if guidance is not None:
            self.assertEqual(guidance.findings, (rejection_reason,))
            self.assertEqual(
                guidance.source_plate,
                "archive/42-example-bird-2/portrait.png",
            )

    def test_retry_selects_source_candidate_after_approved_replacement(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            candidate = state_dir / "archive/42-example-bird-2"
            candidate.mkdir(parents=True)
            portrait = candidate / "portrait.png"
            portrait.write_bytes(b"rejected approved source")
            rejection_reason = "Correct the bill while preserving the accepted anatomy."
            (candidate / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "rejected",
                        "taxon_id": 42,
                        "common_name": "Example Bird",
                        "scientific_name": "Avis exemplum",
                        "rejection_reason": rejection_reason,
                        "assets": {
                            "portrait": {
                                "filename": "portrait.png",
                                "sha256": sha256_file(portrait),
                            }
                        },
                    }
                )
            )
            RetryStore(state_dir / "generation-retries.json").set_quality_guidance(
                42,
                (rejection_reason,),
                invariant_findings=(rejection_reason,),
            )
            config = controller_config(state_dir)
            output = io.StringIO()

            with patch("inky_bird_frame.cli._config", return_value=config), redirect_stdout(output):
                retry_command(Namespace(taxon_id=42, source_candidate="42-example-bird-2"))

            result = json.loads(output.getvalue())["data"]
            guidance = RetryStore(state_dir / "generation-retries.json").quality_guidance(42)

        self.assertEqual(result["archived"], [])
        self.assertTrue(result["preserved_correction_source"])
        self.assertIsNotNone(guidance)
        if guidance is not None:
            self.assertEqual(guidance.findings, (rejection_reason,))
            self.assertEqual(guidance.invariant_findings, (rejection_reason,))
            self.assertEqual(
                guidance.source_plate,
                "archive/42-example-bird-2/portrait.png",
            )

    def test_retry_rejects_invalid_source_candidate_before_archiving(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            failed = state_dir / "failed/42-example-bird/attempt-01"
            failed.mkdir(parents=True)
            (failed / "quality-review.json").write_text(
                json.dumps({"passed": False, "findings": ["Fix the tail"]})
            )
            config = controller_config(state_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                self.assertRaisesRegex(ValueError, "archive directory name"),
            ):
                retry_command(Namespace(taxon_id=42, source_candidate="../42-example-bird"))

            self.assertTrue((state_dir / "failed/42-example-bird").is_dir())
            self.assertFalse((state_dir / "archive").exists())

    def test_retry_rejects_symlinked_source_candidate_manifest(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            candidate = state_dir / "archive/42-example-bird-2"
            candidate.mkdir(parents=True)
            external_manifest = state_dir / "manifest.json"
            external_manifest.write_text("{}")
            (candidate / "manifest.json").symlink_to(external_manifest)
            failed = state_dir / "failed/42-example-bird/attempt-01"
            failed.mkdir(parents=True)
            (failed / "quality-review.json").write_text(
                json.dumps({"passed": False, "findings": ["Fix the tail"]})
            )
            config = controller_config(state_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                self.assertRaisesRegex(SpeciesStateError, "no valid manifest"),
            ):
                retry_command(Namespace(taxon_id=42, source_candidate=candidate.name))

            self.assertTrue((state_dir / "failed/42-example-bird").is_dir())

    def test_retry_rejects_modified_source_candidate_before_archiving(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            candidate = state_dir / "archive/42-example-bird-2"
            candidate.mkdir(parents=True)
            (candidate / "portrait.png").write_bytes(b"modified")
            (candidate / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "rejected",
                        "taxon_id": 42,
                        "rejection_reason": "Correct the bill.",
                        "assets": {
                            "portrait": {
                                "filename": "portrait.png",
                                "sha256": "0" * 64,
                            }
                        },
                    }
                )
            )
            failed = state_dir / "failed/42-example-bird/attempt-01"
            failed.mkdir(parents=True)
            (failed / "quality-review.json").write_text(
                json.dumps({"passed": False, "findings": ["Fix the tail"]})
            )
            config = controller_config(state_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                self.assertRaisesRegex(SpeciesStateError, "checksum mismatch"),
            ):
                retry_command(Namespace(taxon_id=42, source_candidate="42-example-bird-2"))

            self.assertTrue((state_dir / "failed/42-example-bird").is_dir())
            self.assertTrue(candidate.is_dir())

    def test_retry_rejects_missing_source_attempt_before_archiving(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            review = state_dir / "failed/42-example-bird/attempt-01/quality-review.json"
            review.parent.mkdir(parents=True)
            review.write_text(json.dumps({"passed": False, "findings": ["Fix the tail"]}))
            config = controller_config(state_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                self.assertRaisesRegex(ValueError, "choose one of: 1"),
            ):
                retry_command(Namespace(taxon_id=42, source_attempt=2))

            self.assertTrue((state_dir / "failed/42-example-bird").is_dir())
            self.assertFalse((state_dir / "archive").exists())

    def test_retry_rejects_malformed_quality_review_before_archiving(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            failed = state_dir / "failed/42-example-bird"
            review = failed / "attempt-03/quality-review.json"
            review.parent.mkdir(parents=True)
            review.write_text("not json")
            config = controller_config(state_dir)

            with (
                patch("inky_bird_frame.cli._config", return_value=config),
                self.assertRaisesRegex(SpeciesStateError, "Invalid quality review"),
            ):
                retry_command(Namespace(taxon_id=42))

            self.assertTrue(failed.is_dir())
            self.assertFalse((state_dir / "archive").exists())

    def test_retry_is_excluded_by_running_generation_cycle(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            config = controller_config(state_dir)
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
                {"name": "birdbuddy", "status": "ok"},
            ],
            "unresolved_species": [
                {
                    "provider": "birdweather",
                    "species_code": "42",
                    "common_name": "Split Bird",
                    "scientific_name": "Avis split",
                },
                {
                    "provider": "birdbuddy",
                    "species_code": "species-new",
                    "common_name": "New Bird",
                    "scientific_name": "Avis nova",
                },
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
        self.assertIn("birdbuddy-taxonomy", degraded_keys)
        self.assertNotIn("ebird-taxonomy", degraded_keys)
        self.assertIn("ebird-taxonomy", recovered_keys)
        self.assertNotIn("birdweather-taxonomy", recovered_keys)
        self.assertNotIn("birdbuddy-taxonomy", recovered_keys)

    def test_refresh_recovers_birdbuddy_taxonomy_alert(self) -> None:
        config = SimpleNamespace(discovery=SimpleNamespace(sources=(DiscoveryProvider.BIRDBUDDY,)))
        result = {
            "providers": [{"name": "birdbuddy", "status": "ok"}],
            "unresolved_species": [],
            "new_species": [],
        }
        with (
            patch("inky_bird_frame.cli._config", return_value=config),
            patch("inky_bird_frame.cli.run_refresh_cycle", return_value=result),
            patch("inky_bird_frame.cli.safe_record_degradation"),
            patch("inky_bird_frame.cli.safe_record_recovery") as recovery,
            redirect_stdout(io.StringIO()),
        ):
            refresh_command(Namespace())

        recovered_keys = [call.kwargs["key"] for call in recovery.call_args_list]
        self.assertIn("birdbuddy-taxonomy", recovered_keys)


if __name__ == "__main__":
    unittest.main()
