"""Durable, non-blocking notification delivery through embedded Apprise."""

from __future__ import annotations

import fcntl
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import uuid4

from .catalog import write_json_atomic
from .config import AppConfig, NotificationDestination, NotificationEvent
from .errors import CatalogError, ConfigurationError, MissingDependencyError

if TYPE_CHECKING:
    import apprise


@dataclass(frozen=True)
class NotificationItem:
    item_id: str
    event: NotificationEvent
    title: str
    body: str
    created_at: datetime
    attempts: int
    next_attempt_at: datetime
    delivered_to: tuple[str, ...]
    last_error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.item_id,
            "event": self.event.value,
            "title": self.title,
            "body": self.body,
            "created_at": self.created_at.isoformat(),
            "attempts": self.attempts,
            "next_attempt_at": self.next_attempt_at.isoformat(),
            "delivered_to": list(self.delivered_to),
            "last_error": self.last_error,
        }


@dataclass(frozen=True)
class NotificationState:
    pending: tuple[NotificationItem, ...] = ()
    dead_letters: tuple[NotificationItem, ...] = ()
    delivered_ids: tuple[str, ...] = ()


def notification_state_path(config: AppConfig) -> Path:
    return config.controller.state_dir / "notifications.json"


def _new_notifier() -> apprise.Apprise:
    try:
        import apprise
    except ModuleNotFoundError as exc:
        raise MissingDependencyError(
            "Notifications require the controller extra: install inky-bird-frame[controller]"
        ) from exc
    return apprise.Apprise()


@contextmanager
def _notification_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "notifications.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def validate_notification_destinations(config: AppConfig) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for destination in config.notifications.destinations:
        if config.notifications.enabled:
            notifier = _new_notifier()
            if not notifier.add(destination.url) or len(notifier) != 1:
                raise ConfigurationError(
                    f"Notification destination {destination.name} is not a valid Apprise URL"
                )
        results.append(
            {
                "name": destination.name,
                "scheme": urlsplit(destination.url).scheme,
                "events": [event.value for event in destination.events],
            }
        )
    return results


def enqueue_notification(
    config: AppConfig,
    event: NotificationEvent,
    *,
    dedupe_key: str,
    title: str,
    body: str,
    now: datetime | None = None,
) -> bool:
    if not config.notifications.enabled:
        return False
    current = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    item_id = hashlib.sha256(f"{event.value}:{dedupe_key}".encode()).hexdigest()
    with _notification_lock(config.controller.state_dir):
        state = _read_state(notification_state_path(config))
        known_ids = {
            *(item.item_id for item in state.pending),
            *(item.item_id for item in state.dead_letters),
            *state.delivered_ids,
        }
        if item_id in known_ids:
            return False
        item = NotificationItem(
            item_id=item_id,
            event=event,
            title=title,
            body=body,
            created_at=current,
            attempts=0,
            next_attempt_at=current,
            delivered_to=(),
        )
        _write_state(notification_state_path(config), state, pending=(*state.pending, item))
    return True


