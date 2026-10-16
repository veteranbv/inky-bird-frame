from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from inky_bird_frame.access import AccessDeniedError, AccessStore
from inky_bird_frame.config import ControllerConfig, OIDCConfig, OwnerAccessMode, load_config
from inky_bird_frame.oidc import VerifiedIdentity, provider_id
from inky_bird_frame.settings import initialize_config
from inky_bird_frame.web import LOCAL_OWNER_COOKIE, OWNER_COOKIE, create_app

PASSPHRASE = "a long synthetic browser passphrase"


def test_ownerless_upgrade_stays_healthy_but_requires_local_account_setup(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.state_dir.mkdir()
    retained = config.state_dir / "retained-history.json"
    retained.write_bytes(b"private existing bird history")
    origin = "https://inky.example.test"
    for _ in range(2):  # HTTP restarts must not authorize a network owner claim.
        with TestClient(create_app(config), base_url=origin) as client:
            status = client.get("/v1/access").json()["data"]
            assert status["setup_required"] is True
            assert status["registration_allowed"] is False
            assert status["setup_code"]["state"] == "local"
            assert client.get("/health").json()["ok"] is True
            assert client.get("/app").status_code == 200
            result = client.post(
                "/v1/owner/register",
                json={"username": "intruder", "passphrase": PASSPHRASE},
                headers={"Origin": origin},
            )
            assert result.status_code == 401 and "set-cookie" not in result.headers
            assert client.get("/v1/owner/session").status_code == 401
            assert retained.read_bytes() == b"private existing bird history"
    AccessStore(config.state_dir).setup_owner("birder", PASSPHRASE)
    with TestClient(create_app(config), base_url=origin) as client:
        assert not client.get("/v1/access").json()["data"]["setup_required"]
        assert (
            client.post(
                "/v1/owner/login",
                json={"username": "birder", "passphrase": PASSPHRASE},
                headers={"Origin": origin},
            ).status_code
            == 200
        )
    assert retained.read_bytes() == b"private existing bird history"


def test_reserved_setup_cannot_be_claimed_by_browser_registration(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = AccessStore(config.state_dir)
    reserved = store.bootstrap()
    before = store.path.read_bytes()
    with TestClient(create_app(config), base_url="https://inky.example.test") as client:
        assert client.get("/v1/access").json()["data"]["setup_code"]["state"] == "ready"
        response = client.post(
            "/v1/owner/register",
            json={"username": "intruder", "passphrase": PASSPHRASE},
            headers={"Origin": "https://inky.example.test"},
        )
        assert response.status_code == 401
        assert "set-cookie" not in response.headers
    assert not store.owner_exists()
    assert store.path.read_bytes() == before
    assert reserved.exists()


def test_local_network_setup_needs_no_code_and_remember_is_explicit(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    assert initialize_config(path)
    config = replace(load_config(path).controller, port=8000)
    origin = "http://192.168.1.50:8000"
    with TestClient(create_app(config), base_url=origin, client=("192.168.1.20", 42000)) as client:
        state = client.get("/v1/access").json()["data"]
        assert state["secure"] is True and state["encrypted"] is False
        created = client.post(
            "/v1/owner/register",
            json={"username": "birder", "passphrase": PASSPHRASE},
            headers={"Origin": origin},
        )
        assert created.status_code == 200
        assert "max-age" not in created.headers["set-cookie"].lower()
        csrf = created.json()["data"]["csrf"]
        assert (
            client.post(
                "/v1/owner/register",
                json={"username": "other", "passphrase": PASSPHRASE},
                headers={"Origin": origin},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/v1/owner/logout", json={}, headers={"Origin": origin, "X-Inky-CSRF": csrf}
            ).status_code
            == 200
        )
        remembered = client.post(
            "/v1/owner/login",
            json={"username": "BIRDER", "passphrase": PASSPHRASE, "remember": True},
            headers={"Origin": origin},
        )
        assert remembered.status_code == 200
        assert "Max-Age=2592000" in remembered.headers["set-cookie"]
        assert client.get("/v1/owner/access").json()["data"]["username"] == "birder"


def test_username_only_update_keeps_password_and_rotates_browser_credentials(
    tmp_path: Path,
) -> None:
    origin = "https://inky.example.test"
    config = _config(tmp_path)
    AccessStore(config.state_dir).setup_owner("birder", PASSPHRASE)
    with TestClient(create_app(config), base_url=origin) as client:
        created = client.post(
            "/v1/owner/login",
            json={"username": "birder", "passphrase": PASSPHRASE},
            headers={"Origin": origin},
        )
        old_cookie = client.cookies.get(OWNER_COOKIE)
        result = client.post(
            "/v1/owner/account",
            json={"username": "new-birder"},
            headers={"Origin": origin, "X-Inky-CSRF": created.json()["data"]["csrf"]},
        )
        assert result.status_code == 200
        assert client.cookies.get(OWNER_COOKIE) != old_cookie
        assert client.get("/v1/owner/access").json()["data"]["username"] == "new-birder"
        login = client.post(
            "/v1/owner/login",
            json={"username": "new-birder", "passphrase": PASSPHRASE},
            headers={"Origin": origin},
        )
        assert login.status_code == 200


@pytest.mark.parametrize("peer", ["8.8.8.8", "0.0.0.0", "169.254.1.1", "192.0.2.3", "2001:db8::1"])
def test_local_network_access_does_not_treat_reserved_addresses_as_private(
    tmp_path: Path, peer: str
) -> None:
    config = replace(_config(tmp_path), bind_host="0.0.0.0")
    with TestClient(
        create_app(config), base_url="http://192.168.1.50:8000", client=(peer, 42000)
    ) as client:
        assert client.get("/v1/access").json()["data"]["secure"] is False


def test_local_hostname_is_explicit_and_rebinding_does_not_change_the_socket(
    tmp_path: Path,
) -> None:
    config = replace(_config(tmp_path), bind_host="0.0.0.0")
    with TestClient(
        create_app(config), base_url="http://192.168.1.50:8000", client=("192.168.1.20", 42000)
    ) as client:
        assert (
            client.get("/v1/access", headers={"Host": "evil.example.test:8000"}).json()["data"][
                "secure"
            ]
            is False
        )
    config = replace(config, owner_allowed_origins=("http://inky.home:8000",))
    with TestClient(
        create_app(config), base_url="http://192.168.1.50:8000", client=("192.168.1.20", 42000)
    ) as client:
        assert (
            client.get("/v1/access", headers={"Host": "inky.home:8000"}).json()["data"]["secure"]
            is True
        )


def test_https_only_blocks_a_direct_backend_even_on_loopback(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), owner_access_mode=OwnerAccessMode.HTTPS_ONLY)
    with TestClient(
        create_app(config), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 42000)
    ) as client:
        assert client.get("/v1/access").json()["data"]["secure"] is False
        assert (
            client.post(
                "/v1/owner/register",
                json={"username": "birder", "passphrase": PASSPHRASE},
                headers={"Origin": "http://127.0.0.1:8000", "X-Forwarded-Proto": "https"},
            ).status_code
            == 403
        )


def test_oidc_link_requires_owner_csrf_and_callback_is_one_use(tmp_path: Path) -> None:
    oidc = OIDCConfig(
        "https://id.example.test/",
        "client",
        "https://inky.example.test/v1/owner/oidc/callback",
        "SYNTHETIC_SECRET",
    )
    config = replace(_config(tmp_path), oidc=oidc)
    origin = "https://inky.example.test"
    AccessStore(config.state_dir).setup_owner("birder", PASSPHRASE)
    with TestClient(create_app(config), base_url=origin) as client:
        assert (
            client.post("/v1/owner/oidc/link", json={}, headers={"Origin": origin}).status_code
            == 401
        )
        created = client.post(
            "/v1/owner/login",
            json={"username": "birder", "passphrase": PASSPHRASE},
            headers={"Origin": origin},
        )
        csrf = created.json()["data"]["csrf"]
        original_cookie = client.cookies.get(OWNER_COOKIE)
        assert original_cookie is not None
        with patch(
            "inky_bird_frame.web.authorization_url",
            return_value="https://id.example.test/authorize",
        ):
            started = client.post(
                "/v1/owner/oidc/link",
                json={"remember": True, "return_to_access": True},
                headers={"Origin": origin, "X-Inky-CSRF": csrf},
            )
        assert started.status_code == 200
        assert "SameSite=lax" in started.headers["set-cookie"]
        store = AccessStore(config.state_dir, oidc_provider_id=provider_id(oidc))
        import json

        tx = json.loads(store.path.read_text())["oidc_transactions"][0]
        assert tx["return_to_access"] is True and tx["remembered"] is False
        callback = "/v1/owner/oidc/callback?state=" + tx["state"] + "&code=synthetic-code"
        assert client.head(callback).status_code == 405
        assert len(json.loads(store.path.read_text())["oidc_transactions"]) == 1
        with patch(
            "inky_bird_frame.web.verified_identity",
            return_value=VerifiedIdentity("owner-subject", datetime.now(UTC).timestamp()),
        ):
            completed = client.get(callback, follow_redirects=False)
        assert completed.status_code == 303
        assert completed.headers["location"] == "/app?sign_in=linked#account"
        assert "__Host-inky-owner=" in completed.headers["set-cookie"]
        assert "synthetic-code" not in completed.text
        assert store.oidc_linked(oidc.issuer)
        with pytest.raises(AccessDeniedError):
            store.authenticate(original_cookie)
        repeated = client.get(callback, follow_redirects=False)
        assert repeated.status_code == 303
        assert repeated.headers["location"] == "/app?sign_in=failed"
        assert client.get("/v1/owner/session").json()["data"]["single_sign_on"] is True
        assert client.get("/v1/owner/session").json()["data"]["remembered"] is False


def test_sso_only_requires_a_successful_verified_exchange(tmp_path: Path) -> None:
    oidc = OIDCConfig(
        "https://id.example.test/",
        "client",
        "https://inky.example.test/v1/owner/oidc/callback",
        "SYNTHETIC_SECRET",
        owner_subject="owner-subject",
        sso_only=True,
    )
    from inky_bird_frame.access import AccessError

    AccessStore(tmp_path / "state").setup_owner("birder", PASSPHRASE)
    with pytest.raises(AccessError, match="Link and test"):
        create_app(replace(_config(tmp_path), oidc=oidc))


def test_sso_only_cannot_use_password_to_confirm_browser_changes(tmp_path: Path) -> None:
    oidc = OIDCConfig(
        "https://id.example.test/",
        "client",
        "https://inky.example.test/v1/owner/oidc/callback",
        "SYNTHETIC_SECRET",
        sso_only=True,
    )
    config = replace(_config(tmp_path), oidc=oidc)
    store = AccessStore(config.state_dir, oidc_provider_id=provider_id(oidc))
    store.setup_owner("birder", PASSPHRASE)
    local = store.login(PASSPHRASE, username="birder")
    tx, browser = store.begin_oidc(link_secret=local.secret)
    consumed = store.consume_oidc(tx.state, browser)
    sso = store.complete_oidc(
        consumed, oidc.issuer, "owner-subject", authenticated_at=datetime.now(UTC).timestamp()
    )
    origin = "https://inky.example.test"
    with TestClient(create_app(config), base_url=origin) as client:
        client.cookies.set(OWNER_COOKIE, sso.secret)
        result = client.post(
            "/v1/owner/confirm-password",
            json={"passphrase": PASSPHRASE},
            headers={"Origin": origin, "X-Inky-CSRF": sso.csrf},
        )
        assert result.status_code == 400
        assert "disabled" in result.json()["error"]["message"]


def test_incorrect_confirmation_password_keeps_the_session_and_allows_retry(tmp_path: Path) -> None:
    config = _config(tmp_path)
    origin = "https://inky.example.test"
    with TestClient(create_app(config), base_url=origin) as client:
        _setup(client, config, origin)
        session = client.get("/v1/owner/session").json()["data"]
        cookie = client.cookies.get(OWNER_COOKIE)
        headers = {"Origin": origin, "X-Inky-CSRF": session["csrf"]}
        failed = client.post(
            "/v1/owner/confirm-password", json={"passphrase": "incorrect"}, headers=headers
        )
        assert failed.status_code == 403
        assert failed.json()["error"]["code"] == "password_confirmation_failed"
        assert "incorrect" not in failed.text
        assert client.cookies.get(OWNER_COOKIE) == cookie
        assert client.get("/v1/owner/session").status_code == 200
        assert (
            client.post(
                "/v1/owner/confirm-password", json={"passphrase": PASSPHRASE}, headers=headers
            ).status_code
            == 200
        )


def test_oidc_confirmation_requires_a_session_and_csrf_before_contacting_provider(
    tmp_path: Path,
) -> None:
    config = replace(
        _config(tmp_path),
        oidc=OIDCConfig(
            "https://id.example.test/",
            "client",
            "https://inky.example.test/v1/owner/oidc/callback",
            "SYNTHETIC_SECRET",
            owner_subject="owner-subject",
        ),
    )
    origin = "https://inky.example.test"
    with (
        TestClient(create_app(config), base_url=origin) as client,
        patch(
            "inky_bird_frame.web.authorization_url",
            return_value="https://id.example.test/authorize",
        ) as provider,
    ):
        assert (
            client.post(
                "/v1/owner/oidc/start", json={"return_to_access": True}, headers={"Origin": origin}
            ).status_code
            == 401
        )
        _setup(client, config, origin)
        assert (
            client.post(
                "/v1/owner/oidc/start", json={"return_to_access": True}, headers={"Origin": origin}
            ).status_code
            == 401
        )
        provider.assert_not_called()


def test_oidc_confirmation_callback_rotates_only_the_current_session(tmp_path: Path) -> None:
    oidc = OIDCConfig(
        "https://id.example.test/",
        "client",
        "https://inky.example.test/v1/owner/oidc/callback",
        "SYNTHETIC_SECRET",
    )
    config = replace(_config(tmp_path), oidc=oidc)
    store = AccessStore(config.state_dir, oidc_provider_id=provider_id(oidc))
    store.setup_owner("birder", PASSPHRASE)
    local = store.login(PASSPHRASE, username="birder")
    other_local = store.login(PASSPHRASE, username="birder")
    original_local = store.authenticate(other_local.secret)
    tx, browser = store.begin_oidc(link_secret=local.secret)
    current = store.complete_oidc(
        store.consume_oidc(tx.state, browser),
        oidc.issuer,
        "owner-subject",
        authenticated_at=datetime.now(UTC).timestamp(),
    )
    origin = "https://inky.example.test"
    with TestClient(create_app(config), base_url=origin) as client:
        client.cookies.set(OWNER_COOKIE, current.secret, domain="inky.example.test", path="/")
        with patch(
            "inky_bird_frame.web.authorization_url",
            return_value="https://id.example.test/authorize",
        ):
            started = client.post(
                "/v1/owner/oidc/start",
                json={"return_to_access": True, "remember": True},
                headers={"Origin": origin, "X-Inky-CSRF": current.csrf},
            )
        assert started.status_code == 200
        tx_state = json.loads(store.path.read_text())["oidc_transactions"][0]
        with patch(
            "inky_bird_frame.web.verified_identity",
            return_value=VerifiedIdentity("owner-subject", datetime.now(UTC).timestamp()),
        ):
            result = client.get(
                "/v1/owner/oidc/callback?state=" + tx_state["state"] + "&code=synthetic-code",
                follow_redirects=False,
            )
        assert result.headers["location"] == "/app?sign_in=confirmed#account"
        assert "Max-Age" not in result.headers["set-cookie"].split(",")[0]
        assert client.cookies.get(OWNER_COOKIE) != current.secret
        assert client.get("/v1/owner/session").json()["data"]["remembered"] is False
        assert len(store.sessions(other_local.secret)) == 2
        assert store.authenticate(other_local.secret).expires_at == original_local.expires_at
        with pytest.raises(AccessDeniedError):
            store.authenticate(current.secret)


def _config(root: Path) -> ControllerConfig:
    return ControllerConfig(
        workspace_dir=root / "work",
        catalog_dir=root / "catalog",
        state_dir=root / "state",
        codex_path=Path("codex"),
        codex_model=None,
        bind_host="127.0.0.1",
        port=8000,
        references_per_species=3,
        generations_per_cycle=1,
        max_generation_attempts=3,
        owner_allowed_origins=("https://inky.example.test",),
    )


def _setup(client: TestClient, config: ControllerConfig, origin: str) -> str:
    secret = AccessStore(config.state_dir).bootstrap().read_text().strip()
    response = client.post(
        "/v1/owner/setup",
        json={"secret": secret, "passphrase": PASSPHRASE},
        headers={"Origin": origin},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["csrf"])


def test_genuine_loopback_owner_journey_and_session_reload(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with TestClient(
        create_app(config), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 41000)
    ) as client:
        access = client.get("/v1/access").json()["data"]
        assert access == {
            "secure": True,
            "setup_required": True,
            "registration_allowed": False,
            "setup_code": {"state": "local", "expires_in_seconds": None},
            "password_min_length": 15,
            "password_max_length": 1024,
            "encrypted": False,
            "access_mode": "local",
            "legacy_account": False,
            "session_policy": {"regular_hours": 24, "remembered_days": 30},
            "provider": None,
            "local_login": True,
        }
        csrf = _setup(client, config, "http://127.0.0.1:8000")
        cookie = client.cookies[LOCAL_OWNER_COOKIE]
        session = client.get("/v1/owner/session")
        assert session.status_code == 200
        assert session.json()["data"]["csrf"] == csrf
        assert session.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in session.headers["content-security-policy"]
        assert cookie not in session.text
        assert (
            client.post(
                "/v1/owner/logout",
                json={},
                headers={"Origin": "http://127.0.0.1:8000", "X-Inky-CSRF": "wrong"},
            ).status_code
            == 401
        )
        assert client.get("/v1/owner/session").status_code == 200
        response = client.post(
            "/v1/owner/logout",
            json={},
            headers={"Origin": "http://127.0.0.1:8000", "X-Inky-CSRF": csrf},
        )
        assert response.status_code == 200
        assert client.get("/v1/owner/session").status_code == 401
        response = client.post(
            "/v1/owner/login",
            json={"passphrase": PASSPHRASE},
            headers={"Origin": "http://127.0.0.1:8000"},
        )
        assert response.status_code == 200
        assert client.cookies[LOCAL_OWNER_COOKIE] != cookie


def test_https_owner_cookie_has_required_security_attributes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with TestClient(create_app(config), base_url="https://inky.example.test") as client:
        _setup(client, config, "https://inky.example.test")
        login = client.post(
            "/v1/owner/login",
            json={"passphrase": PASSPHRASE},
            headers={"Origin": "https://inky.example.test"},
        )
        cookie = login.headers["set-cookie"]
        assert cookie.startswith(OWNER_COOKIE + "=")
        for attribute in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
            assert attribute.lower() in cookie.lower()
        assert "domain=" not in cookie.lower()


def test_secure_setup_guidance_never_includes_credentials_or_paths(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = AccessStore(config.state_dir)
    secret = store.bootstrap().read_text().strip()
    with TestClient(create_app(config), base_url="https://inky.example.test") as client:
        response = client.get("/v1/access")
        setup = response.json()["data"]["setup_code"]
        assert setup["state"] == "ready"
        assert 0 < setup["expires_in_seconds"] <= 600
        assert secret not in response.text
        assert str(tmp_path) not in response.text
        assert "hash" not in response.text
        assert response.headers["cache-control"] == "no-store"
        _setup(client, config, "https://inky.example.test")
        access = client.get("/v1/access").json()["data"]
        assert access["setup_required"] is False
        assert access["setup_code"] is None


@pytest.mark.parametrize(
    "base_url,peer,bind",
    [
        ("http://inky.example.test", "127.0.0.1", "127.0.0.1"),
        ("http://127.0.0.1:8000", "192.0.2.2", "127.0.0.1"),
        ("https://unapproved.example.test", "192.0.2.2", "127.0.0.1"),
        ("http://127.0.0.1:9000", "127.0.0.1", "127.0.0.1"),
    ],
)
def test_insecure_access_rejects_before_requesting_or_reading_credentials(
    tmp_path: Path, base_url: str, peer: str, bind: str
) -> None:
    config = replace(_config(tmp_path), bind_host=bind)
    with TestClient(create_app(config), base_url=base_url, client=(peer, 41000)) as client:
        assert client.get("/v1/access").json()["data"] == {"secure": False, "setup_required": None}
        with patch("inky_bird_frame.web._json_body") as body:
            response = client.post(
                "/v1/owner/login", content=b"not parsed", headers={"Origin": base_url}
            )
            assert response.status_code == 403
            body.assert_not_called()
        assert not (config.state_dir / "web-access").exists()


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://attacker.example.test"},
        {"Origin": "https://inky.example.test", "Sec-Fetch-Site": "cross-site"},
    ],
)
def test_forged_requests_reject_before_credentials_are_read(
    tmp_path: Path, headers: dict[str, str]
) -> None:
    with (
        TestClient(create_app(_config(tmp_path)), base_url="https://inky.example.test") as client,
        patch("inky_bird_frame.web._json_body") as body,
    ):
        response = client.post("/v1/owner/login", content=b"not parsed", headers=headers)
        assert response.status_code == 403
        body.assert_not_called()
        assert "access-control-allow-origin" not in response.headers


