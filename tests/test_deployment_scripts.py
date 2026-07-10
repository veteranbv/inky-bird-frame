from __future__ import annotations

from pathlib import Path


def test_controller_installer_restores_schedule_without_run_at_load_on_failure() -> None:
    script = (Path(__file__).resolve().parents[1] / "deploy" / "install-controller.sh").read_text()

    validation = script.index('"${app_dir}/.venv/bin/inky-bird-frame" catalog-publish')
    unload = script.rindex(
        'launchctl bootout "gui/${uid}/com.inky-bird-frame.catalog-publish"',
        0,
        validation,
    )
    restore_guard = script.index('if [ "${publisher_was_loaded}" = true ]; then', validation)
    restore = script.index('restore_catalog_publisher_schedule "${uid}"', restore_guard)
    runtime_update = script.index('rsync -a --delete "${root}/src/"')

    assert runtime_update < unload < validation < restore_guard < restore
    assert "publisher_was_loaded=false" in script[unload - 250 : unload]
    assert "publisher_was_loaded=true" in script[unload:validation]
    assert "/usr/bin/plutil -replace RunAtLoad -bool false" in script
    assert 'if [ "${root}" != "${app_dir}" ]; then' in script
    assert 'sync_public_catalog(root / "catalog", config.controller.catalog_dir)' in script
    assert "config.controller.workspace_dir.mkdir(parents=True, exist_ok=True)" in script
    assert "config.controller.catalog_dir.parent.mkdir(parents=True, exist_ok=True)" in script
    assert "rebuild_catalog_index" not in script


def test_systemd_controller_installer_restarts_boot_persistent_services() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "deploy" / "install-controller-systemd.sh"
    ).read_text()

    assert "controller_systemd_units" in script
    assert "systemctl enable inky-bird-frame-controller.service" in script
    assert "systemctl restart inky-bird-frame-controller.service" in script
    assert "systemctl restart inky-bird-frame-refresh.timer" in script
    assert "systemctl restart inky-bird-frame-generate.timer" in script
    initial_refresh = script.index(
        '"${app_dir}/.venv/bin/inky-bird-frame" refresh --config "${config_path}"'
    )
    quiesce = script.index('sudo systemctl stop "${timer}"')
    runtime_update = script.index('rsync -a --delete "${root}/src/"')
    validation = script.index('"${app_dir}/.venv/bin/inky-bird-frame" catalog-publish')
    unit_install = script.index('sudo install -m 0644 "${unit}"')
    assert quiesce < runtime_update < initial_refresh < validation < unit_install
    assert "deadline=$((SECONDS + quiesce_timeout_seconds))" in script
    assert 'systemctl show --property=ActiveState --value "${service}"' in script
    assert "active | activating | deactivating | reloading" in script
    assert 'if [ "${installation_complete}" != true ]; then' in script
    assert 'sudo systemctl start "${timer}" || true' in script
    assert "systemctl is-enabled --quiet inky-bird-frame-catalog-publish.timer" in script
    assert "systemctl is-active --quiet inky-bird-frame-notifications.timer" in script
    assert 'if [ "${root}" != "${app_dir}" ]; then' in script
    assert 'sync_public_catalog(root / "catalog", config.controller.catalog_dir)' in script
    assert "config.controller.workspace_dir.mkdir(parents=True, exist_ok=True)" in script
    assert "config.controller.catalog_dir.parent.mkdir(parents=True, exist_ok=True)" in script
    assert "rebuild_catalog_index" not in script


def test_local_display_installer_uses_configured_schedule_and_verifies_timer() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "deploy" / "install-display-local.sh"
    ).read_text()

    assert "display_systemd_units" in script
    assert "run_initial_display=${INKY_BIRD_RUN_INITIAL_DISPLAY:-false}" in script
    assert "systemctl enable inky-bird-frame-display.timer" in script
    assert "systemctl restart inky-bird-frame-display.timer" in script
    assert "systemctl is-enabled --quiet inky-bird-frame-display.timer" in script
    assert "systemctl is-active --quiet inky-bird-frame-display.timer" in script
    assert "noninteractive_sudo=${INKY_BIRD_NONINTERACTIVE_SUDO:-false}" in script
    assert "sudo_command+=(-n)" in script
    assert '"${sudo_command[@]}" systemctl daemon-reload' in script


def test_remote_display_installer_selects_noninteractive_sudo() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "deploy" / "install-display-node.sh"
    ).read_text()

    assert "INKY_BIRD_NONINTERACTIVE_SUDO=true" in script
