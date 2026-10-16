from __future__ import annotations

import json
import os
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from test_config import CONFIG

from inky_bird_frame.access import AccessError, AccessStore, prepare_browser_registration
from inky_bird_frame.config import load_config
from inky_bird_frame.settings import (
    SettingsConflictError,
    SettingsError,
    _toml,
    initialize_config,
    save_settings,
    settings_status,
)
from inky_bird_frame.web import create_app


def fixture(root: Path, *, owned: bool = True) -> Path:
    path = root / "config.toml"
    path.write_text(
        CONFIG.replace(
            "[controller]\n",
            "[controller]\nconfiguration_ownership = " + ('"app"' if owned else '"managed"') + "\n",
        )
    )
    path.chmod(0o600)
    return path


def test_settings_round_trip_and_private_backup(tmp_path: Path) -> None:
    path = fixture(tmp_path)
    initial = load_config(path, load_secrets=False)
    old = path.read_bytes()
    status = settings_status(initial)
    result = save_settings(
        initial,
        revision=str(status["revision"]),
        confirm_generation=True,
        changes={
            "discovery": {"sources": [], "window": "all-time"},
            "controller": {"generation_enabled": True},
        },
    )
    assert result["saved"] is True
    current = load_config(path)
    assert current.discovery.sources == ()
    assert current.controller.generation_enabled
    backup = path.with_suffix(".toml.previous")
    assert backup.read_bytes() == old
    assert path.stat().st_mode & 0o777 == backup.stat().st_mode & 0o777 == 0o600
    assert current.public_catalog == initial.public_catalog
    assert current.schedule == initial.schedule


def test_managed_requires_explicit_adoption_then_installer_restart(tmp_path: Path) -> None:
    path = fixture(tmp_path, owned=False)
    initial = load_config(path)
    status = settings_status(initial)
    with pytest.raises(SettingsError, match="managed"):
        save_settings(initial, revision=str(status["revision"]), changes={})
    result = save_settings(initial, revision=str(status["revision"]), changes={}, adopt=True)
    assert result["restart_required"] is True
    assert load_config(path).controller.generation_enabled  # Preserve the old default.
    status = settings_status(initial)
    assert status["restart_required"] is True
    assert status["editable"] is False
    with pytest.raises(SettingsError, match="installer"):
        save_settings(initial, revision=str(status["revision"]), changes={})
    assert settings_status(load_config(path))["editable"] is True


def test_concurrent_settings_use_exact_byte_revision(tmp_path: Path) -> None:
    path = fixture(tmp_path)
    config = load_config(path)
    revision = str(settings_status(config)["revision"])

    def save(value: int) -> object:
        try:
            return save_settings(
                config, revision=revision, changes={"discovery": {"radius_km": value}}
            )
        except SettingsConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, [7, 8]))
    assert sum(isinstance(result, SettingsConflictError) for result in results) == 1


def test_secrets_never_echo_and_environment_values_stay_external(tmp_path: Path) -> None:
    path = fixture(tmp_path)
    path.write_text(
        path.read_text().replace(
            "[discovery]\n", '[discovery]\nebird_api_key_env = "PRIVATE_ENV_NAME"\n'
        )
    )
    config = load_config(path, load_secrets=False)
    status = settings_status(config)
    assert "PRIVATE_ENV_NAME" not in json.dumps(status)
    before = path.read_bytes()
    with pytest.raises(SettingsError, match="environment"):
        save_settings(
            config,
            revision=str(status["revision"]),
            changes={"discovery": {"ebird_api_key": "DO_NOT_ECHO"}},
        )
    assert path.read_bytes() == before
    path.write_text(
        path.read_text().replace(
            'ebird_api_key_env = "PRIVATE_ENV_NAME"', 'ebird_api_key = "DO_NOT_ECHO"'
        )
    )
    assert "DO_NOT_ECHO" not in json.dumps(settings_status(config))


@pytest.mark.parametrize(
    "changes",
    [
        {"controller": {"state_dir": "/elsewhere"}},
        {"controller": {"port": 9000}},
        {"discovery": {"sources": ["ebird"], "window": "all-time"}},
        {"notifications": {"enabled": True}},
        {"research": {"enabled": False}},
    ],
)
def test_invalid_or_installer_owned_changes_leave_bytes_untouched(
    tmp_path: Path, changes: object
) -> None:
    path = fixture(tmp_path)
    config = load_config(path)
    before = path.read_bytes()
    with pytest.raises(SettingsError):
        save_settings(config, revision=str(settings_status(config)["revision"]), changes=changes)
    assert path.read_bytes() == before


