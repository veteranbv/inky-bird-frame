"""One HTTP application with separate catalog and owner trust boundaries."""

from __future__ import annotations

import json
from dataclasses import asdict
from ipaddress import ip_address
from pathlib import Path
from typing import TYPE_CHECKING, cast

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from .access import (
    MAX_PASSWORD_CHARACTERS,
    MIN_PASSWORD_CHARACTERS,
    OWNER_ABSOLUTE_SECONDS,
    RECENT_AUTH_SECONDS,
    REMEMBERED_ABSOLUTE_SECONDS,
    AccessConfirmationRequiredError,
    AccessDeniedError,
    AccessError,
    AccessRateLimitError,
    AccessStore,
    SessionCredentials,
    session_csrf_token,
)
from .config import ControllerConfig, OwnerAccessMode
from .oidc import OIDCError, authorization_url, metadata, provider_id, verified_identity
from .server import CatalogService

if TYPE_CHECKING:
    from uvicorn._types import ASGI3Application

MAX_OWNER_JSON_BYTES = 8 * 1024
OWNER_COOKIE = "__Host-inky-owner"
LOCAL_OWNER_COOKIE = "inky-local-owner"
PENDING_OIDC_COOKIE = "__Host-inky-sign-in"
OWNER_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; base-uri 'none'; object-src 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)


def _loopback(value: str | None) -> bool:
    try:
        return value is not None and ip_address(value).is_loopback
    except ValueError:
        return False


def _origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


def _secure_access(request: Request, config: ControllerConfig) -> bool:
    if len(request.headers.getlist("host")) != 1:
        return False
    if request.url.scheme == "https":
        return _origin(request) in config.owner_allowed_origins
    if config.owner_access_mode == OwnerAccessMode.HTTPS_ONLY:
        return False
    if request.url.scheme != "http" or request.client is None:
        return False
    try:
        peer = ip_address(request.client.host)
        # Do not use is_private: it also includes unspecified and reserved ranges.
        from ipaddress import ip_network

        networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
        private_peer = peer.is_loopback or any(peer in ip_network(n) for n in networks)
    except ValueError:
        return False
    if not private_peer:
        return False
    host = request.url.hostname
    if _origin(request) in config.owner_allowed_origins:
        return True
    server = request.scope.get("server")
    if server is None or (request.url.port or 80) != config.port or server[1] != config.port:
        return False
    # Uvicorn obtains this address from the accepted socket, not the Host header.
    # Wildcard binds thus accept the real local IP, never an arbitrary DNS name.
    if host is not None and host == server[0] and host not in {"0.0.0.0", "::"}:
        try:
            address = ip_address(host)
            return address.is_loopback or any(address in ip_network(n) for n in networks)
        except ValueError:
            return False
    return host == "localhost" and _loopback(server[0]) and peer.is_loopback


def _cookie_name(request: Request) -> str:
    return OWNER_COOKIE if request.url.scheme == "https" else LOCAL_OWNER_COOKIE


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": {"code": code, "message": message}, "schema_version": 1},
        status_code=status,
    )


def _result(data: object) -> JSONResponse:
    return JSONResponse({"ok": True, "data": data, "schema_version": 1})


async def _json_body(
    request: Request,
    fields: set[str],
    *,
    optional: set[str] | None = None,
) -> dict[str, str | bool]:
    if request.headers.get("content-type", "").partition(";")[0] != "application/json":
        raise AccessError("Send this operation as application/json")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_OWNER_JSON_BYTES:
            raise AccessError("Owner request exceeds the supported size")
    try:
        value: object = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AccessError("Owner request contains invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or not fields <= set(value)
        or set(value) - fields - (optional or set())
    ):
        raise AccessError("Owner request has unsupported fields")
    boolean_fields = {"remember", "return_to_access"}
    if any(not isinstance(item, str) for key, item in value.items() if key not in boolean_fields):
        raise AccessError("Owner request fields must be text")
    for key in boolean_fields & value.keys():
        if not isinstance(value[key], bool):
            raise AccessError(f"{key} must be true or false")
    return cast(dict[str, str | bool], value)


