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
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from math import ceil
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from .errors import InkyBirdFrameError, MissingDependencyError
from .http import write_bytes_atomic, write_json_atomic

if TYPE_CHECKING:
    from argon2 import PasswordHasher

# A normal login lasts one working day. Remembered logins last at most a
# month, with a week of inactivity allowed for occasional birding visits.
# Background polling does not renew either limit. Wall screens use read-only grants.
OWNER_IDLE_SECONDS = 12 * 60 * 60
OWNER_ABSOLUTE_SECONDS = 24 * 60 * 60
REMEMBERED_IDLE_SECONDS = 7 * 24 * 60 * 60
REMEMBERED_ABSOLUTE_SECONDS = 30 * 24 * 60 * 60
# Password confirmation authorizes account changes for ten minutes only.
RECENT_AUTH_SECONDS = 10 * 60
OIDC_TRANSACTION_SECONDS = 10 * 60
MAX_OIDC_TRANSACTIONS = 16
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


class AccessConfirmationRequiredError(AccessDeniedError):
    """The account is signed in but a sensitive action needs fresh authentication."""


@dataclass(frozen=True)
class OwnerSession:
    secret_hash: str = field(repr=False)
    csrf_hash: str = field(repr=False)
    created_at: float
    last_active_at: float
    remembered: bool = False
    authenticated_at: float = 0
    provider_id: str | None = None
    idle_seconds: int = OWNER_IDLE_SECONDS
    absolute_seconds: int = OWNER_ABSOLUTE_SECONDS

    @property
    def expires_at(self) -> float:
        return self.created_at + self.absolute_seconds


@dataclass(frozen=True)
class SessionCredentials:
    secret: str = field(repr=False)
    csrf: str = field(repr=False)
    remembered: bool = False


@dataclass(frozen=True)
class SetupCodeStatus:
    state: Literal["missing", "ready", "expired"]
    expires_in_seconds: int | None = None


@dataclass(frozen=True)
class OIDCIdentity:
    provider_id: str
    issuer: str
    subject: str = field(repr=False)


@dataclass(frozen=True)
class OIDCTransaction:
    state: str = field(repr=False)
    browser_hash: str = field(repr=False)
    nonce: str = field(repr=False)
    verifier: str = field(repr=False)
    provider_id: str
    created_at: float
    remembered: bool
    owner_session_hash: str | None = field(default=None, repr=False)
    access_revision: int = 0
    return_to_access: bool = False


@dataclass
class AccessState:
    owner_hash: str | None = field(default=None, repr=False)
    bootstrap_hash: str | None = field(default=None, repr=False)
    bootstrap_expires_at: float | None = None
    sessions: list[OwnerSession] = field(default_factory=list, repr=False)
    login_failures: list[float] = field(default_factory=list)
    username: str | None = None
    oidc_identity: OIDCIdentity | None = field(default=None, repr=False)
    oidc_transactions: list[OIDCTransaction] = field(default_factory=list, repr=False)
    access_revision: int = 0
    oidc_verified_provider_id: str | None = None


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


def normalize_username(value: str) -> str:
    username = value.strip().casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,63}", username):
        raise AccessError(
            "Use 3 to 64 letters, numbers, dots, hyphens or underscores for your username"
        )
    return username


def _parse_state(value: object) -> AccessState:
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int:
        raise AccessError("Owner access state is invalid; restore its private backup")
    if value["schema_version"] not in {1, 2}:
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
                _boolean(item.get("remembered", False)),
                _number(item.get("authenticated_at", item.get("created_at"))),
                _optional_hash(item.get("provider_id")),
                _session_limit(
                    item.get("idle_seconds"),
                    value["schema_version"],
                    _boolean(item.get("remembered", False)),
                    idle=True,
                ),
                _session_limit(
                    item.get("absolute_seconds"),
                    value["schema_version"],
                    _boolean(item.get("remembered", False)),
                    idle=False,
                ),
            )
        )
    expiry = value.get("bootstrap_expires_at")
    state = AccessState(
        owner_hash=_optional_hash(value.get("owner_hash")),
        bootstrap_hash=_optional_hash(value.get("bootstrap_hash")),
        bootstrap_expires_at=_number(expiry) if expiry is not None else None,
        sessions=sessions,
        login_failures=[_number(item) for item in raw_failures],
        username=normalize_username(_text(value["username"]))
        if value.get("username") is not None
        else None,
        oidc_identity=_identity(value.get("oidc_identity")),
        oidc_transactions=_transactions(value.get("oidc_transactions", [])),
        access_revision=_revision(value.get("access_revision", 0)),
        oidc_verified_provider_id=_optional_hash(value.get("oidc_verified_provider_id")),
    )
    if (state.bootstrap_hash is None) != (state.bootstrap_expires_at is None):
        raise AccessError("Owner access state is invalid; restore its private backup")
    return state


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise AccessError("Owner access state is invalid; restore its private backup")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise AccessError("Owner access state is invalid; restore its private backup")
    return value


