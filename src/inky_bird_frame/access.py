"""Private owner credentials and revocable browser sessions.

The access store contains hashes, never passphrases or bearer secrets. File
locking serializes password verification as well as state replacement, so
concurrent login attempts cannot multiply Argon2's memory cost.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from math import ceil
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from .errors import InkyBirdFrameError, MissingDependencyError
from .http import write_bytes_atomic, write_json_atomic

if TYPE_CHECKING:
    from argon2 import PasswordHasher

# Owner sessions are for management, not continuous wall viewing. These
# defaults implement the approved 30-minute idle / eight-hour absolute policy.
OWNER_IDLE_SECONDS = 30 * 60
OWNER_ABSOLUTE_SECONDS = 8 * 60 * 60
BOOTSTRAP_SECONDS = 10 * 60
LOGIN_WINDOW_SECONDS = 15 * 60
MAX_LOGIN_FAILURES = 5
# Keep abandoned owner logins bounded. Continuous screens use a different
# scoped grant and do not consume this management-session allowance.
MAX_OWNER_SESSIONS = 16
# A private access file is small. This hard ceiling rejects corrupt or
# malicious input before decoding it; it is not a general history-file limit.
MAX_ACCESS_STATE_BYTES = 256 * 1024
# Password-only owner access uses a minimum of 15 characters. Keep the browser
# guidance and server validation on this same policy.
MIN_PASSWORD_CHARACTERS = 15
MAX_PASSWORD_CHARACTERS = 1024


class AccessError(InkyBirdFrameError):
    """An expected, safe-to-display owner access failure."""


class AccessDeniedError(AccessError):
    """Credentials or a browser session are invalid."""


class AccessRateLimitError(AccessError):
    """The installation's bounded login allowance is exhausted."""


@dataclass(frozen=True)
class OwnerSession:
    secret_hash: str = field(repr=False)
    csrf_hash: str = field(repr=False)
    created_at: float
    last_active_at: float


@dataclass(frozen=True)
class SessionCredentials:
    secret: str = field(repr=False)
    csrf: str = field(repr=False)


@dataclass(frozen=True)
class SetupCodeStatus:
    state: Literal["missing", "ready", "expired"]
    expires_in_seconds: int | None = None


@dataclass
class AccessState:
    owner_hash: str | None = field(default=None, repr=False)
    bootstrap_hash: str | None = field(default=None, repr=False)
    bootstrap_expires_at: float | None = None
    sessions: list[OwnerSession] = field(default_factory=list, repr=False)
    login_failures: list[float] = field(default_factory=list)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def session_csrf_token(secret: str) -> str:
    """Recover the request-forgery token on reload without storing bearer secrets."""
    return _digest("inky-owner-csrf:" + secret)


def _password_hasher() -> PasswordHasher:
    try:
        from argon2 import PasswordHasher
        from argon2.profiles import RFC_9106_LOW_MEMORY
    except ImportError as exc:
        raise MissingDependencyError(
            "Install inky-bird-frame[controller] for owner access"
        ) from exc
    # RFC 9106's low-memory recommendation exceeds the OWASP Argon2id floor.
    return PasswordHasher.from_parameters(RFC_9106_LOW_MEMORY)


def _hash_passphrase(passphrase: str) -> str:
    if not MIN_PASSWORD_CHARACTERS <= len(passphrase) <= MAX_PASSWORD_CHARACTERS:
        raise AccessError(
            f"Use a password between {MIN_PASSWORD_CHARACTERS} and "
            f"{MAX_PASSWORD_CHARACTERS} characters"
        )
    return _password_hasher().hash(passphrase)


def _verify_passphrase(encoded: str, passphrase: str) -> bool:
    hasher = _password_hasher()
    from argon2.exceptions import InvalidHashError, VerificationError

    if len(passphrase) > MAX_PASSWORD_CHARACTERS:
        return False
    try:
        return hasher.verify(encoded, passphrase)
    except (InvalidHashError, VerificationError):
        return False


def _number(value: object) -> float:
    import math

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise AccessError("Owner access state is invalid; restore its private backup")
    if not math.isfinite(value) or value < 0:
        raise AccessError("Owner access state is invalid; restore its private backup")
    return float(value)


