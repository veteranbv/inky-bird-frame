"""Native naming is additive: one scheduler family and no silent new state."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import TypedDict
from unittest.mock import patch

import pytest

from inky_bird_frame import application_version
from inky_bird_frame.config import load_config
from inky_bird_frame.installation import controller_systemd_units, display_systemd_units
from inky_bird_frame.native_names import CANONICAL_NAME, LEGACY_NAME, _loaded, resolve_native_layout

ROOT = Path(__file__).resolve().parents[1]


class UnitArguments(TypedDict):
    executable: Path
    app_dir: Path
    config_path: Path
    user: str
    service_prefix: str


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
@pytest.mark.parametrize("family", [CANONICAL_NAME, LEGACY_NAME, "fresh", "both"])
@pytest.mark.parametrize("marker", ["config", "service", "runtime", "retained-state"])
def test_identity_selection(tmp_path: Path, system: str, family: str, marker: str) -> None:
    home = tmp_path / "home"
    root = tmp_path / "root"
    for name in (CANONICAL_NAME, LEGACY_NAME):
        if family not in (name, "both"):
            continue
        if marker in ("config", "retained-state"):
            path = (
                home
                / "Library/Application Support"
                / ("Inky Bird" if name == CANONICAL_NAME else "Inky Bird Frame")
                / "config.toml"
                if system == "Darwin"
                else home / ".config" / name / "config.toml"
            )
            if marker == "retained-state":
                path = path.parent / "state" / "journal.json"
        elif marker == "runtime":
            path = home / "Services" / name / ".venv" / "pyvenv.cfg"
        else:
            path = (
                root / "Library/LaunchDaemons" / f"com.{name}.scheduler.plist"
                if system == "Darwin"
                else root / "etc/systemd/system" / f"{name}-scheduler.service"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    if family == "both":
        with pytest.raises(ValueError, match="Both inky-bird"):
            resolve_native_layout(home=home, system=system, system_root=root, check_services=False)
        return
    layout = resolve_native_layout(home=home, system=system, system_root=root, check_services=False)
    expected = CANONICAL_NAME if family == "fresh" else family
    assert layout.family == expected
    assert layout.app_dir == home / "Services" / expected
    assert layout.config_path == layout.support_dir / "config.toml"


@pytest.mark.parametrize("family", [CANONICAL_NAME, LEGACY_NAME])
@pytest.mark.parametrize("ownership", ["app", "managed"])
def test_unit_references_never_mix_families(tmp_path: Path, family: str, ownership: str) -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
    config = replace(
        config,
        controller=replace(
            config.controller, configuration_ownership=ownership, codex_path=tmp_path / "codex"
        ),
    )
    common = UnitArguments(
        executable=tmp_path / "inky-bird",
        app_dir=tmp_path,
        config_path=tmp_path / "config.toml",
        user="synthetic",
        service_prefix=family,
    )
    units = controller_systemd_units(config, home=tmp_path, **common)
    units.update(display_systemd_units(config, **common))
    other = LEGACY_NAME if family == CANONICAL_NAME else CANONICAL_NAME
    assert all(name.startswith(f"{family}-") for name in units)
    if family == CANONICAL_NAME:
        assert not any(name.startswith(f"{LEGACY_NAME}-") for name in units)
    assert f"{other}-refresh.service" not in "".join(units.values())
    assert f"Unit={family}-display.service" in units[f"{family}-display.timer"]
    if ownership == "app":
        assert f"{family}-scheduler.service" in units
        assert f"{family}-refresh.timer" not in units


@pytest.mark.parametrize("versions", [{"inky-bird": "1.2.3"}, {"inky-bird-frame": "0.9.7"}, {}])
def test_version_lookup_supports_both_distributions(versions: dict[str, str]) -> None:
    from importlib.metadata import PackageNotFoundError

    def lookup(name: str) -> str:
        if name not in versions:
            raise PackageNotFoundError(name)
        return versions[name]

    with patch("inky_bird_frame.metadata.version", side_effect=lookup):
        assert application_version() == next(iter(versions.values()), "0+unknown")


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_loaded_jobs_without_definition_files_still_select_legacy(
    tmp_path: Path, system: str
) -> None:
    with patch(
        "inky_bird_frame.native_names._loaded", side_effect=lambda name, _: name == LEGACY_NAME
    ):
        assert (
            resolve_native_layout(home=tmp_path, system=system, system_root=tmp_path).family
            == LEGACY_NAME
        )


@pytest.mark.parametrize("state", ["loaded", "masked", "error", "not-found"])
def test_timer_only_manager_identity_is_not_missed(state: str) -> None:
    def inspect(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=state if command[-1] == "inky-bird-frame-display.timer" else "not-found",
            stderr="",
        )

    with patch("inky_bird_frame.native_names.subprocess.run", side_effect=inspect) as run:
        assert _loaded(LEGACY_NAME, "Linux") == (state != "not-found")
        assert any(
            call.args[0][-1] == "inky-bird-frame-display.timer" for call in run.call_args_list
        )


@pytest.mark.parametrize(
    "script",
    [
        "install-controller-systemd.sh",
        "install-display-local.sh",
        "uninstall-controller-systemd.sh",
        "uninstall-display-local.sh",
    ],
)
def test_ambiguous_native_scripts_stop_before_sudo_or_package_mutation(
    tmp_path: Path, script: str
) -> None:
    home = tmp_path / "home"
    for family in (CANONICAL_NAME, LEGACY_NAME):
        config = home / ".config" / family / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text("private-state-sentinel")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Run the real resolver against isolated Linux paths, even on a macOS
    # development host. Only platform/inventory discovery is substituted.
    wrapper = (
        "import sys; from functools import partial; from pathlib import Path; "
        f"sys.path.insert(0, {str(ROOT / 'src/inky_bird_frame')!r}); "
        "import native_names as n; "
        "n.resolve_native_layout = partial(n.resolve_native_layout, system='Linux', "
        f"system_root=Path({str(tmp_path)!r}), check_services=False); "
        "sys.exit(n.main())"
    )
    commands = {
        "uname": "printf '%s\\n' Linux",
        "systemctl": "exit 0",
        "sudo": f"touch {shlex.quote(str(tmp_path / 'MUTATED'))}; exit 99",
        "python3": f"exec {shlex.quote(sys.executable)} -c {shlex.quote(wrapper)}",
    }
    for name, content in commands.items():
        path = bin_dir / name
        path.write_text("#!/bin/sh\n" + content + "\n")
        path.chmod(0o755)
    result = subprocess.run(
        ["bash", str(ROOT / "deploy" / script)],
        env={"HOME": str(home), "PATH": f"{bin_dir}{os.pathsep}{os.defpath}"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "Both inky-bird" in result.stderr
    assert not (tmp_path / "MUTATED").exists()
    assert all(
        path.read_text() == "private-state-sentinel" for path in home.glob(".config/*/config.toml")
    )
