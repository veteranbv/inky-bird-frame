from __future__ import annotations

import unittest
from signal import SIGTERM
from unittest.mock import Mock, patch

from inky_bird_frame.scheduler import ScheduledJob, SubprocessCommandRunner, run_scheduler


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def wait(self, seconds: float) -> None:
        self.now += seconds


class SchedulerTests(unittest.TestCase):
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