def test_forged_forwarded_headers_do_not_enable_owner_access(tmp_path: Path) -> None:
    with TestClient(
        create_app(_config(tmp_path)),
        base_url="http://inky.example.test",
        client=("192.0.2.2", 41000),
    ) as client:
        response = client.get(
            "/v1/access", headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "127.0.0.1"}
        )
        assert response.json()["data"]["secure"] is False


def test_only_the_explicit_proxy_can_report_https(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), trusted_proxy_addresses=("192.0.2.10",))
    for peer, expected in [("192.0.2.10", True), ("192.0.2.11", False)]:
        with TestClient(
            create_app(config), base_url="http://inky.example.test", client=(peer, 41000)
        ) as client:
            response = client.get(
                "/v1/access",
                headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "192.0.2.50"},
            )
            assert response.json()["data"]["secure"] is expected


@pytest.mark.parametrize(
    "content,content_type",
    [
        (b"not json", "application/json"),
        (b"{}", "text/plain"),
        (b'{"passphrase":123}', "application/json"),
        (b'{"passphrase":"synthetic","unexpected":"private"}', "application/json"),
        (b"x" * 8193, "application/json"),
    ],
)
def test_invalid_owner_bodies_are_bounded_and_sanitized(
    tmp_path: Path, content: bytes, content_type: str
) -> None:
    with TestClient(create_app(_config(tmp_path)), base_url="https://inky.example.test") as client:
        response = client.post(
            "/v1/owner/login",
            content=content,
            headers={"Origin": "https://inky.example.test", "Content-Type": content_type},
        )
        assert response.status_code == 400
        assert "private" not in response.text
        assert "synthetic" not in response.text


def test_access_errors_are_not_double_sent_or_logged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path)
    with TestClient(create_app(config), base_url="https://inky.example.test") as client:
        _setup(client, config, "https://inky.example.test")
        response = client.post(
            "/v1/owner/login",
            json={"passphrase": "private invalid secret"},
            headers={"Origin": "https://inky.example.test"},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "sign_in_required"
    output = capsys.readouterr().out
    assert "private invalid secret" not in output
    assert PASSPHRASE not in output
    assert str(tmp_path) not in output


def test_bundled_owner_page_uses_external_assets_and_cannot_frame(tmp_path: Path) -> None:
    with TestClient(create_app(_config(tmp_path))) as client:
        for path in ("/app", "/app/app.js", "/app/app.css"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert client.get("/app/../access.json").status_code == 404
