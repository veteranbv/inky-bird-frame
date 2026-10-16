from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from inky_bird_frame.birds import BirdSpecies, EbirdArchiveSpecies
from inky_bird_frame.config import AppConfig, load_config
from inky_bird_frame.ebird_archive import (
    REQUIRED_COLUMNS,
    import_ebird_archive,
    read_ebird_checklists,
)
from inky_bird_frame.errors import DataSourceError
from inky_bird_frame.imports import HistoryImports, recover_import
from inky_bird_frame.journal import (
    CollectionKind,
    JournalConflictError,
    JournalService,
    read_journal,
    save_collection,
)
from inky_bird_frame.owner_operations import OwnerOperationError, OwnerOperations
from inky_bird_frame.settings import initialize_config, settings_status
from inky_bird_frame.web import create_app

ORIGIN = "https://inky.example.test"


def configuration(root: Path) -> AppConfig:
    path = root / "config.toml"
    initialize_config(path)
    path.write_text(
        path.read_text().replace(
            '["controller"]', '["controller"]\nowner_allowed_origins = ["' + ORIGIN + '"]'
        )
    )
    return load_config(path, load_secrets=False)


def archive(*, extra: bool = True, correction: bool = False) -> bytes:
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=REQUIRED_COLUMNS)
    writer.writeheader()
    records = [
        (
            "S-private-one",
            "2026-04-29",
            "Corvus corax" if correction else "Turdus migratorius",
            "Private park",
        )
    ]
    if extra:
        records.append(("S-private-two", "2026-05-02", "Unmatched species", "Private backyard"))
    for identifier, observed, scientific, location in records:
        row = dict.fromkeys(REQUIRED_COLUMNS, "")
        row.update(
            {
                "Submission ID": identifier,
                "Date": observed,
                "Scientific Name": scientific,
                "Common Name": scientific,
                "Location": location,
            }
        )
        writer.writerow(row)
    return out.getvalue().encode()


def staged(imports: HistoryImports, content: bytes, kind: str = "ebird-archive") -> str:
    identifier, handle = imports.begin(kind)
    with handle:
        handle.write(content)
    imports.finish_upload(identifier, hashlib.sha256(content).hexdigest(), len(content))
    return identifier


def status(imports: HistoryImports) -> dict[str, Any]:
    return cast(dict[str, Any], imports.status()["preview"])


def checked(imports: HistoryImports, identifier: str, revision: str) -> dict[str, Any]:
    imports.preview(identifier, revision=revision)
    return cast(dict[str, Any], status(imports)["preview"])


def test_upload_preview_commit_keeps_locations_transient_and_history_private(
    tmp_path: Path,
) -> None:
    config = configuration(tmp_path)
    imports = HistoryImports(config)
    identifier = staged(imports, archive())
    revision = str(settings_status(config)["revision"])
    preview = checked(imports, identifier, revision)
    assert preview["stats"]["added_checklists"] == 2
    assert "Private park" not in imports.metadata_path.read_text()
    assert "S-private" not in imports.metadata_path.read_text()
    assert imports.path.stat().st_mode & 0o777 == 0o600
    assert imports.root.stat().st_mode & 0o777 == 0o700
    result = imports.commit(
        identifier,
        revision=revision,
        preview_hash=preview["preview_hash"],
        key="synthetic-history-commit",
        allow_reduction=False,
    )
    assert result["message"] == "History updated. Trip not saved yet."
    assert imports.path.exists()
    history = config.controller.state_dir / "ebird-archive-observations.json"
    assert "Private park" not in history.read_text() and "S-private" not in history.read_text()
    assert (
        recover_import(
            config.controller.state_dir, hashlib.sha256(b"synthetic-history-commit").hexdigest()
        )
        is not None
    )
    choices = cast(
        dict[str, Any],
        imports.checklist_choices(identifier, start_date="2026-04-29", end_date="2026-05-02"),
    )
    assert len(choices["checklists"]) == 2
    assert "Private park" in json.dumps(choices)
    assert not (config.controller.state_dir / "generation-queue.json").exists()
    imports.cancel(identifier)
    assert not imports.path.exists()
    assert history.exists()


