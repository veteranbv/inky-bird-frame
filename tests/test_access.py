from __future__ import annotations

import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest

from inky_bird_frame.access import (
    BOOTSTRAP_SECONDS,
    LOGIN_WINDOW_SECONDS,
    MAX_LOGIN_FAILURES,
    MAX_OWNER_SESSIONS,
    OIDC_TRANSACTION_SECONDS,
    OWNER_ABSOLUTE_SECONDS,
    OWNER_IDLE_SECONDS,
    RECENT_AUTH_SECONDS,
    REMEMBERED_ABSOLUTE_SECONDS,
    REMEMBERED_IDLE_SECONDS,
    AccessDeniedError,
    AccessError,
    AccessRateLimitError,
    AccessState,
    AccessStore,
    SessionCredentials,
    prepare_browser_registration,
)
from inky_bird_frame.config import load_config
from inky_bird_frame.settings import initialize_config


def _owner_session(
    store: AccessStore,
    username: str,
    password: str,
    *,
    remembered: bool = False,
    now: datetime | None = None,
) -> SessionCredentials:
    """Session-policy tests use trusted local setup, not an implicit network claim."""
    store.setup_owner(username, password)
    return store.login(password, username=username, remembered=remembered, now=now)


def _prepared_store(root: Path) -> AccessStore:
    path = root / "config.toml"
    assert initialize_config(path)
    return AccessStore(load_config(path, load_secrets=False).controller.state_dir)


@pytest.mark.parametrize("schema", [None, 1, 2])
def test_unprepared_or_legacy_state_never_opens_browser_registration(
    tmp_path: Path, schema: int | None
) -> None:
    store = AccessStore(tmp_path)
    if schema is not None:
        with store._locked() as state:
            store._save(state)
        raw = json.loads(store.path.read_text())
        raw.pop("allow_registration")
        raw["schema_version"] = schema
        store.path.write_text(json.dumps(raw))
    before = store.path.read_bytes() if store.path.exists() else None
    with patch("inky_bird_frame.access._hash_passphrase") as hasher:
        with pytest.raises(AccessDeniedError, match="controller"):
            store.register_owner("intruder", PASSPHRASE)
        hasher.assert_not_called()
    assert (store.path.read_bytes() if store.path.exists() else None) == before
    status = store.setup_status()
    assert status is not None and status.state == "local"


@pytest.mark.parametrize("value", [1, "true", None, [], {}])
def test_registration_authorization_requires_a_strict_boolean(
    tmp_path: Path, value: object
) -> None:
    store = _prepared_store(tmp_path)
    raw = json.loads(store.path.read_text())
    raw["allow_registration"] = value
    store.path.write_text(json.dumps(raw))
    with pytest.raises(AccessError, match="invalid"):
        store.register_owner("intruder", PASSPHRASE)


def test_prepared_browser_registration_is_atomic_and_consumed_once(tmp_path: Path) -> None:
    store = _prepared_store(tmp_path)
    status = store.setup_status()
    assert status is not None and status.state == "missing"

    def claim(index: int) -> str:
        try:
            AccessStore(store.directory.parent).register_owner(f"birder-{index}", PASSPHRASE)
            return "created"
        except AccessDeniedError:
            return "denied"

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(claim, range(4)))
    assert outcomes.count("created") == 1
    assert json.loads(store.path.read_text())["allow_registration"] is False
    assert store.setup_status() is None


def test_local_setup_and_browser_claim_cannot_replace_each_other(tmp_path: Path) -> None:
    store = _prepared_store(tmp_path)

    def claim(local: bool) -> str:
        try:
            if local:
                store.setup_owner("local-birder", PASSPHRASE)
            else:
                store.register_owner("browser-birder", PASSPHRASE)
            return "created"
        except AccessDeniedError:
            return "denied"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, [True, False]))
    assert outcomes.count("created") == 1
    before = store.path.read_bytes()
    with pytest.raises(AccessDeniedError):
        store.setup_owner("replacement", PASSPHRASE)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("operation", ["setup", "recover", "bootstrap", "establish"])
