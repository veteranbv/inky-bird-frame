from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep
from typing import Any, cast
from unittest.mock import patch

import pytest
from PIL import Image
from starlette.testclient import TestClient

from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.catalog import (
    has_valid_approved_candidate,
    read_collection,
    write_candidate_manifest,
)
from inky_bird_frame.config import AppConfig, load_config
from inky_bird_frame.controller import (
    OWNER_SELECTION_SOURCE,
    add_collection_member,
    change_saved_collection,
    queue_owner_selection_locked,
    read_generation_queue,
    run_generation_cycle,
    run_refresh_cycle,
)
from inky_bird_frame.errors import CatalogError
from inky_bird_frame.http import write_json_atomic
from inky_bird_frame.journal import (
    JournalCollection,
    JournalConflictError,
    JournalError,
    JournalMember,
    JournalService,
    save_imported_collection,
)
from inky_bird_frame.models import QualityReview, SourceLink, SpeciesProfileData
from inky_bird_frame.recovery import (
    decide_pending_candidate,
    pending_candidate,
    pending_inventory,
    pending_portrait,
)
from inky_bird_frame.retry import RetryStore
from inky_bird_frame.settings import initialize_config, settings_status
from inky_bird_frame.web import create_app

ORIGIN = "https://inky.example.test"


def candidate(config: AppConfig) -> Path:
    bird = BirdSpecies(10, "Fixture candidate", "Fixture candidate", 1, "fixture")
    root = config.controller.state_dir / "pending/10-fixture-candidate"
    root.mkdir(parents=True)
    for filename, size in (("portrait.png", (1200, 1600)), ("display.png", (1600, 1200))):
        Image.new("RGB", size, "white").save(root / filename)
    links = [
        SourceLink(title="Fixture source one", url="https://first.example/bird"),
        SourceLink(title="Fixture source two", url="https://second.example/bird"),
    ]
    profile = SpeciesProfileData(
        taxon_id=10,
        common_name=bird.common_name,
        scientific_name=bird.scientific_name,
        family="Fixture family",
        measurements={"length": "10 cm", "wingspan": "20 cm", "weight": "30 g"},
        field_marks=["First mark", "Second mark", "Third mark", "Fourth mark"],
        habitat="Fixture habitat",
        behavior="Fixture behavior",
        palette=["white", "black", "gray"],
        sources=links,
    )
    review = QualityReview(True, 5, 5, 5, 5, True, (), tuple(links))
    write_candidate_manifest(
        root, bird, profile, [], review, generator="fixture", prompt_version="fixture"
    )
    return root


def test_saved_membership_preview_binds_catalog_and_uses_existing_collection(
    tmp_path: Path,
) -> None:
    application = config(tmp_path)
    candidate(application)
    view = pending_candidate(application, 10)
    decide_pending_candidate(
        application, 10, revision=str(view["candidate_revision"]), approve=True
    )
    preview = change_saved_collection(
        application, action="import-approved", taxon_id=None, dry_run=True
    )
    assert preview["added_count"] == 1
    assert not (application.controller.state_dir / "collection.json").exists()
    result = change_saved_collection(
        application,
        action="import-approved",
        taxon_id=None,
        dry_run=False,
        expected_revision=str(preview["collection_revision"]),
    )
    assert result["added_count"] == 1
    assert [entry.taxon_id for entry in read_collection(application.controller.state_dir)] == [10]
    assert not (application.controller.state_dir / "generation-queue.json").exists()
    preview = change_saved_collection(application, action="remove", taxon_id=10, dry_run=True)
    add_collection_member(application, 99)
    with pytest.raises(CatalogError, match="changed"):
        change_saved_collection(
            application,
            action="remove",
            taxon_id=10,
            dry_run=False,
            expected_revision=str(preview["collection_revision"]),
        )
    assert 10 in [entry.taxon_id for entry in read_collection(application.controller.state_dir)]
    with pytest.raises(CatalogError, match="approved"):
        change_saved_collection(application, action="add", taxon_id=999, dry_run=True)


