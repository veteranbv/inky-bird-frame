from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import tomllib
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from inky_bird_frame.config import AppConfig, OIDCConfig, load_config
from inky_bird_frame.errors import InstallationError
from inky_bird_frame.installation import (
    CheckStatus,
    CommandResult,
    DiagnosticCheck,
    InstallationRole,
    _birdbuddy_auth_check,
    _codex_auth_check,
    _display_hardware_check,
    controller_executable_path,
    controller_systemd_units,
    display_systemd_units,
    doctor,
    health_url,
    native_oidc_environment,
    setup,
)


def write_config(directory: Path, codex_path: Path) -> Path:
    path = directory / "config.toml"
    path.write_text(
        f"""
[discovery]
zip_code = "12345"
radius_km = 8
species_limit = 50
window = "last-30-days"

[controller]
workspace_dir = "workspace"
catalog_dir = "catalog"
state_dir = "controller-state"
codex_path = "{codex_path}"
bind_host = "0.0.0.0"
port = 8793
references_per_species = 4
generations_per_cycle = 1
max_generation_attempts = 3

[display_node]
controller_url = "http://controller.test:8793"
state_dir = "display-state"
rotation_mode = "shuffle_bag"
"""
    )
    path.chmod(0o600)
    return path


def native_oidc_config(directory: Path) -> tuple[AppConfig, Path]:
    path = write_config(directory, Path("/usr/local/bin/codex"))
    config = load_config(path)
    return replace(
        config,
        controller=replace(
            config.controller,
            generation_enabled=False,
            configuration_ownership="app",
            oidc=OIDCConfig(
                "https://id.example.test/",
                "client",
                "https://inky.example.test/v1/owner/oidc/callback",
                "NATIVE_TEST_SECRET",
            ),
        ),
    ), path


@pytest.mark.parametrize(
    "installer",
    ["install-controller.sh", "install-controller-systemd.sh", "install-display-local.sh"],
)
def test_native_managed_copy_preserves_required_package_inputs(
    tmp_path: Path, installer: str
) -> None:
    project = Path(__file__).resolve().parents[1]
    settings = tomllib.loads((project / "pyproject.toml").read_text())
    required = settings["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    source, managed = tmp_path / "source checkout", tmp_path / "managed app"
    for root in (source, managed):
        for name in ("src", "catalog", "deploy"):
            (root / name).mkdir(parents=True)
    for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE", *required):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic package input\n")
    (source / "deploy" / installer).write_text("# synthetic staging input\n")
    script = (project / "deploy" / installer).read_text()
    staging = script.split('if [ "${root}" != "${app_dir}" ]; then\n', 1)[1].split("\nfi\n", 1)[0]
    # Execute only real file staging, not an installer, service or package manager.
    subprocess.run(
        ["bash", "-e", "-c", "root=$1; app_dir=$2\n" + staging, "stage", str(source), str(managed)],
        check=True,
        capture_output=True,
    )
    for name in required:
        assert (managed / name).read_bytes() == (source / name).read_bytes()


def test_remote_display_copy_includes_the_required_bundled_guide() -> None:
    script = (Path(__file__).resolve().parents[1] / "deploy/install-display-node.sh").read_text()
    assert '"$1/docs"' in script
    assert (
        "for file in pyproject.toml uv.lock README.md LICENSE docs/browser-access.md; do" in script
    )


def test_native_oidc_secret_persists_privately_and_rotates_without_other_environment(
    tmp_path: Path,
) -> None:
    config, path = native_oidc_config(tmp_path)
    secret = "synthetic with spaces, \\ \" and $ % ` apostrophe' ☃"
    with patch.dict(os.environ, {"NATIVE_TEST_SECRET": secret, "UNRELATED": "do-not-copy"}):
        assert native_oidc_environment(config, path) == {"NATIVE_TEST_SECRET": secret}
    cache = tmp_path / "native-access.env"
    assert cache.stat().st_mode & 0o777 == 0o600
    assert json.loads(cache.read_text().partition("=")[2]) == secret
    assert "UNRELATED" not in cache.read_text()
    with patch.dict(os.environ, {}, clear=True):
        assert native_oidc_environment(config, path) == {"NATIVE_TEST_SECRET": secret}
    with patch.dict(os.environ, {"NATIVE_TEST_SECRET": "replacement-synthetic"}):
        assert native_oidc_environment(config, path) == {
            "NATIVE_TEST_SECRET": "replacement-synthetic"
        }


