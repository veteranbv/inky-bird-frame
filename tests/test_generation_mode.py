from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from inky_bird_frame.cli import controller_jobs, generate_command
from inky_bird_frame.config import load_config
from inky_bird_frame.controller import (
    DiscoverySnapshot,
    calculate_generation_work,
    discover_species,
    run_generation_cycle,
    run_refresh_cycle,
)
from inky_bird_frame.installation import controller_systemd_units
from inky_bird_frame.settings import initialize_config


def test_generation_off_has_no_work_or_notification_side_effects(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    initialize_config(path)
    config = load_config(path)
    config.controller.state_dir.mkdir(parents=True)
    retained = config.controller.state_dir / "generation-queue.json"
    retained.write_bytes(b"retained invalid fixture: must not read or replace")
    before = retained.read_bytes()
    with (
        patch("inky_bird_frame.controller.approve_passing_candidates") as approve,
        patch("inky_bird_frame.cli.safe_record_recovery") as recovery,
        patch("inky_bird_frame.cli.safe_record_degradation") as degradation,
        patch("inky_bird_frame.cli.print_result"),
    ):
        assert run_generation_cycle(config)["generation_enabled"] is False
        assert generate_command(argparse.Namespace(config=path)) == 0
    approve.assert_not_called()
    recovery.assert_not_called()
    degradation.assert_not_called()
    assert retained.read_bytes() == before
    assert sorted(item.name for item in config.controller.state_dir.iterdir()) == [
        "generation-queue.json"
    ]


def test_no_sources_refresh_is_healthy_and_does_not_contact_providers(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    initialize_config(path)
    config = load_config(path)
    with patch("inky_bird_frame.controller.resolve_discovery_location") as network:
        result = discover_species(config)
        assert result.species == []
        assert result.providers == []
        refreshed = run_refresh_cycle(config)
    network.assert_not_called()
    assert refreshed["species_count"] == 0
    assert refreshed["providers"] == []
    assert (config.controller.state_dir / "active-catalog.json").exists()


def test_discovery_policy_revision_invalidates_old_and_unbound_app_owned_work() -> None:
    now = datetime.now(UTC)
    for stored, require, complete in [
        ("a" * 64, False, False),
        (None, True, False),
        (None, False, True),
        ("b" * 64, True, True),
    ]:
        work = calculate_generation_work(
            snapshot=DiscoverySnapshot(now, "", "", [], configuration_revision=stored),
            queued_species=[],
            approved=set(),
            terminal_states={},
            retry_records=[],
            now=now,
            maximum_age=timedelta(minutes=30),
            expected_discovery_revision="b" * 64,
            require_bound_discovery=require,
        )
        assert work.complete is complete
        assert work.discovery_status == ("fresh" if complete else "configuration_changed")


def test_generation_off_and_app_owned_service_definitions(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    initialize_config(path)
    config = load_config(path)
    units = controller_systemd_units(
        config,
        executable=Path("/bin/inky"),
        app_dir=tmp_path,
        config_path=path,
        home=tmp_path,
        user="fixture",
    )
    assert set(units) == {"inky-bird-frame-controller.service", "inky-bird-frame-scheduler.service"}
    assert '"scheduler"' in units["inky-bird-frame-scheduler.service"]
    assert [job.name for job in controller_jobs(config, path)] == ["refresh"]
    managed = replace(
        config, controller=replace(config.controller, configuration_ownership="managed")
    )
    units = controller_systemd_units(
        managed,
        executable=Path("/bin/inky"),
        app_dir=tmp_path,
        config_path=path,
        home=tmp_path,
        user="fixture",
    )
    assert "inky-bird-frame-generate.timer" not in units
    assert "inky-bird-frame-refresh.timer" in units
