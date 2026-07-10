"""Role setup orchestration and read-only installation diagnostics."""

from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .config import AppConfig, load_config
from .errors import ConfigurationError, InkyBirdFrameError, InstallationError
from .http import get_json


class InstallationRole(StrEnum):
    CONTROLLER = "controller"
    DISPLAY = "display"


class CheckStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class DiagnosticCheck:
    check_id: str
    status: CheckStatus
    summary: str
    detail: str | None = None
    remediation: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "id": self.check_id,
            "status": self.status.value,
            "summary": self.summary,
        }
        if self.detail is not None:
            result["detail"] = self.detail
        if self.remediation is not None:
            result["remediation"] = self.remediation
        return result


@dataclass(frozen=True)
class DoctorReport:
    role: InstallationRole
    checks: tuple[DiagnosticCheck, ...]

    @property
    def ready(self) -> bool:
        return all(check.status is not CheckStatus.FAIL for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "ready": self.ready,
            "checks": [check.as_dict() for check in self.checks],
        }


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def _run(command: Sequence[str], *, timeout_seconds: int = 15) -> CommandResult:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandResult(1, "", str(exc))
    return CommandResult(completed.returncode, completed.stdout.strip(), completed.stderr.strip())


def _systemd_literal(value: str) -> str:
    if any(character in value for character in ("\0", "\n", "\r")):
        raise InstallationError("systemd values cannot contain NUL or line breaks")
    return value.replace("%", "%%")


def _systemd_quote(value: str, *, escape_dollars: bool = False) -> str:
    escaped = _systemd_literal(value).replace("\\", "\\\\").replace('"', '\\"')
    if escape_dollars:
        escaped = escaped.replace("$", "$$")
    return f'"{escaped}"'


def controller_systemd_units(
    config: AppConfig,
    *,
    executable: Path,
    app_dir: Path,
    config_path: Path,
    home: Path,
    user: str,
) -> dict[str, str]:
    """Render the complete systemd controller unit set for one installation."""
    common = (
        f"User={user}\n"
        f"WorkingDirectory={_systemd_literal(str(app_dir))}\n"
        f"Environment={_systemd_quote(f'HOME={home}')}\n"
        "Environment=PYTHONUNBUFFERED=1\n"
    )

    def command(*arguments: str) -> str:
        return " ".join(
            _systemd_quote(argument, escape_dollars=True)
            for argument in (str(executable), *arguments)
        )

    units = {
        "inky-bird-frame-controller.service": f"""[Unit]
Description=Serve the Inky Bird Frame approved catalog
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
{common}ExecStart={command("serve", "--config", str(config_path))}
Restart=on-failure
RestartSec=10s

[Install]
WantedBy=multi-user.target
"""
    }
    jobs = (
        ("refresh", config.schedule.refresh_minutes, True),
        ("generate", config.schedule.generation_minutes, True),
        ("catalog-publish", config.schedule.catalog_publish_minutes, config.public_catalog.enabled),
        (
            "notifications",
            config.notifications.delivery_retry_minutes,
            config.notifications.enabled,
        ),
    )
    for name, minutes, enabled in jobs:
        if not enabled:
            continue
        arguments = (
            ("notifications", "dispatch", "--config", str(config_path))
            if name == "notifications"
            else (name, "--config", str(config_path))
        )
        units[f"inky-bird-frame-{name}.service"] = f"""[Unit]
Description=Run the Inky Bird Frame {name} cycle
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
{common}ExecStart={command(*arguments)}
TimeoutStartSec=30min
"""
        units[f"inky-bird-frame-{name}.timer"] = f"""[Unit]
Description=Schedule the Inky Bird Frame {name} cycle

[Timer]
OnBootSec=2min
OnUnitActiveSec={minutes}min
Unit=inky-bird-frame-{name}.service

[Install]
WantedBy=timers.target
"""
    return units


def display_systemd_units(
    config: AppConfig,
    *,
    executable: Path,
    app_dir: Path,
    config_path: Path,
    user: str,
) -> dict[str, str]:
    """Render the display service and timer from validated configuration."""
    exec_start = (
        f"{_systemd_quote(str(executable), escape_dollars=True)} display-cycle "
        f"--config {_systemd_quote(str(config_path), escape_dollars=True)}"
    )
    service = f"""[Unit]
Description=Rotate the next approved Inky Bird Frame plate
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User={user}
WorkingDirectory={_systemd_literal(str(app_dir))}
Environment=PYTHONUNBUFFERED=1
ExecStart={exec_start}
TimeoutStartSec=15min
"""
    timer = f"""[Unit]
Description=Rotate the Inky Bird Frame on its configured schedule

[Timer]
OnActiveSec={config.schedule.display_startup_delay_seconds}s
OnUnitActiveSec={config.schedule.rotation_minutes * 60}s
RandomizedDelaySec={config.schedule.rotation_jitter_seconds}s
Unit=inky-bird-frame-display.service

[Install]
WantedBy=timers.target
"""
    return {
        "inky-bird-frame-display.service": service,
        "inky-bird-frame-display.timer": timer,
    }


