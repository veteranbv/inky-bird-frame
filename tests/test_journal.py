from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from inky_bird_frame.birds import EbirdArchiveSpecies, ObservationWindow
from inky_bird_frame.catalog import (
    CollectionEntry,
    CollectionOrigin,
    rebuild_catalog_index,
    write_collection,
)
from inky_bird_frame.config import (
    AppConfig,
    ControllerConfig,
    DiscoveryConfig,
    DiscoveryProvider,
    DisplayNodeConfig,
)
from inky_bird_frame.ebird_archive import EbirdArchiveHistory
from inky_bird_frame.errors import CatalogError
from inky_bird_frame.http import write_json_atomic
from inky_bird_frame.journal import (
    MAX_JOURNAL_BYTES,
    CollectionKind,
    JournalConflictError,
    JournalError,
    JournalMember,
    JournalService,
    JournalState,
    MemberOrigin,
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


def test_manual_selection_retains_names_and_rejects_unknown_ids(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    service = JournalService(controller)
    state = service.save_selection(revision=0, label="Learning birds", taxon_ids=[1])
    assert state.collections[0].members == (JournalMember(1, "Robin", "Turdus migratorius"),)
    (controller.catalog_dir / "index.json").unlink()
    birds = cast(list[dict[str, object]], service.snapshot()["birds"])
    assert birds[0]["scientific_name"] == "Turdus migratorius"
    with pytest.raises(JournalError, match="Choose birds"):
        service.save_selection(revision=1, label="Unknown", taxon_ids=[999])
    assert read_journal(controller.state_dir) == state


def test_retained_library_does_not_truncate_or_publish_partial_source_coverage(
    tmp_path: Path,
) -> None:
    controller = config(tmp_path)
    catalog(controller)
    controller.state_dir.mkdir(parents=True)
    (controller.state_dir / "ebird-archive-observations.json").write_text("{}")
    history = EbirdArchiveHistory(
        [
            EbirdArchiveSpecies("Robin", "Turdus migratorius", 4),
            EbirdArchiveSpecies("Unmatched", "Fixture unknown", 2),
            EbirdArchiveSpecies("Another", "Fixture another", 1),
        ],
        7,
        7,
        3,
        7,
        0,
        STAMP,
        STAMP,
        "2020-01-01",
        "2020-01-07",
    )
    service = JournalService(controller)
    with patch(
        "inky_bird_frame.ebird_archive.read_ebird_archive_history", return_value=history
    ) as reader:
        snapshot = service.snapshot()
    assert reader.call_args.kwargs["limit"] is None
    birds = cast(list[dict[str, object]], snapshot["birds"])
    assert birds[0]["known"] and birds[0]["retained_evidence"] == [
        {"source": "ebird-archive", "count": 4, "unit": "checklists"}
    ]
    assert len(cast(list[object], snapshot["unresolved_history"])) == 2
    assert not (controller.state_dir / "generation-queue.json").exists()
    history.species[1] = EbirdArchiveSpecies("Bad\nname", "Fixture unknown", 2)
    with patch("inky_bird_frame.ebird_archive.read_ebird_archive_history", return_value=history):
        snapshot = service.snapshot()
    assert len(cast(list[object], snapshot["history_coverage"])) == 1
    assert cast(list[dict[str, object]], snapshot["history_coverage"])[0]["status"] == "unavailable"
    assert cast(list[dict[str, object]], snapshot["birds"])[0]["retained_evidence"] == []
    assert snapshot["unresolved_history"] == []


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


def test_first_run_with_canonical_empty_approved_index_is_a_valid_empty_journal(
    tmp_path: Path,
) -> None:
    controller = config(tmp_path)
    rebuild_catalog_index(controller.catalog_dir)
    before = (controller.catalog_dir / "index.json").read_bytes()
    snapshot = JournalService(controller).snapshot()
    assert snapshot["birds"] == []
    assert snapshot["featured_taxon_id"] is None
    assert (controller.catalog_dir / "index.json").read_bytes() == before
    write_json_atomic(
        controller.catalog_dir / "index.json",
        {
            "schema_version": True,
            "generated_at": None,
            "species": [],
        },
    )
    with pytest.raises(CatalogError):
        JournalService(controller).snapshot()


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


def test_storage_limit_checks_exact_persisted_bytes_and_preserves_previous_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = save_collection(tmp_path, revision=0, label="First", taxon_ids=list(range(1, 30)))
    before = (tmp_path / "journal.json").read_bytes()
    next_group = type(first.collections[0])(
        "a" * 32, "Second", tuple(JournalMember(id) for id in range(1, 30))
    )
    proposed = JournalState(2, (*first.collections, next_group))
    compact_bytes = len(json.dumps(proposed.as_dict()).encode())
    persisted_bytes = len(
        (json.dumps(proposed.as_dict(), indent=2, sort_keys=True) + "\n").encode()
    )
    limit = max(compact_bytes, len(before)) + 1
    assert compact_bytes < limit < persisted_bytes
    monkeypatch.setattr("inky_bird_frame.journal.MAX_JOURNAL_BYTES", limit)
    with pytest.raises(JournalError, match="storage is full"):
        save_collection(tmp_path, revision=1, label="Second", taxon_ids=list(range(1, 30)))
    assert (tmp_path / "journal.json").read_bytes() == before
    assert read_journal(tmp_path) == first


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


def test_v1_migration_happens_only_on_edit_and_keeps_exact_private_backup(tmp_path: Path) -> None:
    original = {
        "schema_version": 1,
        "revision": 4,
        "collections": [{"collection_id": "a" * 32, "label": "Old collection", "taxon_ids": [1]}],
    }
    write_json_atomic(tmp_path / "journal.json", original, mode=0o600)
    before = (tmp_path / "journal.json").read_bytes()
    state = read_journal(tmp_path)
    assert state.collections[0].kind == CollectionKind.SELECTION
    assert state.collections[0].members[0].origin == MemberOrigin.SAVED
    assert not (tmp_path / "journal-v1-backup.json").exists()
    assert (tmp_path / "journal.json").read_bytes() == before
    save_collection(tmp_path, revision=4, label="Renamed", taxon_ids=[1], collection_id="a" * 32)
    backup = tmp_path / "journal-v1-backup.json"
    assert backup.read_bytes() == before
    assert backup.stat().st_mode & 0o777 == 0o600
    assert read_journal(tmp_path).revision == 5
    save_collection(tmp_path, revision=5, label="Another", taxon_ids=[])
    assert backup.read_bytes() == before


def test_favorites_are_separate_idempotent_bookmarks_with_stale_edit_protection(
    tmp_path: Path,
) -> None:
    controller = config(tmp_path)
    catalog(controller)
    service = JournalService(controller)
    first = service.favorite(revision=0, taxon_id=1, favorite=True)
    assert first.collections[0].kind == CollectionKind.FAVORITES
    assert first.collections[0].taxon_ids == (1,)
    assert service.favorite(revision=1, taxon_id=1, favorite=True) == first
    with pytest.raises(JournalConflictError):
        service.favorite(revision=0, taxon_id=1, favorite=False)
    with pytest.raises(JournalError):
        remove_collection(
            controller.state_dir, revision=1, collection_id=first.collections[0].collection_id
        )
    with pytest.raises(JournalError):
        service.favorite(revision=1, taxon_id=1, favorite="true")
    second = service.favorite(revision=1, taxon_id=1, favorite=False)
    assert second.collections[0].taxon_ids == ()
    assert not (controller.state_dir / "collection.json").exists()
    assert not (controller.state_dir / "generation-queue.json").exists()


@pytest.mark.parametrize("name", ["x" * 257, "Bird\nName"])
def test_favorite_rejects_unstorable_source_names_before_writing(tmp_path: Path, name: str) -> None:
    controller = config(tmp_path)
    catalog(controller)
    first = save_collection(controller.state_dir, revision=0, label="Keep me", taxon_ids=[1])
    before = (controller.state_dir / "journal.json").read_bytes()
    path = controller.catalog_dir / "index.json"
    raw = json.loads(path.read_text())
    raw["species"][0]["common_name"] = name
    write_json_atomic(path, raw)
    with pytest.raises(JournalError):
        JournalService(controller).favorite(revision=1, taxon_id=1, favorite=True)
    assert (controller.state_dir / "journal.json").read_bytes() == before
    assert read_journal(controller.state_dir) == first


def portable_document() -> dict[str, object]:
    return {
        "format": "inky-collection",
        "schema_version": 1,
        "label": "A trip",
        "start_date": "2026-04-29",
        "end_date": "2026-05-02",
        "members": [
            {
                "taxon_id": 1,
                "common_name": "Robin",
                "scientific_name": "Turdus migratorius",
                "origin": "ebird-checklist",
            },
            {
                "taxon_id": 900,
                "common_name": "Unknown",
                "scientific_name": "Unmatched species",
                "origin": "saved-selection",
            },
        ],
    }


def test_portable_import_preview_exact_identity_unresolved_and_reconnect(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    service = JournalService(controller)
    document = portable_document()
    preview = service.collection_import_preview(document)
    assert preview["resolved"] == preview["ready"] == 1
    assert len(cast(list[object], preview["unresolved"])) == 1
    state = service.import_collection(
        document=document,
        revision=0,
        preview_hash=str(preview["preview_hash"]),
        collection_id="a" * 32,
    )
    group = state.collections[0]
    assert group.kind == CollectionKind.IMPORTED
    assert group.members[0].evidence == ()
    assert group.members[1].taxon_id is None
    assert (
        service.import_collection(
            document=document,
            revision=0,
            preview_hash=str(preview["preview_hash"]),
            collection_id="a" * 32,
        )
        == state
    )
    exported = service.export_collection("a" * 32)
    assert set(exported) == {
        "format",
        "schema_version",
        "label",
        "start_date",
        "end_date",
        "members",
    }
    assert "checklist_hash" not in json.dumps(exported)
    assert "ebird-checklist" in json.dumps(exported)  # Origin label, not linked proof.
    save_collection(
        controller.state_dir, revision=1, label="Changed", taxon_ids=[1], collection_id="a" * 32
    )
    with pytest.raises(JournalConflictError):
        service.import_collection(
            document=document,
            revision=0,
            preview_hash=str(preview["preview_hash"]),
            collection_id="a" * 32,
        )
    assert not (controller.state_dir / "ebird-archive.json").exists()
    assert not (controller.state_dir / "generation-queue.json").exists()


@pytest.mark.parametrize("mutation", ["secret", "link", "duplicate", "date", "boolean"])
def test_portable_documents_fail_closed(tmp_path: Path, mutation: str) -> None:
    controller = config(tmp_path)
    catalog(controller)
    document = portable_document()
    if mutation == "secret":
        document["password"] = "not accepted"
    elif mutation == "link":
        cast(list[dict[str, object]], document["members"])[0]["evidence"] = []
    elif mutation == "duplicate":
        members = cast(list[dict[str, object]], document["members"])
        members.append(members[0])
    elif mutation == "date":
        document["end_date"] = "2026-04-28"
    else:
        document["schema_version"] = True
    with pytest.raises(JournalError):
        JournalService(controller).collection_import_preview(document)
    assert not (controller.state_dir / "journal.json").exists()


def test_portable_taxonomy_conflict_does_not_relabel_or_enqueue(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    service = JournalService(controller)
    document = portable_document()
    cast(list[dict[str, object]], document["members"])[0]["scientific_name"] = "Different species"
    preview = service.collection_import_preview(document)
    assert preview["resolved"] == 0
    document["label"] = "Changed after preview"
    with pytest.raises(JournalConflictError):
        service.import_collection(
            document=document,
            revision=0,
            preview_hash=str(preview["preview_hash"]),
            collection_id="a" * 32,
        )


def test_collection_portability_routes_have_owner_and_csrf_guards(tmp_path: Path) -> None:
    controller = config(tmp_path)
    catalog(controller)
    with TestClient(create_app(controller), base_url=ORIGIN) as client:
        assert (
            client.get("/v1/owner/journal/collections/export?collection=" + "a" * 32).status_code
            == 401
        )
        login = client.post(
            "/v1/owner/register",
            headers={"Origin": ORIGIN},
            json={"username": "qa-owner", "passphrase": "a synthetic long passphrase"},
        )
        headers = {"Origin": ORIGIN, "X-Inky-CSRF": login.json()["data"]["csrf"]}
        assert (
            client.post(
                "/v1/owner/journal/collections/favorite",
                json={"revision": 0, "taxon_id": 1, "favorite": True},
                headers={"Origin": ORIGIN},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/v1/owner/journal/collections/favorite",
                json={"revision": 0, "taxon_id": 1, "favorite": True},
                headers=headers,
            ).status_code
            == 200
        )
        preview = client.post(
            "/v1/owner/journal/collections/import-preview",
            json={"document": portable_document()},
            headers=headers,
        )
        assert preview.status_code == 200
        body = {
            "document": portable_document(),
            "revision": 1,
            "preview_hash": preview.json()["data"]["preview_hash"],
            "collection_id": "a" * 32,
        }
        assert (
            client.post(
                "/v1/owner/journal/collections/import", json=body, headers=headers
            ).status_code
            == 200
        )
        exported = client.get("/v1/owner/journal/collections/export?collection=" + "a" * 32)
        assert exported.status_code == 200
        assert exported.headers["cache-control"] == "no-store"
        assert "checklist_hash" not in exported.text
