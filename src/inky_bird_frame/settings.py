"""One authoritative configuration file, with explicit browser ownership.

The browser can edit application choices, never process identity, networking,
certificate trust, executables or publication settings. Environment references
remain operator-owned. The original bytes form the revision and backup.
"""

from __future__ import annotations

import hashlib
import json
import math
import stat
import tomllib
from pathlib import Path
from typing import cast

from .catalog import catalog_state_lock
from .config import AppConfig, load_config, parse_config
from .errors import ConfigurationError, InkyBirdFrameError
from .http import write_bytes_atomic

MAX_CONFIG_BYTES = 1024 * 1024
EDITABLE: dict[str, frozenset[str]] = {
    "discovery": frozenset(
        {
            "sources",
            "window",
            "zip_code",
            "postal_code",
            "country_code",
            "latitude",
            "longitude",
            "radius_km",
            "species_limit",
            "geoapify_api_key",
            "ebird_api_key",
            "birdweather_token",
            "birdnet_go_url",
            "birdbuddy_include_manual_sightings",
        }
    ),
    "controller": frozenset({"generation_enabled"}),
    "notifications": frozenset({"enabled", "destinations"}),
}
PRIVATE_FIELDS = ("geoapify_api_key", "ebird_api_key", "birdweather_token")


class SettingsError(InkyBirdFrameError):
    """Safe configuration guidance without source text or secret values."""


class SettingsConflictError(SettingsError):
    """Another editor changed the authoritative configuration."""


def _read(path: Path) -> tuple[bytes, dict[str, object], AppConfig]:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise SettingsError("Configuration must be a private regular file with mode 0600")
        with path.open("rb") as handle:
            content = handle.read(MAX_CONFIG_BYTES + 1)
        if len(content) > MAX_CONFIG_BYTES:
            raise SettingsError("Configuration exceeds the supported size")
        raw = tomllib.loads(content.decode("utf-8"))
        return content, raw, parse_config(raw, path=path, load_secrets=False)
    except (OSError, ValueError, ConfigurationError) as exc:
        raise SettingsError(
            "Configuration cannot be read. Ask the installer operator to validate it."
        ) from exc


