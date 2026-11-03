from __future__ import annotations

import unittest
from signal import SIGTERM
from unittest.mock import Mock, patch

from inky_bird_frame.errors import CatalogError
from inky_bird_frame.scheduler import ScheduledJob, SubprocessCommandRunner, run_scheduler


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def wait(self, seconds: float) -> None:
        self.now += seconds


class SchedulerTests(unittest.TestCase):
    def test_completed_job_deadline_is_published_before_the_next_long_job(self) -> None:
        clock = FakeClock()
        calls: list[str] = []
        deadlines: dict[str, float] = {}

        def runner(args: tuple[str, ...]) -> int:
            calls.append(args[0])
            if args[0] == "refresh":
                clock.now += 10
            else:
                assert deadlines["refresh"] == 70
                clock.now += 120
            return 0

        run_scheduler(
            [
                ScheduledJob("refresh", ("refresh",), 60),
                ScheduledJob("generate", ("generate",), 300, True),
            ],
            runner,
            stop_requested=lambda: len(calls) >= 2,
            wait=clock.wait,
            monotonic=clock.monotonic,
            on_schedule=lambda name, delay: deadlines.update({name: clock.now + delay}),
        )
        assert deadlines["generate"] == 430

    def test_deferred_job_deadline_is_published_before_another_job(self) -> None:
        clock = FakeClock()
        calls: list[str] = []
        deadlines: dict[str, float] = {}

        def runner(args: tuple[str, ...]) -> int:
            calls.append(args[0])
            if args[0] == "notifications":
                assert deadlines["generate"] == 60
            return 1 if args[0] == "refresh" else 0

        run_scheduler(
            [
                ScheduledJob("refresh", ("refresh",), 60),
                ScheduledJob("generate", ("generate",), 300, True),
                ScheduledJob("notifications", ("notifications",), 30),
            ],
            runner,
            stop_requested=lambda: len(calls) >= 2,
            wait=clock.wait,
            monotonic=clock.monotonic,
            on_schedule=lambda name, delay: deadlines.update({name: clock.now + delay}),
        )
        assert calls == ["refresh", "notifications"]

    def test_bad_retry_wake_state_does_not_stop_unrelated_jobs(self) -> None:
        clock = FakeClock()
        calls: list[str] = []
        wake = Mock(side_effect=CatalogError("PRIVATE_FILE_PAYLOAD"))

        def runner(args: tuple[str, ...]) -> int:
            calls.append(args[0])
            return 0

        with patch("builtins.print") as log:
            run_scheduler(
                [
                    ScheduledJob("refresh", ("refresh",), 60),
                    ScheduledJob("notifications", ("notifications",), 30),
                ],
                runner,
                stop_requested=lambda: len(calls) >= 5,
                wait=clock.wait,
                monotonic=clock.monotonic,
                wake_provider=wake,
            )
        assert calls == ["refresh", "notifications", "notifications", "refresh", "notifications"]
        messages = [str(call.args[0]) for call in log.call_args_list]
        assert sum("retry_wake_failed" in message for message in messages) == 1
        assert not any("PRIVATE_" in message for message in messages)

    def test_retry_wake_waits_for_active_job_then_refreshes_and_runs_once(self) -> None:
        clock = FakeClock()
        calls: list[tuple[str, float]] = []
        wake: str | None = None

        def runner(arguments: tuple[str, ...]) -> int:
            nonlocal wake
            calls.append((arguments[0], clock.now))
            if calls == [("refresh", 0.0), ("generate", 0.0)]:
                # The owner queues work while this serial runner is occupied.
                wake = "new-request"
                clock.now += 120
            return 0

        run_scheduler(
            [
                ScheduledJob("refresh", ("refresh",), 60),
                ScheduledJob("generate", ("generate",), 300, True),
            ],
            runner,
            stop_requested=lambda: sum(name == "generate" for name, _ in calls) >= 3,
            wait=clock.wait,
            monotonic=clock.monotonic,
            wake_provider=lambda: wake,
        )
        assert calls[:4] == [
            ("refresh", 0.0),
            ("generate", 0.0),
            ("refresh", 120.0),
            ("generate", 120.0),
        ]
        assert [stamp for name, stamp in calls if name == "generate"] == [0.0, 120.0, 420.0]

    def test_reported_next_check_is_after_completion_not_start(self) -> None:
        clock = FakeClock()
        schedules: list[tuple[str, float]] = []

        def runner(arguments: tuple[str, ...]) -> int:
            if arguments[0] == "generate":
                clock.now += 17
            return 0

        run_scheduler(
            [
                ScheduledJob("refresh", ("refresh",), 60),
                ScheduledJob("generate", ("generate",), 300, True),
            ],
            runner,
            stop_requested=lambda: clock.now > 18,
            wait=clock.wait,
            monotonic=clock.monotonic,
            on_schedule=lambda name, delay: schedules.append((name, clock.now + delay)),
        )
        assert ("generate", 317.0) in schedules

    def test_policy_edit_during_refresh_does_not_start_old_generation_job(self) -> None:
        clock = FakeClock()
        revision = "old"
        calls: list[tuple[str, str]] = []

        def jobs() -> list[ScheduledJob]:
            return [
                ScheduledJob("refresh", ("refresh",), 60, policy_revision=revision),
                ScheduledJob("generate", ("generate",), 300, True, revision),
            ]

        def runner(arguments: tuple[str, ...]) -> int:
            nonlocal revision
            calls.append((arguments[0], revision))
            if len(calls) == 1:
                revision = "new"
            return 0

        run_scheduler(
            jobs(),
            runner,
            stop_requested=lambda: len(calls) >= 3,
            wait=clock.wait,
            monotonic=clock.monotonic,
            jobs_provider=jobs,
        )
        self.assertEqual(calls, [("refresh", "old"), ("refresh", "new"), ("generate", "new")])

    def test_removed_generation_never_runs_from_an_already_due_list(self) -> None:
        clock = FakeClock()
        enabled = True
        calls: list[tuple[str, ...]] = []

        def jobs() -> list[ScheduledJob]:
            return [
                ScheduledJob("refresh", ("refresh",), 60),
                *([ScheduledJob("generate", ("generate",), 300, True)] if enabled else []),
            ]

        def runner(arguments: tuple[str, ...]) -> int:
            nonlocal enabled
            calls.append(arguments)
            enabled = False
            return 0

        run_scheduler(
            jobs(),
            runner,
            stop_requested=lambda: len(calls) >= 2,
            wait=clock.wait,
            monotonic=clock.monotonic,
            jobs_provider=jobs,
        )
        self.assertEqual(calls, [("refresh",), ("refresh",)])

    def test_subprocess_runner_forwards_termination_to_active_process_group(self) -> None:
        process = Mock(pid=42)
        runner = SubprocessCommandRunner(("python", "-m", "example"))

        def terminate_while_waiting() -> int:
            runner.terminate(SIGTERM)
            return 143

        process.wait.side_effect = terminate_while_waiting

        with (
            patch("inky_bird_frame.scheduler.subprocess.Popen", return_value=process) as popen,
            patch("inky_bird_frame.scheduler.os.killpg") as killpg,
        ):
            exit_code = runner(("refresh",))

        popen.assert_called_once_with(
            ["python", "-m", "example", "refresh"],
            start_new_session=True,
        )
        killpg.assert_called_once_with(42, SIGTERM)
        self.assertEqual(exit_code, 143)

    def test_subprocess_runner_forwards_signal_requested_during_spawn(self) -> None:
        process = Mock(pid=42)
        process.wait.return_value = 143
        runner = SubprocessCommandRunner(("inky-bird-frame",))
        runner.terminate(SIGTERM)

        with (
            patch("inky_bird_frame.scheduler.subprocess.Popen", return_value=process),
            patch("inky_bird_frame.scheduler.os.killpg") as killpg,
        ):
            runner(("generate",))

        killpg.assert_called_once_with(42, SIGTERM)

    def test_jobs_run_serially_and_repeat_on_their_own_intervals(self) -> None:
        clock = FakeClock()
        calls: list[tuple[str, ...]] = []

        def runner(arguments: tuple[str, ...]) -> int:
            calls.append(arguments)
            return 0

        run_scheduler(
            [
                ScheduledJob("refresh", ("refresh",), 60),
                ScheduledJob("generate", ("generate",), 120, requires_refresh=True),
                ScheduledJob("notifications", ("notifications", "dispatch"), 30),
            ],
            runner,
            stop_requested=lambda: len(calls) >= 6,
            wait=clock.wait,
            monotonic=clock.monotonic,
        )

        self.assertEqual(
            calls,
            [
                ("refresh",),
                ("generate",),
                ("notifications", "dispatch"),
                ("notifications", "dispatch"),
                ("refresh",),
                ("notifications", "dispatch"),
            ],
        )

    def test_generation_waits_for_a_successful_refresh_without_blocking_other_jobs(self) -> None:
        clock = FakeClock()
        calls: list[tuple[str, ...]] = []
        refresh_attempts = 0

        def runner(arguments: tuple[str, ...]) -> int:
            nonlocal refresh_attempts
            calls.append(arguments)
            if arguments == ("refresh",):
                refresh_attempts += 1
                return 0 if refresh_attempts == 2 else 1
            return 0

        run_scheduler(
            [
                ScheduledJob("refresh", ("refresh",), 60),
                ScheduledJob("generate", ("generate",), 300, requires_refresh=True),
                ScheduledJob("notifications", ("notifications",), 30),
            ],
            runner,
            stop_requested=lambda: ("generate",) in calls,
            wait=clock.wait,
            monotonic=clock.monotonic,
        )

        self.assertEqual(calls.count(("refresh",)), 2)
        self.assertGreaterEqual(calls.count(("notifications",)), 2)
        self.assertEqual(calls[-1], ("generate",))

    def test_runner_exception_does_not_stop_later_jobs(self) -> None:
        clock = FakeClock()
        calls: list[tuple[str, ...]] = []

        def runner(arguments: tuple[str, ...]) -> int:
            calls.append(arguments)
            if arguments == ("refresh",):
                raise RuntimeError("temporary failure")
            return 0

        run_scheduler(
            [
                ScheduledJob("refresh", ("refresh",), 60),
                ScheduledJob("notifications", ("notifications",), 30),
            ],
            runner,
            stop_requested=lambda: ("notifications",) in calls,
            wait=clock.wait,
            monotonic=clock.monotonic,
        )

        self.assertEqual(calls, [("refresh",), ("notifications",)])

    def test_duplicate_job_names_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be unique"):
            run_scheduler(
                [
                    ScheduledJob("refresh", ("first",), 1),
                    ScheduledJob("refresh", ("second",), 1),
                ],
                lambda _arguments: 0,
                stop_requested=lambda: True,
                wait=lambda _seconds: None,
            )
