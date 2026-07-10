from __future__ import annotations

from pathlib import Path


def test_controller_installer_unloads_publisher_until_publish_validation_succeeds() -> None:
    script = (Path(__file__).resolve().parents[1] / "deploy" / "install-controller.sh").read_text()

    validation = script.index('"${app_dir}/.venv/bin/inky-bird-frame" catalog-publish')
    unload = script.rindex(
        'launchctl bootout "gui/${uid}/com.inky-bird-frame.catalog-publish"',
        0,
        validation,
    )
    reload_after_validation = script.index(
        'launchctl bootstrap "gui/${uid}" "${catalog_publish_plist}"',
        validation,
    )
    runtime_update = script.index('rsync -a --delete "${root}/src/"')

    assert runtime_update < unload < validation < reload_after_validation
    assert "publisher remains unloaded" in script[validation:reload_after_validation]
