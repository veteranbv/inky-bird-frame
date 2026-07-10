from __future__ import annotations

from pathlib import Path


def test_controller_installer_unloads_publisher_before_publish_validation() -> None:
    script = (Path(__file__).resolve().parents[1] / "deploy" / "install-controller.sh").read_text()

    validation = script.index('"${app_dir}/.venv/bin/inky-bird-frame" catalog-publish')
    unload = script.rindex(
        'launchctl bootout "gui/${uid}/com.inky-bird-frame.catalog-publish"',
        0,
        validation,
    )
    restore = script.index(
        'launchctl bootstrap "gui/${uid}" "${catalog_publish_plist}"',
        validation,
    )
    restore_guard = script.index('if [ "${publisher_was_loaded}" = true ]; then', validation)
    runtime_update = script.index('rsync -a --delete "${root}/src/"')

    assert runtime_update < unload < validation < restore_guard < restore
    assert "publisher_was_loaded=false" in script[unload - 250 : unload]
    assert "publisher_was_loaded=true" in script[unload:validation]