def _optional_hash(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise AccessError("Owner access state is invalid; restore its private backup")
    return value


def _parse_state(value: object) -> AccessState:
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int:
        raise AccessError("Owner access state is invalid; restore its private backup")
    if value["schema_version"] != 1:
        raise AccessError("Owner access state uses an unsupported schema")
    raw_sessions = value.get("sessions")
    raw_failures = value.get("login_failures")
    if not isinstance(raw_sessions, list) or not isinstance(raw_failures, list):
        raise AccessError("Owner access state is invalid; restore its private backup")
    if len(raw_sessions) > MAX_OWNER_SESSIONS or len(raw_failures) > MAX_LOGIN_FAILURES:
        raise AccessError("Owner access state exceeds the supported bounds")
    sessions: list[OwnerSession] = []
    for item in raw_sessions:
        if not isinstance(item, dict):
            raise AccessError("Owner access state is invalid; restore its private backup")
        secret_hash = _optional_hash(item.get("secret_hash"))
        csrf_hash = _optional_hash(item.get("csrf_hash"))
        if secret_hash is None or csrf_hash is None:
            raise AccessError("Owner access state is invalid; restore its private backup")
        sessions.append(
            OwnerSession(
                secret_hash,
                csrf_hash,
                _number(item.get("created_at")),
                _number(item.get("last_active_at")),
            )
        )
    expiry = value.get("bootstrap_expires_at")
    state = AccessState(
        owner_hash=_optional_hash(value.get("owner_hash")),
        bootstrap_hash=_optional_hash(value.get("bootstrap_hash")),
        bootstrap_expires_at=_number(expiry) if expiry is not None else None,
        sessions=sessions,
        login_failures=[_number(item) for item in raw_failures],
    )
    if (state.bootstrap_hash is None) != (state.bootstrap_expires_at is None):
        raise AccessError("Owner access state is invalid; restore its private backup")
    return state


class AccessStore:
    """One owner store, shared by the HTTP service and local recovery commands."""

    def __init__(self, state_dir: Path) -> None:
        self.directory = state_dir / "web-access"
        self.path = self.directory / "access.json"

    @contextmanager
    def _locked(self) -> Iterator[AccessState]:
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory_stat = self.directory.lstat()
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or stat.S_IMODE(directory_stat.st_mode) & 0o077
            ):
                raise AccessError("Owner access directory must be private and not a symlink")
            fd = os.open(
                self.directory / "access.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "a+") as handle:
                lock_stat = os.fstat(handle.fileno())
                if not stat.S_ISREG(lock_stat.st_mode) or stat.S_IMODE(lock_stat.st_mode) & 0o077:
                    raise AccessError("Owner access lock must use mode 0600")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield self._read()
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise AccessError(
                "Cannot use private owner access state; check storage and permissions"
            ) from exc

    def _read(self) -> AccessState:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return AccessState()
        with os.fdopen(fd, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise AccessError("Owner access state must be a regular file with mode 0600")
            raw = handle.read(MAX_ACCESS_STATE_BYTES + 1)
        if len(raw) > MAX_ACCESS_STATE_BYTES:
            raise AccessError("Owner access state exceeds the supported size")
        try:
            return _parse_state(cast(object, json.loads(raw)))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AccessError("Owner access state is invalid; restore its private backup") from exc

    def _save(self, state: AccessState) -> None:
        write_json_atomic(self.path, {"schema_version": 1, **asdict(state)}, mode=0o600)

    def owner_exists(self) -> bool:
        with self._locked() as state:
            return state.owner_hash is not None

    def setup_status(self, *, now: datetime | None = None) -> SetupCodeStatus | None:
        """Report setup progress, never the code, its hash, or private file paths."""
        timestamp = (now or datetime.now(UTC)).timestamp()
        with self._locked() as state:
            if state.owner_hash is not None:
                return None
            if state.bootstrap_expires_at is None:
                return SetupCodeStatus("missing")
            remaining = ceil(state.bootstrap_expires_at - timestamp)
            if remaining <= 0:
                return SetupCodeStatus("expired", 0)
            return SetupCodeStatus("ready", remaining)

    def bootstrap(self, *, reset: bool = False, now: datetime | None = None) -> Path:
        """Write a one-use secret to a private file; never return it in CLI JSON."""
        now = now or datetime.now(UTC)
        with self._locked() as state:
            if state.owner_hash is not None and not reset:
                raise AccessError("An owner already exists; use the explicit local reset operation")
            secret = secrets.token_urlsafe(32)
            state.bootstrap_hash = _digest(secret)
            state.bootstrap_expires_at = (now + timedelta(seconds=BOOTSTRAP_SECONDS)).timestamp()
            if reset:
                state.owner_hash = None
                state.sessions.clear()
                state.login_failures.clear()
            secret_path = self.directory / "bootstrap-secret"
            write_bytes_atomic(secret_path, (secret + "\n").encode(), mode=0o600)
            self._save(state)
            return secret_path

    @staticmethod
    def _prune(state: AccessState, now: float) -> None:
        state.sessions = [
            s
            for s in state.sessions
            if 0 <= now - s.last_active_at < OWNER_IDLE_SECONDS
            and 0 <= now - s.created_at < OWNER_ABSOLUTE_SECONDS
        ]
        state.login_failures = [
            t for t in state.login_failures if 0 <= now - t < LOGIN_WINDOW_SECONDS
        ]

    def _credentials(self, state: AccessState, now: float) -> SessionCredentials:
        if len(state.sessions) >= MAX_OWNER_SESSIONS:
            raise AccessError(
                "Too many owner sessions; sign out on another device or reset access locally"
            )
        secret = secrets.token_urlsafe(32)
        credentials = SessionCredentials(secret, session_csrf_token(secret))
        state.sessions.append(
            OwnerSession(_digest(credentials.secret), _digest(credentials.csrf), now, now)
        )
        return credentials

    def establish_owner(
        self, secret: str, passphrase: str, *, now: datetime | None = None
    ) -> SessionCredentials:
        timestamp = (now or datetime.now(UTC)).timestamp()
        with self._locked() as state:
            if (
                state.owner_hash is not None
                or state.bootstrap_hash is None
                or state.bootstrap_expires_at is None
                or timestamp >= state.bootstrap_expires_at
                or not secrets.compare_digest(state.bootstrap_hash, _digest(secret))
            ):
                raise AccessDeniedError(
                    "Setup code is invalid or expired; create a new code on the Inky computer"
                )
            state.owner_hash = _hash_passphrase(passphrase)
            state.bootstrap_hash = None
            state.bootstrap_expires_at = None
            state.login_failures.clear()
            credentials = self._credentials(state, timestamp)
            self._save(state)
            (self.directory / "bootstrap-secret").unlink(missing_ok=True)
            return credentials

    def login(self, passphrase: str, *, now: datetime | None = None) -> SessionCredentials:
        timestamp = (now or datetime.now(UTC)).timestamp()
        with self._locked() as state:
            self._prune(state, timestamp)
            if len(state.login_failures) >= MAX_LOGIN_FAILURES:
                raise AccessRateLimitError(
                    "Too many sign-in attempts; wait 15 minutes before trying again"
                )
            if state.owner_hash is None or not _verify_passphrase(state.owner_hash, passphrase):
                state.login_failures.append(timestamp)
                self._save(state)
                raise AccessDeniedError("Sign-in failed; check your Inky password")
            state.login_failures.clear()
            credentials = self._credentials(state, timestamp)
            self._save(state)
            return credentials

    def authenticate(
        self,
        secret: str,
        *,
        csrf: str | None = None,
        active: bool = False,
        now: datetime | None = None,
    ) -> OwnerSession:
        timestamp = (now or datetime.now(UTC)).timestamp()
        with self._locked() as state:
            prior_sessions = len(state.sessions)
            self._prune(state, timestamp)
            if len(state.sessions) != prior_sessions:
                # Expiry is revocation, not a transient calculation. Persist it
                # so a later clock correction cannot resurrect old authority.
                self._save(state)
            session = next(
                (
                    s
                    for s in state.sessions
                    if secrets.compare_digest(s.secret_hash, _digest(secret))
                ),
                None,
            )
            if session is None or (
                csrf is not None and not secrets.compare_digest(session.csrf_hash, _digest(csrf))
            ):
                raise AccessDeniedError("Sign in again; this owner session is no longer valid")
            if active:
                state.sessions[state.sessions.index(session)] = OwnerSession(
                    session.secret_hash, session.csrf_hash, session.created_at, timestamp
                )
                self._save(state)
            return session

    def logout(self, secret: str) -> None:
        with self._locked() as state:
            state.sessions = [
                s
                for s in state.sessions
                if not secrets.compare_digest(s.secret_hash, _digest(secret))
            ]
            self._save(state)