def test_trusted_owner_paths_close_prepared_registration(tmp_path: Path, operation: str) -> None:
    store = _prepared_store(tmp_path)
    if operation == "setup":
        store.setup_owner("birder", PASSPHRASE)
    elif operation == "recover":
        store.recover_owner("birder", PASSPHRASE)
    else:
        secret = store.bootstrap(now=NOW).read_text().strip()
        if operation == "establish":
            store.establish_owner(secret, PASSPHRASE, now=NOW)
    assert json.loads(store.path.read_text())["allow_registration"] is False
    with pytest.raises(AccessDeniedError):
        store.register_owner("intruder", PASSPHRASE, now=NOW + timedelta(days=1))


def test_failed_claim_does_not_consume_the_prepared_grant(tmp_path: Path) -> None:
    store = _prepared_store(tmp_path)
    before = store.path.read_bytes()
    with pytest.raises(AccessError):
        store.register_owner("birder", "short")
    assert store.path.read_bytes() == before
    with (
        patch("inky_bird_frame.http.Path.replace", side_effect=OSError("private detail")),
        pytest.raises(AccessError),
    ):
        store.register_owner("birder", PASSPHRASE)
    assert store.path.read_bytes() == before
    store.register_owner("birder", PASSPHRASE)


def test_lost_access_file_does_not_reopen_registration(tmp_path: Path) -> None:
    store = _prepared_store(tmp_path)
    store.register_owner("birder", PASSPHRASE)
    store.path.unlink()
    config = load_config(tmp_path / "config.toml", load_secrets=False)
    assert not prepare_browser_registration(tmp_path / "config.toml", config.controller)
    with pytest.raises(AccessDeniedError):
        store.register_owner("intruder", PASSPHRASE)


@pytest.mark.parametrize("target", ["access.lock", "access.json"])
def test_named_pipes_in_access_state_fail_without_blocking(tmp_path: Path, target: str) -> None:
    store = AccessStore(tmp_path)
    store.directory.mkdir(mode=0o700)
    os.mkfifo(store.directory / target, 0o600)
    with pytest.raises(AccessError, match="regular|0600"):
        store.owner_exists()


@pytest.mark.parametrize("operation", ["read", "activity", "confirm", "login", "link"])
def test_waiting_request_samples_time_after_the_state_lock(tmp_path: Path, operation: str) -> None:
    store = AccessStore(tmp_path, oidc_provider_id="synthetic-provider")
    credential = _owner_session(store, "birder", PASSPHRASE, now=NOW)
    original_lock = store._locked
    waiting = Event()

    @contextmanager
    def marked_lock() -> Iterator[AccessState]:
        waiting.set()
        with original_lock() as state:
            yield state

    def request() -> None:
        if operation == "confirm":
            store.confirm_password(credential.secret, PASSPHRASE)
        elif operation == "login":
            store.login(PASSPHRASE, username="birder")
        elif operation == "link":
            store.begin_oidc(link_secret=credential.secret)
        else:
            store.authenticate(credential.secret, active=operation == "activity")

    with (
        ThreadPoolExecutor(max_workers=1) as pool,
        patch("inky_bird_frame.access.datetime") as clock,
        patch.object(store, "_locked", marked_lock),
    ):
        clock.now.return_value = NOW + timedelta(seconds=10)
        with original_lock() as state:
            future = pool.submit(request)
            assert waiting.wait(timeout=5)
            # An earlier lock holder commits activity while this request waits.
            later = NOW + timedelta(seconds=20)
            state.sessions[0] = replace(state.sessions[0], last_active_at=later.timestamp())
            store._save(state)
            clock.now.return_value = later
        future.result(timeout=5)
        assert store.authenticate(credential.secret).expires_at == (
            NOW.timestamp() + OWNER_ABSOLUTE_SECONDS
        )


def test_first_account_is_atomic_and_username_is_required(tmp_path: Path) -> None:
    def claim(_: int) -> str:
        try:
            _owner_session(AccessStore(tmp_path), "Birder", PASSPHRASE)
            return "created"
        except AccessDeniedError:
            return "denied"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(claim, range(4)))
    assert results.count("created") == 1
    store = AccessStore(tmp_path)
    for name in (None, "someone-else", "évil"):
        with pytest.raises(AccessDeniedError):
            store.login(PASSPHRASE, username=name)
    store.login(PASSPHRASE, username="BIRDER")
    assert PASSPHRASE not in store.path.read_text()


