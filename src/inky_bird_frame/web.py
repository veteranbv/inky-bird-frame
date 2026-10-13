"""One HTTP application with separate catalog and owner trust boundaries."""

from __future__ import annotations

import json
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
    OWNER_ABSOLUTE_SECONDS,
    AccessDeniedError,
    AccessError,
    AccessRateLimitError,
    AccessStore,
    SessionCredentials,
    session_csrf_token,
)
from .config import ControllerConfig
from .server import CatalogService

if TYPE_CHECKING:
    from uvicorn._types import ASGI3Application

MAX_OWNER_JSON_BYTES = 8 * 1024
OWNER_COOKIE = "__Host-inky-owner"
LOCAL_OWNER_COOKIE = "inky-local-owner"
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
    if request.url.scheme == "https":
        return _origin(request) in config.owner_allowed_origins
    host = request.url.hostname
    return (
        request.url.scheme == "http"
        and (host == "localhost" or _loopback(host))
        and _loopback(config.bind_host)
        and request.client is not None
        and _loopback(request.client.host)
        and (request.url.port or 80) == config.port
    )


def _cookie_name(request: Request) -> str:
    return OWNER_COOKIE if request.url.scheme == "https" else LOCAL_OWNER_COOKIE


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": {"code": code, "message": message}, "schema_version": 1},
        status_code=status,
    )


def _result(data: object) -> JSONResponse:
    return JSONResponse({"ok": True, "data": data, "schema_version": 1})


async def _json_body(request: Request, fields: set[str]) -> dict[str, str]:
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
    if not isinstance(value, dict) or set(value) != fields:
        raise AccessError("Owner request has unsupported fields")
    if any(not isinstance(item, str) for item in value.values()):
        raise AccessError("Owner request fields must be text")
    return cast(dict[str, str], value)


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
                "Use HTTPS or genuine loopback access before signing in",
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
    store = AccessStore(config.state_dir)
    catalog = CatalogService(config)
    assets = Path(__file__).parent / "web"

    def issue_session(request: Request, credentials: SessionCredentials) -> JSONResponse:
        response = _result({"csrf": credentials.csrf})
        response.set_cookie(
            _cookie_name(request),
            credentials.secret,
            path="/",
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            max_age=OWNER_ABSOLUTE_SECONDS,
        )
        return response

    async def safe_access_state(request: Request) -> JSONResponse:
        secure = _secure_access(request, config)
        if not secure:
            return _result({"secure": False, "setup_required": None})
        return _result(
            {"secure": True, "setup_required": not await run_in_threadpool(store.owner_exists)}
        )

    async def establish(request: Request) -> JSONResponse:
        body = await _json_body(request, {"secret", "passphrase"})
        credentials = await run_in_threadpool(
            store.establish_owner, body["secret"], body["passphrase"]
        )
        return issue_session(request, credentials)

    async def login(request: Request) -> JSONResponse:
        body = await _json_body(request, {"passphrase"})
        credentials = await run_in_threadpool(store.login, body["passphrase"])
        return issue_session(request, credentials)

    async def session(request: Request) -> JSONResponse:
        secret = request.cookies.get(_cookie_name(request), "")
        active = await run_in_threadpool(store.authenticate, secret)
        return _result(
            {
                "csrf": session_csrf_token(secret),
                "absolute_expires_at": active.created_at + OWNER_ABSOLUTE_SECONDS,
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

    async def page(request: Request) -> Response:
        from starlette.responses import FileResponse

        filename = {
            "/app": "index.html",
            "/app/": "index.html",
            "/app/app.js": "app.js",
            "/app/app.css": "app.css",
        }[request.url.path]
        return FileResponse(assets / filename)

    async def root(request: Request) -> Response:
        return RedirectResponse("/app")

    routes = [
        Route("/", root),
        Route("/app", page),
        Route("/app/", page),
        Route("/app/app.js", page),
        Route("/app/app.css", page),
        Route("/v1/access", safe_access_state),
        Route("/v1/owner/setup", establish, methods=["POST"]),
        Route("/v1/owner/login", login, methods=["POST"]),
        Route("/v1/owner/session", session),
        Route("/v1/owner/logout", logout, methods=["POST"]),
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