def _pass(check_id: str, summary: str, detail: str | None = None) -> DiagnosticCheck:
    return DiagnosticCheck(check_id, CheckStatus.PASS, summary, detail)


def _warn(
    check_id: str,
    summary: str,
    *,
    detail: str | None = None,
    remediation: str | None = None,
) -> DiagnosticCheck:
    return DiagnosticCheck(check_id, CheckStatus.WARN, summary, detail, remediation)


def _fail(
    check_id: str,
    summary: str,
    *,
    detail: str | None = None,
    remediation: str | None = None,
) -> DiagnosticCheck:
    return DiagnosticCheck(check_id, CheckStatus.FAIL, summary, detail, remediation)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _supported_platform(role: InstallationRole) -> DiagnosticCheck:
    system = platform.system()
    if role is InstallationRole.CONTROLLER and system == "Darwin":
        return _pass("platform", "macOS launchd is available for controller services")
    if system == "Linux" and shutil.which("systemctl") is not None:
        return _pass("platform", f"systemd is available for {role.value} services")
    supported = "macOS or systemd Linux" if role is InstallationRole.CONTROLLER else "systemd Linux"
    return _fail(
        "platform",
        (
            "A native service manager is not available for "
            f"{role.value} setup on {system or 'Unknown'}"
        ),
        remediation=f"Use {supported}; see docs/installation.md for the support matrix.",
    )


def _python_check() -> DiagnosticCheck:
    version = platform.python_version()
    return _pass("python", f"Python {version} satisfies the 3.11+ requirement")


def _config_permissions(path: Path) -> DiagnosticCheck:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return _warn(
            "config_permissions",
            "The private configuration is readable by other local users",
            detail=f"Mode is {mode:04o}",
            remediation=f"Run: chmod 600 {path}",
        )
    return _pass("config_permissions", "Private configuration permissions are restricted")


def _writable_directory(check_id: str, path: Path) -> DiagnosticCheck:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK):
        return _pass(check_id, f"Runtime path is writable: {path}")
    return _fail(
        check_id,
        f"Runtime path is not writable: {path}",
        remediation=(
            "Choose a path owned by the service user or correct its ownership and permissions."
        ),
    )


def _executable_check(path: Path) -> DiagnosticCheck:
    if path.is_file() and os.access(path, os.X_OK):
        return _pass("codex_executable", f"Codex CLI is executable: {path}")
    return _fail(
        "codex_executable",
        f"Codex CLI is not executable: {path}",
        remediation="Install Codex CLI and set controller.codex_path to its absolute path.",
    )


def _codex_auth_check(path: Path) -> DiagnosticCheck:
    result = _run([str(path), "login", "status"])
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if result.returncode != 0:
        return _fail(
            "codex_auth",
            "Codex CLI is not authenticated",
            detail=output or "codex login status failed",
            remediation="Run codex login, or codex login --device-auth on a headless controller.",
        )
    if "ChatGPT" in output:
        return _pass("codex_auth", "Codex CLI is authenticated with ChatGPT")
    return _warn(
        "codex_auth",
        "Codex CLI is authenticated without a confirmed ChatGPT login",
        detail=output,
        remediation="Use codex login with ChatGPT to use subscription-backed generation.",
    )


def _health_url(config: AppConfig, role: InstallationRole) -> str:
    if role is InstallationRole.CONTROLLER:
        host = config.controller.bind_host
        if host in {"0.0.0.0", "::", "[::]"}:
            host = "127.0.0.1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{config.controller.port}/health"
    parsed = urlsplit(config.display_node.controller_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))


def _controller_health_check(config: AppConfig, role: InstallationRole) -> DiagnosticCheck:
    url = _health_url(config, role)
    try:
        payload = get_json(url, timeout_seconds=5)
    except (InkyBirdFrameError, OSError, ValueError) as exc:
        return _fail(
            "controller_health",
            "The controller health endpoint is not reachable",
            detail=str(exc),
            remediation=f"Confirm routing and open {url} from this machine.",
        )
    if isinstance(payload, dict) and payload.get("ok") is True:
        return _pass("controller_health", f"Controller health endpoint is ready: {url}")
    return _fail(
        "controller_health",
        "The controller returned an invalid health response",
        detail=f"URL: {url}",
        remediation="Inspect the controller service logs and configuration.",
    )