@pytest.mark.parametrize("kind", ["public", "symlink", "invalid", "oversize"])
def test_native_oidc_cache_rejects_unsafe_or_invalid_state(tmp_path: Path, kind: str) -> None:
    config, path = native_oidc_config(tmp_path)
    cache = tmp_path / "native-access.env"
    cache.write_text('NATIVE_TEST_SECRET="private-synthetic"\n')
    cache.chmod(0o600)
    if kind == "public":
        cache.chmod(0o644)
    elif kind == "symlink":
        target = tmp_path / "private-original"
        cache.rename(target)
        cache.symlink_to(target)
    elif kind == "invalid":
        cache.write_text("NATIVE_TEST_SECRET=not-json-private-detail")
    else:
        cache.write_bytes(b"x" * (64 * 1024 + 1))
    with patch.dict(os.environ, {}, clear=True), pytest.raises(InstallationError) as error:
        native_oidc_environment(config, path)
    assert "private-synthetic" not in str(error.value)
    assert "not-json-private-detail" not in str(error.value)


def test_native_oidc_missing_secret_and_failed_replacement_are_safe(tmp_path: Path) -> None:
    config, path = native_oidc_config(tmp_path)
    with patch.dict(os.environ, {}, clear=True), pytest.raises(InstallationError):
        native_oidc_environment(config, path)
    with patch.dict(os.environ, {"NATIVE_TEST_SECRET": "original-synthetic"}):
        native_oidc_environment(config, path)
    cache = tmp_path / "native-access.env"
    original = cache.read_bytes()
    with (
        patch.dict(os.environ, {"NATIVE_TEST_SECRET": "replacement-synthetic"}),
        patch(
            "inky_bird_frame.http.Path.replace", side_effect=OSError("private replacement detail")
        ),
        pytest.raises(InstallationError) as error,
    ):
        native_oidc_environment(config, path)
    assert "private replacement detail" not in str(error.value)
    assert cache.read_bytes() == original
    assert not list(tmp_path.glob(".native-access.env.*.tmp"))


def test_native_oidc_secret_is_serving_only_in_systemd_and_private_launchd(tmp_path: Path) -> None:
    config, config_path = native_oidc_config(tmp_path)
    units = controller_systemd_units(
        config,
        executable=tmp_path / "inky",
        app_dir=tmp_path,
        config_path=config_path,
        home=tmp_path,
        user="synthetic",
    )
    assert "EnvironmentFile=" in units["inky-bird-frame-controller.service"]
    assert "native-access.env" not in units["inky-bird-frame-scheduler.service"]
    script = (Path(__file__).resolve().parents[1] / "deploy/install-controller.sh").read_text()
    code = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    paths = [
        tmp_path / (name + ".plist")
        for name in (
            "serve",
            "refresh",
            "generate",
            "catalog-publish",
            "notifications",
            "scheduler",
        )
    ]
    arguments = [
        "installer",
        *map(str, paths),
        str(tmp_path),
        str(tmp_path),
        str(config_path),
        str(tmp_path),
    ]
    with (
        patch("sys.argv", arguments),
        patch("inky_bird_frame.config.load_config", return_value=config),
        patch("inky_bird_frame.publisher.sync_public_catalog"),
        patch.dict(os.environ, {"NATIVE_TEST_SECRET": "synthetic-serving-only"}),
    ):
        exec(compile(code, "native-installer-fixture", "exec"), {})
    serving = plistlib.loads(paths[0].read_bytes())
    assert serving["EnvironmentVariables"]["NATIVE_TEST_SECRET"] == "synthetic-serving-only"
    assert paths[0].stat().st_mode & 0o777 == 0o600
    scheduler = plistlib.loads(paths[-1].read_bytes())
    assert "NATIVE_TEST_SECRET" not in scheduler["EnvironmentVariables"]