def _revision(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def current_config(initial: AppConfig) -> AppConfig:
    if initial.configuration_path is None:
        return initial
    try:
        config = load_config(initial.configuration_path, load_secrets=False)
    except (ConfigurationError, OSError) as exc:
        raise SettingsError(
            "Configuration is invalid. Ask the installer operator to validate it."
        ) from exc
    _installation_boundary(initial, config)
    return config


def _installation_boundary(initial: AppConfig, config: AppConfig) -> None:
    # Host-owned changes need a service restart. Never move an existing owner's
    # authority into a different state directory or alter its transport policy.
    from dataclasses import replace

    if (
        replace(
            config.controller,
            generation_enabled=initial.controller.generation_enabled,
            configuration_ownership=initial.controller.configuration_ownership,
        )
        != initial.controller
    ):
        raise SettingsError("Installation settings changed. Restart Inky before continuing.")


def confirmed_config(initial: AppConfig, revision: str, *, load_secrets: bool) -> AppConfig:
    """Bind validation and execution to the same confirmed configuration bytes."""
    path = initial.configuration_path
    if path is None:
        raise SettingsError("Start Inky with its authoritative configuration file")
    content, raw, config = _read(path)
    if _revision(content) != revision:
        raise SettingsConflictError("Settings changed. Reload and review this action again.")
    _installation_boundary(initial, config)
    try:
        return parse_config(raw, path=path, load_secrets=load_secrets)
    except ConfigurationError as exc:
        raise SettingsError(
            "This service cannot load the configured credentials. Check the installer environment."
        ) from exc


def settings_status(initial: AppConfig) -> dict[str, object]:
    path = initial.configuration_path
    if path is None:
        return {"ownership": "managed", "editable": False, "revision": None}
    content, raw, config = _read(path)
    _installation_boundary(initial, config)
    discovery = cast(dict[str, object], raw["discovery"])
    fields = {
        key: value
        for key, value in discovery.items()
        if key in EDITABLE["discovery"] and key not in PRIVATE_FIELDS
    }
    fields.pop("source", None)
    fields["sources"] = [provider.value for provider in config.discovery.sources]
    return {
        "ownership": config.controller.configuration_ownership,
        "editable": config.controller.configuration_ownership == "app"
        and initial.controller.configuration_ownership == "app",
        "restart_required": config.controller.configuration_ownership
        != initial.controller.configuration_ownership,
        "revision": _revision(content),
        "discovery": fields,
        "credentials": {
            key: {
                "configured": key in discovery or f"{key}_env" in discovery,
                "external": f"{key}_env" in discovery,
            }
            for key in PRIVATE_FIELDS
        },
        "generation_enabled": config.controller.generation_enabled,
        "notifications": {
            "enabled": config.notifications.enabled,
            "destinations": [
                {
                    "name": item.name,
                    "external": item.url_env is not None,
                    "events": [event.value for event in item.events],
                }
                for item in config.notifications.destinations
            ],
        },
        "backup_available": path.with_suffix(path.suffix + ".previous").is_file(),
    }


def _value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int or isinstance(value, float) and math.isfinite(value):
        return str(value)
    if isinstance(value, list) and all(not isinstance(item, dict) for item in value):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    raise SettingsError("Configuration contains an unsupported value; keep managed mode")


def _toml(raw: dict[str, object]) -> bytes:
    lines: list[str] = []

    def table(data: dict[str, object], keys: tuple[str, ...], *, array: bool = False) -> None:
        if keys:
            header = ".".join(json.dumps(key) for key in keys)
            lines.append(f"[[{header}]]" if array else f"[{header}]")
        nested: list[tuple[str, object]] = []
        for key, value in data.items():
            if isinstance(value, dict) or (
                isinstance(value, list) and value and isinstance(value[0], dict)
            ):
                nested.append((key, value))
            else:
                lines.append(f"{json.dumps(key)} = {_value(value)}")
        lines.append("")
        for key, value in nested:
            if isinstance(value, dict):
                table(value, (*keys, key))
            elif isinstance(value, list):
                for item in value:
                    if not isinstance(item, dict):
                        raise SettingsError("Configuration contains a mixed table array")
                    table(item, (*keys, key), array=True)

    table(raw, ())
    content = ("\n".join(lines).rstrip() + "\n").encode("utf-8")
    if len(content) > MAX_CONFIG_BYTES:
        raise SettingsError("Configuration exceeds the supported size")
    return content


def save_settings(
    initial: AppConfig,
    *,
    revision: str,
    changes: object,
    adopt: bool = False,
    confirm_generation: bool = False,
) -> dict[str, object]:
    path = initial.configuration_path
    if path is None:
        raise SettingsError("This installation has no editable configuration file")
    with catalog_state_lock(initial.controller.state_dir):
        content, raw, config = _read(path)
        _installation_boundary(initial, config)
        if _revision(content) != revision:
            raise SettingsConflictError("Settings changed in another tab. Reload before saving.")
        if adopt:
            if changes != {} or config.controller.configuration_ownership != "managed":
                raise SettingsError("Preview adoption before changing application settings")
            controller = cast(dict[str, object], raw["controller"])
            controller["configuration_ownership"] = "app"
            # An upgrade retains its actual policy, including the old default.
            controller["generation_enabled"] = config.controller.generation_enabled
        elif config.controller.configuration_ownership != "app":
            raise SettingsError("These settings are managed by the installer, not the browser")
        elif initial.controller.configuration_ownership != "app":
            raise SettingsError("Finish adoption with the installer and restart before editing")
        if not isinstance(changes, dict) or set(changes) - EDITABLE.keys():
            raise SettingsError("Only source, generation and notification choices are editable")
        for section, patch in changes.items():
            if not isinstance(patch, dict) or set(patch) - EDITABLE[section]:
                raise SettingsError("This request includes installer-owned settings")
            target = raw.setdefault(section, {})
            if not isinstance(target, dict):
                raise SettingsError("Configuration section is invalid")
            for key, value in patch.items():
                if (
                    section == "controller"
                    and key == "generation_enabled"
                    and value is True
                    and not config.controller.generation_enabled
                    and not confirm_generation
                ):
                    raise SettingsError("Confirm AI use before enabling generation")
                if key in PRIVATE_FIELDS and f"{key}_env" in target:
                    raise SettingsError("This credential belongs to the installer environment")
                if (
                    section == "notifications"
                    and key == "destinations"
                    and any(item.url_env is not None for item in config.notifications.destinations)
                ):
                    raise SettingsError("Notification destinations belong to the environment")
                if section == "notifications" and key == "destinations":
                    if not isinstance(value, list):
                        raise SettingsError("Use a list of notification destinations")
                    destinations = []
                    previous = target.get("destinations", [])
                    for item in value:
                        if not isinstance(item, dict) or set(item) - {"name", "url", "events"}:
                            raise SettingsError(
                                "Notification environment references are installer-owned"
                            )
                        saved = (
                            next(
                                (
                                    old
                                    for old in previous
                                    if isinstance(old, dict) and old.get("name") == item.get("name")
                                ),
                                {},
                            )
                            if isinstance(previous, list)
                            else {}
                        )
                        if "url" not in item and "url" in saved:
                            item = {"url": saved["url"], **item}
                        destinations.append(item)
                    value = destinations
                if value is None:
                    target.pop(key, None)
                else:
                    target[key] = value
            if section == "discovery" and "sources" in patch:
                target.pop("source", None)
        try:
            parse_config(raw, path=path, load_secrets=False)
            encoded = _toml(raw)
            parse_config(tomllib.loads(encoded.decode()), path=path, load_secrets=False)
        except ConfigurationError as exc:
            # The typed parser reports field constraints, not TOML source text.
            # Raw decoding errors above deliberately use generic guidance.
            raise SettingsError(f"Settings are not valid: {exc}") from exc
        except ValueError as exc:
            raise SettingsError(
                "Settings are not valid. Check the selected sources and fields."
            ) from exc
        write_bytes_atomic(path.with_suffix(path.suffix + ".previous"), content, mode=0o600)
        write_bytes_atomic(path, encoded, mode=0o600)
    return {"saved": True, "revision": _revision(encoded), "restart_required": adopt}


def initialize_config(path: Path, *, container: bool = False) -> None:
    """Create a new art-first installation; never replace an existing config.

    Discovery radius/limit/window and generation limits match config.example.toml.
    They take effect only after their corresponding feature is explicitly enabled.
    """
    base = Path("/data") if container else path.absolute().parent
    raw: dict[str, object] = {
        "discovery": {"sources": [], "radius_km": 8, "species_limit": 50, "window": "last-30-days"},
        "controller": {
            "configuration_ownership": "app",
            "generation_enabled": False,
            "workspace_dir": str(base / "workspace"),
            "catalog_dir": str(base / "catalog"),
            "state_dir": str(base / "var/controller"),
            "codex_path": "codex",
            "bind_host": "0.0.0.0",
            "port": 8793,
            "references_per_species": 4,
            "generations_per_cycle": 1,
            "max_generation_attempts": 3,
        },
        "display_node": {
            "controller_url": "http://127.0.0.1:8793",
            "state_dir": str(base / "var/display"),
        },
    }
    # Lock the destination's directory, not /data when writing a host-side Docker
    # seed. No container directory is created on the operator's host.
    with catalog_state_lock(path.absolute().parent):
        if path.exists() or path.is_symlink():
            raise SettingsError("Configuration already exists; it was not replaced")
        encoded = _toml(raw)
        parse_config(raw, path=path, load_secrets=False)
        write_bytes_atomic(path, encoded, mode=0o600)
