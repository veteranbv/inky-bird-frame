from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from inky_bird_frame.birds import ObservationWindow
from inky_bird_frame.catalog import CollectionEntry, CollectionOrigin, write_collection
from inky_bird_frame.config import (
    AppConfig,
    ControllerConfig,
    DiscoveryConfig,
    DiscoveryProvider,
    DisplayNodeConfig,
)
from inky_bird_frame.http import write_json_atomic
from inky_bird_frame.journal import (
    MAX_JOURNAL_BYTES,
    JournalConflictError,
    JournalError,
    JournalService,
    read_journal,
    remove_collection,
    save_collection,
)
from inky_bird_frame.web import create_app

STAMP = "2026-10-15T12:00:00+00:00"
ORIGIN = "https://inky.example.test"
PNG = b"\x89PNG\r\n\x1a\nsynthetic-test-image"


def config(root: Path) -> ControllerConfig:
    return ControllerConfig(
        root / "work",
        root / "catalog",
        root / "state",
        Path("codex"),
        None,
        "127.0.0.1",
        8000,
        3,
        1,
        3,
        owner_allowed_origins=(ORIGIN,),
    )


def catalog(controller: ControllerConfig) -> None:
    species = controller.catalog_dir / "species/1-robin"
    species.mkdir(parents=True)
    (species / "portrait.png").write_bytes(PNG)
    entry = {
        "taxon_id": 1,
        "common_name": "Robin",
        "scientific_name": "Turdus migratorius",
        "slug": "robin",
        "portrait_path": "species/1-robin/portrait.png",
        "display_path": "species/1-robin/display.png",
        "portrait_sha256": hashlib.sha256(PNG).hexdigest(),
        "display_sha256": "a" * 64,
        "approved_at": STAMP,
        "private_account": "DO_NOT_PROJECT",
    }
    write_json_atomic(
        controller.catalog_dir / "index.json",
        {"schema_version": 1, "generated_at": STAMP, "species": [entry]},
    )
    write_json_atomic(
        species / "manifest.json",
        {"status": "approved", "profile": {}, "private_review": "DO_NOT_PROJECT"},
    )