def test_remembered_session_survives_restart_but_polling_does_not_extend_it(tmp_path: Path) -> None:
    store = AccessStore(tmp_path)
    credential = _owner_session(store, "birder", PASSPHRASE, remembered=True, now=NOW)
    restarted = AccessStore(tmp_path)
    session = restarted.authenticate(credential.secret, now=NOW + timedelta(days=6))
    assert session.expires_at == NOW.timestamp() + REMEMBERED_ABSOLUTE_SECONDS
    with pytest.raises(AccessDeniedError):
        restarted.authenticate(
            credential.secret, now=NOW + timedelta(seconds=REMEMBERED_IDLE_SECONDS)
        )


def test_remembered_actions_cannot_extend_absolute_expiry(tmp_path: Path) -> None:
    store = AccessStore(tmp_path)
    credential = _owner_session(store, "birder", PASSPHRASE, remembered=True, now=NOW)
    for days in range(1, 30):
        store.authenticate(credential.secret, active=True, now=NOW + timedelta(days=days))
    with pytest.raises(AccessDeniedError):
        store.authenticate(
            credential.secret, now=NOW + timedelta(seconds=REMEMBERED_ABSOLUTE_SECONDS)
        )


def test_legacy_session_keeps_its_original_expiry(tmp_path: Path) -> None:
    store = AccessStore(tmp_path)
    credential = _owner_session(store, "birder", PASSPHRASE, now=NOW)
    raw = json.loads(store.path.read_text())
    raw["schema_version"] = 1
    for field in (
        "idle_seconds",
        "absolute_seconds",
        "authenticated_at",
        "remembered",
        "provider_id",
    ):
        raw["sessions"][0].pop(field)
    store.path.write_text(json.dumps(raw))
    with pytest.raises(AccessDeniedError):
        store.authenticate(credential.secret, now=NOW + timedelta(minutes=30))


def test_recovery_preserves_birds_and_replaces_access_without_open_registration(
    tmp_path: Path,
) -> None:
    store = AccessStore(tmp_path)
    first = _owner_session(store, "birder", PASSPHRASE)
    retained = tmp_path / "private-history.json"
    retained.write_text("bird history")
    store.recover_owner("new-owner", "new long synthetic passphrase")
    assert store.owner_exists()
    assert retained.read_text() == "bird history"
    with pytest.raises(AccessDeniedError):
        store.authenticate(first.secret)
    with pytest.raises(AccessDeniedError):
        store.register_owner("intruder", PASSPHRASE)
    store.login("new long synthetic passphrase", username="new-owner")


def test_session_revocation_and_account_change_need_recent_confirmation(tmp_path: Path) -> None:
    store = AccessStore(tmp_path)
    first = _owner_session(store, "birder", PASSPHRASE)
    other = store.login(PASSPHRASE, username="birder")
    raw = json.loads(store.path.read_text())
    raw["sessions"][0]["authenticated_at"] -= 601
    store.path.write_text(json.dumps(raw))
    session_id = next(row["id"] for row in store.sessions(first.secret) if not row["current"])
    with pytest.raises(AccessDeniedError, match="Confirm"):
        store.revoke_session(first.secret, str(session_id))
    store.confirm_password(first.secret, PASSPHRASE)
    replacement = store.update_account(
        first.secret, "new-birder", "a different long synthetic passphrase"
    )
    store.authenticate(replacement.secret)
    with pytest.raises(AccessDeniedError):
        store.authenticate(first.secret)
    with pytest.raises(AccessDeniedError):
        store.authenticate(other.secret)


def test_username_only_change_preserves_password_and_revokes_old_cookies(tmp_path: Path) -> None:
    store = AccessStore(tmp_path)
    first = _owner_session(store, "birder", PASSPHRASE)
    with patch("inky_bird_frame.access._hash_passphrase") as hasher:
        replacement = store.update_account(first.secret, "new-birder")
        hasher.assert_not_called()
    store.authenticate(replacement.secret)
    with pytest.raises(AccessDeniedError):
        store.authenticate(first.secret)
    store.login(PASSPHRASE, username="new-birder")


