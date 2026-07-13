"""Typed TOML configuration for controller and display-node roles."""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from os import environ
from pathlib import Path
from shutil import which
from urllib.parse import urlsplit

from .birds import ObservationWindow, parse_observation_window
from .errors import ConfigurationError


class RotationMode(StrEnum):
    SEQUENTIAL = "sequential"
    SHUFFLE = "shuffle"
    SHUFFLE_BAG = "shuffle_bag"
    WEIGHTED = "weighted"


class DiscoveryProvider(StrEnum):
    INATURALIST = "inaturalist"
    EBIRD = "ebird"
    BIRDWEATHER = "birdweather"


LEGACY_DISCOVERY_SOURCES: dict[str, tuple[DiscoveryProvider, ...]] = {
    "inaturalist": (DiscoveryProvider.INATURALIST,),
    "ebird": (DiscoveryProvider.EBIRD,),
    "combined": (DiscoveryProvider.INATURALIST, DiscoveryProvider.EBIRD),
    "birdweather": (DiscoveryProvider.BIRDWEATHER,),
    "all": (
        DiscoveryProvider.INATURALIST,
        DiscoveryProvider.EBIRD,
        DiscoveryProvider.BIRDWEATHER,
    ),
}


def parse_discovery_provider(value: str) -> DiscoveryProvider:
    try:
        return DiscoveryProvider(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in DiscoveryProvider)
        raise ValueError(f"discovery provider must be one of: {allowed}") from exc


def discovery_source_label(providers: tuple[DiscoveryProvider, ...]) -> str:
    for label, legacy_providers in LEGACY_DISCOVERY_SOURCES.items():
        if providers == legacy_providers:
            return label
    return ",".join(provider.value for provider in providers)


class NotificationEvent(StrEnum):
    DISCOVERY = "discovery"
    GENERATION_APPROVED = "generation_approved"
    TERMINAL_ERROR = "terminal_error"
    DEGRADED = "degraded"
    RECOVERED = "recovered"
    PUBLICATION_ERROR = "publication_error"
    PUBLICATION_RECOVERED = "publication_recovered"


ALL_NOTIFICATION_EVENTS = tuple(NotificationEvent)


def parse_rotation_mode(value: str) -> RotationMode:
    try:
        return RotationMode(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in RotationMode)
        raise ValueError(f"rotation_mode must be one of: {allowed}") from exc


@dataclass(frozen=True)
class DiscoveryConfig:
    zip_code: str
    radius_km: int
    species_limit: int
    observation_window: ObservationWindow
    sources: tuple[DiscoveryProvider, ...] = (DiscoveryProvider.INATURALIST,)
    ebird_api_key: str | None = field(default=None, repr=False)
    ebird_api_key_env: str | None = field(default=None, repr=False)
    birdweather_token: str | None = field(default=None, repr=False)
    birdweather_token_env: str | None = field(default=None, repr=False)


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
    max_species_attempts_per_cycle: int = 5
    retry_initial_minutes: int = 30
    retry_max_minutes: int = 1440
    insufficient_references_retry_minutes: int = 10080


@dataclass(frozen=True)
class ResearchConfig:
    enabled: bool = True
    max_searches_per_day: int = 5
    max_searches_per_species: int = 2
    allowed_domains: tuple[str, ...] = (
        "allaboutbirds.org",
        "audubon.org",
        "birdsoftheworld.org",
        "ebird.org",
        "iucnredlist.org",
        "nationalzoo.si.edu",
        "animaldiversity.org",
        "wikipedia.org",
    )


@dataclass(frozen=True)
class NotificationDestination:
    name: str
    url: str = field(repr=False)
    events: tuple[NotificationEvent, ...]
    url_env: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class NotificationsConfig:
    enabled: bool = False
    destinations: tuple[NotificationDestination, ...] = ()
    degradation_failure_threshold: int = 3
    degradation_window_minutes: int = 30
    cooldown_minutes: int = 360
    delivery_retry_minutes: int = 5
    max_delivery_attempts: int = 20


@dataclass(frozen=True)
class DisplayNodeConfig:
    controller_url: str
    state_dir: Path
    rotation_mode: RotationMode = RotationMode.SEQUENTIAL


@dataclass(frozen=True)
class PublicCatalogConfig:
    enabled: bool = False
    checkout_dir: Path | None = None
    repository: str | None = None
    gh_path: Path = Path("gh")
    remote: str = "origin"
    base_branch: str = "main"
    commit_name: str = "Inky Bird Frame Catalog"
    commit_email: str = "inky-bird-frame@users.noreply.github.com"


