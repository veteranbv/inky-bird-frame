"""Select one native installation identity before any installer mutation.

This file also runs directly with the host's Python 3.9+ before the managed
environment exists. Keep it standard-library-only and independent of the app.
"""

from __future__ import annotations

import argparse
import os
import platform
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

CANONICAL_NAME = "inky-bird"
LEGACY_NAME = "inky-bird-frame"
NATIVE_JOBS = (
    "serve",
    "controller",
    "scheduler",
    "refresh",
    "generate",
    "catalog-publish",
    "notifications",
    "controller-cycle",
    "display",
)


@dataclass(frozen=True)
class NativeLayout:
    family: str
    app_dir: Path
    support_dir: Path
    config_path: Path


def service_family(value: str) -> str:
    if value not in (CANONICAL_NAME, LEGACY_NAME):
        raise ValueError("Unrecognized native service family")
    return value


def _loaded(family: str, system: str) -> bool:
    if system == "Darwin":
        commands = [
            ["launchctl", "print", f"{domain}/com.{family}.{job}"]
            for domain in (f"gui/{os.getuid()}", "system")
            for job in NATIVE_JOBS
        ]
    else:
        commands = [
            ["systemctl", "show", "--property=LoadState", "--value", f"{family}-{job}.service"]
            for job in NATIVE_JOBS
        ]
    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
        except FileNotFoundError:
            return False
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Cannot determine the installed native service identity") from exc
        if result.returncode == 0 and (system == "Darwin" or result.stdout.strip() == "loaded"):
            return True
    return False


def resolve_native_layout(
    *,
    home: Path | None = None,
    system: str | None = None,
    system_root: Path = Path("/"),
    check_services: bool = True,
    app_dir: Path | None = None,
    support_dir: Path | None = None,
    config_path: Path | None = None,
) -> NativeLayout:
    home = Path.home() if home is None else home
    system = platform.system() if system is None else system
    if system not in ("Darwin", "Linux"):
        raise ValueError("Native setup supports macOS and Linux only")
    present: set[str] = set()
    defaults: dict[str, tuple[Path, Path]] = {}
    for family in (CANONICAL_NAME, LEGACY_NAME):
        support = (
            home
            / "Library/Application Support"
            / ("Inky Bird" if family == CANONICAL_NAME else "Inky Bird Frame")
            if system == "Darwin"
            else home / ".config" / family
        )
        app = home / "Services" / family
        defaults[family] = app, support
        # A source checkout alone is not an installed runtime. Config or a
        # managed venv counts, including a dangling link (never silently adopt).
        markers = [support / "config.toml", app / ".venv"]
        if system == "Darwin":
            for directory in (home / "Library/LaunchAgents", system_root / "Library/LaunchDaemons"):
                markers.extend(directory / f"com.{family}.{job}.plist" for job in NATIVE_JOBS)
        else:
            for unit_directory in (
                "etc/systemd/system",
                "usr/lib/systemd/system",
                "lib/systemd/system",
            ):
                for job in NATIVE_JOBS:
                    markers.extend(
                        system_root / unit_directory / f"{family}-{job}.{suffix}"
                        for suffix in ("service", "timer")
                    )
        if any(path.exists() or path.is_symlink() for path in markers) or (
            check_services and _loaded(family, system)
        ):
            present.add(family)
    if len(present) > 1:
        raise ValueError(
            "Both inky-bird and inky-bird-frame installations exist. No changes made; "
            "resolve the duplicate native identities before setup or uninstall."
        )
    family = next(iter(present), CANONICAL_NAME)
    default_app, default_support = defaults[family]
    selected_support = support_dir or default_support
    return NativeLayout(
        family,
        app_dir or default_app,
        selected_support,
        config_path or selected_support / "config.toml",
    )


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()

    def optional_path(name: str) -> Path | None:
        value = os.environ.get(name)
        return Path(value).expanduser() if value else None

    try:
        layout = resolve_native_layout(
            app_dir=optional_path("INKY_BIRD_APP_DIR"),
            support_dir=optional_path("INKY_BIRD_SUPPORT_DIR"),
            config_path=optional_path("INKY_BIRD_CONFIG_PATH"),
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    # Only fixed keys and shell-quoted paths cross the shell boundary.
    for name, value in (
        ("service_prefix", layout.family),
        ("app_dir", layout.app_dir),
        ("support_dir", layout.support_dir),
        ("config_path", layout.config_path),
    ):
        print(f"{name}={shlex.quote(str(value))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
