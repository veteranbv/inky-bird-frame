from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from inky_bird_frame.access import AccessStore
from inky_bird_frame.config import ControllerConfig
from inky_bird_frame.web import LOCAL_OWNER_COOKIE, OWNER_COOKIE, create_app

PASSPHRASE = "a long synthetic browser passphrase"


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
        assert client.get("/v1/access").json()["data"] == {"secure": True, "setup_required": True}
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


@pytest.mark.parametrize(
    "base_url,peer,bind",
    [
        ("http://inky.example.test", "127.0.0.1", "127.0.0.1"),
        ("http://127.0.0.1:8000", "192.0.2.2", "127.0.0.1"),
        ("http://127.0.0.1:8000", "127.0.0.1", "0.0.0.0"),
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