def test_oidc_transactions_are_private_single_use_and_bound_to_the_browser(tmp_path: Path) -> None:
    store = AccessStore(tmp_path, oidc_provider_id="synthetic-provider")
    local = _owner_session(store, "birder", PASSPHRASE, now=NOW)
    tx, browser = store.begin_oidc(link_secret=local.secret, now=NOW)
    assert browser not in store.path.read_text()
    for value in (browser, tx.state, tx.nonce, tx.verifier):
        assert value not in repr(tx)
    with pytest.raises(AccessDeniedError):
        store.consume_oidc(tx.state, "wrong-browser", now=NOW)
    consumed = AccessStore(tmp_path, oidc_provider_id="synthetic-provider").consume_oidc(
        tx.state, browser, now=NOW
    )
    assert consumed == tx
    with pytest.raises(AccessDeniedError):
        store.consume_oidc(tx.state, browser, now=NOW)


def test_oidc_expiry_owner_binding_revocation_and_provider_change(tmp_path: Path) -> None:
    store = AccessStore(tmp_path, oidc_provider_id="synthetic-provider")
    local = _owner_session(store, "birder", PASSPHRASE, now=NOW)
    expired, browser = store.begin_oidc(now=NOW)
    with pytest.raises(AccessDeniedError):
        store.consume_oidc(
            expired.state, browser, now=NOW + timedelta(seconds=OIDC_TRANSACTION_SECONDS)
        )
    link, browser = store.begin_oidc(link_secret=local.secret, now=NOW)
    link = store.consume_oidc(link.state, browser, now=NOW)
    sso = store.complete_oidc(
        link,
        "https://identity.example.test/",
        "owner-subject",
        authenticated_at=NOW.timestamp(),
        now=NOW,
    )
    assert store.oidc_linked("https://identity.example.test/")
    for subject in ("someone-else", "owner-subject"):
        tx, browser = store.begin_oidc(now=NOW)
        tx = store.consume_oidc(tx.state, browser, now=NOW)
        if subject != "owner-subject":
            with pytest.raises(AccessDeniedError):
                store.complete_oidc(
                    tx,
                    "https://identity.example.test/",
                    subject,
                    authenticated_at=NOW.timestamp(),
                    now=NOW,
                )
        else:
            store.complete_oidc(
                tx,
                "https://identity.example.test/",
                subject,
                authenticated_at=NOW.timestamp(),
                now=NOW,
            )
    with pytest.raises(AccessDeniedError):
        AccessStore(tmp_path, oidc_provider_id="replacement-provider").authenticate(
            sso.secret, now=NOW
        )
    with pytest.raises(AccessDeniedError):
        store.authenticate(local.secret, now=NOW)


def test_unlink_prevents_an_inflight_callback_from_relinking(tmp_path: Path) -> None:
    store = AccessStore(tmp_path, oidc_provider_id="synthetic-provider")
    local = _owner_session(store, "birder", PASSPHRASE)
    tx, browser = store.begin_oidc(link_secret=local.secret)
    consumed = store.consume_oidc(tx.state, browser)
    store.unlink_oidc(local.secret)
    with pytest.raises(AccessDeniedError, match="changed"):
        store.complete_oidc(
            consumed,
            "https://identity.example.test/",
            "owner-subject",
            authenticated_at=datetime.now(UTC).timestamp(),
        )


@pytest.mark.parametrize("remembered", [False, True])
def test_oidc_link_replaces_current_session_without_leaking_capacity(
    tmp_path: Path, remembered: bool
) -> None:
    store = AccessStore(tmp_path, oidc_provider_id="synthetic-provider")
    current = _owner_session(store, "birder", PASSPHRASE, remembered=remembered, now=NOW)
    others = [
        store.login(PASSPHRASE, username="birder", now=NOW) for _ in range(MAX_OWNER_SESSIONS - 1)
    ]
    original = store.authenticate(current.secret, now=NOW)
    for attempt in range(3):
        later = NOW + timedelta(minutes=attempt + 1)
        tx, browser = store.begin_oidc(
            link_secret=current.secret, remembered=not remembered, now=later
        )
        assert tx.remembered is remembered
        prior = current
        current = store.complete_oidc(
            store.consume_oidc(tx.state, browser, now=later),
            "https://identity.example.test/",
            "owner-subject",
            authenticated_at=later.timestamp(),
            now=later,
        )
        assert current.secret != prior.secret
        with pytest.raises(AccessDeniedError):
            store.authenticate(prior.secret, now=later)
        session = store.authenticate(current.secret, now=later)
        assert session.provider_id == "synthetic-provider"
        assert session.remembered is remembered
        assert session.created_at == original.created_at
        assert session.expires_at == original.expires_at
        assert len(json.loads(store.path.read_text())["sessions"]) == MAX_OWNER_SESSIONS
        for other in others:
            store.authenticate(other.secret, now=later)


