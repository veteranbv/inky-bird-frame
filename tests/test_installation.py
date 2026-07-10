from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inky_bird_frame.config import load_config
from inky_bird_frame.errors import InstallationError
from inky_bird_frame.installation import (
    CheckStatus,
    CommandResult,
    DiagnosticCheck,
    InstallationRole,
    controller_systemd_units,
    display_systemd_units,
    doctor,
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


class InstallationTests(unittest.TestCase):
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
            '"/opt/inky bird/$$bin/inky-bird-frame"',
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
        self.assertNotIn("inky-bird-frame-notifications.timer", units)
        self.assertNotIn("inky-bird-frame-catalog-publish.timer", units)

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
        self.assertIn('"/home/frame/Pimoroni $$Env/bin/inky-bird-frame"', service)
        self.assertIn('"/home/frame/$$config.toml"', service)
        self.assertIn("OnActiveSec=30s", timer)
        self.assertIn("OnUnitActiveSec=180s", timer)
        self.assertIn("RandomizedDelaySec=7s", timer)

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