def test_named_selections_leave_legacy_membership_and_generation_untouched(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    write_collection(
        controller.state_dir,
        [CollectionEntry(1, STAMP, CollectionOrigin.MANUAL)],
        legacy_seed_queue_migrated_at=None,
    )
    before = (controller.state_dir / "collection.json").read_bytes()
    first = save_collection(
        controller.state_dir, revision=0, label="  Birds to learn  ", taxon_ids=[2, 1]
    )
    assert first.collections[0].taxon_ids == (1, 2)
    assert first.collections[0].label == "Birds to learn"
    assert (controller.state_dir / "journal.json").stat().st_mode & 0o777 == 0o600
    snapshot = JournalService(controller).snapshot()
    birds = snapshot["birds"]
    assert isinstance(birds, list)
    assert [bird["taxon_id"] for bird in birds] == [1, 2]
    assert birds[1]["plate_ready"] is False
    assert snapshot["active_taxon_ids"] == []
    assert (controller.state_dir / "collection.json").read_bytes() == before
    assert not (controller.state_dir / "generation-queue.json").exists()
    assert not (controller.state_dir / "display-last-success.json").exists()
    assert "DO_NOT_PROJECT" not in json.dumps(snapshot)
    removed = remove_collection(
        controller.state_dir, revision=1, collection_id=first.collections[0].collection_id
    )
    assert removed.collections == ()
    assert (controller.state_dir / "collection.json").read_bytes() == before


def test_browsing_art_never_claims_private_evidence_or_writes_state(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    snapshot = JournalService(controller).snapshot()
    assert snapshot["collections"] == []
    assert snapshot["saved_taxon_ids"] == snapshot["active_taxon_ids"] == []
    assert isinstance(snapshot["birds"], list)
    assert snapshot["birds"][0]["known"] is False
    assert snapshot["birds"][0]["sources"] == []
    assert not (controller.state_dir / "journal.json").exists()
    assert not (controller.state_dir / "collection.json").exists()


def test_stale_edits_and_concurrent_creates_do_not_overwrite(tmp_path: Path) -> None:
    def create(label: str) -> object:
        try:
            return save_collection(tmp_path, revision=0, label=label, taxon_ids=[])
        except JournalConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(create, ["First", "Second"]))
    assert sum(isinstance(value, JournalConflictError) for value in outcomes) == 1
    assert len(read_journal(tmp_path).collections) == 1
    with pytest.raises(JournalConflictError):
        remove_collection(tmp_path, revision=0, collection_id="a" * 32)
    with pytest.raises(JournalConflictError):
        save_collection(tmp_path, revision=1, label="Missing", taxon_ids=[], collection_id="a" * 32)


@pytest.mark.parametrize("taxa", [[True], [0], [-1], [1, 1], ["1"], "1"])
def test_invalid_taxa_fail_before_state_write(tmp_path: Path, taxa: object) -> None:
    with pytest.raises(JournalError):
        save_collection(tmp_path, revision=0, label="Birds", taxon_ids=taxa)
    assert not (tmp_path / "journal.json").exists()


@pytest.mark.parametrize("label", ["", "   ", "a" * 129, "Birds\nsecret", "Birds\u202e"])
def test_invalid_labels_fail_closed(tmp_path: Path, label: str) -> None:
    with pytest.raises(JournalError):
        save_collection(tmp_path, revision=0, label=label, taxon_ids=[])


def test_private_state_permissions_symlink_corruption_and_bound(tmp_path: Path) -> None:
    save_collection(tmp_path, revision=0, label="Birds", taxon_ids=[])
    path = tmp_path / "journal.json"
    path.chmod(0o644)
    with pytest.raises(JournalError, match="0600"):
        read_journal(tmp_path)
    path.chmod(0o600)
    path.write_text("x" * (MAX_JOURNAL_BYTES + 1))
    with pytest.raises(JournalError, match="size"):
        read_journal(tmp_path)
    path.write_text("not json")
    with pytest.raises(JournalError, match="cannot be read"):
        read_journal(tmp_path)
    target = tmp_path / "outside.json"
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(JournalError, match="regular file"):
        read_journal(tmp_path)


def test_profile_and_portrait_allowlists_integrity_and_symlink_boundary(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    service = JournalService(controller)
    assert service.portrait(1) == PNG
    assert service.profile(1)["profile"] is None
    assert "DO_NOT_PROJECT" not in json.dumps(service.profile(1))
    with pytest.raises(JournalError):
        service.portrait(99)
    path = controller.catalog_dir / "species/1-robin/portrait.png"
    path.write_bytes(b"corrupt")
    with pytest.raises(JournalError, match="integrity"):
        service.portrait(1)
    outside = tmp_path / "secret"
    outside.write_bytes(PNG)
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(JournalError, match="unavailable"):
        service.portrait(1)


def test_every_journal_route_uses_owner_and_csrf_boundary(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    with TestClient(create_app(controller), base_url=ORIGIN) as client:
        for route in [
            "/v1/owner/journal",
            "/v1/owner/journal/birds/1/portrait",
            "/v1/owner/journal/birds/1/profile",
        ]:
            response = client.get(route)
            assert response.status_code == 401
            assert response.headers["cache-control"] == "no-store"
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        login = client.post(
            "/v1/owner/register",
            headers={"Origin": ORIGIN},
            json={"username": "qa-owner", "passphrase": "a synthetic long passphrase"},
        )
        csrf = login.json()["data"]["csrf"]
        body = {"revision": 0, "label": "<script>not executable</script>", "taxon_ids": [1]}
        assert (
            client.post(
                "/v1/owner/journal/collections/save", json=body, headers={"Origin": ORIGIN}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/v1/owner/journal/collections/save",
                json=body,
                headers={"Origin": "https://wrong.example", "X-Inky-CSRF": csrf},
            ).status_code
            == 403
        )
        saved = client.post(
            "/v1/owner/journal/collections/save",
            json=body,
            headers={"Origin": ORIGIN, "X-Inky-CSRF": csrf},
        )
        assert saved.status_code == 200
        assert (
            client.post(
                "/v1/owner/journal/collections/save",
                json=body,
                headers={"Origin": ORIGIN, "X-Inky-CSRF": csrf},
            ).status_code
            == 409
        )
        assert client.get("/v1/owner/journal/birds/1/portrait").content == PNG
        assert client.get("/v1/owner/journal").json()["data"]["revision"] == 1
        assert client.get("/app/journal.js").status_code == 200
        assert client.get("/app/../journal.json").status_code == 404
        # Corruption errors never echo a private filename or a stored payload.
        (controller.state_dir / "journal.json").write_text("PRIVATE_PAYLOAD")
        failed = client.get("/v1/owner/journal")
        assert failed.status_code == 400
        assert "PRIVATE_PAYLOAD" not in failed.text and str(tmp_path) not in failed.text


def test_journal_keeps_missing_birds_and_projects_all_source_types_without_raw_errors(
    tmp_path: Path,
) -> None:
    controller = config(tmp_path)
    catalog(controller)
    app = AppConfig(
        DiscoveryConfig(
            "12345",
            None,
            None,
            None,
            None,
            8,
            20,
            ObservationWindow.LAST_30_DAYS,
            tuple(DiscoveryProvider),
        ),
        controller,
        DisplayNodeConfig("http://localhost:8000", tmp_path / "display"),
    )
    write_json_atomic(
        controller.state_dir / "discovery.json",
        {
            "schema_version": 2,
            "refreshed_at": STAMP,
            "place_name": "PRIVATE_PLACE",
            "state": "",
            "species": [
                {
                    "taxon_id": 2,
                    "common_name": "Missing bird",
                    "scientific_name": "Example bird",
                    "observation_count": 1,
                    "source": "ebird-archive",
                    "sources": ["ebird-archive"],
                }
            ],
            "notification_baseline_taxon_ids": [2],
            "providers": [
                {
                    "name": "ebird-archive",
                    "status": "ok",
                    "species_count": 1,
                    "details": {"raw_checklist_id": "PRIVATE_ID"},
                },
                {
                    "name": "birdbuddy",
                    "status": "error",
                    "species_count": 0,
                    "error": "PRIVATE_EMAIL_AND_TOKEN",
                    "details": {"feeder_id": "PRIVATE_ID"},
                },
            ],
        },
    )
    payload = JournalService(controller, app_config=app).snapshot()
    assert isinstance(payload["birds"], list)
    missing = next(bird for bird in payload["birds"] if bird["taxon_id"] == 2)
    assert missing["known"] and not missing["plate_ready"]
    assert missing["sources"] == ["ebird-archive"]
    management = payload["management"]
    assert isinstance(management, dict)
    assert len(management["sources"]) == 7
    assert management["work"]["can_run"] == []  # Stale discovery must not invent eligible work.
    assert "PRIVATE_" not in json.dumps(payload)
    assert management["last_confirmed_panel_update"] is None