def _launchd_checks(config: AppConfig) -> Iterable[DiagnosticCheck]:
    labels = [
        "com.inky-bird-frame.serve",
        "com.inky-bird-frame.refresh",
        "com.inky-bird-frame.generate",
    ]
    if config.public_catalog.enabled:
        labels.append("com.inky-bird-frame.catalog-publish")
    if config.notifications.enabled:
        labels.append("com.inky-bird-frame.notifications")
    uid = os.getuid()
    for label in labels:
        result = _run(["launchctl", "print", f"gui/{uid}/{label}"])
        if result.returncode == 0:
            yield _pass(f"service_{label.rsplit('.', 1)[-1]}", f"LaunchAgent is loaded: {label}")
        else:
            yield _fail(
                f"service_{label.rsplit('.', 1)[-1]}",
                f"LaunchAgent is not loaded: {label}",
                remediation="Run setup controller --yes in the logged-in service account.",
            )


def _systemd_unit_check(unit: str) -> DiagnosticCheck:
    enabled = _run(["systemctl", "is-enabled", unit])
    running = _run(["systemctl", "is-active", unit])
    if enabled.returncode == 0 and running.returncode == 0:
        return _pass(f"service_{unit}", f"systemd unit is enabled and active: {unit}")
    states = (
        f"enabled={enabled.stdout or enabled.stderr}; active={running.stdout or running.stderr}"
    )
    return _fail(
        f"service_{unit}",
        f"systemd unit is not ready: {unit}",
        detail=states,
        remediation=f"Inspect: systemctl status {unit}",
    )


def _systemd_last_run_check(
    unit: str,
    *,
    check_id: str,
    description: str,
) -> DiagnosticCheck:
    result = _run(["systemctl", "is-failed", unit])
    if result.returncode == 0:
        return _fail(
            check_id,
            f"The most recent {description} failed",
            remediation=f"Inspect: journalctl -u {unit}",
        )
    if result.returncode == 1 and result.stdout:
        return _pass(check_id, f"The {description} service is not failed")
    detail = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return _fail(
        check_id,
        f"The {description} service state could not be determined",
        detail=detail or f"systemctl exited with status {result.returncode}",
        remediation=f"Inspect: systemctl status {unit}",
    )


def _systemd_controller_checks(config: AppConfig) -> Iterable[DiagnosticCheck]:
    yield _systemd_unit_check("inky-bird-frame-controller.service")
    jobs = [("refresh", "observation refresh"), ("generate", "generation cycle")]
    if config.public_catalog.enabled:
        jobs.append(("catalog-publish", "catalog publication"))
    if config.notifications.enabled:
        jobs.append(("notifications", "notification dispatch"))
    for name, description in jobs:
        yield _systemd_unit_check(f"inky-bird-frame-{name}.timer")
        yield _systemd_last_run_check(
            f"inky-bird-frame-{name}.service",
            check_id=f"job_{name.replace('-', '_')}",
            description=description,
        )


def _display_hardware_check() -> DiagnosticCheck:
    code = (
        "from inky.auto import auto; display = auto(); print(f'{display.width}x{display.height}')"
    )
    result = _run([sys.executable, "-c", code], timeout_seconds=30)
    output = result.stdout.strip()
    if result.returncode != 0:
        return _fail(
            "inky_hardware",
            "Pimoroni Inky auto-detection failed",
            detail=result.stderr or output,
            remediation="Check the 40-pin connection, SPI/I2C settings, and the Inky Python extra.",
        )
    if output == "1600x1200":
        return _pass("inky_hardware", "Detected the supported 1600x1200 Inky display")
    return _fail(
        "inky_hardware",
        f"Detected unsupported Inky geometry: {output or 'unknown'}",
        remediation="This release supports the 13.3-inch PIM774 1600x1200 panel.",
    )