class OwnerBoundary:
    """Protect all owner responses, including method and exception responses."""

    def __init__(self, app: ASGIApp, config: ControllerConfig) -> None:
        self.app = app
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        private = request.url.path.startswith("/v1/owner/")

        async def protected_send(message: Message) -> None:
            if message["type"] == "http.response.start" and (
                private or request.url.path.startswith("/app") or request.url.path == "/v1/access"
            ):
                headers = cast(list[tuple[bytes, bytes]], message.setdefault("headers", []))
                headers.extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"content-security-policy", OWNER_CSP.encode()),
                    ]
                )
            await send(message)

        # The complete owner gate runs before an endpoint reads credentials.
        if private and not _secure_access(request, self.config):
            await _error(
                403,
                "secure_access_required",
                "Open the configured Inky address. Use HTTPS when this installation requires it.",
            )(scope, receive, protected_send)
            return
        if private and request.method not in {"GET", "HEAD"}:
            origins = request.headers.getlist("origin")
            if (
                origins != [_origin(request)]
                or request.headers.get("sec-fetch-site") == "cross-site"
            ):
                await _error(403, "origin_denied", "Open Inky directly before making this change")(
                    scope, receive, protected_send
                )
                return
        await self.app(scope, receive, protected_send)


def create_app(config: ControllerConfig) -> ASGIApp:
    """Build the single application; no separate server or client-side state store."""
    store = AccessStore(
        config.state_dir,
        oidc_provider_id=provider_id(config.oidc) if config.oidc else None,
        password_login_enabled=not (config.oidc and config.oidc.sso_only),
    )
    if config.oidc and config.oidc.sso_only and not store.oidc_tested():
        raise AccessError(
            "Link and test single sign-on before enabling sso_only; "
            "local operator recovery remains available"
        )
    catalog = CatalogService(config)
    assets = Path(__file__).parent / "web"

    def set_session_cookie(
        request: Request, credentials: SessionCredentials, response: Response
    ) -> None:
        response.set_cookie(
            _cookie_name(request),
            credentials.secret,
            path="/",
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            max_age=REMEMBERED_ABSOLUTE_SECONDS if credentials.remembered else None,
        )

    def issue_session(request: Request, credentials: SessionCredentials) -> JSONResponse:
        response = _result({"csrf": credentials.csrf})
        set_session_cookie(request, credentials, response)
        return response

    async def provider_status(
        request: Request, *, check_unlinked: bool = False
    ) -> dict[str, object] | None:
        provider: dict[str, object] | None = None
        if config.oidc and request.url.scheme == "https":
            linked = await run_in_threadpool(
                store.oidc_linked, config.oidc.issuer, config.oidc.owner_subject
            )
            provider = {"label": config.oidc.label, "ready": False, "linked": linked}
            if linked or check_unlinked:
                try:
                    await metadata(config.oidc)
                    # Do not return the secret or its reference to unsigned-in clients.
                    import os

                    if not os.environ.get(config.oidc.client_secret_env):
                        raise OIDCError(
                            "Sign-in provider secret is unavailable; ask the installer to check it"
                        )
                    provider["ready"] = True
                except OIDCError as exc:
                    provider["error"] = str(exc)
        return provider

    async def safe_access_state(request: Request) -> JSONResponse:
        secure = _secure_access(request, config)
        if not secure:
            return _result({"secure": False, "setup_required": None})
        setup = await run_in_threadpool(store.setup_status)
        provider = await provider_status(request)
        return _result(
            {
                "secure": True,
                "setup_required": setup is not None,
                "setup_code": asdict(setup) if setup is not None else None,
                "password_min_length": MIN_PASSWORD_CHARACTERS,
                "password_max_length": MAX_PASSWORD_CHARACTERS,
                "encrypted": request.url.scheme == "https",
                "access_mode": config.owner_access_mode.value,
                "legacy_account": await run_in_threadpool(store.legacy_account),
                "session_policy": {
                    "regular_hours": OWNER_ABSOLUTE_SECONDS // 3600,
                    "remembered_days": REMEMBERED_ABSOLUTE_SECONDS // 86400,
                },
                "provider": provider,
                "local_login": not (config.oidc and config.oidc.sso_only),
            }
        )

    async def establish(request: Request) -> JSONResponse:
        body = await _json_body(request, {"secret", "passphrase"})
        credentials = await run_in_threadpool(
            store.establish_owner, cast(str, body["secret"]), cast(str, body["passphrase"])
        )
        return issue_session(request, credentials)

    async def login(request: Request) -> JSONResponse:
        if config.oidc and config.oidc.sso_only:
            raise AccessDeniedError(
                "Use single sign-on for this installation; "
                "local operator recovery remains available"
            )
        body = await _json_body(request, {"passphrase"}, optional={"username", "remember"})
        credentials = await run_in_threadpool(
            store.login,
            cast(str, body["passphrase"]),
            username=cast(str | None, body.get("username")),
            remembered=body.get("remember") is True,
        )
        return issue_session(request, credentials)

    async def register(request: Request) -> JSONResponse:
        body = await _json_body(request, {"username", "passphrase"}, optional={"remember"})
        credentials = await run_in_threadpool(
            store.register_owner,
            cast(str, body["username"]),
            cast(str, body["passphrase"]),
            remembered=body.get("remember") is True,
        )
        return issue_session(request, credentials)

    async def session(request: Request) -> JSONResponse:
        secret = request.cookies.get(_cookie_name(request), "")
        active = await run_in_threadpool(store.authenticate, secret)
        return _result(
            {
                "csrf": session_csrf_token(secret),
                "absolute_expires_at": active.expires_at,
                "authentication_expires_at": active.authenticated_at + RECENT_AUTH_SECONDS,
                "single_sign_on": active.provider_id is not None,
                "remembered": active.remembered,
            }
        )

    async def logout(request: Request) -> JSONResponse:
        secret = request.cookies.get(_cookie_name(request), "")
        token = request.headers.get("x-inky-csrf")
        if not token:
            raise AccessDeniedError("Refresh the page before making this change")
        await run_in_threadpool(store.authenticate, secret, csrf=token)
        await run_in_threadpool(store.logout, secret)
        response = _result({"signed_out": True})
        response.delete_cookie(
            _cookie_name(request),
            path="/",
            secure=request.url.scheme == "https",
            httponly=True,
            samesite="strict",
        )
        return response

    async def authorized(request: Request) -> str:
        secret = request.cookies.get(_cookie_name(request), "")
        token = request.headers.get("x-inky-csrf")
        if request.method != "GET" and not token:
            raise AccessDeniedError("Refresh the page before making this change")
        await run_in_threadpool(store.authenticate, secret, csrf=token)
        return secret

    async def list_sessions(request: Request) -> JSONResponse:
        secret = await authorized(request)
        return _result({"sessions": await run_in_threadpool(store.sessions, secret)})

    async def activity(request: Request) -> JSONResponse:
        secret = await authorized(request)
        await _json_body(request, set())
        await run_in_threadpool(store.authenticate, secret, active=True)
        return _result({"active": True})

    async def confirm_password(request: Request) -> JSONResponse:
        secret = await authorized(request)
        if config.oidc and config.oidc.sso_only:
            raise AccessError(
                "Local password confirmation is disabled; confirm with single sign-on"
            )
        body = await _json_body(request, {"passphrase"})
        await run_in_threadpool(store.confirm_password, secret, cast(str, body["passphrase"]))
        return _result({"confirmed": True})

    async def revoke_session(request: Request) -> JSONResponse:
        secret = await authorized(request)
        body = await _json_body(request, {"id"})
        await run_in_threadpool(store.revoke_session, secret, cast(str, body["id"]))
        return _result({"revoked": True})

    async def access_settings(request: Request) -> JSONResponse:
        secret = await authorized(request)
        provider = await provider_status(request, check_unlinked=True)
        return _result(
            {
                "mode": config.owner_access_mode.value,
                "encrypted": request.url.scheme == "https",
                "managed": True,
                "origins": config.owner_allowed_origins,
                "provider": {
                    **(provider or {}),
                    "label": config.oidc.label,
                    "issuer": config.oidc.issuer,
                    "client_id": config.oidc.client_id,
                    "callback_url": config.oidc.callback_url,
                    "client_secret_env": config.oidc.client_secret_env,
                    "linked": await run_in_threadpool(
                        store.oidc_linked, config.oidc.issuer, config.oidc.owner_subject
                    ),
                    "identity_managed": config.oidc.owner_subject is not None,
                    "sso_only": config.oidc.sso_only,
                }
                if config.oidc
                else None,
                "username": await run_in_threadpool(store.account_username, secret),
            }
        )

    async def start_oidc(request: Request) -> JSONResponse:
        if config.oidc is None or request.url.scheme != "https":
            raise AccessError("Single sign-on needs a configured HTTPS provider")
        body = await _json_body(request, set(), optional={"remember", "return_to_access"})
        link = request.url.path.endswith("/link")
        secret = await authorized(request) if link else None
        if not link and not await run_in_threadpool(
            store.oidc_linked, config.oidc.issuer, config.oidc.owner_subject
        ):
            raise AccessDeniedError("Sign in locally and link your provider account first")
        tx, browser_secret = await run_in_threadpool(
            store.begin_oidc,
            remembered=body.get("remember") is True,
            link_secret=secret,
            return_to_access=body.get("return_to_access") is True,
        )
        url = await authorization_url(config.oidc, tx)
        response = _result({"authorization_url": url})
        response.set_cookie(
            PENDING_OIDC_COOKIE,
            browser_secret,
            path="/",
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=600,
        )
        return response

    async def oidc_callback(request: Request) -> Response:
        if request.method != "GET":
            return Response(status_code=405, headers={"Allow": "GET"})
        if config.oidc is None or str(request.url).partition("?")[0] != config.oidc.callback_url:
            return RedirectResponse("/app?sign_in=failed", status_code=303)
        response: Response
        try:
            values = request.query_params
            if len(request.scope.get("query_string", b"")) > MAX_OWNER_JSON_BYTES or any(
                len(values.getlist(key)) != 1 for key in values
            ):
                raise AccessDeniedError("Invalid sign-in response; start sign-in again")
            tx = await run_in_threadpool(
                store.consume_oidc,
                values.get("state", ""),
                request.cookies.get(PENDING_OIDC_COOKIE, ""),
            )
            if values.get("error") or not values.get("code"):
                raise AccessDeniedError(
                    "Provider sign-in was cancelled or denied. Start sign-in again."
                )
            if values.get("iss") not in {None, config.oidc.issuer}:
                raise AccessDeniedError("Provider issuer changed; start sign-in again")
            identity = await verified_identity(config.oidc, tx, values["code"])
            credentials = await run_in_threadpool(
                store.complete_oidc,
                tx,
                config.oidc.issuer,
                identity.subject,
                authenticated_at=identity.authenticated_at,
                configured_subject=config.oidc.owner_subject,
            )
            destination = (
                "/app?sign_in=linked#account"
                if tx.owner_session_hash
                else "/app?sign_in=confirmed#account"
                if tx.return_to_access
                else "/app"
            )
            response = RedirectResponse(destination, status_code=303)
            set_session_cookie(request, credentials, response)
        except AccessError:
            # The browser address is cleaned before displaying an error. Neither
            # an authorization code nor provider error text stays in the app URL.
            response = RedirectResponse("/app?sign_in=failed", status_code=303)
        response.delete_cookie(
            PENDING_OIDC_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
        )
        return response

    async def unlink_oidc(request: Request) -> JSONResponse:
        secret = await authorized(request)
        if config.oidc and (config.oidc.sso_only or config.oidc.owner_subject is not None):
            raise AccessError(
                "This identity is managed in configuration. Enable local login "
                "and remove the configured identity before unlinking."
            )
        await run_in_threadpool(store.unlink_oidc, secret)
        return _result({"unlinked": True})

    async def update_account(request: Request) -> JSONResponse:
        secret = await authorized(request)
        body = await _json_body(request, {"username"}, optional={"passphrase"})
        credentials = await run_in_threadpool(
            store.update_account,
            secret,
            cast(str, body["username"]),
            cast(str | None, body.get("passphrase")),
        )
        response = _result(
            {"updated": True, "other_sessions_revoked": True, "csrf": credentials.csrf}
        )
        set_session_cookie(request, credentials, response)
        return response

    async def page(request: Request) -> Response:
        from starlette.responses import FileResponse

        filename = {
            "/app": "index.html",
            "/app/": "index.html",
            "/app/app.js": "app.js",
            "/app/app.css": "app.css",
            "/app/access-guide": "access-guide.txt",
        }[request.url.path]
        if filename == "access-guide.txt":
            guide = assets / filename
            if not guide.is_file():
                # Editable installs use the same source document that a wheel bundles.
                guide = Path(__file__).resolve().parents[2] / "docs" / "browser-access.md"
            return FileResponse(guide, media_type="text/plain", filename="Inky-access-guide.md")
        return FileResponse(assets / filename)

    async def root(request: Request) -> Response:
        return RedirectResponse("/app")

    routes = [
        Route("/", root),
        Route("/app", page),
        Route("/app/", page),
        Route("/app/app.js", page),
        Route("/app/app.css", page),
        Route("/app/access-guide", page),
        Route("/v1/access", safe_access_state),
        Route("/v1/owner/setup", establish, methods=["POST"]),
        Route("/v1/owner/register", register, methods=["POST"]),
        Route("/v1/owner/login", login, methods=["POST"]),
        Route("/v1/owner/session", session),
        Route("/v1/owner/logout", logout, methods=["POST"]),
        Route("/v1/owner/sessions", list_sessions),
        Route("/v1/owner/activity", activity, methods=["POST"]),
        Route("/v1/owner/sessions/revoke", revoke_session, methods=["POST"]),
        Route("/v1/owner/confirm-password", confirm_password, methods=["POST"]),
        Route("/v1/owner/access", access_settings),
        Route("/v1/owner/account", update_account, methods=["POST"]),
        Route("/v1/owner/oidc/start", start_oidc, methods=["POST"]),
        Route("/v1/owner/oidc/link", start_oidc, methods=["POST"]),
        Route("/v1/owner/oidc/unlink", unlink_oidc, methods=["POST"]),
        Route("/v1/owner/oidc/callback", oidc_callback),
        Route(
            "/{path:path}",
            catalog.dispatch,
            methods=[
                "GET",
                "HEAD",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
                "OPTIONS",
                "TRACE",
                "CONNECT",
            ],
        ),
    ]

    async def access_error(request: Request, exc: Exception) -> Response:
        if isinstance(exc, AccessRateLimitError):
            return _error(429, "sign_in_rate_limited", str(exc))
        if isinstance(exc, AccessConfirmationRequiredError):
            return _error(403, "confirmation_required", str(exc))
        if isinstance(exc, AccessDeniedError):
            return _error(401, "sign_in_required", str(exc))
        return _error(400, "access_error", str(exc))

    app: ASGIApp = OwnerBoundary(
        Starlette(routes=routes, exception_handlers={AccessError: access_error}), config
    )
    if config.trusted_proxy_addresses:
        # Both libraries implement ASGI 3, but publish different typing aliases.
        app = cast(
            ASGIApp,
            ProxyHeadersMiddleware(
                cast("ASGI3Application", app), trusted_hosts=list(config.trusted_proxy_addresses)
            ),
        )
    return app
