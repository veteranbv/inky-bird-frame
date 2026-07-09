"""Typed TOML configuration for controller and display-node roles."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Final

from .birds import ObservationWindow, parse_observation_window
from .errors import ConfigurationError

PORTRAIT_SIZE: Final = (1200, 1600)
HARDWARE_SIZE: Final = (1600, 1200)


@dataclass(frozen=True)
class DisplayConfig:
    portrait_size: tuple[int, int] = PORTRAIT_SIZE
    hardware_size: tuple[int, int] = HARDWARE_SIZE
    rotation_degrees: int = 90


@dataclass(frozen=True)
class DiscoveryConfig:
    zip_code: str
    radius_km: int
    species_limit: int
    observation_window: ObservationWindow


@dataclass(frozen=True)
class ControllerConfig:
    workspace_dir: Path
    catalog_dir: Path
    state_dir: Path
    codex_path: Path
    bind_host: str
    port: int
    references_per_species: int
    generations_per_cycle: int
    max_generation_attempts: int


@dataclass(frozen=True)
class DisplayNodeConfig:
    controller_url: str
    state_dir: Path


@dataclass(frozen=True)
class AppConfig:
    discovery: DiscoveryConfig
    controller: ControllerConfig
    display_node: DisplayNodeConfig
    display: DisplayConfig = DisplayConfig()


def _section(data: object, name: str) -> dict[str, object]:
    if not isinstance(data, dict):
        raise ConfigurationError("Configuration root must be a TOML table")
    value = data.get(name)
    if not isinstance(value, dict):
        raise ConfigurationError(f"Missing [{name}] configuration section")
    return value


def _string(section: dict[str, object], name: str) -> str:
    value = section.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{name} must be a non-empty string")
    return value.strip()


def _integer(section: dict[str, object], name: str, *, minimum: int = 1) -> int:
    value = section.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ConfigurationError(f"{name} must be an integer greater than or equal to {minimum}")
    return value


def _optional_integer(
    section: dict[str, object], name: str, *, default: int, minimum: int = 1
) -> int:
    if name not in section:
        return default
    return _integer(section, name, minimum=minimum)


def _path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base_dir / path).resolve()


def _executable_path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute() or value.startswith("./") or len(path.parts) > 1:
        return _path(value, base_dir)
    resolved = which(value)
    return Path(resolved) if resolved is not None else path


def load_config(path: Path) -> AppConfig:
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Configuration file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"Invalid TOML in {path}: {exc}") from exc

    discovery = _section(raw, "discovery")
    controller = _section(raw, "controller")
    display_node = _section(raw, "display_node")
    base_dir = path.parent.resolve()

    zip_code = _string(discovery, "zip_code")
    if not zip_code.isdigit() or len(zip_code) != 5:
        raise ConfigurationError("zip_code must be a five digit US ZIP code")

    window_value = _string(discovery, "window")
    try:
        window = parse_observation_window(window_value)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc

    controller_url = _string(display_node, "controller_url").rstrip("/")
    if not controller_url.startswith(("http://", "https://")):
        raise ConfigurationError("controller_url must start with http:// or https://")

    return AppConfig(
        discovery=DiscoveryConfig(
            zip_code=zip_code,
            radius_km=_integer(discovery, "radius_km"),
            species_limit=_integer(discovery, "species_limit"),
            observation_window=window,
        ),
        controller=ControllerConfig(
            workspace_dir=_path(_string(controller, "workspace_dir"), base_dir),
            catalog_dir=_path(_string(controller, "catalog_dir"), base_dir),
            state_dir=_path(_string(controller, "state_dir"), base_dir),
            codex_path=_executable_path(_string(controller, "codex_path"), base_dir),
            bind_host=_string(controller, "bind_host"),
            port=_integer(controller, "port"),
            references_per_species=_integer(controller, "references_per_species"),
            generations_per_cycle=_integer(controller, "generations_per_cycle"),
            max_generation_attempts=_optional_integer(
                controller, "max_generation_attempts", default=3
            ),
        ),
        display_node=DisplayNodeConfig(
            controller_url=controller_url,
            state_dir=_path(_string(display_node, "state_dir"), base_dir),
        ),
    )
