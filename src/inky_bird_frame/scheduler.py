"""Sequential scheduling for controller one-shot commands."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass


@dataclass(frozen=True)
class ScheduledJob:
    name: str
    arguments: tuple[str, ...]
    interval_seconds: int
    requires_refresh: bool = False
    policy_revision: str | None = None


CommandRunner = Callable[[tuple[str, ...]], int]
Waiter = Callable[[float], None]


class SubprocessCommandRunner:
    """Run one child process group and forward container termination signals."""

    def __init__(self, command_prefix: Sequence[str]) -> None:
        self._command_prefix = tuple(command_prefix)
        self._active: subprocess.Popen[bytes] | None = None
        self._termination_signal: int | None = None

    def __call__(self, arguments: tuple[str, ...]) -> int:
        process = subprocess.Popen(
            [*self._command_prefix, *arguments],
            start_new_session=True,
        )
        self._active = process
        if self._termination_signal is not None:
            self._forward_signal(process, self._termination_signal)
        try:
            return process.wait()
        finally:
            self._active = None

    def terminate(self, signum: int) -> None:
        self._termination_signal = signum
        if self._active is not None:
            self._forward_signal(self._active, signum)

    @staticmethod
    def _forward_signal(process: subprocess.Popen[bytes], signum: int) -> None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signum)


def _log_event(event: str, job: ScheduledJob, **details: object) -> None:
    print(
        json.dumps(
            {"event": event, "job": job.name, **details},
            sort_keys=True,
        ),
        flush=True,
    )


def run_scheduler(
    jobs: Sequence[ScheduledJob],
    runner: CommandRunner,
    *,
    stop_requested: Callable[[], bool],
    wait: Waiter,
    monotonic: Callable[[], float] = time.monotonic,
    jobs_provider: Callable[[], Sequence[ScheduledJob]] | None = None,
    wake_provider: Callable[[], str | None] | None = None,
    on_schedule: Callable[[str, float], None] | None = None,
) -> None:
    """Run due jobs serially, isolating failures and gating generation on refresh."""
    if not jobs:
        raise ValueError("Scheduler requires at least one job")
    if any(job.interval_seconds < 1 for job in jobs):
        raise ValueError("Scheduler intervals must be at least one second")

    next_runs = {job.name: monotonic() for job in jobs}
    if len(next_runs) != len(jobs):
        raise ValueError("Scheduler job names must be unique")
    refresh_succeeded = False
    seen_wake: str | None = None
    published: dict[str, float] = {}
    wake_failed = False

    def publish_schedule() -> None:
        if on_schedule is None:
            return
        observed_at = monotonic()
        for name, deadline in next_runs.items():
            if published.get(name) != deadline:
                on_schedule(name, max(0, deadline - observed_at))
        published.clear()
        published.update(next_runs)

    while not stop_requested():
        now = monotonic()
        if jobs_provider is not None:
            updated = jobs_provider()
            if (
                not updated
                or any(job.interval_seconds < 1 for job in updated)
                or len({job.name for job in updated}) != len(updated)
            ):
                raise ValueError("Scheduler configuration has invalid jobs")
            previous = {job.name: job for job in jobs}
            if tuple(updated) != tuple(jobs):
                # A changed policy must complete a new refresh before generation.
                refresh_succeeded = False
                next_runs = {
                    job.name: min(next_runs.get(job.name, now), now + job.interval_seconds)
                    if previous.get(job.name) == job
                    else now
                    for job in updated
                }
                next_runs["refresh"] = now
            jobs = updated
        if wake_provider is not None:
            try:
                wake = wake_provider()
            except Exception as exc:
                # Corrupt retry state must fail generation closed, not stop
                # discovery, catalog publication or notification delivery.
                if not wake_failed:
                    print(
                        json.dumps(
                            {"event": "retry_wake_failed", "error_type": type(exc).__name__}
                        ),
                        flush=True,
                    )
                wake_failed = True
                wake = None
            else:
                if wake_failed:
                    print(json.dumps({"event": "retry_wake_recovered"}), flush=True)
                wake_failed = False
            if wake is not None and wake != seen_wake and "generate" in next_runs:
                seen_wake = wake
                next_runs["generate"] = now
                if "refresh" in next_runs:
                    next_runs["refresh"] = now
                    refresh_succeeded = False
        publish_schedule()
        due_jobs = [job for job in jobs if next_runs[job.name] <= now]
        if not due_jobs:
            delay = max(0.0, min(next_runs.values()) - now)
            wait(
                min(delay, 1.0) if jobs_provider is not None or wake_provider is not None else delay
            )
            continue

        for job in due_jobs:
            if stop_requested():
                return
            if jobs_provider is not None and job not in jobs_provider():
                # Settings can change while the previous child is running.
                # Never start a removed or changed job from a stale due list.
                continue
            if job.requires_refresh and not refresh_succeeded:
                refresh_next_run = next_runs.get("refresh")
                next_runs[job.name] = (
                    max(now + 1, refresh_next_run)
                    if refresh_next_run is not None
                    else now + job.interval_seconds
                )
                _log_event("job_deferred", job, reason="awaiting_successful_refresh")
                publish_schedule()
                continue

            started = monotonic()
            _log_event("job_started", job)
            try:
                exit_code = runner(job.arguments)
            except Exception as exc:  # A scheduler must isolate unexpected job failures.
                exit_code = 1
                _log_event(
                    "job_failed",
                    job,
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
            else:
                _log_event(
                    "job_finished" if exit_code == 0 else "job_failed",
                    job,
                    exit_code=exit_code,
                    duration_seconds=round(monotonic() - started, 3),
                )
            if job.name == "refresh" and exit_code == 0:
                refresh_succeeded = True
            next_runs[job.name] = monotonic() + job.interval_seconds
            # A later job may occupy the runner for minutes. Publish this
            # deadline now, not after the entire due batch has completed.
            publish_schedule()
