from __future__ import annotations

import asyncio
import gzip
import time
from dataclasses import replace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from joserfc import jwt
from joserfc.jwk import ECKey, RSAKey

from inky_bird_frame.access import OIDCTransaction
from inky_bird_frame.config import OIDCConfig
from inky_bird_frame.oidc import (
    MAX_OIDC_RESPONSE_BYTES,
    BoundedTransport,
    OIDCError,
    authorization_url,
    metadata,
    provider_id,
    verified_subject,
)

CONFIG = OIDCConfig(
    "https://id.example.test/application/o/inky/",
    "synthetic-client",
    "https://inky.example.test/v1/owner/oidc/callback",
    "SYNTHETIC_OIDC_SECRET",
)
TX = OIDCTransaction(
    "synthetic-state",
    "browser-hash",
    "synthetic-nonce",
    "v" * 64,
    provider_id(CONFIG),
    time.time(),
    False,
)


def _document() -> dict[str, object]:
    return {
        "issuer": CONFIG.issuer,
        "authorization_endpoint": "https://id.example.test/authorize",
        "token_endpoint": "https://id.example.test/token",
        "jwks_uri": "https://id.example.test/keys",
        "code_challenge_methods_supported": ["S256"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }


def test_authorization_uses_code_pkce_minimal_scope_and_exact_callback() -> None:
    with (
        patch.dict("os.environ", {CONFIG.client_secret_env: "synthetic-secret"}),
        patch(
            "inky_bird_frame.oidc.httpx2.AsyncHTTPTransport",
            return_value=httpx2.MockTransport(
                lambda request: httpx2.Response(200, json=_document())
            ),
        ),
    ):
        url = asyncio.run(authorization_url(CONFIG, TX))
    query = parse_qs(urlsplit(url).query)
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["openid"]
    assert query["redirect_uri"] == [CONFIG.callback_url]
    assert query["state"] == [TX.state]
    assert query["nonce"] == [TX.nonce]
    assert query["code_challenge_method"] == ["S256"]
    assert "code_challenge" in query
    assert "synthetic-secret" not in url and TX.verifier not in url


@pytest.mark.parametrize(
    "bad_claim", [None, "iss", "aud", "nonce", "exp", "iat", "sub", "azp", "signature"]
)
@pytest.mark.parametrize("algorithm", ["RS256", "ES256"])
def test_real_signed_identity_tokens_are_validated_and_secrets_are_redacted(
    bad_claim: str | None,
    algorithm: str,
) -> None:
    key = (
        RSAKey.generate_key(2048, parameters={"kid": "synthetic-signing-key"})
        if algorithm == "RS256"
        else ECKey.generate_key("P-256", parameters={"kid": "synthetic-signing-key"})
    )
    claims: dict[str, str | int] = {
        "iss": CONFIG.issuer,
        "aud": CONFIG.client_id,
        "sub": "owner-subject",
        "nonce": TX.nonce,
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    }
    if bad_claim in {"iss", "aud", "nonce", "azp"}:
        claims[str(bad_claim)] = "private-bad-claim"
    elif bad_claim == "exp":
        claims["exp"] = int(time.time()) - 1
    elif bad_claim == "iat":
        claims["iat"] = int(time.time()) + 300
    elif bad_claim == "sub":
        claims.pop("sub")
    token = jwt.encode({"alg": algorithm, "kid": "synthetic-signing-key"}, claims, key)
    if bad_claim == "signature":
        different = (
            RSAKey.generate_key(2048, parameters={"kid": "synthetic-signing-key"})
            if algorithm == "RS256"
            else ECKey.generate_key("P-256", parameters={"kid": "synthetic-signing-key"})
        )
        token = jwt.encode({"alg": algorithm, "kid": "synthetic-signing-key"}, claims, different)

    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("openid-configuration"):
            document = _document()
            document["id_token_signing_alg_values_supported"] = [algorithm]
            return httpx2.Response(200, json=document)
        if request.url.path == "/keys":
            return httpx2.Response(200, json={"keys": [key.as_dict()]})
        assert request.url.path == "/token" and request.method == "POST"
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["authorization_code"]
        assert form["code_verifier"] == [TX.verifier]
        assert form["redirect_uri"] == [CONFIG.callback_url]
        assert request.headers["authorization"].startswith("Basic ")
        assert "synthetic-secret" not in str(request.url)
        return httpx2.Response(
            200,
            json={
                "id_token": token,
                "access_token": "private-access-token",
                "token_type": "Bearer",
            },
        )

    with (
        patch.dict("os.environ", {CONFIG.client_secret_env: "synthetic-secret"}),
        patch(
            "inky_bird_frame.oidc.httpx2.AsyncHTTPTransport",
            return_value=httpx2.MockTransport(respond),
        ),
    ):
        if bad_claim is None:
            assert (
                asyncio.run(verified_subject(CONFIG, TX, "private-authorization-code"))
                == "owner-subject"
            )
        else:
            with pytest.raises(OIDCError) as raised:
                asyncio.run(verified_subject(CONFIG, TX, "private-authorization-code"))
            for secret in (
                token,
                "private-access-token",
                "private-bad-claim",
                "private-authorization-code",
                "synthetic-secret",
            ):
                assert secret not in str(raised.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("issuer", "https://wrong.example.test/"),
        ("token_endpoint", "http://id.example.test/token"),
        ("jwks_uri", "file:///keys"),
        ("code_challenge_methods_supported", ["plain"]),
        ("id_token_signing_alg_values_supported", ["HS256"]),
        ("authorization_endpoint", "https://user:password@id.example.test/auth"),
    ],
)
def test_metadata_fails_closed(field: str, value: object) -> None:
    document = _document()
    document[field] = value
    with (
        patch(
            "inky_bird_frame.oidc.httpx2.AsyncHTTPTransport",
            return_value=httpx2.MockTransport(lambda request: httpx2.Response(200, json=document)),
        ),
        pytest.raises(OIDCError),
    ):
        asyncio.run(metadata(CONFIG))