def test_pending_review_uses_publication_gates_and_exact_candidate_binding(tmp_path: Path) -> None:
    application = config(tmp_path)
    root = candidate(application)
    view = cast(dict[str, Any], pending_candidate(application, 10))
    assert view["can_approve"]
    assert view["profile"]["common_name"] == "Fixture candidate"
    assert len(view["verification_sources"]) == 2
    assert (
        pending_portrait(application, 10, revision=view["candidate_revision"])
        == (root / "portrait.png").read_bytes()
    )
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["quality_review"]["findings"] = ["Fixture revised finding"]
    write_json_atomic(root / "manifest.json", manifest)
    with pytest.raises(JournalConflictError):
        decide_pending_candidate(application, 10, revision=view["candidate_revision"], approve=True)
    with pytest.raises(JournalConflictError):
        pending_portrait(application, 10, revision=view["candidate_revision"])
    updated = pending_candidate(application, 10)
    outcome = decide_pending_candidate(
        application, 10, revision=str(updated["candidate_revision"]), approve=True
    )
    assert outcome["status"] == "approved"
    assert has_valid_approved_candidate(application.controller.catalog_dir, 10)
    assert not root.exists()
    assert not (application.controller.state_dir / "display-last-success.json").exists()


@pytest.mark.parametrize(
    "problem",
    ["missing-sources", "incomplete-profile", "malformed-source", "extra-file", "private-metadata"],
)
def test_invalid_candidates_cannot_bypass_ordinary_approval(tmp_path: Path, problem: str) -> None:
    application = config(tmp_path)
    root = candidate(application)
    manifest = json.loads((root / "manifest.json").read_text())
    if problem == "missing-sources":
        manifest["quality_review"]["verification_sources"] = []
    elif problem == "incomplete-profile":
        manifest["profile"] = {}
    elif problem == "malformed-source":
        manifest["profile"]["sources"][0]["url"] = "https://["
    elif problem == "extra-file":
        (root / "private.env").write_text("MUST_NOT_PROJECT")
    else:
        manifest["password"] = "MUST_NOT_PROJECT"
    write_json_atomic(root / "manifest.json", manifest)
    view = pending_candidate(application, 10)
    assert not view["can_approve"]
    assert "MUST_NOT_PROJECT" not in json.dumps(view)
    with pytest.raises(JournalError):
        decide_pending_candidate(
            application, 10, revision=str(view["candidate_revision"]), approve=True
        )
    assert root.exists()
    assert not has_valid_approved_candidate(application.controller.catalog_dir, 10)
    result = decide_pending_candidate(
        application,
        10,
        revision=str(view["candidate_revision"]),
        approve=False,
        reason="Fixture rejection: inspect the shape",
    )
    assert result["status"] == "rejected"
    assert not root.exists()
    assert (
        application.controller.state_dir / "rejected/10-fixture-candidate/portrait.png"
    ).is_file()


def test_pending_inventory_retains_incomplete_work_without_reading_unsafe_art(
    tmp_path: Path,
) -> None:
    application = config(tmp_path)
    root = candidate(application)
    portrait = root / "portrait.png"
    portrait.unlink()
    portrait.symlink_to(tmp_path / "outside.png")
    inventory = cast(dict[str, Any], pending_inventory(application))
    assert not inventory["candidates"][0]["inspect_available"]
    with pytest.raises(JournalError):
        pending_candidate(application, 10)