@pytest.mark.parametrize("remembered", [False, True])
def test_oidc_confirmation_rotates_current_session_at_capacity(
    tmp_path: Path, remembered: bool
) -> None:
    store = AccessStore(tmp_path, oidc_provider_id="synthetic-provider")
    local = _owner_session(store, "birder", PASSPHRASE, remembered=remembered, now=NOW)
    tx, browser = store.begin_oidc(link_secret=local.secret, remembered=remembered, now=NOW)
    current = store.complete_oidc(
        store.consume_oidc(tx.state, browser, now=NOW),
        "https://identity.example.test/",
        "owner-subject",
        authenticated_at=NOW.timestamp(),
        now=NOW,
    )
    store.logout(local.secret)
    for _ in range(MAX_OWNER_SESSIONS - 1):
        store.login(PASSPHRASE, username="birder", now=NOW)
    original = store.authenticate(current.secret, now=NOW)
    later = NOW + timedelta(minutes=11)
    tx, browser = store.begin_oidc(confirm_secret=current.secret, return_to_access=True, now=later)
    restarted = AccessStore(tmp_path, oidc_provider_id="synthetic-provider")
    confirmed = restarted.complete_oidc(
        restarted.consume_oidc(tx.state, browser, now=later),
        "https://identity.example.test/",
        "owner-subject",
        authenticated_at=later.timestamp(),
        now=later,
    )
    assert confirmed.secret != current.secret and confirmed.csrf != current.csrf
    session = restarted.authenticate(confirmed.secret, now=later)
    assert session.authenticated_at == later.timestamp()
    assert session.created_at == original.created_at
    assert session.expires_at == original.expires_at
    assert session.remembered is remembered
    assert len(json.loads(store.path.read_text())["sessions"]) == MAX_OWNER_SESSIONS
    with pytest.raises(AccessDeniedError):
        restarted.authenticate(current.secret, now=later)
    with patch("inky_bird_frame.access.datetime") as clock:
        clock.now.return_value = later
        other_id = next(
            s["id"] for s in restarted.sessions(confirmed.secret) if s["current"] is False
        )
        restarted.revoke_session(confirmed.secret, str(other_id))
        assert len(restarted.sessions(confirmed.secret)) == MAX_OWNER_SESSIONS - 1


@pytest.mark.parametrize("change", ["logout", "wrong_identity"])
def test_oidc_confirmation_requires_the_original_session_and_identity(
    tmp_path: Path, change: str
) -> None:
    store = AccessStore(tmp_path, oidc_provider_id="synthetic-provider")
    local = _owner_session(store, "birder", PASSPHRASE, now=NOW)
    tx, browser = store.begin_oidc(link_secret=local.secret, now=NOW)
    current = store.complete_oidc(
        store.consume_oidc(tx.state, browser, now=NOW),
        "https://identity.example.test/",
        "owner-subject",
        authenticated_at=NOW.timestamp(),
        now=NOW,
    )
    tx, browser = store.begin_oidc(confirm_secret=current.secret, return_to_access=True, now=NOW)
    tx = store.consume_oidc(tx.state, browser, now=NOW)
    if change == "logout":
        store.logout(current.secret)
    before = store.path.read_bytes()
    with pytest.raises(AccessDeniedError):
        store.complete_oidc(
            tx,
            "https://identity.example.test/",
            "someone-else" if change == "wrong_identity" else "owner-subject",
            authenticated_at=NOW.timestamp(),
            now=NOW,
        )
    assert store.path.read_bytes() == before