def test_permissions_symlinks_and_changed_installation_fail_closed(tmp_path: Path) -> None:
    path = fixture(tmp_path)
    config = load_config(path)
    path.chmod(0o644)
    with pytest.raises(SettingsError, match="private regular"):
        settings_status(config)
    path.chmod(0o600)
    link = tmp_path / "link.toml"
    link.symlink_to(path)
    with pytest.raises(SettingsError, match="private regular"):
        settings_status(load_config(link))
    path.write_text(path.read_text().replace("port = 8793", "port = 8794"))
    with pytest.raises(SettingsError, match="Restart"):
        settings_status(config)


def test_toml_serialization_preserves_tables_arrays_and_unicode() -> None:
    raw = {
        "controller": {"generation_enabled": False, "oidc": {"issuer": "https://example.test"}},
        "notifications": {
            "destinations": [{"name": "Birds 🐦", "url": "test://secret", "events": ["discovery"]}]
        },
    }
    assert tomllib.loads(_toml(raw).decode()) == raw


def test_generation_needs_deliberate_consent(tmp_path: Path) -> None:
    path = fixture(tmp_path)
    config = load_config(path)
    before = path.read_bytes()
    with pytest.raises(SettingsError, match="Confirm AI use"):
        save_settings(
            config,
            revision=str(settings_status(config)["revision"]),
            changes={"controller": {"generation_enabled": True}},
        )
    assert path.read_bytes() == before
    assert not path.with_suffix(".toml.previous").exists()


def test_browser_cannot_introduce_environment_notification_references(tmp_path: Path) -> None:
    path = fixture(tmp_path)
    config = load_config(path)
    before = path.read_bytes()
    with pytest.raises(SettingsError, match="installer-owned"):
        save_settings(
            config,
            revision=str(settings_status(config)["revision"]),
            changes={
                "notifications": {"destinations": [{"name": "Test", "url_env": "DO_NOT_READ"}]}
            },
        )
    assert path.read_bytes() == before
    assert not path.with_suffix(".toml.previous").exists()


def test_notification_edit_retains_secret_only_for_same_named_destination(tmp_path: Path) -> None:
    path = fixture(tmp_path)
    path.write_text(
        path.read_text()
        + '\n[[notifications.destinations]]\nname="Birds"\nurl="json://DO_NOT_ECHO"\nevents=["discovery"]\n'
    )
    config = load_config(path, load_secrets=False)
    save_settings(
        config,
        revision=str(settings_status(config)["revision"]),
        changes={
            "notifications": {"destinations": [{"name": "Birds", "events": ["terminal_error"]}]}
        },
    )
    assert (
        tomllib.loads(path.read_text())["notifications"]["destinations"][0]["url"]
        == "json://DO_NOT_ECHO"
    )
    assert "DO_NOT_ECHO" not in json.dumps(settings_status(config))


@pytest.mark.parametrize("container", [False, True])
def test_initialization_is_art_first_private_and_never_overwrites(
    tmp_path: Path, container: bool
) -> None:
    path = tmp_path / "new/config.toml"
    assert initialize_config(path, container=container) is not container
    config = load_config(path)
    assert config.discovery.sources == ()
    assert not config.controller.generation_enabled
    assert config.controller.configuration_ownership == "app"
    assert path.stat().st_mode & 0o777 == 0o600
    assert config.controller.catalog_dir == (
        Path("/data/catalog") if container else path.parent / "catalog"
    )
    before = path.read_bytes()
    with pytest.raises(SettingsError, match="already exists"):
        initialize_config(path, container=container)
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "broken-symlink"])
def test_initialization_with_any_retained_entry_keeps_network_setup_closed(
    tmp_path: Path, kind: str
) -> None:
    retained = tmp_path / "unrecognized-state"
    if kind == "file":
        retained.write_bytes(b"private history")
    elif kind == "directory":
        retained.mkdir()
    else:
        target = tmp_path.parent / (tmp_path.name + "-outside")
        if kind == "symlink":
            target.write_bytes(b"private history")
        retained.symlink_to(target)
    path = tmp_path / "config.toml"
    assert not initialize_config(path)
    controller = load_config(path, load_secrets=False).controller
    assert not (controller.state_dir / "web-access").exists()
    assert not prepare_browser_registration(path, controller)
    assert retained.is_symlink() if "symlink" in kind else retained.exists()


@pytest.mark.parametrize("name", ["owner-init.lock", "catalog-state.lock"])
def test_nonempty_bookkeeping_files_do_not_count_as_an_empty_installation(
    tmp_path: Path, name: str
) -> None:
    retained = tmp_path / name
    retained.write_bytes(b"unrecognized retained data")
    retained.chmod(0o600)
    assert not initialize_config(tmp_path / "config.toml")
    assert retained.read_bytes() == b"unrecognized retained data"


