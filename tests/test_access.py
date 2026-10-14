from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from inky_bird_frame.access import (
    BOOTSTRAP_SECONDS,
    LOGIN_WINDOW_SECONDS,
    MAX_LOGIN_FAILURES,
    MAX_OWNER_SESSIONS,
    OWNER_ABSOLUTE_SECONDS,
    OWNER_IDLE_SECONDS,
    AccessDeniedError,
    AccessError,
    AccessRateLimitError,
    AccessStore,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
PASSPHRASE = "a long synthetic owner passphrase"


def _establish(store: AccessStore) -> tuple[str, str]:
    path = store.bootstrap(now=NOW)
    secret = path.read_text().strip()
    credentials = store.establish_owner(secret, PASSPHRASE, now=NOW)
    return credentials.secret, credentials.csrf


def test_bootstrap_is_private_one_use_and_does_not_retain_secrets(tmp_path: Path) -> None:
    store = AccessStore(tmp_path)
    path = store.bootstrap(now=NOW)
    secret = path.read_text().strip()
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.directory.stat().st_mode & 0o777 == 0o700
    credentials = store.establish_owner(secret, PASSPHRASE, now=NOW)
    assert store.owner_exists()
    assert not path.exists()
    saved = store.path.read_text()
    for value in (secret, PASSPHRASE, credentials.secret, credentials.csrf):
        assert value not in saved
        assert value not in repr(credentials)
    assert "$argon2id$" in saved
    assert store.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(AccessDeniedError):
        store.establish_owner(secret, PASSPHRASE, now=NOW)
    with pytest.raises(AccessError, match="already exists"):
        store.bootstrap(now=NOW)


def test_expired_bootstrap_never_hashes_a_password(tmp_path: Path) -> None:
    store = AccessStore(tmp_path)
    secret = store.bootstrap(now=NOW).read_text().strip()
    with patch("inky_bird_frame.access._hash_passphrase") as hasher:
        with pytest.raises(AccessDeniedError):
            store.establish_owner(
                secret, PASSPHRASE, now=NOW + timedelta(seconds=BOOTSTRAP_SECONDS)
            )
        hasher.assert_not_called()
    assert not store.owner_exists()


def test_setup_status_tracks_missing_ready_expired_and_established(tmp_path: Path) -> None:
    store = AccessStore(tmp_path)
    missing = store.setup_status(now=NOW)
    assert missing is not None and missing.state == "missing"
    assert missing.expires_in_seconds is None
    secret = store.bootstrap(now=NOW).read_text().strip()
    ready = store.setup_status(now=NOW + timedelta(seconds=1.25))
    assert ready is not None and ready.state == "ready"
    assert ready.expires_in_seconds == BOOTSTRAP_SECONDS - 1
    expired = store.setup_status(now=NOW + timedelta(seconds=BOOTSTRAP_SECONDS))
    assert expired is not None and expired.state == "expired"
    assert expired.expires_in_seconds == 0
    assert secret not in repr(ready)
    store.establish_owner(secret, PASSPHRASE, now=NOW)
    assert store.setup_status(now=NOW) is None


@pytest.mark.parametrize("password", ["short", "x" * 1025])
def test_invalid_passphrase_keeps_bootstrap_usable(tmp_path: Path, password: str) -> None:
    store = AccessStore(tmp_path)
    secret = store.bootstrap(now=NOW).read_text().strip()
    with pytest.raises(AccessError, match="15 and 1024"):
        store.establish_owner(secret, password, now=NOW)
    assert not store.owner_exists()
    store.establish_owner(secret, PASSPHRASE, now=NOW)


def test_login_uses_real_hash_and_logout_is_durable(tmp_path: Path) -> None:
    store = AccessStore(tmp_path)
    _establish(store)
    credentials = store.login(PASSPHRASE, now=NOW)
    restarted = AccessStore(tmp_path)
    restarted.authenticate(credentials.secret, csrf=credentials.csrf, now=NOW)
    with pytest.raises(AccessDeniedError):
        restarted.authenticate(credentials.secret, csrf="wrong", now=NOW)
    restarted.logout(credentials.secret)
    with pytest.raises(AccessDeniedError):
        store.authenticate(credentials.secret, now=NOW)


def test_owner_session_allowance_is_bounded_and_reusable_after_logout(tmp_path: Path) -> None:
    store = AccessStore(tmp_path)
    first, _ = _establish(store)
    for _ in range(MAX_OWNER_SESSIONS - 1):
        store.login(PASSPHRASE, now=NOW)
    with pytest.raises(AccessError, match="Too many owner sessions"):
        store.login(PASSPHRASE, now=NOW)
    store.authenticate(first, now=NOW)
    store.logout(first)
    store.login(PASSPHRASE, now=NOW)


def test_polling_does_not_extend_idle_session(tmp_path: Path) -> None:
    store = AccessStore(tmp_path)
    secret, _ = _establish(store)
    store.authenticate(secret, now=NOW + timedelta(seconds=OWNER_IDLE_SECONDS - 1))
    with pytest.raises(AccessDeniedError):
        store.authenticate(secret, now=NOW + timedelta(seconds=OWNER_IDLE_SECONDS))


def test_owner_action_extends_idle_but_not_absolute_expiry(tmp_path: Path) -> None:
    store = AccessStore(tmp_path)
    secret, csrf = _establish(store)
    for elapsed in range(60, OWNER_ABSOLUTE_SECONDS, 60):
        store.authenticate(secret, csrf=csrf, active=True, now=NOW + timedelta(seconds=elapsed))
    with pytest.raises(AccessDeniedError):
        store.authenticate(secret, now=NOW + timedelta(seconds=OWNER_ABSOLUTE_SECONDS))


def test_clock_rollback_cannot_resurrect_a_session(tmp_path: Path) -> None:
    store = AccessStore(tmp_path)
    secret, _ = _establish(store)
    with pytest.raises(AccessDeniedError):
        store.authenticate(secret, now=NOW - timedelta(seconds=1))
    with pytest.raises(AccessDeniedError):
        AccessStore(tmp_path).authenticate(secret, now=NOW)


def test_expiry_is_durable_even_after_the_clock_moves_back(tmp_path: Path) -> None:
    store = AccessStore(tmp_path)
    secret, _ = _establish(store)
    with pytest.raises(AccessDeniedError):
        store.authenticate(secret, now=NOW + timedelta(seconds=OWNER_IDLE_SECONDS))
    with pytest.raises(AccessDeniedError):
        AccessStore(tmp_path).authenticate(secret, now=NOW)


def test_failure_budget_survives_restart_and_avoids_extra_hashing(tmp_path: Path) -> None:
    store = AccessStore(tmp_path)
    _establish(store)
    for _ in range(MAX_LOGIN_FAILURES):
        with pytest.raises(AccessDeniedError):
            store.login("wrong", now=NOW)
    with patch("inky_bird_frame.access._verify_passphrase") as verify:
        with pytest.raises(AccessRateLimitError):
            AccessStore(tmp_path).login(PASSPHRASE, now=NOW)
        verify.assert_not_called()
    store.login(PASSPHRASE, now=NOW + timedelta(seconds=LOGIN_WINDOW_SECONDS))


def test_concurrent_login_attempts_do_not_exceed_budget(tmp_path: Path) -> None:
    store = AccessStore(tmp_path)
    _establish(store)

    def fail(_: int) -> str:
        try:
            AccessStore(tmp_path).login("wrong", now=NOW)
        except AccessError as exc:
            return type(exc).__name__
        raise AssertionError("wrong password accepted")

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(fail, range(10)))
    assert results.count("AccessDeniedError") == MAX_LOGIN_FAILURES
    assert results.count("AccessRateLimitError") == 10 - MAX_LOGIN_FAILURES


