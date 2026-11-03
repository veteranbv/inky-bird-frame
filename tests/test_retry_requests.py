from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from inky_bird_frame.activity import activity_run, next_job_run, progress, read_activity
from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.config import AppConfig, DiscoveryProvider, load_config
from inky_bird_frame.controller import (
    discover_species,
    ensure_generation_retry,
    exclusive_cycle_lock,
    read_generation_queue,
    read_generation_work,
    record_failure,
    run_generation_cycle,
)
from inky_bird_frame.errors import CatalogError, DataSourceError, QualityReviewError
from inky_bird_frame.http import write_json_atomic
from inky_bird_frame.owner_operations import execute_owner_operation
from inky_bird_frame.recovery import retry_species
from inky_bird_frame.retry_requests import (
    MAX_REQUESTS,
    prepare_retry_requests,
    queue_retries,
    retry_requests,
    retry_wake_token,
)
from inky_bird_frame.settings import initialize_config, settings_status


def app(root: Path) -> AppConfig:
    path = root / "config.toml"
    initialize_config(path)
    return load_config(path, load_secrets=False)


def entries(root: Path) -> dict[str, dict[str, object]]:
    return cast(dict[str, dict[str, object]], read_activity(root)["entries"])


def fresh_snapshot(config: AppConfig) -> None:
    write_json_atomic(
        config.controller.state_dir / "discovery.json",
        {
            "schema_version": 2,
            "configuration_revision": config.discovery_revision,
            "refreshed_at": datetime.now(UTC).isoformat(),
            "place_name": "",
            "state": "",
            "providers": [],
            "species": [],
            "notification_baseline_taxon_ids": [],
        },
    )