@pytest.mark.parametrize("compressed", [False, True])
def test_network_transport_caps_decoded_responses(compressed: bool) -> None:
    content = b"x" * (MAX_OIDC_RESPONSE_BYTES + 1)
    headers = {"Content-Encoding": "gzip"} if compressed else {}
    if compressed:
        content = gzip.compress(content)
    transport = BoundedTransport(
        httpx2.MockTransport(lambda request: httpx2.Response(200, content=content, headers=headers))
    )

    async def read() -> None:
        async with httpx2.AsyncClient(transport=transport) as client:
            await client.get("https://id.example.test/metadata")

    with pytest.raises(OIDCError, match="uncompressed" if compressed else "too large"):
        asyncio.run(read())


def test_missing_secret_and_provider_timeout_are_actionable_and_redacted() -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        patch(
            "inky_bird_frame.oidc.httpx2.AsyncHTTPTransport",
            return_value=httpx2.MockTransport(
                lambda request: httpx2.Response(200, json=_document())
            ),
        ),
        pytest.raises(OIDCError, match="secret is unavailable"),
    ):
        asyncio.run(authorization_url(CONFIG, TX))

    def timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("sensitive-provider-payload", request=request)

    with patch(
        "inky_bird_frame.oidc.httpx2.AsyncHTTPTransport", return_value=httpx2.MockTransport(timeout)
    ):
        with pytest.raises(OIDCError) as raised:
            asyncio.run(metadata(CONFIG))
        assert "sensitive-provider-payload" not in str(raised.value)


def test_identity_configuration_changes_have_a_new_session_binding() -> None:
    assert provider_id(CONFIG) != provider_id(replace(CONFIG, owner_subject="replacement-owner"))
    assert provider_id(CONFIG) != provider_id(
        replace(CONFIG, issuer="https://replacement.example.test/")
    )
