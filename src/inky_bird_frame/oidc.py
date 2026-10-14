"""Bounded OpenID Connect code exchange using maintained protocol libraries.

Authlib owns OAuth request construction and ID-token claim validation.
JosERFC owns signature validation. Inky owns endpoint policy, response bounds
and the private one-use browser transaction, not protocol cryptography.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import urlsplit

import httpx2
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oidc.core import CodeIDToken
from joserfc import jwt
from joserfc.jwk import KeySet, KeySetSerialization

from .access import AccessError, OIDCTransaction
from .config import OIDCConfig

# Metadata, key sets and tokens are small documents, not media or history.
MAX_OIDC_RESPONSE_BYTES = 256 * 1024
OIDC_TIMEOUT_SECONDS = 10
ALGORITHMS = ("RS256", "ES256")


class OIDCError(AccessError):
    """A sanitized provider failure with no raw payload or protocol secrets."""


@dataclass(frozen=True)
class ProviderMetadata:
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    algorithms: tuple[str, ...]


class OAuthClient(Protocol):
    def create_authorization_url(self, url: str, **kwargs: object) -> tuple[str, str]: ...
    async def fetch_token(self, url: str, **kwargs: object) -> dict[str, object]: ...
    async def aclose(self) -> None: ...


class ClaimsValidator(Protocol):
    def validate(self, *, leeway: int = 0) -> None: ...


class BoundedTransport(httpx2.AsyncBaseTransport):
    """Cap decoded responses before the OAuth client reads or logs a payload."""

    def __init__(self, inner: httpx2.AsyncBaseTransport | None = None) -> None:
        self.inner = inner or httpx2.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self.inner.handle_async_request(request)
        try:
            # These small documents do not need compression. Reject a provider
            # that ignores identity encoding before an expansion can consume memory.
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise OIDCError("Sign-in provider must honor uncompressed identity responses")
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_OIDC_RESPONSE_BYTES:
                raise OIDCError("Sign-in provider response is too large; check its configuration")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_OIDC_RESPONSE_BYTES:
                    raise OIDCError(
                        "Sign-in provider response is too large; check its configuration"
                    )
            return httpx2.Response(
                response.status_code,
                content=bytes(body),
                headers={"content-type": response.headers.get("content-type", "")},
                request=request,
            )
        finally:
            await response.aclose()

    async def aclose(self) -> None:
        await self.inner.aclose()


def provider_id(config: OIDCConfig) -> str:
    value = json.dumps([config.issuer, config.client_id, config.callback_url, config.owner_subject])
    return hashlib.sha256(value.encode()).hexdigest()


def _https_endpoint(value: object) -> str:
    if not isinstance(value, str):
        raise OIDCError("Sign-in provider metadata is incomplete; check its discovery document")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise OIDCError("Sign-in provider has an invalid HTTPS endpoint") from None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or parts.query
        or port == 0
    ):
        raise OIDCError("Sign-in provider endpoints must use HTTPS without credentials or query")
    return value


def _client(config: OIDCConfig, transaction: OIDCTransaction | None = None) -> OAuthClient:
    secret = os.environ.get(config.client_secret_env)
    if not secret:
        raise OIDCError(
            "Sign-in provider secret is unavailable; set its configured environment variable"
        )
    # Authlib lacks PEP 561 metadata. Keep that untyped boundary inside this
    # narrow adapter; application code consumes the checked models above.
    factory = cast(Callable[..., OAuthClient], AsyncOAuth2Client)
    return factory(
        client_id=config.client_id,
        client_secret=secret,
        scope="openid",
        redirect_uri=config.callback_url,
        state=transaction.state if transaction else None,
        code_challenge_method="S256",
        token_endpoint_auth_method="client_secret_basic",
        transport=BoundedTransport(),
        timeout=OIDC_TIMEOUT_SECONDS,
        follow_redirects=False,
        trust_env=False,
        headers={"Accept-Encoding": "identity"},
    )


async def _get_json(url: str) -> dict[str, object]:
    async with httpx2.AsyncClient(
        transport=BoundedTransport(),
        timeout=OIDC_TIMEOUT_SECONDS,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        response = await client.get(
            url, headers={"Accept": "application/json", "Accept-Encoding": "identity"}
        )
        if response.status_code != 200:
            raise OIDCError(
                "Sign-in provider is unavailable; check its service and HTTPS endpoints"
            )
        value: object = response.json()
    if not isinstance(value, dict):
        raise OIDCError("Sign-in provider returned invalid metadata")
    return cast(dict[str, object], value)


async def metadata(config: OIDCConfig) -> ProviderMetadata:
    try:
        async with asyncio.timeout(OIDC_TIMEOUT_SECONDS):
            value = await _get_json(config.issuer.rstrip("/") + "/.well-known/openid-configuration")
            if value.get("issuer") != config.issuer:
                raise OIDCError("Sign-in provider issuer does not match the configured issuer")
            algorithms = value.get("id_token_signing_alg_values_supported")
            supported = tuple(
                a for a in ALGORITHMS if isinstance(algorithms, list) and a in algorithms
            )
            if not supported:
                raise OIDCError("Sign-in provider has no supported asymmetric signing algorithm")
            methods = value.get("code_challenge_methods_supported")
            if not isinstance(methods, list) or "S256" not in methods:
                raise OIDCError("Sign-in provider must support PKCE S256")
            return ProviderMetadata(
                _https_endpoint(value.get("authorization_endpoint")),
                _https_endpoint(value.get("token_endpoint")),
                _https_endpoint(value.get("jwks_uri")),
                supported,
            )
    except OIDCError:
        raise
    except (httpx2.HTTPError, TimeoutError, ValueError):
        raise OIDCError(
            "Cannot connect to the sign-in provider; check HTTPS, discovery and service health"
        ) from None


async def authorization_url(config: OIDCConfig, tx: OIDCTransaction) -> str:
    endpoints = await metadata(config)
    client = _client(config, tx)
    try:
        url, _ = client.create_authorization_url(
            endpoints.authorization_endpoint,
            state=tx.state,
            nonce=tx.nonce,
            code_verifier=tx.verifier,
            response_mode="query",
        )
        return url
    finally:
        await client.aclose()


async def verified_subject(config: OIDCConfig, tx: OIDCTransaction, code: str) -> str:
    try:
        async with asyncio.timeout(3 * OIDC_TIMEOUT_SECONDS):
            endpoints = await metadata(config)
            client = _client(config, tx)
            try:
                tokens = await client.fetch_token(
                    endpoints.token_endpoint,
                    code=code,
                    grant_type="authorization_code",
                    code_verifier=tx.verifier,
                )
            finally:
                await client.aclose()
            token = tokens.get("id_token")
            if not isinstance(token, str) or not token:
                raise OIDCError(
                    "Sign-in provider did not return an identity token; check the openid scope"
                )
            # Fetch a fresh key set for every bounded exchange. There is no stale
            # key cache to defeat normal provider signing-key rotation.
            raw_keys = await _get_json(endpoints.jwks_uri)
            if not isinstance(raw_keys.get("keys"), list):
                raise OIDCError("Sign-in provider returned an invalid signing key set")
            keys = KeySet.import_key_set(cast(KeySetSerialization, raw_keys))
            decoded = jwt.decode(token, keys, algorithms=endpoints.algorithms)
            factory = cast(Callable[..., ClaimsValidator], CodeIDToken)
            claims = factory(
                decoded.claims,
                decoded.header,
                {
                    "iss": {"essential": True, "value": config.issuer},
                    "aud": {"essential": True, "value": config.client_id},
                },
                {
                    "nonce": tx.nonce,
                    "client_id": config.client_id,
                    "access_token": tokens.get("access_token"),
                },
            )
            claims.validate(leeway=0)
            subject = decoded.claims.get("sub")
            if (
                not isinstance(subject, str)
                or not subject.isascii()
                or not 1 <= len(subject) <= 255
            ):
                raise OIDCError("Sign-in provider returned an invalid account identity")
            return subject
    except OIDCError:
        raise
    except Exception:
        # Protocol exceptions can include provider descriptions, claims or
        # response URLs. Never forward them into logs, snapshots or HTTP errors.
        raise OIDCError(
            "Provider sign-in could not be verified. Try again; "
            "check the provider configuration if it repeats."
        ) from None