def _revision(value: object) -> int:
    if type(value) is not int or value < 0:
        raise AccessError("Owner access revision is invalid")
    return value


def _session_limit(value: object, schema: int, remembered: bool, *, idle: bool) -> int:
    if value is None:
        if schema == 1:
            return 30 * 60 if idle else 8 * 60 * 60
        return (
            (REMEMBERED_IDLE_SECONDS if remembered else OWNER_IDLE_SECONDS)
            if idle
            else (REMEMBERED_ABSOLUTE_SECONDS if remembered else OWNER_ABSOLUTE_SECONDS)
        )
    allowed = (
        {30 * 60, OWNER_IDLE_SECONDS, REMEMBERED_IDLE_SECONDS}
        if idle
        else {8 * 60 * 60, OWNER_ABSOLUTE_SECONDS, REMEMBERED_ABSOLUTE_SECONDS}
    )
    if type(value) is not int or value not in allowed:
        raise AccessError("Owner access session policy is invalid")
    return value


def _identity(value: object) -> OIDCIdentity | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AccessError("Owner identity state is invalid; restore its private backup")
    return OIDCIdentity(
        _text(value.get("provider_id")), _text(value.get("issuer")), _text(value.get("subject"))
    )


def _transactions(value: object) -> list[OIDCTransaction]:
    if not isinstance(value, list) or len(value) > MAX_OIDC_TRANSACTIONS:
        raise AccessError("Sign-in transaction state exceeds the supported bounds")
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise AccessError("Sign-in transaction state is invalid")
        result.append(
            OIDCTransaction(
                _text(item.get("state")),
                _text(item.get("browser_hash")),
                _text(item.get("nonce")),
                _text(item.get("verifier")),
                _text(item.get("provider_id")),
                _number(item.get("created_at")),
                _boolean(item.get("remembered")),
                _optional_hash(item.get("owner_session_hash")),
                _revision(item.get("access_revision", 0)),
                _boolean(item.get("return_to_access", False)),
            )
        )
    return result