def test_reset_invalidates_all_sessions_but_keeps_other_state(tmp_path: Path) -> None:
    unrelated = tmp_path / "retained-history.json"
    unrelated.write_text("history")
    store = AccessStore(tmp_path)
    secret, _ = _establish(store)
    store.bootstrap(reset=True, now=NOW)
    assert not store.owner_exists()
    assert unrelated.read_text() == "history"
    with pytest.raises(AccessDeniedError):
        store.authenticate(secret, now=NOW)


@pytest.mark.parametrize("kind", ["directory", "file", "lock", "symlink-file", "symlink-directory"])
def test_access_store_rejects_insecure_permissions_and_symlinks(tmp_path: Path, kind: str) -> None:
    store = AccessStore(tmp_path)
    store.bootstrap(now=NOW)
    if kind == "directory":
        store.directory.chmod(0o755)
    elif kind == "file":
        store.path.chmod(0o644)
    elif kind == "lock":
        (store.directory / "access.lock").chmod(0o644)
    elif kind == "symlink-file":
        target = tmp_path / "outside.json"
        store.path.rename(target)
        store.path.symlink_to(target)
    else:
        target = tmp_path / "outside"
        store.directory.rename(target)
        store.directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(AccessError):
        store.owner_exists()


def test_failed_atomic_replacement_preserves_prior_state(tmp_path: Path) -> None:
    store = AccessStore(tmp_path)
    _establish(store)
    before = store.path.read_bytes()
    with (
        patch("inky_bird_frame.http.Path.replace", side_effect=OSError("synthetic private detail")),
        pytest.raises(AccessError) as caught,
    ):
        store.login(PASSPHRASE, now=NOW)
    assert "synthetic private detail" not in str(caught.value)
    assert store.path.read_bytes() == before
    assert not list(store.directory.glob("*.tmp"))


@pytest.mark.parametrize("value", [{"schema_version": True}, {"schema_version": 2}, []])
def test_malformed_state_fails_closed(tmp_path: Path, value: object) -> None:
    store = AccessStore(tmp_path)
    store.bootstrap(now=NOW)
    store.path.write_text(json.dumps(value))
    os.chmod(store.path, 0o600)
    with pytest.raises(AccessError):
        store.owner_exists()