class InstallationTests(unittest.TestCase):
    def test_controller_executable_path_prepends_and_deduplicates_codex_directory(
        self,
    ) -> None:
        executable_path = controller_executable_path(Path("/opt/homebrew/bin/codex"))

        paths = executable_path.split(":")
        self.assertEqual(paths[0], "/opt/homebrew/bin")
        self.assertEqual(paths.count("/opt/homebrew/bin"), 1)
        self.assertIn("/usr/bin", paths)
        self.assertIn("/bin", paths)
        self.assertEqual(
            controller_executable_path(Path("/usr/local/bin/codex"))
            .split(":")
            .count("/usr/local/bin"),
            1,
        )

    def test_controller_executable_path_rejects_relative_codex_path(self) -> None:
        with self.assertRaisesRegex(InstallationError, "absolute path"):
            controller_executable_path(Path("bin/codex"))

    def test_controller_executable_path_runs_env_wrapper_with_sibling_runtime(self) -> None:
        with TemporaryDirectory() as temporary:
            binary_dir = Path(temporary) / "bin"
            binary_dir.mkdir()
            node = binary_dir / "node"
            node.write_text("#!/bin/sh\nprintf 'node-runtime-ok\\n'\n")
            node.chmod(0o700)
            codex = binary_dir / "codex"
            codex.write_text("#!/usr/bin/env node\n")
            codex.chmod(0o700)

            completed = subprocess.run(
                [codex],
                check=False,
                capture_output=True,
                env={"PATH": controller_executable_path(codex)},
                text=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "node-runtime-ok\n")

    def test_codex_auth_check_uses_configured_runtime_path(self) -> None:
        with TemporaryDirectory() as temporary:
            binary_dir = Path(temporary) / "bin"
            binary_dir.mkdir()
            node = binary_dir / "node"
            node.write_text("#!/bin/sh\nprintf 'Logged in using ChatGPT\n'\n")
            node.chmod(0o700)
            codex = binary_dir / "codex"
            codex.write_text("#!/usr/bin/env node\n")
            codex.chmod(0o700)

            with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
                check = _codex_auth_check(codex)

        self.assertEqual(check.status, CheckStatus.PASS)
        self.assertEqual(check.summary, "Codex CLI is authenticated with ChatGPT")

    def test_birdbuddy_doctor_check_warns_when_login_is_missing(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = write_config(root, Path("/usr/bin/false"))
            config_path.write_text(
                config_path.read_text().replace(
                    "[discovery]\n",
                    '[discovery]\nsources = ["birdbuddy"]\n',
                )
            )
            config = load_config(config_path)
            with patch(
                "inky_bird_frame.installation.birdbuddy_status",
                return_value={"authenticated": False},
            ):
                check = _birdbuddy_auth_check(config)

        self.assertEqual(check.status, CheckStatus.WARN)
        self.assertIn("not authenticated", check.summary)

    def test_birdbuddy_doctor_check_reports_redacted_selected_feeder(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(write_config(root, Path("/usr/bin/false")))
            with patch(
                "inky_bird_frame.installation.birdbuddy_status",
                return_value={
                    "authenticated": True,
                    "feeder": {"name": "Garden feeder", "role": "member"},
                },
            ):
                check = _birdbuddy_auth_check(config)

        self.assertEqual(check.status, CheckStatus.PASS)
        self.assertEqual(check.detail, "Selected feeder: Garden feeder (member)")

    def test_display_hardware_check_accepts_both_supported_geometries(self) -> None:
        for geometry in ("1600x1200", "800x480"):
            with (
                self.subTest(geometry=geometry),
                patch(
                    "inky_bird_frame.installation._run",
                    return_value=CommandResult(0, geometry, ""),
                ),
            ):
                check = _display_hardware_check()

            self.assertEqual(check.status, CheckStatus.PASS)
            self.assertIn(geometry, check.summary)

    def test_display_hardware_check_rejects_unknown_geometry(self) -> None:
        with patch(
            "inky_bird_frame.installation._run",
            return_value=CommandResult(0, "600x400", ""),
        ):
            check = _display_hardware_check()

        self.assertEqual(check.status, CheckStatus.FAIL)
        self.assertIn("PIM773", check.remediation or "")
        self.assertIn("PIM774", check.remediation or "")

    def test_controller_systemd_units_quote_paths_and_use_configured_schedules(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex"
            codex.touch(mode=0o700)
            config = load_config(write_config(root, codex))
            units = controller_systemd_units(
                config,
                executable=Path("/opt/inky bird/$bin/inky-bird-frame"),
                app_dir=Path("/opt/inky bird/100% cache"),
                config_path=Path("/etc/inky bird/100% config.toml"),
                home=Path("/home/frame user"),
                user="frame",
            )

        self.assertEqual(len(units), 5)
        self.assertIn(
            "WorkingDirectory=/opt/inky bird/100%% cache",
            units["inky-bird-frame-controller.service"],
        )
        self.assertIn(
            '"/opt/inky bird/$bin/inky-bird-frame"',
            units["inky-bird-frame-controller.service"],
        )
        self.assertIn(
            '"/etc/inky bird/100%% config.toml"', units["inky-bird-frame-refresh.service"]
        )
        self.assertIn(
            f'Environment="PATH={codex.parent}:', units["inky-bird-frame-generate.service"]
        )
        self.assertIn("OnUnitActiveSec=15min", units["inky-bird-frame-refresh.timer"])
        self.assertIn("OnUnitActiveSec=360min", units["inky-bird-frame-generate.timer"])
        self.assertIn("OnActiveSec=2min", units["inky-bird-frame-generate.timer"])
        self.assertNotIn("OnBootSec=", units["inky-bird-frame-generate.timer"])
        self.assertIn(
            "After=network-online.target inky-bird-frame-refresh.service",
            units["inky-bird-frame-generate.service"],
        )
        self.assertIn(
            "Requires=inky-bird-frame-refresh.service",
            units["inky-bird-frame-generate.service"],
        )
        self.assertNotIn(
            "Requires=inky-bird-frame-refresh.service",
            units["inky-bird-frame-refresh.service"],
        )
        self.assertNotIn("inky-bird-frame-notifications.timer", units)
        self.assertNotIn("inky-bird-frame-catalog-publish.timer", units)
        serve_unit = units["inky-bird-frame-controller.service"]
        self.assertIn("NoNewPrivileges=true", serve_unit)
        self.assertIn("PrivateTmp=true", serve_unit)
        self.assertIn("ProtectKernelTunables=true", serve_unit)
        self.assertIn("ProtectKernelModules=true", serve_unit)
        self.assertIn("ProtectControlGroups=true", serve_unit)
        self.assertIn("RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX", serve_unit)
        self.assertNotIn("ProtectSystem=", serve_unit)
        self.assertNotIn("ProtectHome=", serve_unit)
        for codex_unit in ("inky-bird-frame-refresh.service", "inky-bird-frame-generate.service"):
            self.assertNotIn("NoNewPrivileges=", units[codex_unit])
            self.assertNotIn("PrivateTmp=", units[codex_unit])
            self.assertNotIn("ProtectKernel", units[codex_unit])
            self.assertNotIn("ProtectControlGroups=", units[codex_unit])
            self.assertNotIn("RestrictAddressFamilies=", units[codex_unit])

    def test_display_systemd_units_use_startup_rotation_and_jitter_values(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex"
            codex.touch(mode=0o700)
            path = write_config(root, codex)
            path.write_text(
                path.read_text()
                + """
[schedule]
rotation_minutes = 3
rotation_jitter_seconds = 7
display_startup_delay_seconds = 30
"""
            )
            config = load_config(path)
            units = display_systemd_units(
                config,
                executable=Path("/home/frame/Pimoroni $Env/bin/inky-bird-frame"),
                app_dir=Path("/home/frame/Inky Bird Frame"),
                config_path=Path("/home/frame/$config.toml"),
                user="frame",
            )

        service = units["inky-bird-frame-display.service"]
        timer = units["inky-bird-frame-display.timer"]
        self.assertEqual(len(units), 2)
        self.assertIn("WorkingDirectory=/home/frame/Inky Bird Frame", service)
        self.assertIn('"/home/frame/Pimoroni $Env/bin/inky-bird-frame"', service)
        self.assertIn('"/home/frame/$config.toml"', service)
        self.assertIn("OnActiveSec=30s", timer)
        self.assertIn("OnUnitActiveSec=180s", timer)
        self.assertIn("RandomizedDelaySec=7s", timer)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("PrivateTmp=true", service)
        self.assertIn("ProtectKernelTunables=true", service)
        self.assertIn("ProtectKernelModules=true", service)
        self.assertIn("ProtectControlGroups=true", service)
        self.assertIn("RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX", service)
        self.assertNotIn("ProtectSystem=", service)
        self.assertNotIn("ProtectHome=", service)
        self.assertNotIn("DevicePolicy=", service)
        self.assertNotIn("PrivateDevices=", service)

    def test_display_health_url_preserves_controller_path_prefix(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex"
            codex.touch(mode=0o700)
            path = write_config(root, codex)
            path.write_text(
                path.read_text().replace(
                    'controller_url = "http://controller.test:8793"',
                    'controller_url = "https://frame.example/inky/"',
                )
            )
            config = load_config(path)

        self.assertEqual(
            health_url(config, InstallationRole.DISPLAY),
            "https://frame.example/inky/health",
        )

    def test_controller_health_uses_direct_tls_origin_and_ipv6_loopback(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex"
            codex.touch(mode=0o700)
            path = write_config(root, codex)
            tls_settings = (
                '\ntls_certificate = "cert.pem"\ntls_private_key = "key.pem"\n'
                'owner_allowed_origins = ["https://inky.example.test:8793"]'
            )
            path.write_text(
                path.read_text().replace(
                    'bind_host = "0.0.0.0"',
                    'bind_host = "::"' + tls_settings,
                )
            )
            config = load_config(path)
            self.assertEqual(
                health_url(config, InstallationRole.CONTROLLER),
                "https://inky.example.test:8793/health",
            )
            path.write_text(
                path.read_text().replace(
                    tls_settings,
                    "",
                )
            )
            self.assertEqual(
                health_url(load_config(path), InstallationRole.CONTROLLER),
                "http://[::1]:8793/health",
            )

    def test_controller_health_origin_is_separate_from_owner_and_display_origins(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex"
            codex.touch(mode=0o700)
            path = write_config(root, codex)
            path.write_text(
                path.read_text().replace(
                    'bind_host = "0.0.0.0"',
                    'bind_host = "0.0.0.0"\n'
                    'tls_certificate = "cert.pem"\ntls_private_key = "key.pem"\n'
                    'healthcheck_origin = "https://backend.example.test:8793"\n'
                    'owner_allowed_origins = ["https://inky.example.test"]',
                )
            )
            config = load_config(path)
            self.assertEqual(
                health_url(config, InstallationRole.CONTROLLER),
                "https://backend.example.test:8793/health",
            )
            self.assertEqual(
                config.controller.owner_allowed_origins, ("https://inky.example.test",)
            )
            self.assertEqual(
                health_url(config, InstallationRole.DISPLAY),
                config.display_node.controller_url + "/health",
            )

    def test_setup_preview_does_not_run_installer(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex"
            codex.touch(mode=0o700)
            config = write_config(root, codex)
            with (
                patch("inky_bird_frame.installation.platform.system", return_value="Darwin"),
                patch("inky_bird_frame.installation.subprocess.run") as run,
            ):
                result = setup(InstallationRole.CONTROLLER, config, apply=False)

        self.assertFalse(result["applied"])
        self.assertIn("--yes", str(result["confirmation"]))
        run.assert_not_called()

    def test_managed_controller_rejects_environment_provider_credentials(self) -> None:
        cases = (
            (
                'source = "ebird"\nebird_api_key_env = "TEST_PROVIDER_TOKEN"\n',
                "ebird_api_key_env",
            ),
            (
                'source = "birdweather"\nbirdweather_token_env = "TEST_PROVIDER_TOKEN"\n',
                "birdweather_token_env",
            ),
        )
        for configuration, expected in cases:
            with self.subTest(expected=expected), TemporaryDirectory() as temporary:
                root = Path(temporary)
                codex = root / "codex"
                codex.touch(mode=0o700)
                config = write_config(root, codex)
                config.write_text(
                    config.read_text().replace("[discovery]\n", f"[discovery]\n{configuration}")
                )
                with (
                    patch.dict("os.environ", {"TEST_PROVIDER_TOKEN": "secret"}),
                    self.assertRaisesRegex(InstallationError, expected),
                ):
                    setup(InstallationRole.CONTROLLER, config, apply=False)

    def test_managed_controller_rejects_geoapify_environment_credential(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex"
            codex.touch(mode=0o700)
            config = write_config(root, codex)
            config.write_text(
                config.read_text().replace(
                    'zip_code = "12345"',
                    'postal_code = "SW1A 1AA"\n'
                    'country_code = "gb"\n'
                    'geoapify_api_key_env = "TEST_PROVIDER_TOKEN"',
                )
            )
            with (
                patch.dict("os.environ", {"TEST_PROVIDER_TOKEN": "secret"}),
                self.assertRaisesRegex(InstallationError, "geoapify_api_key_env"),
            ):
                setup(InstallationRole.CONTROLLER, config, apply=False)

    def test_setup_uses_explicit_source_checkout(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex"
            codex.touch(mode=0o700)
            config = write_config(root, codex)
            source = root / "source"
            script = source / "deploy/install-controller.sh"
            script.parent.mkdir(parents=True)
            script.touch(mode=0o700)
            resolved_source = source.resolve()
            resolved_script = script.resolve()
            with (
                patch("inky_bird_frame.installation.platform.system", return_value="Darwin"),
                patch(
                    "inky_bird_frame.installation.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, "installed", ""),
                ) as run,
            ):
                result = setup(
                    InstallationRole.CONTROLLER,
                    config,
                    apply=True,
                    source_dir=source,
                )

        self.assertEqual(run.call_args.args[0], [str(resolved_script)])
        self.assertEqual(result["source"], str(resolved_source))

    def test_setup_apply_passes_only_explicit_overrides(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex"
            codex.touch(mode=0o700)
            config = write_config(root, codex)
            app_dir = root / "managed app"
            with (
                patch.dict(
                    os.environ,
                    {
                        "UV_BIN": "/untrusted/uv",
                        "INKY_BIRD_SUPPORT_DIR": "/untrusted/support",
                        "INKY_BIRD_DISPLAY_VENV": "/untrusted/venv",
                        "INKY_BIRD_RUN_INITIAL_DISPLAY": "true",
                    },
                ),
                patch("inky_bird_frame.installation.platform.system", return_value="Darwin"),
                patch(
                    "inky_bird_frame.installation.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, "installed", ""),
                ) as run,
            ):
                result = setup(
                    InstallationRole.CONTROLLER,
                    config,
                    apply=True,
                    app_dir=app_dir,
                    python_version="3.12",
                )

        environment = run.call_args.kwargs["env"]
        self.assertTrue(result["applied"])
        self.assertEqual(environment["INKY_BIRD_CONFIG_PATH"], str(config.resolve()))
        self.assertEqual(environment["INKY_BIRD_APP_DIR"], str(app_dir.resolve()))
        self.assertEqual(environment["INKY_BIRD_PYTHON_VERSION"], "3.12")
        self.assertNotIn("UV_BIN", environment)
        self.assertNotIn("INKY_BIRD_SUPPORT_DIR", environment)
        self.assertNotIn("INKY_BIRD_DISPLAY_VENV", environment)
        self.assertNotIn("INKY_BIRD_RUN_INITIAL_DISPLAY", environment)

    def test_linux_setup_authorizes_sudo_before_streamed_installer(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex"
            codex.touch(mode=0o700)
            config = write_config(root, codex)
            source = root / "source"
            script = source / "deploy/install-display-local.sh"
            script.parent.mkdir(parents=True)
            script.touch(mode=0o700)
            with (
                patch("inky_bird_frame.installation.platform.system", return_value="Linux"),
                patch(
                    "inky_bird_frame.installation.subprocess.run",
                    side_effect=(
                        subprocess.CompletedProcess(["sudo", "-v"], 0),
                        subprocess.CompletedProcess([], 0),
                    ),
                ) as run,
            ):
                result = setup(
                    InstallationRole.DISPLAY,
                    config,
                    apply=True,
                    source_dir=source,
                )

        authorization, installer = run.call_args_list
        self.assertEqual(authorization.args[0], ["sudo", "-v"])
        self.assertNotIn("capture_output", authorization.kwargs)
        self.assertEqual(installer.args[0], [str(script.resolve())])
        self.assertNotIn("capture_output", installer.kwargs)
        self.assertIs(installer.kwargs["stdout"], sys.stderr)
        self.assertNotIn("stderr", installer.kwargs)
        self.assertTrue(result["applied"])
        self.assertEqual(result["summary"], "Installer completed successfully.")

    def test_linux_setup_stops_when_sudo_authorization_fails(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex"
            codex.touch(mode=0o700)
            config = write_config(root, codex)
            source = root / "source"
            script = source / "deploy/install-controller-systemd.sh"
            script.parent.mkdir(parents=True)
            script.touch(mode=0o700)
            with (
                patch("inky_bird_frame.installation.platform.system", return_value="Linux"),
                patch(
                    "inky_bird_frame.installation.subprocess.run",
                    return_value=subprocess.CompletedProcess(["sudo", "-v"], 1),
                ) as run,
                self.assertRaisesRegex(InstallationError, "Administrator authorization failed"),
            ):
                setup(
                    InstallationRole.CONTROLLER,
                    config,
                    apply=True,
                    source_dir=source,
                )

        run.assert_called_once()

    def test_doctor_reports_invalid_config_without_dependent_checks(self) -> None:
        with TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.toml"
            with patch("inky_bird_frame.installation.platform.system", return_value="Darwin"):
                report = doctor(InstallationRole.CONTROLLER, missing)

        self.assertFalse(report.ready)
        self.assertEqual(report.checks[-1].check_id, "config")
        self.assertEqual(report.checks[-1].status, CheckStatus.FAIL)

    def test_controller_doctor_accepts_ready_dependencies_and_services(self) -> None:
        passing = DiagnosticCheck("mock", CheckStatus.PASS, "ready")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex"
            codex.touch(mode=0o700)
            config = write_config(root, codex)
            (root / "workspace").mkdir()
            with (
                patch("inky_bird_frame.installation.platform.system", return_value="Darwin"),
                patch("inky_bird_frame.installation._codex_auth_check", return_value=passing),
                patch("inky_bird_frame.installation._launchd_checks", return_value=[passing]),
                patch(
                    "inky_bird_frame.installation._controller_health_check", return_value=passing
                ),
            ):
                report = doctor(InstallationRole.CONTROLLER, config)

        self.assertTrue(report.ready)
        self.assertTrue(all(check.status is not CheckStatus.FAIL for check in report.checks))

    def test_controller_doctor_fails_when_workspace_is_missing(self) -> None:
        passing = DiagnosticCheck("mock", CheckStatus.PASS, "ready")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex"
            codex.touch(mode=0o700)
            config = write_config(root, codex)
            with (
                patch("inky_bird_frame.installation.platform.system", return_value="Darwin"),
                patch("inky_bird_frame.installation._codex_auth_check", return_value=passing),
                patch("inky_bird_frame.installation._launchd_checks", return_value=[passing]),
                patch(
                    "inky_bird_frame.installation._controller_health_check", return_value=passing
                ),
            ):
                report = doctor(InstallationRole.CONTROLLER, config)

        check = next(item for item in report.checks if item.check_id == "workspace")
        self.assertEqual(check.status, CheckStatus.FAIL)
        self.assertIn("does not exist", check.summary)
        self.assertFalse(report.ready)

    def test_controller_doctor_reports_failed_scheduled_job(self) -> None:
        passing = DiagnosticCheck("mock", CheckStatus.PASS, "ready")

        def job_state(command: list[str], *, timeout_seconds: int = 15) -> CommandResult:
            del timeout_seconds
            if command[-1] == "inky-bird-frame-generate.service":
                return CommandResult(0, "failed", "")
            return CommandResult(1, "inactive", "")

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex"
            codex.touch(mode=0o700)
            config = write_config(root, codex)
            with (
                patch("inky_bird_frame.installation.platform.system", return_value="Linux"),
                patch("inky_bird_frame.installation.shutil.which", return_value="/bin/systemctl"),
                patch("inky_bird_frame.installation._codex_auth_check", return_value=passing),
                patch("inky_bird_frame.installation._systemd_unit_check", return_value=passing),
                patch("inky_bird_frame.installation._run", side_effect=job_state),
                patch(
                    "inky_bird_frame.installation._controller_health_check", return_value=passing
                ),
            ):
                report = doctor(InstallationRole.CONTROLLER, config)

        check = next(item for item in report.checks if item.check_id == "job_generate")
        self.assertEqual(check.status, CheckStatus.FAIL)
        self.assertFalse(report.ready)

    def test_display_doctor_keeps_collecting_after_hardware_failure(self) -> None:
        passing = DiagnosticCheck("mock", CheckStatus.PASS, "ready")
        failing = DiagnosticCheck("inky_hardware", CheckStatus.FAIL, "not detected")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex"
            codex.touch(mode=0o700)
            config = write_config(root, codex)
            with (
                patch("inky_bird_frame.installation.platform.system", return_value="Linux"),
                patch("inky_bird_frame.installation.shutil.which", return_value="/bin/systemctl"),
                patch(
                    "inky_bird_frame.installation._display_boot_config_check", return_value=passing
                ),
                patch("inky_bird_frame.installation._display_hardware_check", return_value=failing),
                patch("inky_bird_frame.installation._systemd_unit_check", return_value=passing),
                patch(
                    "inky_bird_frame.installation._run",
                    return_value=CommandResult(1, "inactive", ""),
                ),
                patch(
                    "inky_bird_frame.installation._controller_health_check", return_value=passing
                ),
            ):
                report = doctor(InstallationRole.DISPLAY, config)

        self.assertFalse(report.ready)
        self.assertEqual(report.checks[-1], passing)

    def test_display_doctor_fails_when_last_run_state_is_unavailable(self) -> None:
        passing = DiagnosticCheck("mock", CheckStatus.PASS, "ready")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex"
            codex.touch(mode=0o700)
            config = write_config(root, codex)
            with (
                patch("inky_bird_frame.installation.platform.system", return_value="Linux"),
                patch("inky_bird_frame.installation.shutil.which", return_value="/bin/systemctl"),
                patch(
                    "inky_bird_frame.installation._display_boot_config_check", return_value=passing
                ),
                patch("inky_bird_frame.installation._display_hardware_check", return_value=passing),
                patch("inky_bird_frame.installation._systemd_unit_check", return_value=passing),
                patch(
                    "inky_bird_frame.installation._run",
                    return_value=CommandResult(4, "unknown", "Unit not found"),
                ),
                patch(
                    "inky_bird_frame.installation._controller_health_check", return_value=passing
                ),
            ):
                report = doctor(InstallationRole.DISPLAY, config)

        check = next(item for item in report.checks if item.check_id == "display_last_run")
        self.assertEqual(check.status, CheckStatus.FAIL)
        self.assertFalse(report.ready)

    def test_setup_environment_does_not_modify_process_environment(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex"
            codex.touch(mode=0o700)
            config = write_config(root, codex)
            before = os.environ.copy()
            with (
                patch("inky_bird_frame.installation.platform.system", return_value="Darwin"),
                patch(
                    "inky_bird_frame.installation.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, "installed", ""),
                ),
            ):
                setup(InstallationRole.CONTROLLER, config, apply=True)

        self.assertEqual(os.environ, before)


if __name__ == "__main__":
    unittest.main()