def test_http_candidate_decision_requires_confirmation_and_exact_sourced_art(
    tmp_path: Path,
) -> None:
    application = config(tmp_path)
    candidate(application)
    with TestClient(
        create_app(application.controller, app_config=application), base_url=ORIGIN
    ) as client:
        assert client.get("/v1/owner/candidates/10").status_code == 401
        login = client.post(
            "/v1/owner/register",
            json={"username": "fixture-owner", "passphrase": "long synthetic password"},
            headers={"Origin": ORIGIN},
        )
        headers = {"Origin": ORIGIN, "X-Inky-CSRF": login.json()["data"]["csrf"]}
        view = client.get("/v1/owner/candidates/10").json()["data"]
        portrait = client.get(view["portrait_url"])
        assert portrait.status_code == 200
        assert portrait.headers["cache-control"] == "no-store"
        assert (
            client.get("/v1/owner/candidates/10/portrait?revision=" + "0" * 64).status_code == 409
        )
        body = {
            "key": "synthetic-candidate-approval",
            "configuration_revision": settings_status(application)["revision"],
            "candidate_revision": view["candidate_revision"],
            "confirm": True,
        }
        route = "/v1/owner/candidates/10/approve"
        assert client.post(route, json=body, headers={"Origin": ORIGIN}).status_code == 401
        assert (
            client.post(route, json={**body, "confirm": False}, headers=headers).status_code == 400
        )
        started = client.post(route, json=body, headers=headers)
        assert started.status_code == 200
        receipt_id = started.json()["data"]["operation_id"]
        deadline = monotonic() + 5
        while monotonic() < deadline:
            receipt = client.get("/v1/owner/operations/" + receipt_id).json()["data"]
            if receipt["status"] not in {"pending", "running"}:
                break
            sleep(0.005)
        assert receipt["status"] == "succeeded"
        assert receipt["result"]["status"] == "approved"
        assert (
            client.post(route, json=body, headers=headers).json()["data"]["operation_id"]
            == receipt_id
        )
        assert has_valid_approved_candidate(application.controller.catalog_dir, 10)


def config(root: Path) -> AppConfig:
    path = root / "config.toml"
    initialize_config(path)
    path.write_text(
        path.read_text().replace(
            '["controller"]', '["controller"]\nowner_allowed_origins = ["' + ORIGIN + '"]'
        )
    )
    return load_config(path, load_secrets=False)


def group(config: AppConfig) -> JournalCollection:
    selection = JournalCollection(
        "a" * 32,
        "Learning birds",
        (
            JournalMember(1, "Fixture Robin", "Fixture robin"),
            JournalMember(2, "Held bird", "Fixture held"),
            JournalMember(None, "Unresolved source bird", "Fixture unresolved"),
        ),
        created_revision=1,
        updated_revision=1,
    )
    save_imported_collection(config.controller.state_dir, revision=0, collection=selection)
    return selection


def test_explicit_requests_preserve_holds_unresolved_and_separate_membership(
    tmp_path: Path,
) -> None:
    application = config(tmp_path)
    selection = group(application)
    (application.controller.state_dir / "failed/2-fixture").mkdir(parents=True)
    service = JournalService(application.controller, app_config=application)
    preview = cast(
        dict[str, Any], service.queue_preview(collection_id=selection.collection_id, revision=1)
    )
    assert preview["plan"]["new_requests"] == [1]
    assert preview["plan"]["held"] == [2]
    assert preview["unresolved"] == 1
    assert not (application.controller.state_dir / "generation-queue.json").exists()
    result = service.queue_collection(
        collection_id=selection.collection_id, revision=1, preview_hash=preview["preview_hash"]
    )
    assert result["queued"] == 1
    queued = read_generation_queue(application)
    assert queued[0].sources == (OWNER_SELECTION_SOURCE,)
    assert queued[0].latest_detection_at is None
    assert not (application.controller.state_dir / "collection.json").exists()
    assert not (application.controller.state_dir / "display-last-success.json").exists()
    before = (application.controller.state_dir / "generation-queue.json").read_bytes()
    preview = cast(
        dict[str, Any], service.queue_preview(collection_id=selection.collection_id, revision=1)
    )
    assert preview["plan"]["already_queued"] == [1]
    service.queue_collection(
        collection_id=selection.collection_id, revision=1, preview_hash=preview["preview_hash"]
    )
    assert (application.controller.state_dir / "generation-queue.json").read_bytes() == before
    run_refresh_cycle(application)
    assert read_collection(application.controller.state_dir) == []