@dataclass(frozen=True)
class ScheduleConfig:
    refresh_minutes: int = 15
    generation_minutes: int = 360
    rotation_minutes: int = 30
    rotation_jitter_seconds: int = 0
    display_startup_delay_seconds: int = 120
    catalog_publish_minutes: int = 5


@dataclass(frozen=True)
class AppConfig:
    discovery: DiscoveryConfig
    controller: ControllerConfig
    display_node: DisplayNodeConfig
    public_catalog: PublicCatalogConfig = PublicCatalogConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    research: ResearchConfig = ResearchConfig()
    notifications: NotificationsConfig = NotificationsConfig()


def _section(data: object, name: str) -> dict[str, object]:
    if not isinstance(data, dict):
        raise ConfigurationError("Configuration root must be a TOML table")
    value = data.get(name)
    if not isinstance(value, dict):
        raise ConfigurationError(f"Missing [{name}] configuration section")
    return value


def _optional_section(data: object, name: str) -> dict[str, object]:
    if not isinstance(data, dict):
        raise ConfigurationError("Configuration root must be a TOML table")
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigurationError(f"[{name}] must be a TOML table")
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


def _optional_string(section: dict[str, object], name: str, *, default: str) -> str:
    if name not in section:
        return default
    return _string(section, name)


def _optional_boolean(section: dict[str, object], name: str, *, default: bool) -> bool:
    if name not in section:
        return default
    value = section[name]
    if not isinstance(value, bool):
        raise ConfigurationError(f"{name} must be a boolean")
    return value


def _string_tuple(
    section: dict[str, object], name: str, *, default: Iterable[str] = ()
) -> tuple[str, ...]:
    if name not in section:
        return tuple(default)
    value = section[name]
    if not isinstance(value, list):
        raise ConfigurationError(f"{name} must be an array of strings")
    parsed: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigurationError(f"{name} must contain only non-empty strings")
        parsed.append(item.strip())
    if len(parsed) != len(set(parsed)):
        raise ConfigurationError(f"{name} must not contain duplicates")
    return tuple(parsed)


def _notification_destinations(
    raw: object, *, load_secrets: bool
) -> tuple[NotificationDestination, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigurationError("notifications.destinations must be an array of tables")
    destinations: list[NotificationDestination] = []
    names: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigurationError("Each notifications destination must be a TOML table")
        name = _string(item, "name")
        if name in names:
            raise ConfigurationError(f"Duplicate notification destination name: {name}")
        names.add(name)
        has_url = "url" in item
        has_url_env = "url_env" in item
        if has_url == has_url_env:
            raise ConfigurationError(
                f"Notification destination {name} must set exactly one of url or url_env"
            )
        if has_url:
            configured_url = _string(item, "url")
            scheme = urlsplit(configured_url).scheme
            url = configured_url if load_secrets else f"{scheme}://redacted"
            variable = None
        else:
            variable = _string(item, "url_env")
            if load_secrets:
                url = environ.get(variable, "").strip()
                if not url:
                    raise ConfigurationError(
                        f"Notification destination {name} requires environment variable {variable}"
                    )
            else:
                url = f"env://{variable}"
        if not urlsplit(url).scheme:
            raise ConfigurationError(f"Notification destination {name} URL has no scheme")
        event_values = _string_tuple(
            item,
            "events",
            default=(event.value for event in ALL_NOTIFICATION_EVENTS),
        )
        try:
            events = tuple(NotificationEvent(value) for value in event_values)
        except ValueError as exc:
            allowed = ", ".join(event.value for event in ALL_NOTIFICATION_EVENTS)
            raise ConfigurationError(
                f"Notification destination {name} has an unsupported event; use: {allowed}"
            ) from exc
        if not events:
            raise ConfigurationError(
                f"Notification destination {name} must subscribe to at least one event"
            )
        destinations.append(
            NotificationDestination(name=name, url=url, events=events, url_env=variable)
        )
    return tuple(destinations)


def _path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base_dir / path).resolve()


def _executable_path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute() or value.startswith("./") or len(path.parts) > 1:
        return _path(value, base_dir)
    resolved = which(value)
    return Path(resolved) if resolved is not None else path