def dispatch_notifications(config: AppConfig, *, now: datetime | None = None) -> dict[str, object]:
    current = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    config.controller.state_dir.mkdir(parents=True, exist_ok=True)
    with (config.controller.state_dir / "notifications-dispatch.lock").open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            with _notification_lock(config.controller.state_dir):
                state = _read_state(notification_state_path(config))
            return {
                **_delivery_result(state, attempted=0, delivered=0, failed=0),
                "dispatcher_busy": True,
            }
        try:
            with _notification_lock(config.controller.state_dir):
                state = _read_state(notification_state_path(config))
            next_state, counts = _deliver_notification_state(config, state, current)
            with _notification_lock(config.controller.state_dir):
                latest = _read_state(notification_state_path(config))
                merged = _merge_delivery_state(state, next_state, latest)
                _write_state(notification_state_path(config), merged)
            return {
                **_delivery_result(merged, **counts),
                "dispatcher_busy": False,
            }
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _deliver_notification_state(
    config: AppConfig, state: NotificationState, current: datetime
) -> tuple[NotificationState, dict[str, int]]:
    if not config.notifications.enabled:
        return state, {"attempted": 0, "delivered": 0, "failed": 0}

    pending: list[NotificationItem] = []
    dead_letters = list(state.dead_letters)
    delivered_ids = list(state.delivered_ids)
    attempted = 0
    delivered = 0
    failed = 0
    destinations = {item.name: item for item in config.notifications.destinations}

    for item in state.pending:
        if item.next_attempt_at > current:
            pending.append(item)
            continue
        targets = [
            destination
            for destination in destinations.values()
            if item.event in destination.events and destination.name not in item.delivered_to
        ]
        delivered_to = list(item.delivered_to)
        errors: list[str] = []
        for destination in targets:
            attempted += 1
            try:
                _deliver(destination, item)
            except Exception as exc:  # Apprise plugins expose provider-specific exceptions.
                failed += 1
                errors.append(f"{destination.name}: {type(exc).__name__}")
            else:
                delivered += 1
                delivered_to.append(destination.name)

        configured_targets = {
            destination.name
            for destination in destinations.values()
            if item.event in destination.events
        }
        if configured_targets.issubset(delivered_to):
            delivered_ids.append(item.item_id)
            continue

        attempts = item.attempts + 1
        updated = NotificationItem(
            item_id=item.item_id,
            event=item.event,
            title=item.title,
            body=item.body,
            created_at=item.created_at,
            attempts=attempts,
            next_attempt_at=current
            + timedelta(minutes=config.notifications.delivery_retry_minutes),
            delivered_to=tuple(delivered_to),
            last_error="; ".join(errors) if errors else "No configured destination accepts event",
        )
        if attempts >= config.notifications.max_delivery_attempts:
            dead_letters.append(updated)
        else:
            pending.append(updated)

    next_state = NotificationState(
        pending=tuple(pending),
        dead_letters=tuple(dead_letters),
        delivered_ids=tuple(delivered_ids[-1000:]),
    )
    return next_state, {"attempted": attempted, "delivered": delivered, "failed": failed}


def _merge_delivery_state(
    snapshot: NotificationState,
    delivered: NotificationState,
    latest: NotificationState,
) -> NotificationState:
    snapshot_pending_ids = {item.item_id for item in snapshot.pending}
    snapshot_dead_ids = {item.item_id for item in snapshot.dead_letters}
    pending = (
        *delivered.pending,
        *(item for item in latest.pending if item.item_id not in snapshot_pending_ids),
    )
    new_dead_letters = (
        item for item in delivered.dead_letters if item.item_id not in snapshot_dead_ids
    )
    dead_letters = (*latest.dead_letters, *new_dead_letters)
    delivered_ids = tuple(dict.fromkeys((*latest.delivered_ids, *delivered.delivered_ids)).keys())[
        -1000:
    ]
    return NotificationState(
        pending=pending,
        dead_letters=dead_letters,
        delivered_ids=delivered_ids,
    )


def notification_status(config: AppConfig) -> dict[str, object]:
    with _notification_lock(config.controller.state_dir):
        state = _read_state(notification_state_path(config))
    return {
        "enabled": config.notifications.enabled,
        "destinations": [
            {
                "name": destination.name,
                "scheme": urlsplit(destination.url).scheme,
                "events": [event.value for event in destination.events],
            }
            for destination in config.notifications.destinations
        ],
        "pending": len(state.pending),
        "dead_letters": len(state.dead_letters),
        "oldest_pending_at": state.pending[0].created_at.isoformat() if state.pending else None,
    }


def send_notification_test(config: AppConfig) -> dict[str, object]:
    if not config.notifications.enabled:
        raise ConfigurationError("Notifications are disabled")
    if not config.notifications.destinations:
        raise ConfigurationError("No notification destinations are configured")
    validate_notification_destinations(config)
    now = datetime.now(UTC).replace(microsecond=0)
    item = NotificationItem(
        item_id="manual-test",
        event=NotificationEvent.TERMINAL_ERROR,
        title="Inky Bird Frame notification test",
        body="Notifications are configured and delivery was requested successfully.",
        created_at=now,
        attempts=0,
        next_attempt_at=now,
        delivered_to=(),
    )
    delivered: list[str] = []
    failures: list[dict[str, str]] = []
    for destination in config.notifications.destinations:
        try:
            _deliver(destination, item)
        except Exception as exc:  # Apprise plugins expose provider-specific exceptions.
            failures.append({"name": destination.name, "error_type": type(exc).__name__})
        else:
            delivered.append(destination.name)
    return {"delivered": delivered, "failures": failures}