class AccessStore:
    """One owner store, shared by the HTTP service and local recovery commands."""

    def __init__(
        self,
        state_dir: Path,
        *,
        oidc_provider_id: str | None = None,
        password_login_enabled: bool = True,
    ) -> None:
        self.directory = state_dir / "web-access"
        self.path = self.directory / "access.json"
        self.oidc_provider_id = oidc_provider_id
        self.password_login_enabled = password_login_enabled

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
        write_json_atomic(self.path, {"schema_version": 2, **asdict(state)}, mode=0o600)

    def owner_exists(self) -> bool:
        with self._locked() as state:
            return state.owner_hash is not None

    def legacy_account(self) -> bool:
        with self._locked() as state:
            return state.owner_hash is not None and state.username is None

    def register_owner(
        self,
        username: str,
        passphrase: str,
        *,
        remembered: bool = False,
        now: datetime | None = None,
    ) -> SessionCredentials:
        """Claim the first local account once, under the same durable lock as login."""
        timestamp = (now or datetime.now(UTC)).timestamp()
        name = normalize_username(username)
        with self._locked() as state:
            if state.owner_hash is not None:
                raise AccessDeniedError("An owner already exists; sign in instead")
            state.owner_hash = _hash_passphrase(passphrase)
            state.username = name
            state.bootstrap_hash = None
            state.bootstrap_expires_at = None
            state.login_failures.clear()
            credentials = self._credentials(state, timestamp, remembered=remembered)
            self._save(state)
            (self.directory / "bootstrap-secret").unlink(missing_ok=True)
            return credentials

    def recover_owner(self, username: str, passphrase: str) -> None:
        """Local operator recovery: never leave registration open on the network."""
        name = normalize_username(username)
        encoded = _hash_passphrase(passphrase)
        with self._locked() as state:
            state.owner_hash = encoded
            state.username = name
            state.bootstrap_hash = None
            state.bootstrap_expires_at = None
            state.sessions.clear()
            state.login_failures.clear()
            state.oidc_identity = None
            state.oidc_transactions.clear()
            state.access_revision += 1
            state.oidc_verified_provider_id = None
            self._save(state)
            (self.directory / "bootstrap-secret").unlink(missing_ok=True)

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
        if reset:
            raise AccessError(
                "Use access recover in a private terminal; reset must not reopen setup"
            )
        now = now or datetime.now(UTC)
        with self._locked() as state:
            if state.owner_hash is not None:
                raise AccessError(
                    "An owner already exists; use access recover in a private terminal"
                )
            secret = secrets.token_urlsafe(32)
            state.bootstrap_hash = _digest(secret)
            state.bootstrap_expires_at = (now + timedelta(seconds=BOOTSTRAP_SECONDS)).timestamp()
            secret_path = self.directory / "bootstrap-secret"
            write_bytes_atomic(secret_path, (secret + "\n").encode(), mode=0o600)
            self._save(state)
            return secret_path

    def _prune(self, state: AccessState, now: float) -> None:
        counts = (len(state.sessions), len(state.login_failures), len(state.oidc_transactions))
        state.sessions = [
            s
            for s in state.sessions
            if 0 <= now - s.last_active_at < s.idle_seconds
            and s.created_at <= now < s.expires_at
            and (
                (s.provider_id is None and self.password_login_enabled)
                or (s.provider_id is not None and s.provider_id == self.oidc_provider_id)
            )
        ]
        state.login_failures = [
            t for t in state.login_failures if 0 <= now - t < LOGIN_WINDOW_SECONDS
        ]
        state.oidc_transactions = [
            t
            for t in state.oidc_transactions
            if 0 <= now - t.created_at < OIDC_TRANSACTION_SECONDS
            and t.provider_id == self.oidc_provider_id
        ]
        if counts != (len(state.sessions), len(state.login_failures), len(state.oidc_transactions)):
            # Expiry stays revoked if a later permission check fails or the clock rolls back.
            self._save(state)

    def _credentials(
        self,
        state: AccessState,
        now: float,
        *,
        remembered: bool = False,
        provider_id: str | None = None,
        authenticated_at: float | None = None,
    ) -> SessionCredentials:
        if len(state.sessions) >= MAX_OWNER_SESSIONS:
            raise AccessError(
                "Too many owner sessions; sign out on another device or use access recover locally"
            )
        secret = secrets.token_urlsafe(32)
        credentials = SessionCredentials(secret, session_csrf_token(secret), remembered)
        state.sessions.append(
            OwnerSession(
                _digest(credentials.secret),
                _digest(credentials.csrf),
                now,
                now,
                remembered,
                now if authenticated_at is None else authenticated_at,
                provider_id,
                REMEMBERED_IDLE_SECONDS if remembered else OWNER_IDLE_SECONDS,
                REMEMBERED_ABSOLUTE_SECONDS if remembered else OWNER_ABSOLUTE_SECONDS,
            )
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

    def login(
        self,
        passphrase: str,
        *,
        username: str | None = None,
        remembered: bool = False,
        now: datetime | None = None,
    ) -> SessionCredentials:
        timestamp = (now or datetime.now(UTC)).timestamp()
        with self._locked() as state:
            self._prune(state, timestamp)
            if len(state.login_failures) >= MAX_LOGIN_FAILURES:
                raise AccessRateLimitError(
                    "Too many sign-in attempts; wait 15 minutes before trying again"
                )
            correct_password = state.owner_hash is not None and _verify_passphrase(
                state.owner_hash, passphrase
            )
            correct_name = state.username is None or (
                username is not None
                and username.isascii()
                and secrets.compare_digest(state.username, username.strip().casefold())
            )
            if not correct_password or not correct_name:
                state.login_failures.append(timestamp)
                self._save(state)
                raise AccessDeniedError("Sign-in failed; check your username and Inky password")
            state.login_failures.clear()
            credentials = self._credentials(state, timestamp, remembered=remembered)
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
            self._prune(state, timestamp)
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
                state.sessions[state.sessions.index(session)] = replace(
                    session, last_active_at=timestamp
                )
                self._save(state)
            return session

    def confirm_password(
        self, secret: str, passphrase: str, *, now: datetime | None = None
    ) -> None:
        timestamp = (now or datetime.now(UTC)).timestamp()
        with self._locked() as state:
            self._prune(state, timestamp)
            if len(state.login_failures) >= MAX_LOGIN_FAILURES:
                raise AccessRateLimitError(
                    "Too many sign-in attempts; wait 15 minutes before trying again"
                )
            session = next(
                (
                    s
                    for s in state.sessions
                    if secrets.compare_digest(s.secret_hash, _digest(secret))
                ),
                None,
            )
            if session is None:
                self._save(state)
                raise AccessDeniedError("Sign in again before changing your account")
            if state.owner_hash is None or not _verify_passphrase(state.owner_hash, passphrase):
                state.login_failures.append(timestamp)
                self._save(state)
                raise AccessDeniedError("Password confirmation failed")
            state.login_failures.clear()
            state.sessions[state.sessions.index(session)] = replace(
                session, authenticated_at=timestamp
            )
            self._save(state)

    def sessions(self, secret: str) -> list[dict[str, object]]:
        self.authenticate(secret)
        with self._locked() as state:
            return [
                {
                    "id": s.secret_hash,
                    "current": secrets.compare_digest(s.secret_hash, _digest(secret)),
                    "created_at": s.created_at,
                    "last_active_at": s.last_active_at,
                    "expires_at": s.expires_at,
                    "remembered": s.remembered,
                }
                for s in state.sessions
            ]

    def account_username(self, secret: str) -> str | None:
        self.authenticate(secret)
        with self._locked() as state:
            return state.username

    def update_account(
        self, secret: str, username: str, passphrase: str | None = None
    ) -> SessionCredentials:
        name = normalize_username(username)
        timestamp = datetime.now(UTC).timestamp()
        with self._locked() as state:
            self._prune(state, timestamp)
            session = self._recent_session(state, _digest(secret), timestamp)
            if passphrase is not None:
                state.owner_hash = _hash_passphrase(passphrase)
            state.username = name
            # Rotate this device's secret too: copied pre-change cookies must not survive.
            state.sessions.clear()
            credentials = self._credentials(
                state, timestamp, remembered=session.remembered, provider_id=session.provider_id
            )
            state.oidc_transactions.clear()
            state.access_revision += 1
            self._save(state)
            return credentials

    def revoke_session(self, secret: str, session_id: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", session_id):
            raise AccessError("Choose a sign-in from the device list")
        timestamp = datetime.now(UTC).timestamp()
        with self._locked() as state:
            self._prune(state, timestamp)
            self._recent_session(state, _digest(secret), timestamp)
            state.sessions = [
                s for s in state.sessions if not secrets.compare_digest(s.secret_hash, session_id)
            ]
            self._save(state)

    @staticmethod
    def _recent_session(state: AccessState, secret_hash: str, timestamp: float) -> OwnerSession:
        session = next(
            (s for s in state.sessions if secrets.compare_digest(s.secret_hash, secret_hash)), None
        )
        if session is None:
            raise AccessDeniedError("Sign in again before changing account access")
        if not 0 <= timestamp - session.authenticated_at < RECENT_AUTH_SECONDS:
            raise AccessConfirmationRequiredError(
                "Confirm your sign-in before changing account access"
            )
        return session

    def oidc_linked(self, issuer: str, configured_subject: str | None = None) -> bool:
        with self._locked() as state:
            if state.owner_hash is None:
                return False
            identity = state.oidc_identity
            return configured_subject is not None or (
                identity is not None
                and identity.issuer == issuer
                and identity.provider_id == self.oidc_provider_id
            )

    def oidc_tested(self) -> bool:
        with self._locked() as state:
            return (
                self.oidc_provider_id is not None
                and state.oidc_verified_provider_id == self.oidc_provider_id
            )

    def begin_oidc(
        self,
        *,
        remembered: bool = False,
        link_secret: str | None = None,
        return_to_access: bool = False,
        now: datetime | None = None,
    ) -> tuple[OIDCTransaction, str]:
        timestamp = (now or datetime.now(UTC)).timestamp()
        with self._locked() as state:
            self._prune(state, timestamp)
            if self.oidc_provider_id is None or state.owner_hash is None:
                raise AccessDeniedError(
                    "Create your local owner account before linking single sign-on"
                )
            owner_hash = _digest(link_secret) if link_secret else None
            if owner_hash:
                self._recent_session(state, owner_hash, timestamp)
            if len(state.oidc_transactions) >= MAX_OIDC_TRANSACTIONS:
                raise AccessRateLimitError(
                    "Too many sign-in requests; wait ten minutes before trying again"
                )
            browser_secret = secrets.token_urlsafe(32)
            transaction = OIDCTransaction(
                secrets.token_urlsafe(32),
                _digest(browser_secret),
                secrets.token_urlsafe(32),
                secrets.token_urlsafe(48),
                self.oidc_provider_id,
                timestamp,
                remembered,
                owner_hash,
                state.access_revision,
                return_to_access,
            )
            state.oidc_transactions.append(transaction)
            self._save(state)
            return transaction, browser_secret

    def consume_oidc(
        self,
        state_value: str,
        browser_secret: str,
        *,
        now: datetime | None = None,
    ) -> OIDCTransaction:
        timestamp = (now or datetime.now(UTC)).timestamp()
        if not state_value.isascii() or len(state_value) > 128:
            raise AccessDeniedError("Invalid sign-in response; start sign-in again")
        with self._locked() as state:
            self._prune(state, timestamp)
            tx = next(
                (
                    t
                    for t in state.oidc_transactions
                    if secrets.compare_digest(t.state, state_value)
                    and secrets.compare_digest(t.browser_hash, _digest(browser_secret))
                ),
                None,
            )
            if tx is None:
                self._save(state)
                raise AccessDeniedError(
                    "This sign-in request expired or was already used. Start sign-in again."
                )
            state.oidc_transactions.remove(tx)
            self._save(state)
            return tx

    def complete_oidc(
        self,
        tx: OIDCTransaction,
        issuer: str,
        subject: str,
        *,
        authenticated_at: float,
        configured_subject: str | None = None,
        now: datetime | None = None,
    ) -> SessionCredentials:
        timestamp = (now or datetime.now(UTC)).timestamp()
        if not 0 <= timestamp - authenticated_at < RECENT_AUTH_SECONDS:
            raise AccessDeniedError("Single sign-on needs recent provider authentication")
        with self._locked() as state:
            self._prune(state, timestamp)
            if (
                tx.provider_id != self.oidc_provider_id
                or state.owner_hash is None
                or tx.access_revision != state.access_revision
            ):
                raise AccessDeniedError("Single sign-on configuration changed; start sign-in again")
            if tx.owner_session_hash is not None:
                self._recent_session(state, tx.owner_session_hash, timestamp)
                if configured_subject is not None and not secrets.compare_digest(
                    configured_subject, subject
                ):
                    raise AccessDeniedError("This identity is not the configured Inky owner")
                state.oidc_identity = OIDCIdentity(tx.provider_id, issuer, subject)
                state.sessions = [s for s in state.sessions if s.provider_id is None]
                state.oidc_transactions.clear()
                state.access_revision += 1
            else:
                allowed = configured_subject
                identity = state.oidc_identity
                if (
                    allowed is None
                    and identity is not None
                    and identity.provider_id == tx.provider_id
                    and identity.issuer == issuer
                ):
                    allowed = identity.subject
                if allowed is None or not secrets.compare_digest(allowed, subject):
                    raise AccessDeniedError("This provider account is not linked to the Inky owner")
            credentials = self._credentials(
                state,
                timestamp,
                remembered=tx.remembered,
                provider_id=tx.provider_id,
                authenticated_at=authenticated_at,
            )
            state.oidc_verified_provider_id = tx.provider_id
            self._save(state)
            return credentials

    def unlink_oidc(self, secret: str) -> None:
        timestamp = datetime.now(UTC).timestamp()
        with self._locked() as state:
            self._prune(state, timestamp)
            self._recent_session(state, _digest(secret), timestamp)
            state.oidc_identity = None
            state.oidc_transactions.clear()
            state.sessions = [s for s in state.sessions if s.provider_id is None]
            state.access_revision += 1
            state.oidc_verified_provider_id = None
            self._save(state)

    def logout(self, secret: str) -> None:
        with self._locked() as state:
            state.sessions = [
                s
                for s in state.sessions
                if not secrets.compare_digest(s.secret_hash, _digest(secret))
            ]
            self._save(state)
