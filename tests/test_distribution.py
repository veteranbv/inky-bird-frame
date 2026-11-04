"""Build and install both distribution identities without a network or live data."""

from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def run(*args: str, cwd: Path, environment: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        args, cwd=cwd, env=environment, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture(scope="module")
def distribution_wheels(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    root = tmp_path_factory.mktemp("distribution-migration")
    uv = shutil.which("uv")
    assert uv is not None
    wheels = []
    for name in ("inky-bird-frame", "inky-bird"):
        project = root / name
        project.mkdir()
        shutil.copytree(ROOT / "src", project / "src", ignore=shutil.ignore_patterns("__pycache__"))
        (project / "docs").mkdir()
        shutil.copyfile(ROOT / "docs/browser-access.md", project / "docs/browser-access.md")
        for filename in ("README.md", "LICENSE"):
            shutil.copyfile(ROOT / filename, project / filename)
        settings = (ROOT / "pyproject.toml").read_text()
        if name == "inky-bird-frame":
            settings = settings.replace('name = "inky-bird"', 'name = "inky-bird-frame"')
            settings = settings.replace('inky-bird = "inky_bird_frame.entrypoint:main"\n', "")
        (project / "pyproject.toml").write_text(settings)
        run(uv, "build", "--offline", "--wheel", str(project), cwd=root)
        wheels.append(next((project / "dist").glob("*.whl")))
    return root, wheels[0], wheels[1]


def test_built_wheel_has_both_commands_and_bundled_access_guide(
    distribution_wheels: tuple[Path, Path, Path],
) -> None:
    _, _, wheel = distribution_wheels
    with zipfile.ZipFile(wheel) as archive:
        metadata = archive.read(f"inky_bird-{VERSION}.dist-info/METADATA").decode()
        assert "Name: inky-bird\n" in metadata
        assert archive.read("inky_bird_frame/web/access-guide.txt")
        assert archive.read("inky_bird_frame/native_names.py")
        parser = configparser.ConfigParser()
        parser.read_string(archive.read(f"inky_bird-{VERSION}.dist-info/entry_points.txt").decode())
        assert dict(parser["console_scripts"]) == {
            "inky-bird": "inky_bird_frame.entrypoint:main",
            "inky-bird-frame": "inky_bird_frame.entrypoint:main",
        }


def test_old_to_new_distribution_and_rollback_remove_overlapping_records(
    distribution_wheels: tuple[Path, Path, Path],
) -> None:
    root, old, new = distribution_wheels
    assert_distribution_migration(root, old, new)


def assert_distribution_migration(
    root: Path, old: Path, new: Path, *, legacy_banner: str = "inky-bird"
) -> None:
    """Also reusable with a retained, real historical wheel for release proof."""
    venv = root / "venv"
    run(sys.executable, "-m", "venv", str(venv), cwd=root)
    python = venv / "bin/python"
    site = Path(
        run(str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))", cwd=root)
    )
    # Reuse dependency files without exposing the checkout, its editable .pth
    # files or either application distribution's metadata to the test venv.
    # The package under test is installed ONLY from the newly built wheel.
    for entry in sys.path:
        if not entry.endswith("site-packages"):
            continue
        for dependency in Path(entry).iterdir():
            if (
                dependency.name.startswith(
                    ("inky_bird", "_inky_bird", "__pycache__", "pip", "setuptools")
                )
                or dependency.suffix == ".pth"
            ):
                continue
            target = site / dependency.name
            if not target.exists():
                target.symlink_to(dependency, target_is_directory=dependency.is_dir())
    environment = {"PATH": os.environ["PATH"], "HOME": str(root)}
    for wheel, expected, removed in (
        (old, "inky-bird-frame", "inky-bird"),
        (new, "inky-bird", "inky-bird-frame"),
        (old, "inky-bird-frame", "inky-bird"),
    ):
        # pip-based installs must remove the previous owner FIRST; this order
        # is also used by the display installer. uv sync does this reconciliation
        # for its locked controller environment.
        run(
            str(python),
            "-m",
            "pip",
            "uninstall",
            "--yes",
            removed,
            cwd=root,
            environment=environment,
        )
        run(
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            str(wheel),
            cwd=root,
            environment=environment,
        )
        proof = run(
            str(python),
            "-c",
            "from importlib.metadata import distributions; import inky_bird_frame; "
            "print(inky_bird_frame.__version__); print(inky_bird_frame.__file__); "
            "print(','.join(sorted(d.metadata['Name'] for d in distributions() "
            "if d.metadata['Name'] in ('inky-bird','inky-bird-frame'))))",
            cwd=root,
            environment=environment,
        )
        assert proof.splitlines()[0] == VERSION
        assert str(venv) in proof.splitlines()[1]
        assert proof.splitlines()[2] == expected
        assert (
            run(str(venv / "bin/inky-bird-frame"), "--version", cwd=root, environment=environment)
            == f"{'inky-bird' if expected == 'inky-bird' else legacy_banner} {VERSION}"
        )
        if expected == "inky-bird":
            assert (
                run(str(venv / "bin/inky-bird"), "--version", cwd=root, environment=environment)
                == f"inky-bird {VERSION}"
            )
