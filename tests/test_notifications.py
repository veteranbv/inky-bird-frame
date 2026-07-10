from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inky_bird_frame.config import AppConfig, NotificationEvent, NotificationsConfig, load_config
from inky_bird_frame.errors import ConfigurationError
from inky_bird_frame.notifications import (
    dispatch_notifications,
    enqueue_notification,
    notification_status,
    record_degradation,
    record_recovery,
    requeue_dead_letters,
    safe_notify,
    send_notification_test,
    validate_notification_destinations,
)

CONFIG = """
[discovery]
zip_code = "12345"
radius_km = 8
species_limit = 12
window = "last-week"

[controller]
workspace_dir = "."
catalog_dir = "catalog"
state_dir = "state"
codex_path = "/usr/bin/false"
bind_host = "127.0.0.1"
port = 8793
references_per_species = 4
generations_per_cycle = 1
max_generation_attempts = 3

[display_node]
controller_url = "http://controller.test:8793"
state_dir = "display"

[notifications]
enabled = true
degradation_failure_threshold = 2
degradation_window_minutes = 30
cooldown_minutes = 360
delivery_retry_minutes = 5
max_delivery_attempts = 3

[[notifications.destinations]]
name = "first"
url = "pover://user@token"
events = ["terminal_error", "degraded", "recovered"]

[[notifications.destinations]]
name = "second"
url = "ntfy://example-topic"
events = ["terminal_error"]
"""


class NotificationTests(unittest.TestCase):
    def _config(self, temporary: str) -> AppConfig:
        path = Path(temporary) / "config.toml"
        path.write_text(CONFIG)
        return load_config(path)

    def test_validation_and_status_redact_service_urls(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            validated = validate_notification_destinations(config)
            status = notification_status(config)

        serialized = json.dumps(status)
        self.assertEqual([item["scheme"] for item in validated], ["pover", "ntfy"])
        self.assertNotIn("user", serialized)
        self.assertNotIn("token", serialized)
        self.assertNotIn("example-topic", serialized)

    def test_manual_delivery_rejects_disabled_notifications(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            config = AppConfig(
                discovery=config.discovery,
                controller=config.controller,
                display_node=config.display_node,
                schedule=config.schedule,
                public_catalog=config.public_catalog,
                research=config.research,
                notifications=NotificationsConfig(),
            )

            with self.assertRaisesRegex(ConfigurationError, "disabled"):
                send_notification_test(config)

    def test_safe_notify_enqueues_without_provider_delivery(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            with patch("inky_bird_frame.notifications._deliver") as deliver:
                result = safe_notify(
                    config,
                    NotificationEvent.TERMINAL_ERROR,
                    dedupe_key="queued-only",
                    title="Failure",
                    body="Something failed",
                )
            status = notification_status(config)

        deliver.assert_not_called()
        self.assertTrue(result["queued"])
        self.assertEqual(result["attempted"], 0)
        self.assertEqual(status["pending"], 1)

    def test_partial_delivery_retries_only_failed_destination(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            enqueue_notification(
                config,
                NotificationEvent.TERMINAL_ERROR,
                dedupe_key="one",
                title="Failure",
                body="Something failed",
                now=now,
            )
            with patch(
                "inky_bird_frame.notifications._deliver",
                side_effect=[None, RuntimeError("down")],
            ) as deliver:
                first = dispatch_notifications(config, now=now)
            with patch("inky_bird_frame.notifications._deliver") as retry:
                second = dispatch_notifications(config, now=now + timedelta(minutes=5))
            duplicate = enqueue_notification(
                config,
                NotificationEvent.TERMINAL_ERROR,
                dedupe_key="one",
                title="Failure",
                body="Something failed",
                now=now,
            )

        self.assertEqual(deliver.call_count, 2)
        self.assertEqual(first["pending"], 1)
        self.assertEqual(retry.call_count, 1)
        self.assertEqual(retry.call_args.args[0].name, "second")
        self.assertEqual(second["pending"], 0)
        self.assertFalse(duplicate)

    def test_enqueue_during_delivery_is_preserved_for_next_dispatch(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            enqueue_notification(
                config,
                NotificationEvent.DEGRADED,
                dedupe_key="first",
                title="First",
                body="First",
                now=now,
            )

            def enqueue_another(*_args: object) -> None:
                enqueue_notification(
                    config,
                    NotificationEvent.DEGRADED,
                    dedupe_key="second",
                    title="Second",
                    body="Second",
                    now=now,
                )

            with patch("inky_bird_frame.notifications._deliver", side_effect=enqueue_another):
                result = dispatch_notifications(config, now=now)
            status = notification_status(config)

        self.assertEqual(result["delivered"], 1)
        self.assertEqual(status["pending"], 1)

    def test_dead_letters_can_be_requeued_after_provider_recovery(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            enqueue_notification(
                config,
                NotificationEvent.DEGRADED,
                dedupe_key="dead-letter",
                title="Down",
                body="Down",
                now=now,
            )
            with patch("inky_bird_frame.notifications._deliver", side_effect=RuntimeError("down")):
                for offset in (0, 5, 10):
                    dispatch_notifications(config, now=now + timedelta(minutes=offset))
            before = notification_status(config)
            requeued = requeue_dead_letters(config, now=now + timedelta(minutes=15))
            with patch("inky_bird_frame.notifications._deliver"):
                dispatch_notifications(config, now=now + timedelta(minutes=15))
            after = notification_status(config)

        self.assertEqual(before["dead_letters"], 1)
        self.assertEqual(requeued, 1)
        self.assertEqual(after["dead_letters"], 0)
        self.assertEqual(after["pending"], 0)

    def test_degradation_threshold_and_recovery_are_deduplicated(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            with patch("inky_bird_frame.notifications.safe_notify") as notify:
                first = record_degradation(
                    config, key="refresh", title="down", body="down", now=now
                )
                second = record_degradation(
                    config,
                    key="refresh",
                    title="down",
                    body="down",
                    now=now + timedelta(minutes=5),
                )
                record_recovery(config, key="refresh", title="up", body="up")
                repeated = record_recovery(config, key="refresh", title="up", body="up")

        self.assertFalse(first["queued"])
        self.assertEqual(second, notify.return_value)
        self.assertEqual(notify.call_count, 2)
        self.assertFalse(repeated["queued"])

    def test_distinct_incidents_send_distinct_recovery_notifications(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            with patch("inky_bird_frame.notifications.safe_notify") as notify:
                for incident in range(2):
                    record_degradation(
                        config,
                        key="refresh",
                        title="down",
                        body="down",
                        now=now + timedelta(hours=incident, minutes=1),
                    )
                    record_degradation(
                        config,
                        key="refresh",
                        title="down",
                        body="down",
                        now=now + timedelta(hours=incident, minutes=2),
                    )
                    record_recovery(config, key="refresh", title="up", body="up")

        recovery_keys = [
            call.kwargs["dedupe_key"]
            for call in notify.call_args_list
            if call.args[1] is NotificationEvent.RECOVERED
        ]
        self.assertEqual(len(recovery_keys), 2)
        self.assertEqual(len(set(recovery_keys)), 2)


if __name__ == "__main__":
    unittest.main()