def test_preview_has_history_cas_and_explicit_reduction_confirmation(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    imports = HistoryImports(config)
    identifier = staged(imports, archive())
    revision = str(settings_status(config)["revision"])
    preview = checked(imports, identifier, revision)
    changed = tmp_path / "changed.csv"
    changed.write_bytes(archive(extra=False))
    import_ebird_archive(changed, config.controller.state_dir)
    with pytest.raises(DataSourceError, match="changed"):
        imports.commit(
            identifier,
            revision=revision,
            preview_hash=preview["preview_hash"],
            key="stale-history-commit",
            allow_reduction=False,
        )
    imports.cancel(identifier)
    identifier = staged(imports, archive())
    preview = checked(imports, identifier, revision)
    imports.commit(
        identifier,
        revision=revision,
        preview_hash=preview["preview_hash"],
        key="larger-history-commit",
        allow_reduction=False,
    )
    imports.cancel(identifier)
    identifier = staged(imports, archive(extra=False))
    preview = checked(imports, identifier, revision)
    with pytest.raises(OwnerOperationError, match="Confirm"):
        imports.commit(
            identifier,
            revision=revision,
            preview_hash=preview["preview_hash"],
            key="reduced-history-commit",
            allow_reduction=False,
        )
    imports.commit(
        identifier,
        revision=revision,
        preview_hash=preview["preview_hash"],
        key="reduced-history-commit",
        allow_reduction=True,
    )
    backup = config.controller.state_dir / "ebird-archive-observations.json.previous"
    assert backup.stat().st_mode & 0o777 == 0o600
    assert len(json.loads(backup.read_text())["checklists"]) == 2


def test_changed_collection_impact_requires_a_new_history_preview(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    imports = HistoryImports(config)
    identifier = staged(imports, archive())
    revision = str(settings_status(config)["revision"])
    preview = checked(imports, identifier, revision)
    save_collection(
        config.controller.state_dir, revision=0, label="Concurrent collection", taxon_ids=[]
    )
    with pytest.raises(DataSourceError, match="Collections changed"):
        imports.commit(
            identifier,
            revision=revision,
            preview_hash=preview["preview_hash"],
            key="changed-journal-import",
            allow_reduction=False,
        )
    assert not read_ebird_checklists(config.controller.state_dir)[1]
    preview = checked(imports, identifier, revision)
    imports.commit(
        identifier,
        revision=revision,
        preview_hash=preview["preview_hash"],
        key="fresh-journal-import",
        allow_reduction=False,
    )
    assert len(read_ebird_checklists(config.controller.state_dir)[1]) == 2


def test_analyzer_import_preserves_unknown_dates_and_uses_history_revision(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    imports = HistoryImports(config)
    csv_data = (
        b"Start (s),End (s),Scientific name,Common name,Confidence,File\n"
        b"0,3,Turdus migratorius,Robin,0.99,fixture.wav\n"
    )
    identifier = staged(imports, csv_data, "birdnet-analyzer")
    revision = str(settings_status(config)["revision"])
    preview = checked(imports, identifier, revision)
    assert preview["stats"]["undated"] == 1
    imports.commit(
        identifier,
        revision=revision,
        preview_hash=preview["preview_hash"],
        key="undated-analyzer-import",
        allow_reduction=False,
    )
    path = config.controller.state_dir / "birdnet-analyzer-detections.json"
    raw = json.loads(path.read_text())
    assert next(iter(raw["detections"].values()))["observed_on"] is None
    assert "fixture.wav" not in path.read_text()
    imports.cancel(identifier)
    identifier, handle = imports.begin("birdnet-analyzer", observed_on="2026-04-29")
    with handle:
        handle.write(csv_data)
    imports.finish_upload(identifier, hashlib.sha256(csv_data).hexdigest(), len(csv_data))
    preview = checked(imports, identifier, revision)
    assert preview["stats"]["updated"] == 1
    imports.commit(
        identifier,
        revision=revision,
        preview_hash=preview["preview_hash"],
        key="dated-analyzer-import",
        allow_reduction=False,
    )
    assert (
        next(iter(json.loads(path.read_text())["detections"].values()))["observed_on"]
        == "2026-04-29"
    )
    assert path.with_suffix(".json.previous").stat().st_mode & 0o777 == 0o600


def test_committed_history_reconciles_after_metadata_and_receipt_interruption(
    tmp_path: Path,
) -> None:
    config = configuration(tmp_path)
    imports = HistoryImports(config)
    identifier = staged(imports, archive())
    revision = str(settings_status(config)["revision"])
    preview = checked(imports, identifier, revision)
    original_write = imports._write

    def stop_after_history(value: dict[str, object]) -> None:
        if value["status"] == "committed":
            raise OSError("synthetic interrupted metadata write")
        original_write(value)

    with patch.object(imports, "_write", side_effect=stop_after_history), pytest.raises(OSError):
        imports.commit(
            identifier,
            revision=revision,
            preview_hash=preview["preview_hash"],
            key="interrupted-import-key",
            allow_reduction=False,
        )
    restarted = HistoryImports(config)
    assert status(restarted)["status"] == "committed"
    assert (
        len(
            cast(
                dict[str, Any],
                restarted.checklist_choices(
                    identifier, start_date="2026-04-29", end_date="2026-05-02"
                ),
            )["checklists"]
        )
        == 2
    )
    receipt = {
        "operation_id": "a" * 32,
        "key_hash": hashlib.sha256(b"interrupted-import-key").hexdigest(),
        "input_hash": "b" * 64,
        "kind": "import-commit",
        "status": "running",
        "created_at": "2026-10-15T10:00:00+00:00",
        "updated_at": "2026-10-15T10:00:00+00:00",
        "result": None,
    }
    path = config.controller.state_dir / "owner-operations.json"
    path.write_text(json.dumps({"schema_version": 1, "receipts": [receipt]}))
    path.chmod(0o600)
    worker = OwnerOperations(config.controller.state_dir)
    try:
        result = cast(dict[str, Any], worker.status("a" * 32))
        assert result["status"] == "succeeded"
        assert result["result"]["checklists"] == 2
    finally:
        worker.close()


def test_trip_keeps_complete_members_and_unresolved_names_without_implicit_work(
    tmp_path: Path,
) -> None:
    config = configuration(tmp_path)
    imports = HistoryImports(config)
    identifier = staged(imports, archive())
    revision = str(settings_status(config)["revision"])
    preview = checked(imports, identifier, revision)
    imports.commit(
        identifier,
        revision=revision,
        preview_hash=preview["preview_hash"],
        key="trip-history-commit",
        allow_reduction=False,
    )
    choices = cast(
        dict[str, Any],
        imports.checklist_choices(identifier, start_date="2026-04-29", end_date="2026-05-02"),
    )
    selection = {
        "history_revision": choices["history_revision"],
        "checklist_keys": [item["key"] for item in choices["checklists"]],
        "start_date": "2026-04-29",
        "end_date": "2026-05-02",
        "label": "Private trip",
        "collection_id": "a" * 32,
        "revision": 0,
    }

    def resolve(
        species: list[EbirdArchiveSpecies], _path: Path
    ) -> tuple[list[BirdSpecies], list[EbirdArchiveSpecies]]:
        if species[0].scientific_name == "Turdus migratorius":
            return [BirdSpecies(1, "Robin", "Turdus migratorius", 1, "eBird Archive")], []
        return [], species

    with patch("inky_bird_frame.imports.resolve_ebird_archive_species", side_effect=resolve):
        imports.preview_trip(identifier, **selection)
        trip = status(imports)["trip"]["preview"]
        assert trip["matched"] == 1 and len(trip["unresolved"]) == 1
        save_collection(
            config.controller.state_dir, revision=0, label="Concurrent edit", taxon_ids=[]
        )
        with pytest.raises(JournalConflictError):
            imports.save_trip(identifier, preview_hash=trip["preview_hash"])
        assert imports.path.exists()
        imports.edit_trip(identifier)
        assert "trip" not in status(imports)
        assert status(imports)["draft"]["checklist_keys"] == selection["checklist_keys"]
        selection["revision"] = 1
        imports.preview_trip(identifier, **selection)
        trip = status(imports)["trip"]["preview"]
        imports.save_trip(identifier, preview_hash=trip["preview_hash"])
    state = read_journal(config.controller.state_dir)
    group = state.collections[1]
    assert group.kind == CollectionKind.TRIP
    assert len(group.members) == 2
    assert group.members[0].evidence[0].scientific_name == "Turdus migratorius"
    assert not imports.path.exists()
    assert not (config.controller.state_dir / "collection.json").exists()
    assert not (config.controller.state_dir / "generation-queue.json").exists()
    # Correct only the source date. Identity and species stay the same, but the
    # record no longer supports the saved trip's inclusive date interval.
    corrected = tmp_path / "date-correction.csv"
    corrected.write_bytes(archive().replace(b"2026-04-29", b"2026-10-01"))
    identifier = staged(imports, corrected.read_bytes())
    changed = checked(imports, identifier, revision)
    assert changed["impact"]["affected_collections"] == ["Private trip"]
    with pytest.raises(OwnerOperationError, match="Confirm"):
        imports.commit(
            identifier,
            revision=revision,
            preview_hash=changed["preview_hash"],
            key="date-correction-commit",
            allow_reduction=False,
        )
    imports.commit(
        identifier,
        revision=revision,
        preview_hash=changed["preview_hash"],
        key="date-correction-commit",
        allow_reduction=True,
    )
    projected = cast(dict[str, Any], JournalService(config.controller).snapshot())
    trip_projection = next(
        item for item in projected["collections"] if item["kind"] == "personal-trip"
    )
    assert trip_projection["evidence"][0]["status"] == "source-changed"
    assert read_journal(config.controller.state_dir).collections[1] == group


def test_startup_removes_expired_source_file_without_removing_committed_history(
    tmp_path: Path,
) -> None:
    config = configuration(tmp_path)
    imports = HistoryImports(config)
    identifier = staged(imports, archive())
    revision = str(settings_status(config)["revision"])
    preview = checked(imports, identifier, revision)
    imports.commit(
        identifier,
        revision=revision,
        preview_hash=preview["preview_hash"],
        key="expiry-history-commit",
        allow_reduction=False,
    )
    history = config.controller.state_dir / "ebird-archive-observations.json"
    before = history.read_bytes()
    raw = json.loads(imports.metadata_path.read_text())
    raw["expires_at"] = "2000-01-01T00:00:00+00:00"
    imports.metadata_path.write_text(json.dumps(raw))
    with TestClient(create_app(config.controller, app_config=config)):
        assert not imports.path.exists()
        assert not imports.metadata_path.exists()
        assert history.read_bytes() == before
    retained = HistoryImports(config)
    result = retained.use_retained_history()
    choices = cast(
        dict[str, Any],
        retained.checklist_choices(
            str(result["upload_id"]), start_date="2026-04-29", end_date="2026-05-02"
        ),
    )
    assert len(choices["checklists"]) == 2
    assert all(item["location"] is None for item in choices["checklists"])
    assert not retained.path.exists()


def test_upload_guards_private_permissions_expiry_low_disk_and_tampering(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    imports = HistoryImports(config)
    identifier = staged(imports, archive())
    with pytest.raises(OwnerOperationError, match="existing preview"):
        imports.begin("ebird-archive")
    imports.path.write_bytes(b"tampered")
    with pytest.raises(OwnerOperationError, match="changed"):
        imports.preview(identifier, revision=str(settings_status(config)["revision"]))
    imports.cancel(identifier)
    with patch("inky_bird_frame.imports.shutil.disk_usage") as disk:
        disk.return_value.free = 0
        with pytest.raises(OwnerOperationError, match="2 GiB"):
            imports.begin("ebird-archive")
    identifier = staged(imports, archive())
    raw = json.loads(imports.metadata_path.read_text())
    raw["expires_at"] = "2000-01-01T00:00:00+00:00"
    imports.metadata_path.write_text(json.dumps(raw))
    with pytest.raises(OwnerOperationError, match="expired"):
        imports.preview(identifier, revision=str(settings_status(config)["revision"]))
    imports.cancel(identifier)
    imports.root.chmod(0o755)
    with pytest.raises(OwnerOperationError, match="0700"):
        HistoryImports(config)


def test_http_upload_requires_owner_csrf_and_never_commits_before_confirmation(
    tmp_path: Path,
) -> None:
    config = configuration(tmp_path)
    with TestClient(create_app(config.controller, app_config=config), base_url=ORIGIN) as client:
        route = "/v1/owner/imports/upload/ebird-archive"
        assert client.post(route, content=archive(), headers={"Origin": ORIGIN}).status_code == 401
        login = client.post(
            "/v1/owner/register",
            headers={"Origin": ORIGIN},
            json={"username": "qa-owner", "passphrase": "long synthetic password"},
        )
        headers = {
            "Origin": ORIGIN,
            "X-Inky-CSRF": login.json()["data"]["csrf"],
            "Content-Type": "application/octet-stream",
        }
        assert (
            client.post(
                route,
                content=archive(),
                headers={"Origin": ORIGIN, "Content-Type": "application/octet-stream"},
            ).status_code
            == 401
        )
        uploaded = client.post(route, content=archive(), headers=headers)
        assert uploaded.status_code == 200
        assert client.get("/v1/owner/imports").json()["data"]["preview"]["status"] == "uploaded"
        assert not read_ebird_checklists(config.controller.state_dir)[1]
        assert (
            client.post(
                "/v1/owner/logout",
                json={},
                headers={"Origin": ORIGIN, "X-Inky-CSRF": headers["X-Inky-CSRF"]},
            ).status_code
            == 200
        )
        assert not HistoryImports(config).path.exists()