def test_queue_confirmation_rejects_changed_work_and_keeps_retry_timing(tmp_path: Path) -> None:
    application = config(tmp_path)
    selection = group(application)
    service = JournalService(application.controller, app_config=application)
    preview = cast(
        dict[str, Any], service.queue_preview(collection_id=selection.collection_id, revision=1)
    )
    bird = BirdSpecies(1, "Fixture Robin", "Fixture robin", 1, OWNER_SELECTION_SOURCE)
    retry = RetryStore(application.controller.state_dir / "generation-retries.json")
    retry.record_failure(
        bird.taxon_id,
        RuntimeError("fixture transient failure"),
        now=datetime.now(UTC),
        initial_minutes=30,
        maximum_minutes=1440,
        species=bird,
    )
    before = (application.controller.state_dir / "generation-retries.json").read_bytes()
    with pytest.raises(JournalConflictError):
        service.queue_collection(
            collection_id=selection.collection_id, revision=1, preview_hash=preview["preview_hash"]
        )
    preview = cast(
        dict[str, Any], service.queue_preview(collection_id=selection.collection_id, revision=1)
    )
    assert preview["plan"]["waiting"][0]["taxon_id"] == 1
    service.queue_collection(
        collection_id=selection.collection_id, revision=1, preview_hash=preview["preview_hash"]
    )
    assert (application.controller.state_dir / "generation-retries.json").read_bytes() == before
    assert read_generation_queue(application)[1].taxon_id == 2
    record = retry.get(1)
    assert record is not None
    assert record.next_attempt_at > datetime.now(UTC) + timedelta(minutes=29)


def test_http_queue_confirmation_is_private_csrf_protected_and_idempotent(tmp_path: Path) -> None:
    application = config(tmp_path)
    selection = group(application)
    with TestClient(
        create_app(application.controller, app_config=application), base_url=ORIGIN
    ) as client:
        route = "/v1/owner/journal/collections/queue"
        assert client.post(route, json={}, headers={"Origin": ORIGIN}).status_code == 401
        login = client.post(
            "/v1/owner/register",
            json={"username": "fixture-owner", "passphrase": "long synthetic password"},
            headers={"Origin": ORIGIN},
        )
        headers = {"Origin": ORIGIN, "X-Inky-CSRF": login.json()["data"]["csrf"]}
        body: dict[str, object] = {
            "collection_id": selection.collection_id,
            "revision": 1,
            "configuration_revision": settings_status(application)["revision"],
        }
        preview = client.post(
            "/v1/owner/journal/collections/queue-preview", json=body, headers=headers
        )
        assert preview.status_code == 200
        body.update(
            preview_hash=preview.json()["data"]["preview_hash"],
            key="synthetic-queue-request",
            confirm=True,
        )
        assert client.post(route, json=body, headers={"Origin": ORIGIN}).status_code == 401
        assert not (application.controller.state_dir / "generation-queue.json").exists()
        started = client.post(route, json=body, headers=headers)
        assert started.status_code == 200
        receipt_id = started.json()["data"]["operation_id"]
        deadline = monotonic() + 5
        while monotonic() < deadline:
            receipt = client.get("/v1/owner/operations/" + receipt_id).json()["data"]
            if receipt["status"] not in {"pending", "running"}:
                break
            sleep(0.005)
        assert receipt["status"] == "succeeded"
        assert receipt["result"]["queued"] == 2
        assert (
            client.post(route, json=body, headers=headers).json()["data"]["operation_id"]
            == receipt_id
        )
        assert len(read_generation_queue(application)) == 2
        assert not (application.controller.state_dir / "collection.json").exists()


def test_generation_completion_keeps_owner_requests_added_during_the_cycle(tmp_path: Path) -> None:
    application = config(tmp_path)
    run_refresh_cycle(application)
    enabled = replace(
        application, controller=replace(application.controller, generation_enabled=True)
    )
    bird = BirdSpecies(44, "Later request", "Fixture later", 1, OWNER_SELECTION_SOURCE)
    from inky_bird_frame import controller as controller_module

    original = controller_module._write_active_catalog
    added = False

    def add_before_cycle_finishes(config: AppConfig, species: list[BirdSpecies]) -> int:
        nonlocal added
        if not added:
            # run_generation_cycle already owns the catalog lock here.
            queue_owner_selection_locked(config, [bird])
            added = True
        return original(config, species)

    with (
        patch(
            "inky_bird_frame.controller._write_active_catalog",
            side_effect=add_before_cycle_finishes,
        ),
        patch("inky_bird_frame.controller.CodexRunner"),
    ):
        run_generation_cycle(enabled)
    assert read_generation_queue(application) == [bird]
    assert read_collection(application.controller.state_dir) == []