def test_sso_return_preserves_provider_authentication_age(tmp_path: Path) -> None:
    store = AccessStore(tmp_path, oidc_provider_id="synthetic-provider")
    local = _owner_session(store, "birder", PASSPHRASE, now=NOW)
    tx, browser = store.begin_oidc(link_secret=local.secret, now=NOW)
    tx = store.consume_oidc(tx.state, browser, now=NOW)
    authenticated_at = NOW.timestamp() - RECENT_AUTH_SECONDS + 10
    credentials = store.complete_oidc(
        tx,
        "https://identity.example.test/",
        "owner-subject",
        authenticated_at=authenticated_at,
        now=NOW,
    )
    session = store.authenticate(credentials.secret, now=NOW)
    assert session.authenticated_at == authenticated_at
    with patch("inky_bird_frame.access.datetime") as clock:
        clock.now.return_value = NOW + timedelta(seconds=10)
        with pytest.raises(AccessDeniedError, match="Confirm"):
            store.unlink_oidc(credentials.secret)


@pytest.mark.parametrize("sso", [False, True])
@pytest.mark.parametrize("change_password", [False, True])
def test_account_rotation_does_not_refresh_authentication(
    tmp_path: Path, sso: bool, change_password: bool
) -> None:
    store = AccessStore(tmp_path, oidc_provider_id="synthetic-provider")
    credentials = _owner_session(store, "birder", PASSPHRASE, now=NOW)
    if sso:
        tx, browser = store.begin_oidc(link_secret=credentials.secret, now=NOW)
        tx = store.consume_oidc(tx.state, browser, now=NOW)
        credentials = store.complete_oidc(
            tx,
            "https://identity.example.test/",
            "owner-subject",
            authenticated_at=NOW.timestamp(),
            now=NOW,
        )
    with patch("inky_bird_frame.access.datetime") as clock:
        clock.now.return_value = NOW + timedelta(seconds=RECENT_AUTH_SECONDS - 10)
        replacement = store.update_account(
            credentials.secret,
            "new-birder",
            "a different synthetic passphrase" if change_password else None,
        )
        session = store.authenticate(replacement.secret)
        assert session.authenticated_at == NOW.timestamp()
        clock.now.return_value = NOW + timedelta(seconds=RECENT_AUTH_SECONDS)
        with pytest.raises(AccessDeniedError, match="Confirm"):
            store.update_account(replacement.secret, "new-birder")


@pytest.mark.parametrize("age", [RECENT_AUTH_SECONDS, -1, float("nan")])
def test_sso_return_rejects_old_or_invalid_authentication_before_mutating(
    tmp_path: Path, age: float
) -> None:
    store = AccessStore(tmp_path, oidc_provider_id="synthetic-provider")
    local = _owner_session(store, "birder", PASSPHRASE, now=NOW)
    tx, browser = store.begin_oidc(link_secret=local.secret, now=NOW)
    tx = store.consume_oidc(tx.state, browser, now=NOW)
    before = store.path.read_bytes()
    with pytest.raises(AccessDeniedError, match="recent"):
        store.complete_oidc(
            tx,
            "https://identity.example.test/",
            "owner-subject",
            authenticated_at=NOW.timestamp() - age,
            now=NOW,
        )
    assert store.path.read_bytes() == before


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


@pytest.mark.parametrize("elapsed", [0, BOOTSTRAP_SECONDS + 1])
def test_browser_registration_cannot_override_reserved_setup(tmp_path: Path, elapsed: int) -> None:
    store = AccessStore(tmp_path)
    secret_path = store.bootstrap(now=NOW)
    secret_bytes = secret_path.read_bytes()
    state_bytes = store.path.read_bytes()
    with patch("inky_bird_frame.access._hash_passphrase") as hash_password:
        with pytest.raises(AccessDeniedError, match="reserved owner setup"):
            store.register_owner("intruder", PASSPHRASE, now=NOW + timedelta(seconds=elapsed))
        hash_password.assert_not_called()
    assert not store.owner_exists()
    assert store.path.read_bytes() == state_bytes
    assert secret_path.read_bytes() == secret_bytes


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
    assert missing is not None and missing.state == "local"
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


def test_legacy_reset_cannot_reopen_owner_registration(tmp_path: Path) -> None:
    unrelated = tmp_path / "retained-history.json"
    unrelated.write_text("history")
    store = AccessStore(tmp_path)
    secret, _ = _establish(store)
    with pytest.raises(AccessError, match="access recover"):
        store.bootstrap(reset=True, now=NOW)
    assert store.owner_exists()
    assert unrelated.read_text() == "history"
    store.authenticate(secret, now=NOW)
    with pytest.raises(AccessDeniedError):
        store.register_owner("intruder", PASSPHRASE, now=NOW)


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