def unavailable_discovery(config: AppConfig, status: str) -> None:
    if status == "missing":
        return
    fresh_snapshot(config)
    path = config.controller.state_dir / "discovery.json"
    raw = json.loads(path.read_text())
    if status == "stale":
        raw["refreshed_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    else:
        raw["configuration_revision"] = "0" * 64
    write_json_atomic(path, raw)


@pytest.mark.parametrize("status", ["missing", "stale", "configuration_changed"])
def test_discovery_outage_preserves_retry_intents_until_a_healthy_refresh(
    tmp_path: Path, status: str
) -> None:
    config = app(tmp_path)
    state = config.controller.state_dir
    ensure_generation_retry(config, 101, "Ready Bird", "Avis parata", tmp_path / "fixture")
    held = BirdSpecies(202, "Held Bird", "Avis retenta", 1, "fixture")
    failure = record_failure(state, held, QualityReviewError("synthetic review failure"))
    ensure_generation_retry(
        config, 202, held.common_name, held.scientific_name, tmp_path / "fixture"
    )
    queue_retries(state, [202, 101], run_now=True)
    before = (state / "retry-requests.json").read_bytes()
    queue_before = (state / "generation-queue.json").read_bytes()
    requests_before = retry_requests(state)
    wake_before = retry_wake_token(state)
    unavailable_discovery(config, status)
    assert read_generation_work(config).discovery_status == status
    with (
        exclusive_cycle_lock(state),
        patch("inky_bird_frame.recovery.retry_species") as recovery,
        patch("inky_bird_frame.controller.ensure_generation_retry") as seed,
        patch("inky_bird_frame.controller.generate_candidate") as model,
    ):
        prepare_retry_requests(config)
    recovery.assert_not_called()
    seed.assert_not_called()
    model.assert_not_called()
    assert (state / "retry-requests.json").read_bytes() == before
    assert (state / "generation-queue.json").read_bytes() == queue_before
    assert failure.is_dir()
    assert retry_wake_token(state) == wake_before
    # The same accepted intents continue; the owner need not submit them again.
    fresh_snapshot(config)
    with exclusive_cycle_lock(state):
        prepare_retry_requests(config)
    after = retry_requests(state)
    assert [row.status for row in after] == ["eligible", "eligible"]
    assert [(row.request_id, row.created_at) for row in after] == [
        (row.request_id, row.created_at) for row in requests_before
    ]
    assert {bird.taxon_id for bird in read_generation_work(config).actionable} == {101, 202}


@pytest.mark.parametrize("status", ["missing", "stale", "configuration_changed"])
def test_generation_cycle_waits_then_resumes_the_original_retry_request(
    tmp_path: Path, status: str
) -> None:
    config = app(tmp_path)
    config = replace(config, controller=replace(config.controller, generation_enabled=True))
    state = config.controller.state_dir
    ensure_generation_retry(config, 101, "Ready Bird", "Avis parata", tmp_path / "fixture")
    queue_retries(state, [101])
    request = retry_requests(state)[0]
    unavailable_discovery(config, status)
    with (
        patch("inky_bird_frame.controller.generate_candidate") as model,
        pytest.raises(DataSourceError),
    ):
        run_generation_cycle(config)
    model.assert_not_called()
    assert retry_requests(state) == [request]
    fresh_snapshot(config)
    with patch(
        "inky_bird_frame.controller.generate_candidate",
        side_effect=DataSourceError("synthetic model boundary"),
    ) as model:
        result = run_generation_cycle(config)
    assert model.call_count == 1
    assert result["attempted_count"] == 1
    resumed = retry_requests(state)[0]
    assert resumed.request_id == request.request_id and resumed.status == "eligible"


def test_owner_can_queue_multiple_retries_while_generation_lock_is_held(tmp_path: Path) -> None:
    config = app(tmp_path)
    revision = str(settings_status(config)["revision"])
    with (
        exclusive_cycle_lock(config.controller.state_dir),
        patch("inky_bird_frame.controller.generate_candidate") as model,
    ):
        first = execute_owner_operation(config, "retry", revision=revision, taxon_ids=[101, 202])
        second = execute_owner_operation(config, "retry-now", revision=revision, taxon_id=303)
        duplicate = execute_owner_operation(config, "retry", revision=revision, taxon_id=101)
    assert first["taxon_ids"] == [101, 202]
    assert second["taxon_ids"] == [303]
    assert duplicate["request_ids"] == cast(list[str], first["request_ids"])[:1]
    assert [row.taxon_id for row in retry_requests(config.controller.state_dir)] == [101, 202, 303]
    assert retry_wake_token(config.controller.state_dir)
    model.assert_not_called()


def test_concurrent_requests_are_durable_and_deduplicated(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda id: queue_retries(tmp_path, [id]), [*range(1, 20), *range(1, 20)]))
    assert len(retry_requests(tmp_path)) == 19
    assert (tmp_path / "retry-requests.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "retry-requests.lock").stat().st_mode & 0o777 == 0o600


def test_two_real_held_taxa_prepare_under_the_existing_cycle_lock(tmp_path: Path) -> None:
    config = app(tmp_path)
    fresh_snapshot(config)
    for id in (101, 202):
        record_failure(
            config.controller.state_dir,
            BirdSpecies(id, f"Example {id}", f"Avis example{id}", 1, "fixture"),
            QualityReviewError("PRIVATE_UPSTREAM_ERROR"),
        )
    queue_retries(config.controller.state_dir, [202, 101])
    with (
        exclusive_cycle_lock(config.controller.state_dir),
        patch("inky_bird_frame.controller.generate_candidate") as model,
    ):
        prepare_retry_requests(config)
    assert [row.status for row in retry_requests(config.controller.state_dir)] == [
        "eligible",
        "eligible",
    ]
    assert [bird.taxon_id for bird in read_generation_queue(config)] == [202, 101]
    assert all("human-review" in bird.sources for bird in read_generation_queue(config))
    assert not list((config.controller.state_dir / "failed").glob("*-*"))
    assert "PRIVATE_" not in (config.controller.state_dir / "retry-requests.json").read_text()
    model.assert_not_called()


def test_interrupted_preparation_is_held_not_replayed(tmp_path: Path) -> None:
    config = app(tmp_path)
    queue_retries(config.controller.state_dir, [101])
    path = config.controller.state_dir / "retry-requests.json"
    raw = json.loads(path.read_text())
    raw["requests"][0]["status"] = "preparing"
    write_json_atomic(path, raw)
    with (
        exclusive_cycle_lock(config.controller.state_dir),
        patch("inky_bird_frame.recovery.retry_species") as mutation,
    ):
        prepare_retry_requests(config)
    mutation.assert_not_called()
    assert retry_requests(config.controller.state_dir)[0].outcome == "interrupted"


def test_approved_plate_is_not_replaced_and_errors_are_sanitized(tmp_path: Path) -> None:
    config = app(tmp_path)
    fresh_snapshot(config)
    queue_retries(config.controller.state_dir, [101, 202])
    with (
        exclusive_cycle_lock(config.controller.state_dir),
        patch(
            "inky_bird_frame.retry_requests.has_valid_approved_candidate", side_effect=[True, False]
        ),
        patch(
            "inky_bird_frame.recovery.retry_species",
            side_effect=ValueError("PRIVATE_PATH_AND_TOKEN"),
        ) as mutation,
    ):
        prepare_retry_requests(config)
    assert mutation.call_count == 1
    assert [row.status for row in retry_requests(config.controller.state_dir)] == [
        "approved",
        "blocked",
    ]
    assert "PRIVATE_" not in (config.controller.state_dir / "retry-requests.json").read_text()


def test_disabled_generation_never_prepares_or_dequeues_requests(tmp_path: Path) -> None:
    config = app(tmp_path)
    config = replace(config, controller=replace(config.controller, generation_enabled=False))
    queue_retries(config.controller.state_dir, [101], run_now=True)
    with patch("inky_bird_frame.controller.generate_candidate") as model:
        assert run_generation_cycle(config)["generation_enabled"] is False
    assert retry_requests(config.controller.state_dir)[0].status == "queued"
    model.assert_not_called()


def test_queue_limits_and_corruption_fail_closed(tmp_path: Path) -> None:
    for id in range(MAX_REQUESTS):
        queue_retries(tmp_path, [id + 1])
    with pytest.raises(CatalogError, match="full"):
        queue_retries(tmp_path, [1000])
    with pytest.raises(ValueError):
        queue_retries(tmp_path, [True])
    with pytest.raises(ValueError):
        queue_retries(tmp_path, [1, 1])
    with pytest.raises(ValueError):
        queue_retries(tmp_path, list(range(1, 34)))
    path = tmp_path / "retry-requests.json"
    write_json_atomic(path, {})
    with pytest.raises(CatalogError, match="invalid"):
        queue_retries(tmp_path, [1])
    path.unlink()
    path.symlink_to(tmp_path / "missing")
    with pytest.raises(OSError):
        queue_retries(tmp_path, [1])


def test_activity_has_real_history_and_stale_running_is_not_claimed_live(tmp_path: Path) -> None:
    with activity_run(tmp_path, "generation"):
        progress("generating", taxon_id=101, common_name="Example", attempt=2)
        row = entries(tmp_path)["generation"]
        assert row["status"] == "running" and row["attempt"] == 2
    finished = entries(tmp_path)["generation"]
    assert finished["status"] == "succeeded"
    first = finished["first_started_at"]
    with activity_run(tmp_path, "generation") as run:
        run.succeeded = False
    assert entries(tmp_path)["generation"]["first_started_at"] == first
    assert entries(tmp_path)["generation"]["last_success_at"] == finished["last_success_at"]
    path = tmp_path / "activity.json"
    raw = json.loads(path.read_text())
    raw["entries"]["generation"].update(
        status="running", updated_at=(datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    )
    write_json_atomic(path, raw)
    assert entries(tmp_path)["generation"]["status"] == "unknown"
    next_job_run(tmp_path, "generate", 300)
    assert entries(tmp_path)["job-generate"]["next_run_at"]
    assert "run_id" not in json.dumps(read_activity(tmp_path))


def test_cycle_prepares_then_attempts_multiple_birds_in_request_order(tmp_path: Path) -> None:
    config = app(tmp_path)
    config = replace(config, controller=replace(config.controller, generation_enabled=True))
    for id in (101, 202):
        record_failure(
            config.controller.state_dir,
            BirdSpecies(id, f"Example {id}", f"Avis example{id}", 1, "fixture"),
            QualityReviewError("synthetic review failure"),
        )
    write_json_atomic(
        config.controller.state_dir / "discovery.json",
        {
            "schema_version": 2,
            "configuration_revision": config.discovery_revision,
            "refreshed_at": datetime.now(UTC).isoformat(),
            "place_name": "",
            "state": "",
            "providers": [],
            "species": [],
            "notification_baseline_taxon_ids": [],
        },
    )
    queue_retries(config.controller.state_dir, [202, 101], run_now=True)
    with patch(
        "inky_bird_frame.controller.generate_candidate",
        side_effect=DataSourceError("synthetic source failure"),
    ) as model:
        result = run_generation_cycle(config)
    assert [call.args[1].taxon_id for call in model.call_args_list] == [202, 101]
    assert result["attempted_count"] == 2
    assert not retry_wake_token(config.controller.state_dir)
    assert entries(config.controller.state_dir)["generation"]["status"] == "succeeded"


def test_ambiguous_preparation_cannot_generate_from_a_partially_written_seed(
    tmp_path: Path,
) -> None:
    config = app(tmp_path)
    fresh_snapshot(config)
    ensure_generation_retry(config, 101, "Example", "Avis example", tmp_path / "fixture")
    queue_retries(config.controller.state_dir, [101])
    path = config.controller.state_dir / "retry-requests.json"
    raw = json.loads(path.read_text())
    raw["requests"][0]["status"] = "preparing"
    write_json_atomic(path, raw)
    assert not read_generation_work(config).actionable
    with exclusive_cycle_lock(config.controller.state_dir):
        prepare_retry_requests(config)
    assert not read_generation_work(config).actionable
    assert read_generation_work(config).terminal_blocked[0].state == "retry_preparation_blocked"
    # A deliberately new request permits safe reconciliation of already-runnable state.
    queue_retries(config.controller.state_dir, [101])
    with exclusive_cycle_lock(config.controller.state_dir):
        prepare_retry_requests(config)
    assert retry_requests(config.controller.state_dir)[-1].status == "eligible"


def test_full_history_never_evicts_unresolved_holds_and_allows_explicit_reconciliation(
    tmp_path: Path,
) -> None:
    config = app(tmp_path)
    fresh_snapshot(config)
    state = config.controller.state_dir
    ensure_generation_retry(config, 1, "Example", "Avis example", tmp_path / "fixture")
    queue_retries(state, [1])
    path = state / "retry-requests.json"
    raw = json.loads(path.read_text())
    first = raw["requests"][0]
    raw["requests"] = [
        {
            **first,
            "request_id": f"{id:032x}",
            "taxon_id": id,
            "status": "blocked",
            "outcome": "interrupted",
        }
        for id in range(1, MAX_REQUESTS + 1)
    ]
    write_json_atomic(path, raw)
    with pytest.raises(CatalogError, match="full"):
        queue_retries(state, [1000])
    assert read_generation_work(config).terminal_blocked[0].species.taxon_id == 1
    queue_retries(state, [1])
    assert not read_generation_work(config).actionable
    assert len(retry_requests(state)) == MAX_REQUESTS
    assert retry_requests(state)[-1].status == "queued"
    with exclusive_cycle_lock(state):
        prepare_retry_requests(config)
    assert retry_requests(state)[-1].status == "eligible"
    assert [bird.taxon_id for bird in read_generation_work(config).actionable] == [1]


def test_queued_retry_is_not_runnable_before_preparation(tmp_path: Path) -> None:
    config = app(tmp_path)
    fresh_snapshot(config)
    ensure_generation_retry(config, 101, "Example", "Avis example", tmp_path / "fixture")
    assert [bird.taxon_id for bird in read_generation_work(config).actionable] == [101]
    queue_retries(config.controller.state_dir, [101])
    assert not read_generation_work(config).actionable
    with exclusive_cycle_lock(config.controller.state_dir):
        prepare_retry_requests(config)
    assert [bird.taxon_id for bird in read_generation_work(config).actionable] == [101]


def test_successful_explicit_cli_recovery_clears_a_browser_hold(tmp_path: Path) -> None:
    config = app(tmp_path)
    fresh_snapshot(config)
    state = config.controller.state_dir
    bird = BirdSpecies(101, "Example", "Avis example", 1, "fixture")
    record_failure(state, bird, QualityReviewError("synthetic review failure"))
    ensure_generation_retry(
        config, 101, bird.common_name, bird.scientific_name, tmp_path / "fixture"
    )
    queue_retries(state, [101])
    path = state / "retry-requests.json"
    raw = json.loads(path.read_text())
    raw["requests"][0].update(status="blocked", outcome="retained_state")
    write_json_atomic(path, raw)
    assert not read_generation_work(config).actionable
    result = retry_species(config, 101, refresh_research=True)
    assert result["status"] == "eligible"
    assert retry_requests(state)[-1].status == "eligible"
    assert [bird.taxon_id for bird in read_generation_work(config).actionable] == [101]


def test_provider_activity_tracks_io_and_preserves_last_success_without_errors(
    tmp_path: Path,
) -> None:
    config = app(tmp_path)
    config = replace(
        config,
        discovery=replace(
            config.discovery,
            sources=(DiscoveryProvider.BIRDWEATHER,),
            birdweather_token="synthetic-not-a-real-token",
        ),
    )

    def fetch(**kwargs: object) -> list[object]:
        assert entries(config.controller.state_dir)["source-birdweather"]["status"] == "running"
        return []

    with (
        patch("inky_bird_frame.controller.fetch_birdweather_species", side_effect=fetch),
        patch("inky_bird_frame.controller.resolve_birdweather_species", return_value=([], [])),
    ):
        discover_species(config, record_activity=True)
    first = entries(config.controller.state_dir)["source-birdweather"]
    assert first["status"] == "succeeded" and first["species_count"] == 0
    with (
        patch(
            "inky_bird_frame.controller.fetch_birdweather_species",
            side_effect=DataSourceError("PRIVATE_UPSTREAM_RESPONSE"),
        ),
        pytest.raises(DataSourceError),
    ):
        discover_species(config, record_activity=True)
    failed = entries(config.controller.state_dir)["source-birdweather"]
    assert failed["status"] == "failed" and failed["last_success_at"] == first["last_success_at"]
    assert failed["first_started_at"] == first["first_started_at"]
    assert "PRIVATE_" not in json.dumps(read_activity(config.controller.state_dir))
