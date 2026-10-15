"""One bounded worker for explicit owner operations, with private receipts.

This is not a generation queue. Operations never start a model. A disconnected
browser can read the outcome; an interrupted process never replays a mutation.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Literal, cast
from uuid import uuid4

from .catalog import catalog_state_lock
from .config import AppConfig, load_config
from .errors import InkyBirdFrameError
from .http import write_bytes_atomic
from .settings import current_config, settings_status
from .timeutil import parse_utc_timestamp

# A week covers interrupted work discovered after a weekend. Receipts are small,
# not imported files. A full allowance fails closed rather than evict active work.
RECEIPT_RETENTION = timedelta(days=7)
MAX_RECEIPTS = 256
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 16 * 1024
OperationKind = Literal["refresh", "retry", "notification-test", "notification-retry"]
OperationStatus = Literal["pending", "running", "succeeded", "failed", "interrupted"]
KINDS = {"refresh", "retry", "notification-test", "notification-retry"}
STATUSES = {"pending", "running", "succeeded", "failed", "interrupted"}


class OwnerOperationError(InkyBirdFrameError):
    """Safe guidance without exception payloads or local paths."""


class OwnerOperationBusyError(OwnerOperationError):
    """Another owner operation is already running."""


@dataclass(frozen=True)
class OperationReceipt:
    operation_id: str
    key_hash: str
    input_hash: str
    kind: OperationKind
    status: OperationStatus
    created_at: str
    updated_at: str
    result: dict[str, object] | None = None

    def public(self) -> dict[str, object]:
        # This is an authenticated owner projection, not a dashboard payload.
        return {
            key: value
            for key, value in asdict(self).items()
            if key not in {"key_hash", "input_hash"}
        }


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _stamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@contextmanager
def _exclusive(state_dir: Path) -> Iterator[IO[bytes]]:
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            state_dir / "owner-operation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "r+b") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise OwnerOperationError("Owner-operation lock must be private with mode 0600")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise OwnerOperationBusyError(
                    "Another owner operation is running. Check its outcome first."
                ) from exc
            try:
                yield handle
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise OwnerOperationError(
            "Owner-operation storage is unavailable. Check private file permissions."
        ) from exc


class OwnerOperations:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.path = state_dir / "owner-operations.json"
        self._guard = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inky-owner")
        self._active = False
        self._closed = False

    def _read(self) -> list[OperationReceipt]:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return []
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise OwnerOperationError("Owner receipts must be private regular files with mode 0600")
        try:
            with self.path.open("rb") as handle:
                content = handle.read(MAX_RECEIPT_BYTES + 1)
            if len(content) > MAX_RECEIPT_BYTES:
                raise ValueError("size")
            raw = json.loads(content)
            if (
                not isinstance(raw, dict)
                or set(raw) != {"schema_version", "receipts"}
                or type(raw["schema_version"]) is not int
                or raw["schema_version"] != 1
            ):
                raise ValueError("schema")
            rows = raw["receipts"]
            if not isinstance(rows, list) or len(rows) > MAX_RECEIPTS:
                raise ValueError("receipts")
            result = []
            for row in rows:
                if not isinstance(row, dict) or set(row) != {
                    "operation_id",
                    "key_hash",
                    "input_hash",
                    "kind",
                    "status",
                    "created_at",
                    "updated_at",
                    "result",
                }:
                    raise ValueError("receipt")
                for key, length in (("operation_id", 32), ("key_hash", 64), ("input_hash", 64)):
                    if not isinstance(row[key], str) or not re.fullmatch(
                        f"[0-9a-f]{{{length}}}", row[key]
                    ):
                        raise ValueError("identity")
                if row["kind"] not in KINDS or row["status"] not in STATUSES:
                    raise ValueError("status")
                if any(
                    parse_utc_timestamp(row[key]) is None for key in ("created_at", "updated_at")
                ):
                    raise ValueError("time")
                if row["result"] is not None and not isinstance(row["result"], dict):
                    raise ValueError("result")
                result.append(OperationReceipt(**row))
            if len({item.operation_id for item in result}) != len(result) or len(
                {item.key_hash for item in result}
            ) != len(result):
                raise ValueError("duplicate")
            return result
        except (OSError, ValueError, TypeError) as exc:
            raise OwnerOperationError(
                "Owner receipts are invalid. Restore their matching backup; "
                "do not replay unknown work."
            ) from exc

    def _write(self, receipts: list[OperationReceipt]) -> None:
        body = (
            json.dumps(
                {"schema_version": 1, "receipts": [asdict(row) for row in receipts]},
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode()
        if len(body) > MAX_RECEIPT_BYTES:
            raise OwnerOperationError(
                "Owner receipt storage is full. Back it up before removing old receipts."
            )
        write_bytes_atomic(self.path, body, mode=0o600)

    def _replace(self, receipt: OperationReceipt) -> None:
        with catalog_state_lock(self.state_dir):
            receipts = self._read()
            if not any(row.operation_id == receipt.operation_id for row in receipts):
                raise OwnerOperationError("Owner receipt was removed while work was running")
            self._write(
                [receipt if row.operation_id == receipt.operation_id else row for row in receipts]
            )

    def _reconcile(self) -> None:
        if self._active:
            return
        try:
            with _exclusive(self.state_dir), catalog_state_lock(self.state_dir):
                receipts = self._read()
                if any(row.status in {"pending", "running"} for row in receipts):
                    self._write(
                        [
                            replace(
                                row,
                                status="interrupted",
                                updated_at=_stamp(),
                                result={
                                    "message": "Interrupted. The outcome is not confirmed. "
                                    "Inspect state before an explicit retry."
                                },
                            )
                            if row.status in {"pending", "running"}
                            else row
                            for row in receipts
                        ]
                    )
        except OwnerOperationBusyError:
            # Another service process holds the operation lock. It owns recovery.
            return

    def status(self, operation_id: str | None = None) -> dict[str, object]:
        with self._guard:
            self._reconcile()
            with catalog_state_lock(self.state_dir):
                receipts = self._read()
        if operation_id is not None:
            found = next((row for row in receipts if row.operation_id == operation_id), None)
            if found is None:
                raise OwnerOperationError("This operation receipt is unavailable or has expired")
            return found.public()
        return {"operations": [row.public() for row in receipts[-20:]]}

    def submit(
        self,
        kind: OperationKind,
        *,
        key: str,
        inputs: dict[str, object],
        execute: Callable[[], dict[str, object]],
    ) -> dict[str, object]:
        if kind not in KINDS or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key):
            raise OwnerOperationError("Use a new operation identifier for this request")
        # Only non-secret action parameters belong here. Bootstrap credentials
        # must never enter durable idempotency hashes or receipts.
        digest = _hash(json.dumps({"kind": kind, "inputs": inputs}, sort_keys=True))
        with self._guard:
            self._reconcile()
            with catalog_state_lock(self.state_dir):
                receipts = self._read()
                existing = next((row for row in receipts if row.key_hash == _hash(key)), None)
                if existing is not None:
                    if existing.input_hash != digest:
                        raise OwnerOperationError(
                            "This operation identifier was already used with different choices"
                        )
                    return existing.public()
            if self._active or self._closed:
                raise OwnerOperationBusyError(
                    "Another owner operation is running or Inky is stopping"
                )
            lock = _exclusive(self.state_dir)
            lock.__enter__()
            handed_off = False
            try:
                with catalog_state_lock(self.state_dir):
                    # Another process may have completed between the first read
                    # and taking the cross-process operation lock.
                    receipts = self._read()
                    existing = next((row for row in receipts if row.key_hash == _hash(key)), None)
                    if existing is not None:
                        if existing.input_hash != digest:
                            raise OwnerOperationError(
                                "This identifier was used with different choices"
                            )
                        return existing.public()
                    cutoff = datetime.now(UTC) - RECEIPT_RETENTION
                    receipts = [
                        row
                        for row in receipts
                        if row.status in {"pending", "running"}
                        or cast(datetime, parse_utc_timestamp(row.updated_at)) >= cutoff
                    ]
                    if len(receipts) >= MAX_RECEIPTS:
                        raise OwnerOperationError(
                            "The receipt allowance is full. "
                            "Back up older outcomes before continuing."
                        )
                    receipt = OperationReceipt(
                        uuid4().hex, _hash(key), digest, kind, "pending", _stamp(), _stamp()
                    )
                    self._write([*receipts, receipt])
                self._active = True

                def run() -> None:
                    final: OperationReceipt | None = None
                    try:
                        running = replace(receipt, status="running", updated_at=_stamp())
                        self._replace(running)
                        try:
                            result = execute()
                            if len(json.dumps(result).encode()) > MAX_RESULT_BYTES:
                                raise OwnerOperationError(
                                    "Operation result exceeds the supported size"
                                )
                        except Exception as exc:
                            # Never copy raw exception/server payloads into receipts.
                            final = replace(
                                running,
                                status="failed",
                                updated_at=_stamp(),
                                result={
                                    "error_type": type(exc).__name__,
                                    "message": str(exc)
                                    if isinstance(exc, OwnerOperationError)
                                    else "The operation did not finish. "
                                    "Check its source or work state before retrying.",
                                },
                            )
                        else:
                            final = replace(
                                running, status="succeeded", updated_at=_stamp(), result=result
                            )
                    finally:
                        # Publish a terminal outcome and release the worker as
                        # one local critical section. A following action must
                        # not see "finished" and then hit our stale busy flag.
                        with self._guard:
                            try:
                                if final is not None:
                                    self._replace(final)
                            finally:
                                try:
                                    lock.__exit__(None, None, None)
                                finally:
                                    self._active = False

                self._executor.submit(run)
                handed_off = True
                return receipt.public()
            except BaseException:
                self._active = False
                raise
            finally:
                if not handed_off:
                    lock.__exit__(None, None, None)

    def close(self) -> None:
        with self._guard:
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=False)


def execute_owner_operation(
    initial: AppConfig, kind: OperationKind, *, revision: str, taxon_id: int | None = None
) -> dict[str, object]:
    """Adapt only named, typed application operations; never shell text or models."""
    from .controller import refresh_observations
    from .notifications import dispatch_notifications, requeue_dead_letters, send_notification_test
    from .recovery import retry_species

    if settings_status(initial).get("revision") != revision:
        raise OwnerOperationError("Settings changed. Reload and review this action again.")
    current_config(initial)
    path = initial.configuration_path
    if path is None:
        raise OwnerOperationError("Start the controller with its authoritative configuration file")
    try:
        config = load_config(path, load_secrets=kind != "retry")
    except (InkyBirdFrameError, OSError) as exc:
        raise OwnerOperationError(
            "This service cannot load the configured credentials. "
            "Check the installer environment before retrying."
        ) from exc
    if kind == "refresh":
        refreshed = refresh_observations(config)
        providers = cast(list[dict[str, object]], refreshed["providers"])
        return {
            "message": "Discovery refreshed. Provider results remain separate; "
            "existing generation policy applies to newly eligible birds.",
            "species_count": refreshed["species_count"],
            "providers": [
                {
                    field: row.get(field)
                    for field in ("name", "status", "species_count", "unresolved_count")
                }
                for row in providers
            ],
        }
    if kind == "retry":
        if type(taxon_id) is not int or taxon_id <= 0:
            raise OwnerOperationError("Choose a species that needs a permitted retry")
        result = retry_species(config, taxon_id)
        return {
            "message": "Made eligible for a new attempt. This did not start generation "
            "or bypass sourced review. The generation policy and scheduler still apply.",
            **{
                key: result[key]
                for key in (
                    "taxon_id",
                    "queued_for_generation",
                    "preserved_quality_findings_count",
                    "preserved_profile_conflicts_count",
                    "preserved_correction_source",
                )
            },
        }
    if kind == "notification-test":
        result = send_notification_test(config)
        failures = cast(list[object], result["failures"])
        return {
            "message": "Test delivery failed for one or more destinations."
            if failures
            else "Test delivered to every configured destination.",
            "delivered_count": len(cast(list[object], result["delivered"])),
            "failed_count": len(failures),
        }
    if kind == "notification-retry":
        requeued = requeue_dead_letters(config)
        delivery = dispatch_notifications(config)
        return {
            "message": "Dead letters were returned to the existing outbox. "
            "Delivery results below are not a promise that every destination accepted them.",
            "requeued": requeued,
            **{
                key: value
                for key, value in delivery.items()
                if key
                in {
                    "attempted",
                    "delivered",
                    "failed",
                    "pending",
                    "dead_letters",
                    "dispatcher_busy",
                }
            },
        }
    raise OwnerOperationError("This owner operation is not supported")