def safe_notify(
    config: AppConfig,
    event: NotificationEvent,
    *,
    dedupe_key: str,
    title: str,
    body: str,
) -> dict[str, object]:
    try:
        queued = enqueue_notification(
            config,
            event,
            dedupe_key=dedupe_key,
            title=title,
            body=body,
        )
        result = dispatch_notifications(config)
        return {"queued": queued, **result}
    except Exception as exc:
        return {
            "queued": False,
            "attempted": 0,
            "delivered": 0,
            "failed": 1,
            "error_type": type(exc).__name__,
        }


def safe_record_degradation(
    config: AppConfig,
    *,
    key: str,
    title: str,
    body: str,
    event: NotificationEvent = NotificationEvent.DEGRADED,
) -> dict[str, object]:
    try:
        return record_degradation(
            config,
            key=key,
            title=title,
            body=body,
            event=event,
        )
    except Exception as exc:
        return {"queued": False, "error_type": type(exc).__name__}


def safe_record_recovery(
    config: AppConfig,
    *,
    key: str,
    title: str,
    body: str,
    event: NotificationEvent = NotificationEvent.RECOVERED,
) -> dict[str, object]:
    try:
        return record_recovery(
            config,
            key=key,
            title=title,
            body=body,
            event=event,
        )
    except Exception as exc:
        return {"queued": False, "error_type": type(exc).__name__}


def requeue_dead_letters(config: AppConfig, *, now: datetime | None = None) -> int:
    current = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    with _notification_lock(config.controller.state_dir):
        path = notification_state_path(config)
        state = _read_state(path)
        retried = tuple(
            NotificationItem(
                item_id=item.item_id,
                event=item.event,
                title=item.title,
                body=item.body,
                created_at=item.created_at,
                attempts=0,
                next_attempt_at=current,
                delivered_to=item.delivered_to,
            )
            for item in state.dead_letters
        )
        _write_state(
            path,
            NotificationState(
                pending=(*state.pending, *retried),
                dead_letters=(),
                delivered_ids=state.delivered_ids,
            ),
        )
    return len(retried)


def record_degradation(
    config: AppConfig,
    *,
    key: str,
    title: str,
    body: str,
    event: NotificationEvent = NotificationEvent.DEGRADED,
    now: datetime | None = None,
) -> dict[str, object]:
    if not config.notifications.enabled:
        return {"queued": False}
    current = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    with _notification_lock(config.controller.state_dir):
        path = config.controller.state_dir / "notification-health.json"
        state = _read_health(path)
        item = state.get(key, {})
        incident_id = item.get("incident_id")
        if not isinstance(incident_id, str) or not incident_id:
            incident_id = uuid4().hex
        first_failure = _health_datetime(item.get("first_failure")) or current
        last_notice = _health_datetime(item.get("last_notice"))
        count = item.get("count", 0)
        count = count if isinstance(count, int) and not isinstance(count, bool) else 0
        count += 1
        threshold_met = count >= config.notifications.degradation_failure_threshold
        duration_met = current - first_failure >= timedelta(
            minutes=config.notifications.degradation_window_minutes
        )
        cooldown_elapsed = last_notice is None or current - last_notice >= timedelta(
            minutes=config.notifications.cooldown_minutes
        )
        should_notify = (threshold_met or duration_met) and cooldown_elapsed
        state[key] = {
            "incident_id": incident_id,
            "count": count,
            "first_failure": first_failure.isoformat(),
            "last_failure": current.isoformat(),
            "last_notice": current.isoformat() if should_notify else item.get("last_notice"),
            "notified": bool(item.get("notified")) or should_notify,
        }
        _write_health(path, state)
    if not should_notify:
        return {"queued": False, "failure_count": count}
    return safe_notify(
        config,
        event,
        dedupe_key=f"{key}:{current.isoformat()}",
        title=title,
        body=body,
    )


def record_recovery(
    config: AppConfig,
    *,
    key: str,
    title: str,
    body: str,
    event: NotificationEvent = NotificationEvent.RECOVERED,
) -> dict[str, object]:
    if not config.notifications.enabled:
        return {"queued": False}
    with _notification_lock(config.controller.state_dir):
        path = config.controller.state_dir / "notification-health.json"
        state = _read_health(path)
        item = state.pop(key, None)
        if item is None:
            return {"queued": False}
        _write_health(path, state)
    if not item.get("notified"):
        return {"queued": False}
    incident_id = item.get("incident_id")
    if not isinstance(incident_id, str) or not incident_id:
        incident_id = str(item.get("first_failure", "unknown"))
    return safe_notify(
        config,
        event,
        dedupe_key=f"{key}:{incident_id}",
        title=title,
        body=body,
    )


