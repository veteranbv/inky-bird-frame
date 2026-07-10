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
