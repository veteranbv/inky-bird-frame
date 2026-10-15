"""Private, revocable presentation grants. No grant can perform owner actions."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import unicodedata
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from .access import AccessDeniedError, AccessRateLimitError
from .catalog import catalog_state_lock
from .errors import DataSourceError
from .http import read_private_json, write_bytes_atomic
from .journal import JournalConflictError, JournalError, JournalService, JournalViewRemovedError

# One household, not a fleet: pending requests expire in ten minutes; credentials
# last at most a year. A lost screen can always be revoked from the owner app.
MAX_GRANTS = 32
MAX_PENDING = 8
PAIR_SECONDS = 10 * 60
GRANT_DAYS = 90
MAX_STATE_BYTES = 256 * 1024
HEX = re.compile(r"[0-9a-f]{64}\Z")
ID = re.compile(r"[0-9a-f]{32}\Z")


@dataclass(frozen=True)
class PresentationGrant:
    id: str
    secret_hash: str
    kind: Literal["pending", "viewer", "status"]
    name: str
    view: str
    created_at: float
    expires_at: float
    last_seen_at: float = 0
    pairing_code: str = ""


@dataclass(frozen=True)
class PresentationState:
    revision: int = 0
    wall_view: str = "current"
    dashboard_view: str | None = None
    grants: tuple[PresentationGrant, ...] = ()
    last_pair_request_at: float = 0


def _name(value: object) -> str:
    if not isinstance(value, str):
        raise JournalError("Use a visible name, not another value type")
    name = unicodedata.normalize("NFC", value.strip())
    if not 1 <= len(name) <= 128 or any(unicodedata.category(c).startswith("C") for c in name):
        raise JournalError("Use a name with 1–128 visible characters")
    return name


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _view(value: object) -> str:
    if (
        not isinstance(value, str)
        or value not in {"current", "saved", "favorites", "all"}
        and not ID.fullmatch(value)
    ):
        raise JournalError("Choose an existing view")
    return value


class PresentationAccess:
    def __init__(self, state_dir: Path, journal: JournalService) -> None:
        self.state_dir, self.journal = state_dir, journal
        self.path = state_dir / "presentation-access.json"

    def _load(self) -> PresentationState:
        if not self.path.exists() and not self.path.is_symlink():
            return PresentationState()
        try:
            raw = read_private_json(self.path, maximum=MAX_STATE_BYTES)
            if (
                not isinstance(raw, dict)
                or set(raw)
                != {
                    "schema_version",
                    "revision",
                    "wall_view",
                    "dashboard_view",
                    "grants",
                    "last_pair_request_at",
                }
                or type(raw["schema_version"]) is not int
                or raw["schema_version"] != 1
            ):
                raise ValueError("shape")
            if (
                type(raw["revision"]) is not int
                or raw["revision"] < 0
                or not isinstance(raw["grants"], list)
                or len(raw["grants"]) > MAX_GRANTS
            ):
                raise ValueError("bound")
            if (
                type(raw["last_pair_request_at"]) not in {int, float}
                or not isfinite(raw["last_pair_request_at"])
                or not 0 <= raw["last_pair_request_at"] < 10**11
            ):
                raise ValueError("timestamp")
            grants: list[PresentationGrant] = []
            for item in raw["grants"]:
                if not isinstance(item, dict) or set(item) != set(
                    PresentationGrant.__dataclass_fields__
                ):
                    raise ValueError("grant")
                grant = PresentationGrant(**item)
                if (
                    not ID.fullmatch(grant.id)
                    or not HEX.fullmatch(grant.secret_hash)
                    or grant.kind not in {"pending", "viewer", "status"}
                    or _name(grant.name) != grant.name
                    or any(
                        type(stamp) not in {int, float} or not 0 <= stamp < 10**11
                        for stamp in (grant.created_at, grant.expires_at, grant.last_seen_at)
                    )
                    or grant.expires_at <= grant.created_at
                ):
                    raise ValueError("grant")
                _view(grant.view)
                if (
                    not isinstance(grant.pairing_code, str)
                    or (
                        grant.kind == "pending"
                        and not re.fullmatch(r"[A-F0-9]{8}", grant.pairing_code)
                    )
                    or grant.kind != "pending"
                    and grant.pairing_code
                ):
                    raise ValueError("pairing")
                grants.append(grant)
            if len({g.id for g in grants}) != len(grants) or len(
                {g.secret_hash for g in grants}
            ) != len(grants):
                raise ValueError("duplicate")
            return PresentationState(
                raw["revision"],
                _view(raw["wall_view"]),
                None if raw["dashboard_view"] is None else _view(raw["dashboard_view"]),
                tuple(grants),
                raw["last_pair_request_at"],
            )
        except (DataSourceError, ValueError, TypeError) as exc:
            raise JournalError(
                "Presentation access state is invalid. Restore its matching private backup."
            ) from exc

    def _save(self, state: PresentationState) -> None:
        encoded = json.dumps({"schema_version": 1, **asdict(state)}, sort_keys=True).encode()
        if len(encoded) > MAX_STATE_BYTES:
            raise JournalError("Presentation access storage is full")
        write_bytes_atomic(self.path, encoded, mode=0o600)

    @staticmethod
    def _current(state: PresentationState, now: float) -> list[PresentationGrant]:
        return [g for g in state.grants if g.expires_at > now]

    def status(self) -> dict[str, object]:
        with catalog_state_lock(self.state_dir):
            state = self._load()
            now = datetime.now(UTC).timestamp()
            return {
                "revision": state.revision,
                "wall_view": state.wall_view,
                "dashboard_view": state.dashboard_view,
                "grants": [
                    {key: value for key, value in asdict(g).items() if key != "secret_hash"}
                    for g in self._current(state, now)
                ],
            }

    def request_pair(self, existing: str) -> tuple[str, dict[str, object]]:
        with catalog_state_lock(self.state_dir):
            state = self._load()
            now = datetime.now(UTC).timestamp()
            grants = self._current(state, now)
            current = (
                next(
                    (g for g in grants if secrets.compare_digest(g.secret_hash, _hash(existing))),
                    None,
                )
                if existing
                else None
            )
            if current is not None and current.kind in {"pending", "viewer"}:
                return existing, {"status": current.kind, "pairing_code": current.pairing_code}
            if (
                len(grants) >= MAX_GRANTS
                or sum(g.kind == "pending" for g in grants) >= MAX_PENDING
                or now - state.last_pair_request_at < 15
            ):
                raise AccessRateLimitError(
                    "Pairing is busy. Wait a moment or remove an unused request in Displays."
                )
            secret = secrets.token_urlsafe(32)
            grant = PresentationGrant(
                uuid4().hex,
                _hash(secret),
                "pending",
                "New screen",
                "current",
                now,
                now + PAIR_SECONDS,
                pairing_code=secrets.token_hex(4).upper(),
            )
            self._save(
                replace(
                    state,
                    revision=state.revision + 1,
                    grants=(*grants, grant),
                    last_pair_request_at=now,
                )
            )
            return secret, {
                "status": "pending",
                "pairing_code": grant.pairing_code,
                "expires_in_seconds": PAIR_SECONDS,
            }

    def update(
        self,
        *,
        revision: object,
        action: str,
        id: str = "",
        name: str = "",
        view: str = "current",
        days: object = GRANT_DAYS,
        confirm: bool = False,
    ) -> dict[str, object]:
        if type(revision) is not int or not confirm:
            raise JournalError("Review and confirm this presentation change")
        selected = _view(view)
        # No nested state locks: validate the view before acquiring access state.
        if action in {"pair", "wall", "dashboard", "status"}:
            self.journal.view_entries(selected)
        if action in {"pair", "status"} and (type(days) is not int or not 1 <= days <= 365):
            raise JournalError("Choose an expiry from 1 to 365 days")
        with catalog_state_lock(self.state_dir):
            state = self._load()
            now = datetime.now(UTC).timestamp()
            if state.revision != revision:
                raise JournalConflictError("Screen access changed. Reload before confirming.")
            grants = self._current(state, now)
            result: dict[str, object] = {"updated": True}
            if action == "pair":
                pending = next((g for g in grants if g.id == id and g.kind == "pending"), None)
                if pending is None:
                    raise JournalConflictError(
                        "This pairing request expired. Request it again on that screen."
                    )
                grants[grants.index(pending)] = replace(
                    pending,
                    kind="viewer",
                    name=_name(name),
                    view=selected,
                    pairing_code="",
                    expires_at=now + cast(int, days) * 86400,
                )
            elif action == "revoke":
                if not any(g.id == id for g in grants):
                    raise JournalConflictError("This access was already removed or expired")
                grants = [g for g in grants if g.id != id]
            elif action == "status":
                if len(grants) >= MAX_GRANTS:
                    raise JournalError("Remove an unused credential or screen first")
                secret = secrets.token_urlsafe(32)
                grant = PresentationGrant(
                    uuid4().hex,
                    _hash(secret),
                    "status",
                    _name(name),
                    selected,
                    now,
                    now + cast(int, days) * 86400,
                )
                grants.append(grant)
                result = {"id": grant.id, "secret": secret, "expires_at": grant.expires_at}
            elif action == "wall":
                state = replace(state, wall_view=selected)
            elif action == "dashboard":
                state = replace(state, dashboard_view=selected)
            elif action == "dashboard-off":
                state = replace(state, dashboard_view=None)
            else:
                raise JournalError("Choose a supported presentation action")
            self._save(replace(state, revision=state.revision + 1, grants=tuple(grants)))
            return result

    def authenticate(self, secret: str, kind: Literal["viewer", "status"]) -> PresentationGrant:
        if not isinstance(secret, str) or not 32 <= len(secret) <= 128:
            raise AccessDeniedError("Screen or status access expired or was revoked")
        with catalog_state_lock(self.state_dir):
            state = self._load()
            now = datetime.now(UTC).timestamp()
            grant = next(
                (
                    g
                    for g in self._current(state, now)
                    if g.kind == kind and secrets.compare_digest(g.secret_hash, _hash(secret))
                ),
                None,
            )
            if grant is None:
                raise AccessDeniedError("Screen or status access expired or was revoked")
            # Connection telemetry has hour precision; polling does not rewrite
            # flash every minute and never extends the credential's lifetime.
            if now - grant.last_seen_at >= 3600:
                grants = tuple(
                    replace(g, last_seen_at=now) if g.id == grant.id else g for g in state.grants
                )
                self._save(replace(state, grants=grants))
            return grant

    def selected(self, destination: Literal["wall", "dashboard"]) -> list[dict[str, object]]:
        with catalog_state_lock(self.state_dir):
            state = self._load()
            view = state.wall_view if destination == "wall" else state.dashboard_view
        if view is None:
            raise AccessDeniedError("Dashboard art is off")
        try:
            return self.journal.view_entries(view, wall_detection_times=destination == "wall")
        except JournalViewRemovedError:
            return []