def _display_boot_config_check() -> DiagnosticCheck:
    paths = (Path("/boot/firmware/config.txt"), Path("/boot/config.txt"))
    path = next((candidate for candidate in paths if candidate.is_file()), None)
    if path is None:
        return _warn(
            "boot_config",
            "Raspberry Pi boot configuration was not found",
            remediation="Use Raspberry Pi OS Bookworm or later and follow Pimoroni's Inky setup.",
        )
    try:
        content = path.read_text(errors="replace")
    except OSError as exc:
        return _warn(
            "boot_config",
            f"Raspberry Pi boot configuration could not be read: {path}",
            detail=str(exc),
            remediation="Run doctor with an account that can read the Pi boot configuration.",
        )
    lines = {
        line.strip().lower()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    missing = [
        setting for setting in ("dtparam=spi=on", "dtoverlay=spi0-0cs") if setting not in lines
    ]
    if not missing:
        return _pass("boot_config", f"SPI and Inky chip-select settings are present in {path}")
    return _warn(
        "boot_config",
        "Recommended Pimoroni boot settings are not both visible",
        detail=f"Missing from {path}: {', '.join(missing)}",
        remediation="Run the Pimoroni Inky installer or apply its documented manual settings.",
    )


def doctor(role: InstallationRole, config_path: Path) -> DoctorReport:
    checks: list[DiagnosticCheck] = [_supported_platform(role), _python_check()]
    try:
        config = load_config(config_path)
    except (ConfigurationError, OSError) as exc:
        checks.append(
            _fail(
                "config",
                "Application configuration is invalid",
                detail=str(exc),
                remediation="Start from config.example.toml, then run config validate.",
            )
        )
        return DoctorReport(role, tuple(checks))
    checks.append(_pass("config", f"Configuration is valid: {config_path}"))
    checks.append(_config_permissions(config_path))

    if role is InstallationRole.CONTROLLER:
        checks.extend(
            (
                _writable_directory("workspace", config.controller.workspace_dir),
                _writable_directory("catalog", config.controller.catalog_dir),
                _writable_directory("controller_state", config.controller.state_dir),
                _executable_check(config.controller.codex_path),
            )
        )
        if config.controller.codex_path.is_file():
            checks.append(_codex_auth_check(config.controller.codex_path))
        if platform.system() == "Darwin":
            checks.extend(_launchd_checks(config))
        elif shutil.which("systemctl") is not None:
            checks.extend(_systemd_controller_checks(config))
        checks.append(_controller_health_check(config, role))
    else:
        checks.append(_writable_directory("display_state", config.display_node.state_dir))
        checks.append(_display_boot_config_check())
        checks.append(_display_hardware_check())
        if platform.system() == "Linux" and shutil.which("systemctl") is not None:
            checks.append(_systemd_unit_check("inky-bird-frame-display.timer"))
            checks.append(
                _systemd_last_run_check(
                    "inky-bird-frame-display.service",
                    check_id="display_last_run",
                    description="display refresh",
                )
            )
        checks.append(_controller_health_check(config, role))
    return DoctorReport(role, tuple(checks))


def _setup_script(role: InstallationRole, source_dir: Path | None) -> Path:
    system = platform.system()
    root = source_dir.expanduser().resolve() if source_dir is not None else _repository_root()
    if role is InstallationRole.CONTROLLER and system == "Darwin":
        return root / "deploy/install-controller.sh"
    if role is InstallationRole.CONTROLLER and system == "Linux":
        return root / "deploy/install-controller-systemd.sh"
    if role is InstallationRole.DISPLAY and system == "Linux":
        return root / "deploy/install-display-local.sh"
    raise InstallationError(f"Setup does not support {role.value} on {system}")


def setup(
    role: InstallationRole,
    config_path: Path,
    *,
    apply: bool,
    source_dir: Path | None = None,
    app_dir: Path | None = None,
    support_dir: Path | None = None,
    uv_bin: Path | None = None,
    python_version: str | None = None,
    venv: Path | None = None,
) -> dict[str, object]:
    config_path = config_path.expanduser().resolve()
    load_config(config_path)
    script = _setup_script(role, source_dir)
    if not script.is_file():
        raise InstallationError(f"Installer is missing: {script}")
    changes = [
        "copy the application into its managed runtime directory",
        "install or update the role's Python environment",
        "install and enable native service-manager definitions",
        "verify the installed services with the native service manager",
    ]
    result: dict[str, object] = {
        "role": role.value,
        "source": str(script.parent.parent),
        "config": str(config_path),
        "applied": False,
        "changes": changes,
    }
    if not apply:
        result["confirmation"] = "Repeat the same command with --yes to apply these changes."
        return result

    environment = os.environ.copy()
    for variable in (
        "INKY_BIRD_APP_DIR",
        "INKY_BIRD_SUPPORT_DIR",
        "UV_BIN",
        "INKY_BIRD_PYTHON_VERSION",
        "INKY_BIRD_DISPLAY_VENV",
        "INKY_BIRD_RUN_INITIAL_DISPLAY",
    ):
        environment.pop(variable, None)
    environment["INKY_BIRD_CONFIG_PATH"] = str(config_path)
    if app_dir is not None:
        environment["INKY_BIRD_APP_DIR"] = str(app_dir.expanduser().resolve())
    if support_dir is not None:
        environment["INKY_BIRD_SUPPORT_DIR"] = str(support_dir.expanduser().resolve())
    if uv_bin is not None:
        environment["UV_BIN"] = str(uv_bin.expanduser().resolve())
    if python_version is not None:
        environment["INKY_BIRD_PYTHON_VERSION"] = python_version
    if venv is not None:
        environment["INKY_BIRD_DISPLAY_VENV"] = str(venv.expanduser().resolve())
    try:
        completed = subprocess.run(
            [str(script)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1800,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallationError(f"Installer could not run: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown installer error"
        raise InstallationError(detail)
    result["applied"] = True
    result["summary"] = completed.stdout.strip()
    return result
