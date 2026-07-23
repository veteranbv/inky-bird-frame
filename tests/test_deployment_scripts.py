from __future__ import annotations

from pathlib import Path


def _controller_installers() -> list[str]:
    root = Path(__file__).resolve().parents[1] / "deploy"
    return [
        (root / "install-controller.sh").read_text(),
        (root / "install-controller-systemd.sh").read_text(),
    ]


def test_controller_installers_use_provider_lists_for_managed_credentials() -> None:
    for script in _controller_installers():
        assert 'environment_credentials.append("geoapify_api_key_env")' in script
        assert "DiscoveryProvider.EBIRD in config.discovery.sources" in script
        assert "DiscoveryProvider.BIRDWEATHER in config.discovery.sources" in script
        assert "config.discovery.source." not in script


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
    assert 'rsync -a "${root}/catalog/" "${app_dir}/catalog/"' not in script
    assert "sync_public_catalog(source_catalog, managed_catalog)" in script
    assert "sync_public_catalog(source_catalog, config.controller.catalog_dir)" in script
    assert "managed_catalog.resolve() != config.controller.catalog_dir.resolve()" in script
    assert "cannot use {names}" in script
    assert 'environment_credentials.append("ebird_api_key_env")' in script
    assert 'environment_credentials.append("birdweather_token_env")' in script
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
    assert 'rsync -a "${root}/catalog/" "${app_dir}/catalog/"' not in script
    assert "sync_public_catalog(source_catalog, managed_catalog)" in script
    assert "sync_public_catalog(source_catalog, config.controller.catalog_dir)" in script
    assert "managed_catalog.resolve() != config.controller.catalog_dir.resolve()" in script
    assert "cannot use {names}" in script
    assert 'environment_credentials.append("ebird_api_key_env")' in script
    assert 'environment_credentials.append("birdweather_token_env")' in script
    assert "config.controller.workspace_dir.mkdir(parents=True, exist_ok=True)" in script
    assert "config.controller.catalog_dir.parent.mkdir(parents=True, exist_ok=True)" in script
    assert "rebuild_catalog_index" not in script


def test_macos_controller_installer_restores_previously_loaded_agents_on_failure() -> None:
    script = (Path(__file__).resolve().parents[1] / "deploy" / "install-controller.sh").read_text()

    backup = script.index('cp "${plist}" "${plist_backup_dir}/"')
    plist_write = script.index("plistlib.dump(payload, handle, sort_keys=True)")
    unconditional_bootout = script.index('launchctl bootout "gui/${uid}/com.inky-bird-frame.serve"')
    bootstrap = script.index('launchctl bootstrap "gui/${uid}" "${serve_plist}"')
    completion = script.index("installation_complete=true")

    assert backup < plist_write < unconditional_bootout < bootstrap < completion
    assert "trap cleanup EXIT" in script
    assert 'if [ "${installation_complete}" != true ]' in script
    assert 'previously_loaded_labels+=("${label}")' in script
    assert 'restore_previous_agent "${label}"' in script
    assert 'was_previously_loaded "${label}"' in script
    assert script.index('if was_previously_loaded "${label}"') < script.index(
        "# Not running before this install: undo any half-installed agent."
    )
    assert "for label in serve refresh generate catalog-publish notifications; do" in script
    assert 'restore_catalog_publisher_schedule "${uid}" || true' in script
    assert 'rm -rf "${plist_backup_dir}"' in script


def test_actions_controller_installer_uses_gui_launchd_without_exporting_tokens() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "deploy" / "install-controller-via-launchd.sh").read_text()
    workflow = (root / ".github" / "workflows" / "deploy.yml").read_text()

    assert 'launchctl bootstrap "gui/${uid}" "${plist_path}"' in script
    assert 'launchctl bootout "gui/${uid}/${label}"' in script
    assert '"RunAtLoad": True' in script
    assert '"KeepAlive"' not in script
    assert '"${root}/deploy/install-controller.sh"' in script
    assert "GH_TOKEN" not in script
    assert "GITHUB_TOKEN" not in script
    assert "./deploy/install-controller-via-launchd.sh" in workflow
    assert "./deploy/install-controller.sh\n" not in workflow


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


def test_macos_controller_uninstaller_removes_agents_and_preserves_data() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "deploy" / "uninstall-controller.sh"
    ).read_text()

    assert "set -euo pipefail" in script
    assert (
        "for label in serve refresh generate catalog-publish notifications controller-cycle; do"
        in script
    )
    assert 'launchctl bootout "gui/${uid}/${agent}"' in script
    assert 'rm -f "${plist}"' in script
    assert "Intentionally left in place:" in script
    assert "${HOME}/Services/inky-bird-frame" in script
    assert "rm -rf" not in script
    assert 'rm -f "${config_path}"' not in script


def test_systemd_controller_uninstaller_removes_units_and_preserves_data() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "deploy" / "uninstall-controller-systemd.sh"
    ).read_text()

    assert "set -euo pipefail" in script
    assert "for name in refresh generate catalog-publish notifications; do" in script
    assert 'sudo systemctl disable --now "${timer}"' in script
    assert "sudo systemctl disable --now inky-bird-frame-controller.service" in script
    assert "for name in controller refresh generate catalog-publish notifications; do" in script
    assert 'sudo rm -f "/etc/systemd/system/${unit}"' in script
    assert "sudo systemctl daemon-reload" in script
    assert "Intentionally left in place:" in script
    assert "rm -rf" not in script
    assert 'rm -f "${config_path}"' not in script


def test_local_display_uninstaller_removes_units_and_preserves_data() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "deploy" / "uninstall-display-local.sh"
    ).read_text()

    assert "set -euo pipefail" in script
    assert "noninteractive_sudo=${INKY_BIRD_NONINTERACTIVE_SUDO:-false}" in script
    assert "sudo_command+=(-n)" in script
    assert '"${sudo_command[@]}" systemctl disable --now inky-bird-frame-display.timer' in script
    assert '"${sudo_command[@]}" systemctl stop inky-bird-frame-display.service' in script
    assert '"${sudo_command[@]}" rm -f "/etc/systemd/system/${unit}"' in script
    assert '"${sudo_command[@]}" systemctl daemon-reload' in script
    assert "Intentionally left in place:" in script
    assert "Pimoroni Python environment" in script
    assert "rm -rf" not in script
    assert 'rm -f "${config_path}"' not in script