def _deliver(destination: NotificationDestination, item: NotificationItem) -> None:
    notifier = _new_notifier()
    if not notifier.add(destination.url) or len(notifier) != 1:
        raise ValueError("invalid Apprise service URL")
    result = notifier.notify(title=item.title, body=item.body)
    if result is not True:
        raise RuntimeError("provider did not confirm delivery")


def _delivery_result(
    state: NotificationState, *, attempted: int, delivered: int, failed: int
) -> dict[str, object]:
    return {
        "attempted": attempted,
        "delivered": delivered,
        "failed": failed,
        "pending": len(state.pending),
        "dead_letters": len(state.dead_letters),
    }


def _read_state(path: Path) -> NotificationState:
    if not path.exists():
        return NotificationState()
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise CatalogError(f"Invalid notification state: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise CatalogError(f"Unsupported notification state: {path}")
    pending = raw.get("pending")
    dead_letters = raw.get("dead_letters")
    delivered_ids = raw.get("delivered_ids")
    if (
        not isinstance(pending, list)
        or not isinstance(dead_letters, list)
        or not isinstance(delivered_ids, list)
    ):
        raise CatalogError(f"Invalid notification state: {path}")
    if any(not isinstance(item, str) or not item for item in delivered_ids):
        raise CatalogError(f"Invalid delivered notification IDs: {path}")
    return NotificationState(
        pending=tuple(_parse_item(item, path) for item in pending),
        dead_letters=tuple(_parse_item(item, path) for item in dead_letters),
        delivered_ids=tuple(delivered_ids),
    )


def _write_state(
    path: Path,
    state: NotificationState,
    *,
    pending: tuple[NotificationItem, ...] | None = None,
) -> None:
    write_json_atomic(
        path,
        {
            "schema_version": 1,
            "pending": [item.as_dict() for item in pending]
            if pending is not None
            else [item.as_dict() for item in state.pending],
            "dead_letters": [item.as_dict() for item in state.dead_letters],
            "delivered_ids": list(state.delivered_ids),
        },
    )


def _parse_item(raw: object, source: Path) -> NotificationItem:
    if not isinstance(raw, dict):
        raise CatalogError(f"Invalid notification item: {source}")
    item_id = raw.get("id")
    title = raw.get("title")
    body = raw.get("body")
    attempts = raw.get("attempts")
    delivered_to = raw.get("delivered_to")
    last_error = raw.get("last_error")
    if (
        not isinstance(item_id, str)
        or not item_id
        or not isinstance(title, str)
        or not title
        or not isinstance(body, str)
        or not body
        or not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or attempts < 0
        or not isinstance(delivered_to, list)
        or any(not isinstance(item, str) or not item for item in delivered_to)
        or (last_error is not None and not isinstance(last_error, str))
    ):
        raise CatalogError(f"Invalid notification item: {source}")
    event_value = raw.get("event")
    if not isinstance(event_value, str):
        raise CatalogError(f"Invalid notification event: {source}")
    try:
        event = NotificationEvent(event_value)
    except (TypeError, ValueError) as exc:
        raise CatalogError(f"Invalid notification event: {source}") from exc
    return NotificationItem(
        item_id=item_id,
        event=event,
        title=title,
        body=body,
        created_at=_required_datetime(raw.get("created_at"), source),
        attempts=attempts,
        next_attempt_at=_required_datetime(raw.get("next_attempt_at"), source),
        delivered_to=tuple(delivered_to),
        last_error=last_error,
    )


def _required_datetime(value: object, source: Path) -> datetime:
    parsed = _health_datetime(value)
    if parsed is None:
        raise CatalogError(f"Invalid notification timestamp: {source}")
    return parsed


def _health_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _read_health(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise CatalogError(f"Invalid notification health state: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise CatalogError(f"Unsupported notification health state: {path}")
    services = raw.get("services")
    if not isinstance(services, dict) or any(
        not isinstance(key, str) or not isinstance(value, dict) for key, value in services.items()
    ):
        raise CatalogError(f"Invalid notification health state: {path}")
    return services


def _write_health(path: Path, services: dict[str, dict[str, object]]) -> None:
    write_json_atomic(path, {"schema_version": 1, "services": services})
