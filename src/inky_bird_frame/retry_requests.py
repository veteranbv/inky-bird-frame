"""Durable owner retry intents; only the locked generation cycle prepares them.

This is deliberately not a replay queue for arbitrary owner operations. A lost
reply can be deduplicated; an interrupted preparation is held for inspection,
never blindly replayed. Approved artwork is never replaced by this path.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
from uuid import uuid4

from .catalog import find_taxon_directory, has_valid_approved_candidate, utc_now
from .config import AppConfig
from .errors import CatalogError, InkyBirdFrameError, SpeciesStateError
from .runtime_state import read_state, state_lock, state_lock_held, write_state
from .timeutil import parse_utc_timestamp

MAX_REQUESTS = 256
MAX_BATCH = 32
RETRY_WAKE_LISTENER = "retry-wake-listener.lock"
_STATUSES = {"queued", "preparing", "eligible", "approved", "blocked"}


@dataclass(frozen=True)
class RetryRequest:
    request_id: str
    taxon_id: int
    created_at: str
    updated_at: str
    status: str = "queued"
    outcome: str | None = None
    run_now: bool = False


def _read(state_dir: Path) -> tuple[list[RetryRequest], str | None]:
    raw = read_state(state_dir / "retry-requests.json")
    if raw is None:
        return [], None
    rows, wake = raw.get("requests"), raw.get("wake")
    if (
        set(raw) != {"schema_version", "requests", "wake"}
        or type(raw["schema_version"]) is not int
        or raw["schema_version"] != 1
        or not isinstance(rows, list)
        or len(rows) > MAX_REQUESTS
        or (wake is not None and (not isinstance(wake, str) or len(wake) != 32))
    ):
        raise CatalogError("Retry request state is invalid")
    requests: list[RetryRequest] = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != set(RetryRequest.__dataclass_fields__)
            or not isinstance(row.get("request_id"), str)
            or len(row["request_id"]) != 32
            or type(row.get("taxon_id")) is not int
            or row["taxon_id"] <= 0
            or type(row.get("run_now")) is not bool
            or not isinstance(row.get("status"), str)
            or row["status"] not in _STATUSES
            or (row.get("outcome") is not None and not isinstance(row["outcome"], str))
            or row.get("outcome")
            not in {
                None,
                "prepared",
                "already_approved",
                "interrupted",
                "preparation_failed",
                "pending_review",
                "retained_state",
                "not_retryable",
            }
            or parse_utc_timestamp(row.get("created_at")) is None
            or parse_utc_timestamp(row.get("updated_at")) is None
        ):
            raise CatalogError("Retry request record is invalid")
        requests.append(RetryRequest(**row))
    if len({row.request_id for row in requests}) != len(requests):
        raise CatalogError("Retry requests repeat an identifier")
    return requests, wake


def _write(state_dir: Path, rows: list[RetryRequest], wake: str | None) -> None:
    write_state(
        state_dir / "retry-requests.json",
        {
            "schema_version": 1,
            "requests": [asdict(row) for row in rows],
            "wake": wake,
        },
    )


def queue_retries(
    state_dir: Path, taxon_ids: list[int], *, run_now: bool = False
) -> list[RetryRequest]:
    if (
        not 1 <= len(taxon_ids) <= MAX_BATCH
        or any(type(id) is not int or id <= 0 for id in taxon_ids)
        or len(set(taxon_ids)) != len(taxon_ids)
    ):
        raise ValueError("Choose between 1 and 32 distinct species for retry")
    with state_lock(state_dir / "retry-requests.lock"):
        rows, wake = _read(state_dir)
        active = {row.taxon_id: row for row in rows if row.status in {"queued", "preparing"}}
        renewed = set(taxon_ids) - set(active)
        needed = len(renewed)
        # A new explicit request replaces that bird's older outcomes atomically.
        # The replacement remains held until safe preparation completes.
        rows = [row for row in rows if row.taxon_id not in renewed]
        latest = {row.taxon_id: row for row in rows}
        # Unresolved blocked records carry generation safety holds, not history.
        while len(rows) + needed > MAX_REQUESTS:
            removable = next(
                (row for row in rows if latest[row.taxon_id].status in {"eligible", "approved"}),
                None,
            )
            if removable is None:
                raise CatalogError("Retry request queue is full; wait for existing work")
            # Drop a resolved bird's whole history so an older blocked record
            # cannot become its authoritative latest state again.
            rows = [row for row in rows if row.taxon_id != removable.taxon_id]
        selected = []
        now = utc_now()
        for id in taxon_ids:
            row = active.get(id)
            if row is None:
                row = RetryRequest(uuid4().hex, id, now, now, run_now=run_now)
                rows.append(row)
            elif run_now and not row.run_now:
                updated = replace(row, run_now=True)
                rows[rows.index(row)] = updated
                row = updated
            selected.append(row)
        if run_now:
            wake = uuid4().hex
        _write(state_dir, rows, wake)
    return selected


def retry_requests(state_dir: Path) -> list[RetryRequest]:
    return _read(state_dir)[0]


def retry_wake_token(state_dir: Path) -> str | None:
    rows, wake = _read(state_dir)
    return wake if any(row.status == "queued" and row.run_now for row in rows) else None


def retry_wake_available(config: AppConfig) -> bool:
    return config.controller.generation_enabled and state_lock_held(
        config.controller.state_dir / RETRY_WAKE_LISTENER
    )


def complete_operator_retry(state_dir: Path, taxon_id: int) -> None:
    """A successful explicit CLI recovery supersedes its browser preparation hold."""
    with state_lock(state_dir / "retry-requests.lock"):
        rows, wake = _read(state_dir)
        matching = [row for row in rows if row.taxon_id == taxon_id]
        if not matching:
            return
        latest = matching[-1]
        if latest.status not in {"queued", "preparing", "blocked"}:
            return
        _write(
            state_dir,
            [
                replace(row, status="eligible", outcome="prepared", updated_at=utc_now())
                if row.request_id == latest.request_id
                else row
                for row in rows
            ],
            wake,
        )


def prepare_retry_requests(config: AppConfig) -> None:
    """Prepare only with usable discovery, while the controller cycle lock is held."""
    from .controller import ensure_generation_retry, read_generation_work
    from .recovery import retry_species

    state_dir = config.controller.state_dir
    with state_lock(state_dir / "retry-requests.lock"):
        rows, wake = _read(state_dir)
        # A prior process may have archived evidence before losing its reply.
        # Hold that ambiguous operation rather than repeat its side effects.
        reconciled = [
            replace(row, status="blocked", outcome="interrupted", updated_at=utc_now())
            if row.status == "preparing"
            else row
            for row in rows
        ]
        queued = [row for row in reconciled if row.status == "queued"]
        if reconciled != rows:
            _write(state_dir, reconciled, wake)
    if not queued:
        return
    # Use the same freshness/policy gate as generation, before starting any
    # new preparation. An observation outage is a wait, not a species failure.
    work = read_generation_work(config, _include_retry_requests=False)
    if not work.complete:
        return
    actionable = {bird.taxon_id: bird for bird in work.actionable}
    for request in queued:

        def save(
            status: str, outcome: str | None = None, *, request_id: str = request.request_id
        ) -> None:
            with state_lock(state_dir / "retry-requests.lock"):
                current, current_wake = _read(state_dir)
                _write(
                    state_dir,
                    [
                        replace(row, status=status, outcome=outcome, updated_at=utc_now())
                        if row.request_id == request_id
                        else row
                        for row in current
                    ],
                    current_wake,
                )

        save("preparing")
        if has_valid_approved_candidate(config.controller.catalog_dir, request.taxon_id):
            save("approved", "already_approved")
            continue
        pending = find_taxon_directory(state_dir / "pending", request.taxon_id)
        if pending is not None and (pending / "manifest.json").is_file():
            save("blocked", "pending_review")
            continue
        try:
            bird = actionable.get(request.taxon_id)
            if bird is not None:
                ensure_generation_retry(
                    config,
                    bird.taxon_id,
                    bird.common_name,
                    bird.scientific_name,
                    state_dir / "discovery.json",
                )
            else:
                retry_species(config, request.taxon_id, _cycle_locked=True)
        except SpeciesStateError:
            save("blocked", "retained_state")
        except ValueError:
            save("blocked", "not_retryable")
        except (OSError, InkyBirdFrameError):
            save("blocked", "preparation_failed")
        else:
            save("eligible", "prepared")