def test_interrupted_first_initialization_stays_closed_until_local_setup(tmp_path: Path) -> None:
    from unittest.mock import patch

    path = tmp_path / "config.toml"
    with (
        patch.object(AccessStore, "_save", side_effect=OSError("synthetic failure")),
        pytest.raises(AccessError),
    ):
        initialize_config(path)
    controller = load_config(path, load_secrets=False).controller
    assert not prepare_browser_registration(path, controller)
    store = AccessStore(controller.state_dir)
    status = store.setup_status()
    assert status is not None and status.state == "local"
    store.setup_owner("birder", "a synthetic long password")
    assert store.owner_exists()


@pytest.mark.parametrize("outside", ["state_dir", "workspace_dir", "catalog_dir"])
def test_prepare_rejects_runtime_paths_outside_the_empty_installation(
    tmp_path: Path, outside: str
) -> None:
    from dataclasses import replace

    path = tmp_path / "config.toml"
    initialize_config(path, container=True)
    controller = replace(
        load_config(path, load_secrets=False).controller,
        state_dir=tmp_path / "state",
        workspace_dir=tmp_path / "work",
        catalog_dir=tmp_path / "catalog",
    )
    target = tmp_path.parent / "outside"
    if outside == "state_dir":
        controller = replace(controller, state_dir=target)
    elif outside == "workspace_dir":
        controller = replace(controller, workspace_dir=target)
    else:
        controller = replace(controller, catalog_dir=target)
    assert not prepare_browser_registration(path, controller)
    assert set(p.name for p in tmp_path.iterdir()) == {"config.toml", "catalog-state.lock"}


@pytest.mark.parametrize("kind", ["config-permissions", "lock-permissions", "lock-fifo"])
def test_prepare_rejects_unsafe_config_and_initialization_locks(tmp_path: Path, kind: str) -> None:
    from dataclasses import replace

    path = tmp_path / "config.toml"
    initialize_config(path, container=True)
    controller = replace(
        load_config(path, load_secrets=False).controller,
        state_dir=tmp_path / "state",
        workspace_dir=tmp_path / "work",
        catalog_dir=tmp_path / "catalog",
    )
    if kind == "config-permissions":
        path.chmod(0o644)
    elif kind == "lock-permissions":
        (tmp_path / "owner-init.lock").touch(mode=0o644)
    else:
        os.mkfifo(tmp_path / "owner-init.lock", 0o600)
    with pytest.raises(AccessError, match="private regular"):
        prepare_browser_registration(path, controller)
    assert not AccessStore(controller.state_dir).path.exists()


def test_settings_http_auth_origin_csrf_redaction_and_revision(tmp_path: Path) -> None:
    path = fixture(tmp_path)
    origin = "https://inky.example.test"
    path.write_text(
        path.read_text()
        .replace("[controller]\n", '[controller]\nowner_allowed_origins = ["' + origin + '"]\n')
        .replace("[discovery]\n", '[discovery]\nebird_api_key = "DO_NOT_ECHO"\n')
    )
    config = load_config(path, load_secrets=False)
    with TestClient(create_app(config.controller, app_config=config), base_url=origin) as client:
        assert client.get("/v1/owner/settings").status_code == 401
        from inky_bird_frame.access import AccessStore

        AccessStore(config.controller.state_dir).setup_owner(
            "fixture-owner", "synthetic only long password"
        )
        signed = client.post(
            "/v1/owner/login",
            json={
                "username": "fixture-owner",
                "passphrase": "synthetic only long password",
                "remember": False,
            },
            headers={"Origin": origin},
        )
        assert signed.status_code == 200
        headers = {"Origin": origin, "X-Inky-CSRF": signed.json()["data"]["csrf"]}
        status = client.get("/v1/owner/settings")
        assert status.status_code == 200
        assert "DO_NOT_ECHO" not in status.text and str(tmp_path) not in status.text
        body = {
            "revision": status.json()["data"]["revision"],
            "changes": {"discovery": {"radius_km": 9}},
        }
        assert client.post("/v1/owner/settings", json=body).status_code == 403
        assert (
            client.post(
                "/v1/owner/settings", json=body, headers={**headers, "Origin": "https://wrong.test"}
            ).status_code
            == 403
        )
        assert client.post("/v1/owner/settings", json=body, headers=headers).status_code == 200
        assert client.post("/v1/owner/settings", json=body, headers=headers).status_code == 409
        assert client.get("/app/settings.js").status_code == 200
