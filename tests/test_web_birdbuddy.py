from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from inky_bird_frame.access import AccessStore
from inky_bird_frame.birdbuddy import BirdBuddyFeeder
from inky_bird_frame.config import ControllerConfig
from inky_bird_frame.errors import DataSourceError
from inky_bird_frame.web import create_app

ORIGIN = "https://inky.example.test"


def controller(root: Path, origin: str = ORIGIN) -> ControllerConfig:
    return ControllerConfig(
        root / "workspace",
        root / "catalog",
        root / "state",
        Path("codex"),
        None,
        "127.0.0.1",
        8793,
        3,
        1,
        3,
        owner_allowed_origins=(origin,),
    )


def login(client: TestClient, config: ControllerConfig, origin: str = ORIGIN) -> dict[str, str]:
    AccessStore(config.state_dir).setup_owner("qa-owner", "long synthetic password")
    result = client.post(
        "/v1/owner/login",
        json={"username": "qa-owner", "passphrase": "long synthetic password"},
        headers={"Origin": origin},
    )
    assert result.status_code == 200
    return {"Origin": origin, "X-Inky-CSRF": result.json()["data"]["csrf"]}


def credentials() -> dict[str, object]:
    return {
        "confirm": True,
        "email": "synthetic-guest@example.test",
        "password": "SYNTHETIC_GUEST_PASSWORD",
    }


def test_birdbuddy_account_requires_owner_csrf_and_explicit_attestation(tmp_path: Path) -> None:
    config = controller(tmp_path)
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        assert client.get("/v1/owner/birdbuddy").status_code == 401
        headers = login(client, config)
        with patch("inky_bird_frame.birdbuddy._authenticate") as authenticate:
            assert (
                client.post(
                    "/v1/owner/birdbuddy/login", json=credentials(), headers={"Origin": ORIGIN}
                ).status_code
                == 401
            )
            assert (
                client.post(
                    "/v1/owner/birdbuddy/login",
                    json={**credentials(), "confirm": False},
                    headers=headers,
                ).status_code
                == 400
            )
            assert (
                client.post(
                    "/v1/owner/birdbuddy/login",
                    json={**credentials(), "confirm": "true"},
                    headers=headers,
                ).status_code
                == 400
            )
            authenticate.assert_not_called()
        assert not (config.state_dir / "birdbuddy-auth.json").exists()


def test_bootstrap_is_denied_on_local_network_http_without_contacting_provider(
    tmp_path: Path,
) -> None:
    origin = "http://inky.example.test"
    config = controller(tmp_path, origin)
    with TestClient(create_app(config), base_url=origin, client=("192.168.1.12", 12345)) as client:
        headers = login(client, config, origin)
        assert not client.get("/v1/owner/birdbuddy").json()["data"]["secure_bootstrap"]
        with patch("inky_bird_frame.birdbuddy._authenticate") as authenticate:
            response = client.post("/v1/owner/birdbuddy/login", json=credentials(), headers=headers)
            assert response.status_code == 400
            assert "HTTPS" in response.json()["error"]["message"]
            authenticate.assert_not_called()


def test_structured_feeder_choice_and_bootstrap_secret_redaction(tmp_path: Path) -> None:
    config = controller(tmp_path)
    feeders = [
        BirdBuddyFeeder("fixture-first", "Fixture feeder one", "member"),
        BirdBuddyFeeder("fixture-second", "Fixture feeder two", "member"),
    ]
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        headers = login(client, config)
        with patch(
            "inky_bird_frame.birdbuddy._authenticate",
            return_value=("SYNTHETIC_ACCESS", "SYNTHETIC_REFRESH", feeders),
        ) as authenticate:
            response = client.post("/v1/owner/birdbuddy/login", json=credentials(), headers=headers)
            assert response.status_code == 200
            assert response.json()["data"]["choose_feeder"]
            assert len(response.json()["data"]["feeders"]) == 2
            assert not (config.state_dir / "birdbuddy-auth.json").exists()
            selected = client.post(
                "/v1/owner/birdbuddy/login",
                json={**credentials(), "feeder_id": "fixture-second"},
                headers=headers,
            )
            assert selected.status_code == 200
            assert selected.json()["data"]["feeder"]["id"] == "fixture-second"
            assert selected.json()["data"]["least_privilege_guest"]
            assert authenticate.call_count == 2
        state = client.get("/v1/owner/birdbuddy")
        assert state.status_code == 200
        for secret in (
            "SYNTHETIC_ACCESS",
            "SYNTHETIC_REFRESH",
            "SYNTHETIC_GUEST_PASSWORD",
            "synthetic-guest@example.test",
        ):
            assert secret not in response.text + selected.text + state.text
        content = (config.state_dir / "birdbuddy-auth.json").read_text()
        assert "SYNTHETIC_REFRESH" in content and "SYNTHETIC_GUEST_PASSWORD" not in content
        assert "synthetic-guest@example.test" not in content
        assert (config.state_dir / "birdbuddy-auth.json").stat().st_mode & 0o777 == 0o600
        assert not (config.state_dir / "owner-operations.json").exists()
        history = config.state_dir / "birdbuddy-detections.json"
        history.write_bytes(b"synthetic retained history")
        removed = client.post("/v1/owner/birdbuddy/logout", json={"confirm": True}, headers=headers)
        assert removed.status_code == 200
        assert removed.json()["data"]["history_preserved"]
        assert removed.json()["data"]["remote_token_revoked"] is False
        assert history.read_bytes() == b"synthetic retained history"
        assert not (config.state_dir / "birdbuddy-auth.json").exists()


def test_provider_errors_never_echo_credentials_paths_or_server_payloads(tmp_path: Path) -> None:
    config = controller(tmp_path)
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        headers = login(client, config)
        with patch(
            "inky_bird_frame.birdbuddy._authenticate",
            side_effect=DataSourceError(
                "synthetic-guest@example.test SECRET_SERVER_PAYLOAD /private/source"
            ),
        ):
            response = client.post("/v1/owner/birdbuddy/login", json=credentials(), headers=headers)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "birdbuddy_account"
        assert (
            "SECRET_SERVER_PAYLOAD" not in response.text and "/private/source" not in response.text
        )
        assert "synthetic-guest@example.test" not in response.text
        assert not (config.state_dir / "birdbuddy-auth.json").exists()


@pytest.mark.parametrize(
    "field,value", [("email", ""), ("password", ""), ("email", "x" * 321), ("password", "x" * 4097)]
)
def test_invalid_bootstrap_inputs_fail_before_provider_call(
    tmp_path: Path, field: str, value: str
) -> None:
    config = controller(tmp_path)
    with TestClient(create_app(config), base_url=ORIGIN) as client:
        headers = login(client, config)
        with patch("inky_bird_frame.birdbuddy._authenticate") as authenticate:
            assert (
                client.post(
                    "/v1/owner/birdbuddy/login",
                    json={**credentials(), field: value},
                    headers=headers,
                ).status_code
                == 400
            )
            authenticate.assert_not_called()
