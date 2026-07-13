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
    check_display_heartbeat,
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

DISPLAY_CONFIG = """
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

[schedule]
rotation_minutes = {rotation_minutes}

[notifications]
enabled = true
degradation_failure_threshold = 1
degradation_window_minutes = 30
cooldown_minutes = 360
delivery_retry_minutes = 5
max_delivery_attempts = 3

[[notifications.destinations]]
name = "first"
url = "pover://user@token"
events = ["display_stale", "display_recovered"]
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

    def test_enqueue_ignores_events_without_a_subscribed_destination(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            queued = enqueue_notification(
                config,
                NotificationEvent.DISCOVERY,
                dedupe_key="unsubscribed",
                title="Discovery",
                body="A bird was discovered",
            )
            status = notification_status(config)

        self.assertFalse(queued)
        self.assertEqual(status["pending"], 0)

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
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True},
            ) as notify:
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

    def test_failed_degradation_enqueue_remains_eligible_for_notice(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": False, "failed": 1},
            ) as notify:
                record_degradation(config, key="refresh", title="down", body="down", now=now)
                record_degradation(
                    config,
                    key="refresh",
                    title="down",
                    body="down",
                    now=now + timedelta(minutes=5),
                )
                record_degradation(
                    config,
                    key="refresh",
                    title="down",
                    body="down",
                    now=now + timedelta(minutes=10),
                )
            health = json.loads(
                (config.controller.state_dir / "notification-health.json").read_text()
            )["services"]["refresh"]

        self.assertEqual(notify.call_count, 2)
        self.assertFalse(health["notified"])
        self.assertIsNone(health["last_notice"])

    def test_failed_recovery_enqueue_preserves_health_for_retry(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True, "failed": 0},
            ):
                record_degradation(config, key="refresh", title="down", body="down", now=now)
                record_degradation(
                    config,
                    key="refresh",
                    title="down",
                    body="down",
                    now=now + timedelta(minutes=5),
                )
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": False, "failed": 1},
            ):
                failed = record_recovery(config, key="refresh", title="up", body="up")
            retained = json.loads(
                (config.controller.state_dir / "notification-health.json").read_text()
            )["services"]
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True, "failed": 0},
            ):
                retried = record_recovery(config, key="refresh", title="up", body="up")
            cleared = json.loads(
                (config.controller.state_dir / "notification-health.json").read_text()
            )["services"]

        self.assertEqual(failed["failed"], 1)
        self.assertIn("refresh", retained)
        self.assertTrue(retried["queued"])
        self.assertNotIn("refresh", cleared)

    def test_distinct_incidents_send_distinct_recovery_notifications(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True},
            ) as notify:
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


class DisplayHeartbeatTests(unittest.TestCase):
    def _config(self, temporary: str, *, rotation_minutes: int = 30) -> AppConfig:
        path = Path(temporary) / "config.toml"
        path.write_text(DISPLAY_CONFIG.format(rotation_minutes=rotation_minutes))
        return load_config(path)

    def _write_heartbeat(self, config: AppConfig, fetched_at: datetime) -> None:
        config.controller.state_dir.mkdir(parents=True, exist_ok=True)
        (config.controller.state_dir / "display-last-fetch.json").write_text(
            json.dumps({"schema_version": 1, "fetched_at": fetched_at.isoformat()})
        )

    def _write_success(self, config: AppConfig, succeeded_at: datetime) -> None:
        config.controller.state_dir.mkdir(parents=True, exist_ok=True)
        (config.controller.state_dir / "display-last-success.json").write_text(
            json.dumps({"schema_version": 1, "succeeded_at": succeeded_at.isoformat()})
        )

    def test_stale_display_updates_alert_despite_fresh_fetches(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_heartbeat(config, now - timedelta(minutes=5))
            self._write_success(config, now - timedelta(minutes=200))
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True},
            ) as notify:
                result = check_display_heartbeat(config, now=now)

        self.assertTrue(result["stale"])
        self.assertEqual(result["signal"], "display-update")
        self.assertIs(notify.call_args.args[1], NotificationEvent.DISPLAY_STALE)

    def test_fresh_display_update_overrides_stale_fetch_signal(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_heartbeat(config, now - timedelta(minutes=200))
            self._write_success(config, now - timedelta(minutes=10))
            with patch("inky_bird_frame.notifications.safe_notify") as notify:
                result = check_display_heartbeat(config, now=now)

        self.assertFalse(result["stale"])
        self.assertEqual(result["signal"], "display-update")
        notify.assert_not_called()

    def test_corrupt_success_file_falls_back_to_fetch_signal(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_heartbeat(config, now - timedelta(minutes=10))
            config.controller.state_dir.mkdir(parents=True, exist_ok=True)
            (config.controller.state_dir / "display-last-success.json").write_bytes(
                b'{"schema_version": 1, "succeeded_at": "2026-07-10T\xff'
            )
            with patch("inky_bird_frame.notifications.safe_notify") as notify:
                result = check_display_heartbeat(config, now=now)

        self.assertFalse(result["stale"])
        self.assertEqual(result["signal"], "catalog-fetch")
        self.assertIn("warning", result)
        notify.assert_not_called()

    def test_non_utf8_heartbeat_is_reported_not_fatal(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            config.controller.state_dir.mkdir(parents=True, exist_ok=True)
            (config.controller.state_dir / "display-last-fetch.json").write_bytes(
                b'{"schema_version": 1, "fetched_at": "2026-\xff\xfe'
            )
            result = check_display_heartbeat(config, now=now)

        self.assertFalse(result["checked"])
        self.assertIn("warning", result)

    def test_fresh_heartbeat_does_not_alert(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_heartbeat(config, now - timedelta(minutes=10))
            with patch("inky_bird_frame.notifications.safe_notify") as notify:
                result = check_display_heartbeat(config, now=now)

        self.assertTrue(result["checked"])
        self.assertFalse(result["stale"])
        notify.assert_not_called()

    def test_stale_heartbeat_alerts_once_across_dispatch_runs(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_heartbeat(config, now - timedelta(minutes=200))
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True},
            ) as notify:
                first = check_display_heartbeat(config, now=now)
                second = check_display_heartbeat(config, now=now + timedelta(minutes=5))

        self.assertTrue(first["stale"])
        self.assertTrue(second["stale"])
        self.assertEqual(notify.call_count, 1)
        self.assertIs(notify.call_args.args[1], NotificationEvent.DISPLAY_STALE)

    def test_stale_alert_enqueues_display_stale_event(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_heartbeat(config, now - timedelta(minutes=200))
            check_display_heartbeat(config, now=now)
            check_display_heartbeat(config, now=now + timedelta(minutes=5))
            status = notification_status(config)
            state = json.loads((config.controller.state_dir / "notifications.json").read_text())

        self.assertEqual(status["pending"], 1)
        self.assertEqual(state["pending"][0]["event"], "display_stale")

    def test_recovery_after_stale_alert_notifies_once(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_heartbeat(config, now - timedelta(minutes=200))
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True},
            ) as notify:
                check_display_heartbeat(config, now=now)
                self._write_heartbeat(config, now + timedelta(minutes=10))
                recovered = check_display_heartbeat(config, now=now + timedelta(minutes=15))
                repeated = check_display_heartbeat(config, now=now + timedelta(minutes=20))

        self.assertFalse(recovered["stale"])
        self.assertFalse(repeated["stale"])
        events = [call.args[1] for call in notify.call_args_list]
        self.assertEqual(
            events, [NotificationEvent.DISPLAY_STALE, NotificationEvent.DISPLAY_RECOVERED]
        )

    def test_missing_heartbeat_stays_silent(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            with patch("inky_bird_frame.notifications.safe_notify") as notify:
                result = check_display_heartbeat(config, now=now)

        self.assertEqual(result, {"checked": False, "stale": None})
        notify.assert_not_called()

    def test_corrupt_heartbeat_is_no_signal_with_warning(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            config.controller.state_dir.mkdir(parents=True, exist_ok=True)
            (config.controller.state_dir / "display-last-fetch.json").write_text("not json")
            with patch("inky_bird_frame.notifications.safe_notify") as notify:
                result = check_display_heartbeat(config, now=now)

        self.assertFalse(result["checked"])
        self.assertIn("Invalid display heartbeat", str(result["warning"]))
        notify.assert_not_called()

    def test_threshold_respects_sixty_minute_floor(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary, rotation_minutes=5)
            self._write_heartbeat(config, now - timedelta(minutes=50))
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True},
            ) as notify:
                within_floor = check_display_heartbeat(config, now=now)
                self._write_heartbeat(config, now - timedelta(minutes=61))
                past_floor = check_display_heartbeat(config, now=now)

        self.assertEqual(within_floor["threshold_minutes"], 60)
        self.assertFalse(within_floor["stale"])
        self.assertTrue(past_floor["stale"])
        self.assertEqual(notify.call_count, 1)


if __name__ == "__main__":
    unittest.main()