def _private_value(
    section: dict[str, object],
    name: str,
    *,
    required: bool,
    provider_name: str,
    load_secret: bool,
) -> tuple[str | None, str | None]:
    direct_value = section.get(name)
    environment_name = section.get(f"{name}_env")
    if direct_value is not None and (not isinstance(direct_value, str) or not direct_value.strip()):
        raise ConfigurationError(f"{name} must be a non-empty string")
    if environment_name is not None and (
        not isinstance(environment_name, str) or not environment_name.strip()
    ):
        raise ConfigurationError(f"{name}_env must be a non-empty string")
    if direct_value is not None and environment_name is not None:
        raise ConfigurationError(f"Choose {name} or {name}_env, not both")

    resolved = direct_value.strip() if load_secret and isinstance(direct_value, str) else None
    variable = environment_name.strip() if isinstance(environment_name, str) else None
    if variable is not None and load_secret:
        environment_value = environ.get(variable)
        resolved = environment_value.strip() if environment_value else None
        if required and resolved is None:
            raise ConfigurationError(
                f"{provider_name} discovery requires environment variable {variable}"
            )
    if required and direct_value is None and variable is None:
        raise ConfigurationError(f"{provider_name} discovery requires {name} or {name}_env")
    return resolved, variable


def load_config(path: Path, *, load_secrets: bool = True) -> AppConfig:
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Configuration file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"Invalid TOML in {path}: {exc}") from exc

    discovery = _section(raw, "discovery")
    controller = _section(raw, "controller")
    display_node = _section(raw, "display_node")
    public_catalog = _optional_section(raw, "public_catalog")
    schedule = _optional_section(raw, "schedule")
    research = _optional_section(raw, "research")
    notifications = _optional_section(raw, "notifications")
    base_dir = path.parent.resolve()

    zip_code = _string(discovery, "zip_code")
    if not zip_code.isdigit() or len(zip_code) != 5:
        raise ConfigurationError("zip_code must be a five digit US ZIP code")

    window_value = _string(discovery, "window")
    try:
        window = parse_observation_window(window_value)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc

    if "source" in discovery and "sources" in discovery:
        raise ConfigurationError("Choose discovery.source or discovery.sources, not both")
    if "sources" in discovery:
        source_values = _string_tuple(discovery, "sources")
        if not source_values:
            raise ConfigurationError("discovery.sources must contain at least one provider")
        try:
            discovery_sources = tuple(parse_discovery_provider(value) for value in source_values)
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc
    else:
        source_value = _optional_string(discovery, "source", default="inaturalist")
        try:
            discovery_sources = LEGACY_DISCOVERY_SOURCES[source_value]
        except KeyError as exc:
            allowed = ", ".join(LEGACY_DISCOVERY_SOURCES)
            raise ConfigurationError(f"discovery source must be one of: {allowed}") from exc
    radius_km = _integer(discovery, "radius_km")
    species_limit = _integer(discovery, "species_limit")
    if radius_km <= 0:
        raise ConfigurationError("radius_km must be greater than zero")
    if species_limit <= 0:
        raise ConfigurationError("species_limit must be greater than zero")

    ebird_api_key, ebird_api_key_env = _private_value(
        discovery,
        "ebird_api_key",
        required=DiscoveryProvider.EBIRD in discovery_sources,
        provider_name="eBird",
        load_secret=load_secrets,
    )
    birdweather_token, birdweather_token_env = _private_value(
        discovery,
        "birdweather_token",
        required=DiscoveryProvider.BIRDWEATHER in discovery_sources,
        provider_name="BirdWeather",
        load_secret=load_secrets,
    )
    if DiscoveryProvider.EBIRD in discovery_sources:
        if window in {ObservationWindow.LAST_YEAR, ObservationWindow.ALL_TIME}:
            raise ConfigurationError("eBird discovery supports observation windows up to 30 days")
        if radius_km > 50:
            raise ConfigurationError("eBird discovery radius_km must not exceed 50")
        if species_limit > 10_000:
            raise ConfigurationError("eBird species_limit must not exceed 10000")
    if DiscoveryProvider.BIRDWEATHER in discovery_sources and species_limit > 100:
        raise ConfigurationError("BirdWeather species_limit must not exceed 100")

    controller_url = _string(display_node, "controller_url").rstrip("/")
    if not controller_url.startswith(("http://", "https://")):
        raise ConfigurationError("controller_url must start with http:// or https://")

    rotation_mode_value = _optional_string(
        display_node, "rotation_mode", default=RotationMode.SEQUENTIAL.value
    )
    try:
        rotation_mode = parse_rotation_mode(rotation_mode_value)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc

    public_catalog_enabled = _optional_boolean(public_catalog, "enabled", default=False)
    checkout_dir = (
        _path(_string(public_catalog, "checkout_dir"), base_dir)
        if "checkout_dir" in public_catalog
        else None
    )
    if public_catalog_enabled and checkout_dir is None:
        raise ConfigurationError("checkout_dir is required when catalog publishing is enabled")
    repository = _string(public_catalog, "repository") if "repository" in public_catalog else None
    if public_catalog_enabled and repository is None:
        raise ConfigurationError("repository is required when catalog publishing is enabled")
    if repository is not None:
        parts = repository.split("/")
        if len(parts) != 2 or any(not part or part in {".", ".."} for part in parts):
            raise ConfigurationError("repository must use the owner/name format")

    allowed_domains = tuple(
        domain.casefold()
        for domain in _string_tuple(
            research,
            "allowed_domains",
            default=ResearchConfig().allowed_domains,
        )
    )
    if any(
        not domain or "://" in domain or "/" in domain or domain.startswith(".")
        for domain in allowed_domains
    ):
        raise ConfigurationError("research.allowed_domains must contain bare DNS domains")
    if len(set(allowed_domains)) < 2:
        raise ConfigurationError(
            "research.allowed_domains must contain at least two distinct domains"
        )
    notifications_enabled = _optional_boolean(notifications, "enabled", default=False)
    destinations = _notification_destinations(
        notifications.get("destinations"),
        load_secrets=notifications_enabled and load_secrets,
    )
    if notifications_enabled and not destinations:
        raise ConfigurationError(
            "At least one notifications destination is required when notifications are enabled"
        )
    retry_initial_minutes = _optional_integer(controller, "retry_initial_minutes", default=30)
    retry_max_minutes = _optional_integer(controller, "retry_max_minutes", default=1440)
    if retry_max_minutes < retry_initial_minutes:
        raise ConfigurationError(
            "retry_max_minutes must be greater than or equal to retry_initial_minutes"
        )

    return AppConfig(
        discovery=DiscoveryConfig(
            sources=discovery_sources,
            zip_code=zip_code,
            radius_km=radius_km,
            species_limit=species_limit,
            observation_window=window,
            ebird_api_key=ebird_api_key,
            ebird_api_key_env=ebird_api_key_env,
            birdweather_token=birdweather_token,
            birdweather_token_env=birdweather_token_env,
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
            max_species_attempts_per_cycle=_optional_integer(
                controller, "max_species_attempts_per_cycle", default=5
            ),
            retry_initial_minutes=retry_initial_minutes,
            retry_max_minutes=retry_max_minutes,
            insufficient_references_retry_minutes=_optional_integer(
                controller, "insufficient_references_retry_minutes", default=10080
            ),
        ),
        display_node=DisplayNodeConfig(
            controller_url=controller_url,
            state_dir=_path(_string(display_node, "state_dir"), base_dir),
            rotation_mode=rotation_mode,
        ),
        public_catalog=PublicCatalogConfig(
            enabled=public_catalog_enabled,
            checkout_dir=checkout_dir,
            repository=repository,
            gh_path=_executable_path(
                _optional_string(public_catalog, "gh_path", default="gh"), base_dir
            ),
            remote=_optional_string(public_catalog, "remote", default="origin"),
            base_branch=_optional_string(public_catalog, "base_branch", default="main"),
            commit_name=_optional_string(
                public_catalog, "commit_name", default="Inky Bird Frame Catalog"
            ),
            commit_email=_optional_string(
                public_catalog,
                "commit_email",
                default="inky-bird-frame@users.noreply.github.com",
            ),
        ),
        schedule=ScheduleConfig(
            refresh_minutes=_optional_integer(schedule, "refresh_minutes", default=15),
            generation_minutes=_optional_integer(schedule, "generation_minutes", default=360),
            rotation_minutes=_optional_integer(schedule, "rotation_minutes", default=30),
            rotation_jitter_seconds=_optional_integer(
                schedule, "rotation_jitter_seconds", default=0, minimum=0
            ),
            display_startup_delay_seconds=_optional_integer(
                schedule, "display_startup_delay_seconds", default=120, minimum=0
            ),
            catalog_publish_minutes=_optional_integer(
                schedule, "catalog_publish_minutes", default=5
            ),
        ),
        research=ResearchConfig(
            enabled=_optional_boolean(research, "enabled", default=True),
            max_searches_per_day=_optional_integer(research, "max_searches_per_day", default=5),
            max_searches_per_species=_optional_integer(
                research, "max_searches_per_species", default=2
            ),
            allowed_domains=allowed_domains,
        ),
        notifications=NotificationsConfig(
            enabled=notifications_enabled,
            destinations=destinations,
            degradation_failure_threshold=_optional_integer(
                notifications, "degradation_failure_threshold", default=3
            ),
            degradation_window_minutes=_optional_integer(
                notifications, "degradation_window_minutes", default=30
            ),
            cooldown_minutes=_optional_integer(notifications, "cooldown_minutes", default=360),
            delivery_retry_minutes=_optional_integer(
                notifications, "delivery_retry_minutes", default=5
            ),
            max_delivery_attempts=_optional_integer(
                notifications, "max_delivery_attempts", default=20
            ),
        ),
    )
