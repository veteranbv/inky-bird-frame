"""One HTTP application with separate catalog and owner trust boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from . import __version__
from .access import (
    MAX_PASSWORD_CHARACTERS,
    MIN_PASSWORD_CHARACTERS,
    OWNER_ABSOLUTE_SECONDS,
    RECENT_AUTH_SECONDS,
    REMEMBERED_ABSOLUTE_SECONDS,
    AccessConfirmationRequiredError,
    AccessDeniedError,
    AccessError,
    AccessPasswordConfirmationError,
    AccessRateLimitError,
    AccessStore,
    SessionCredentials,
    session_csrf_token,
)
from .birdbuddy import (
    BirdBuddyFeederSelectionRequiredError,
    birdbuddy_status,
    login_birdbuddy,
    logout_birdbuddy,
)
from .catalog import read_json
from .community import CommunityHistory
from .config import AppConfig, ControllerConfig, OwnerAccessMode
from .controller import change_saved_collection, read_generation_work
from .errors import CatalogError, DataSourceError
from .imports import MAX_UPLOAD_BYTES, HistoryImports
from .journal import (
    MAX_JOURNAL_BYTES,
    JournalConflictError,
    JournalError,
    JournalService,
    JournalViewRemovedError,
    remove_collection,
)
from .oidc import OIDCError, authorization_url, metadata, provider_id, verified_identity
from .owner_operations import (
    OperationKind,
    OwnerOperationBusyError,
    OwnerOperationError,
    OwnerOperations,
    execute_owner_operation,
)
from .presentation import GRANT_DAYS, PresentationAccess
from .server import CatalogService
from .settings import (
    SettingsConflictError,
    SettingsError,
    confirmed_config,
    current_config,
    save_settings,
    settings_status,
)
from .timeutil import parse_utc_timestamp

if TYPE_CHECKING:
    from uvicorn._types import ASGI3Application

MAX_OWNER_JSON_BYTES = 8 * 1024
OWNER_COOKIE = "__Host-inky-owner"
LOCAL_OWNER_COOKIE = "inky-local-owner"
PENDING_OIDC_COOKIE = "__Host-inky-sign-in"
OWNER_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
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
    structured: set[str] | None = None,
    maximum: int = MAX_OWNER_JSON_BYTES,
) -> dict[str, object]:
    if request.headers.get("content-type", "").partition(";")[0] != "application/json":
        raise AccessError("Send this operation as application/json")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum:
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
    if any(
        not isinstance(item, str)
        for key, item in value.items()
        if key not in boolean_fields | (structured or set())
    ):
        raise AccessError("Owner request fields must be text")
    for key in boolean_fields & value.keys():
        if not isinstance(value[key], bool):
            raise AccessError(f"{key} must be true or false")
    return cast(dict[str, object], value)


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
        private = request.url.path.startswith(
            ("/v1/owner/", "/v1/screen/", "/v1/integrations/", "/v1/dashboard/", "/wall/")
        )

        async def protected_send(message: Message) -> None:
            if message["type"] == "http.response.start" and (
                private
                or request.url.path.startswith("/app")
                or request.url.path in {"/v1/access", "/screen"}
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
                if request.url.path == "/v1/dashboard/art":
                    # Only this minimal explicitly enabled document can be framed.
                    # Its own CSP is built from validated installer-owned origins.
                    headers[:] = [
                        (key, value)
                        for key, value in headers
                        if key != b"content-security-policy" or value != OWNER_CSP.encode()
                    ]
            await send(message)

        # The complete owner gate runs before an endpoint reads credentials.
        if private and not _secure_access(request, self.config):
            await _error(
                403,
                "secure_access_required",
                "Open the configured Inky address. Use HTTPS when this installation requires it.",
            )(scope, receive, protected_send)
            return
        if (
            private
            and not request.url.path.startswith("/wall/")
            and request.method not in {"GET", "HEAD"}
        ):
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


def create_app(config: ControllerConfig, *, app_config: AppConfig | None = None) -> ASGIApp:
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
    journal = JournalService(config, app_config=app_config)
    community = CommunityHistory(config.state_dir)
    presentation = PresentationAccess(config.state_dir, journal)
    operations = OwnerOperations(config.state_dir)
    assets = Path(__file__).parent / "web"

    @asynccontextmanager
    async def foreground() -> AsyncIterator[None]:
        reservation = operations.foreground()
        # State locks can wait for scheduled CLI activity. Never acquire them
        # on the ASGI event loop and stall unrelated reads or sign-in requests.
        await run_in_threadpool(reservation.__enter__)
        try:
            yield
        finally:
            await run_in_threadpool(reservation.__exit__, None, None, None)

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
        if app_config is not None:
            try:
                async with foreground():
                    await run_in_threadpool(
                        HistoryImports(app_config).cancel, uncommitted_only=True
                    )
            except OwnerOperationBusyError:
                # An already confirmed commit may finish. Uploads and previews
                # check authority again before leaving their reservation.
                pass
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
        confirm = body.get("return_to_access") is True and not link
        secret = await authorized(request) if link or confirm else None
        if not link and not await run_in_threadpool(
            store.oidc_linked, config.oidc.issuer, config.oidc.owner_subject
        ):
            raise AccessDeniedError("Sign in locally and link your provider account first")
        tx, browser_secret = await run_in_threadpool(
            store.begin_oidc,
            remembered=body.get("remember") is True,
            link_secret=secret if link else None,
            confirm_secret=secret if confirm else None,
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

    async def journal_snapshot(request: Request) -> JSONResponse:
        await authorized(request)
        return _result(await run_in_threadpool(journal.snapshot))

    async def journal_health(request: Request) -> JSONResponse:
        await authorized(request)
        return _result(await run_in_threadpool(journal.health))

    async def application_settings(request: Request) -> JSONResponse:
        await authorized(request)
        if app_config is None:
            raise SettingsError("Start Inky with its configuration file to manage settings")
        if request.method == "GET":
            return _result(await run_in_threadpool(settings_status, app_config))
        body = await _json_body(
            request,
            {"revision", "changes"},
            optional={"adopt", "confirm_generation"},
            structured={"changes", "adopt", "confirm_generation"},
        )
        adopt = body.get("adopt", False)
        if type(adopt) is not bool:
            raise SettingsError("Adoption must be explicitly true or false")
        consent = body.get("confirm_generation", False)
        if type(consent) is not bool:
            raise SettingsError("Generation confirmation must be true or false")
        return _result(
            await run_in_threadpool(
                save_settings,
                app_config,
                revision=cast(str, body["revision"]),
                changes=body["changes"],
                adopt=adopt,
                confirm_generation=consent,
            )
        )

    async def owner_operations(request: Request) -> JSONResponse:
        await authorized(request)
        if request.method == "GET":
            return _result(
                await run_in_threadpool(operations.status, request.path_params.get("operation_id"))
            )
        if app_config is None:
            raise OwnerOperationError("Start Inky with its configuration file")
        body = await _json_body(
            request,
            {"kind", "key", "revision", "confirm"},
            optional={"taxon_id"},
            structured={"confirm", "taxon_id"},
        )
        kind = body["kind"]
        if (
            kind not in {"refresh", "retry", "notification-test", "notification-retry"}
            or body["confirm"] is not True
        ):
            raise OwnerOperationError("Review and confirm a supported owner action")
        taxon_id = body.get("taxon_id")
        if kind == "retry":
            if type(taxon_id) is not int or taxon_id <= 0:
                raise OwnerOperationError("Choose a valid species for this retry")
        elif taxon_id is not None:
            raise OwnerOperationError("This action does not accept a species identifier")
        inputs = {"revision": body["revision"], "taxon_id": taxon_id}
        return _result(
            await run_in_threadpool(
                operations.submit,
                kind,
                key=cast(str, body["key"]),
                inputs=inputs,
                execute=lambda: execute_owner_operation(
                    app_config,
                    kind,
                    revision=cast(str, body["revision"]),
                    taxon_id=taxon_id,
                ),
            )
        )

    def history_imports() -> HistoryImports:
        if app_config is None:
            raise OwnerOperationError("Start Inky with its configuration file")
        return HistoryImports(app_config)

    async def upload_history(request: Request) -> JSONResponse:
        await authorized(request)
        if request.headers.get("content-type") != "application/octet-stream":
            raise OwnerOperationError("Upload the file as application/octet-stream")
        declared = request.headers.get("content-length")
        if declared is not None and (
            not declared.isascii()
            or not declared.isdigit()
            or len(declared) > 10
            or not 0 < int(declared) <= MAX_UPLOAD_BYTES
        ):
            raise OwnerOperationError("Upload must be between 1 byte and 512 MiB")
        async with foreground():
            imports = history_imports()
            identifier, handle = await run_in_threadpool(
                imports.begin,
                request.path_params["kind"],
                observed_on=request.query_params.get("observed_on"),
            )
            try:
                digest, size = hashlib.sha256(), 0
                # 30 minutes allows a full 512 MiB transfer at roughly 300 KiB/s.
                # A stalled request must not reserve the single worker forever.
                async with asyncio.timeout(30 * 60):
                    with handle:
                        async for chunk in request.stream():
                            size += len(chunk)
                            if size > MAX_UPLOAD_BYTES:
                                raise OwnerOperationError("Upload exceeds the 512 MiB wire limit")
                            digest.update(chunk)
                            await run_in_threadpool(handle.write, chunk)
                        await run_in_threadpool(handle.flush)
                        await run_in_threadpool(os.fsync, handle.fileno())
                if declared is not None and size != int(declared):
                    raise OwnerOperationError("Upload ended before the declared file length")
                await authorized(request)
                await run_in_threadpool(imports.finish_upload, identifier, digest.hexdigest(), size)
            except BaseException as exc:
                handle.close()
                await run_in_threadpool(imports.cancel, identifier)
                if isinstance(exc, TimeoutError):
                    raise OwnerOperationError(
                        "Upload timed out. No history changed; upload the file again."
                    ) from exc
                raise
        return _result(
            {"upload_id": identifier, "message": "File received. It has not changed history."}
        )

    async def import_status(request: Request) -> JSONResponse:
        await authorized(request)
        return _result(await run_in_threadpool(history_imports().status))

    async def presentation_settings(request: Request) -> JSONResponse:
        secret = await authorized(request)
        if request.method == "GET":
            state = await run_in_threadpool(presentation.status)
            state["embed_allowed_origins"] = list(config.embed_allowed_origins)
            return _result(state)
        await run_in_threadpool(store.require_recent, secret)
        body = await _json_body(
            request,
            {"revision", "action", "confirm"},
            optional={"id", "name", "view", "days"},
            structured={"revision", "confirm", "days"},
        )
        if body["confirm"] is not True:
            raise JournalError("Review this screen or integration access before confirming")
        return _result(
            await run_in_threadpool(
                presentation.update,
                revision=body["revision"],
                action=cast(str, body["action"]),
                id=cast(str, body.get("id", "")),
                name=cast(str, body.get("name", "")),
                view=cast(str, body.get("view", "current")),
                days=body.get("days", GRANT_DAYS),
                confirm=True,
            )
        )

    async def saved_collection(request: Request) -> JSONResponse:
        await authorized(request)
        if app_config is None:
            raise JournalError("Start Inky with its configuration file")
        if request.path_params["action"] not in {"preview", "change"}:
            return _error(404, "not_found", "Choose a supported Saved birds step")
        preview = request.path_params["action"] == "preview"
        fields = {"action", "configuration_revision"} | (
            set() if preview else {"key", "confirm", "collection_revision"}
        )
        body = await _json_body(
            request, fields, optional={"taxon_id"}, structured={"confirm", "taxon_id"}
        )
        action, id = body["action"], body.get("taxon_id")
        if (
            action not in {"add", "remove", "import-approved"}
            or (action == "import-approved") != (id is None)
            or id is not None
            and (type(id) is not int or id <= 0)
        ):
            raise JournalError("Choose approved art or an existing Saved birds member")

        def execute() -> dict[str, object]:
            selected = confirmed_config(
                app_config, cast(str, body["configuration_revision"]), load_secrets=False
            )
            result = change_saved_collection(
                selected,
                action=action,
                taxon_id=id,
                dry_run=preview,
                expected_revision=None if preview else cast(str, body["collection_revision"]),
            )
            return {
                key: result[key]
                for key in (
                    "added_count",
                    "removed_count",
                    "migrated_legacy_queue_count",
                    "collection_revision",
                )
            }

        if preview:
            return _result(await run_in_threadpool(execute))
        if body["confirm"] is not True:
            raise JournalError("Confirm this Saved birds membership change")
        return _result(
            await run_in_threadpool(
                operations.submit,
                "saved-collection",
                key=cast(str, body["key"]),
                inputs={key: value for key, value in body.items() if key != "key"},
                execute=execute,
            )
        )

    def screen_cookie(request: Request) -> str:
        return "__Host-inky-screen" if request.url.scheme == "https" else "inky-local-screen"

    def require_screen_transport(request: Request) -> None:
        if request.url.scheme != "https" and not (
            _loopback(request.url.hostname)
            and request.client is not None
            and _loopback(request.client.host)
        ):
            raise AccessDeniedError(
                "Paired screens need HTTPS or the controller's genuine loopback address. "
                "Local owner Frame remains available without pairing."
            )

    async def screen_pair(request: Request) -> JSONResponse:
        require_screen_transport(request)
        await _json_body(request, set())
        if await run_in_threadpool(store.setup_status) is not None:
            raise AccessDeniedError("Set up the Inky owner first, then pair this screen")
        secret, result = await run_in_threadpool(
            presentation.request_pair, request.cookies.get(screen_cookie(request), "")
        )
        response = _result(result)
        response.set_cookie(
            screen_cookie(request),
            secret,
            max_age=365 * 86400,
            path="/",
            secure=request.url.scheme == "https",
            httponly=True,
            samesite="strict",
        )
        return response

    async def screen_data(request: Request) -> Response:
        require_screen_transport(request)
        grant = await run_in_threadpool(
            presentation.authenticate, request.cookies.get(screen_cookie(request), ""), "viewer"
        )
        try:
            entries = await run_in_threadpool(journal.view_entries, grant.view)
        except JournalViewRemovedError:
            entries = []  # Authoritative empty scope; never retain withdrawn art.
        id = request.path_params.get("taxon_id")
        if id is not None:
            entry = next((item for item in entries if item["taxon_id"] == id), None)
            if entry is None or request.query_params.getlist("revision") != [
                entry["portrait_sha256"]
            ]:
                return _error(404, "not_found", "This approved image is outside the screen's view")
            return Response(await run_in_threadpool(journal.portrait, id), media_type="image/png")
        return _result(
            {
                "name": grant.name,
                "expires_at": grant.expires_at,
                "species": [
                    {
                        "taxon_id": item["taxon_id"],
                        "common_name": item["common_name"],
                        "portrait_sha256": item["portrait_sha256"],
                        "portrait_url": f"/v1/screen/plates/{item['taxon_id']}"
                        f"?revision={item['portrait_sha256']}",
                    }
                    for item in entries
                ],
            }
        )

    async def screen_forget(request: Request) -> JSONResponse:
        await _json_body(request, set())
        # Forgetting a browser removes its cookie. Revoke the grant in Displays
        # too if the device was lost; no management authority is accepted here.
        response = _result({"forgotten": True})
        response.delete_cookie(
            screen_cookie(request),
            path="/",
            secure=request.url.scheme == "https",
            httponly=True,
            samesite="strict",
        )
        return response

    def integration_status(secret: str) -> dict[str, object]:
        grant = presentation.authenticate(secret, "status")
        entries = journal.view_entries(grant.view)
        approved = {cast(int, item["taxon_id"]) for item in journal.catalog()}
        work = (
            read_generation_work(current_config(app_config), approved=approved)
            if app_config
            else None
        )
        path = config.state_dir / "display-last-success.json"
        panel = read_json(path) if path.exists() else {}
        timestamp = parse_utc_timestamp(
            panel.get("succeeded_at") if isinstance(panel, dict) else None
        )
        health = "unknown"
        if timestamp and app_config:
            age = (datetime.now(UTC) - timestamp).total_seconds()
            # Allow one missed rotation plus its documented startup delay.
            bound = (
                app_config.schedule.rotation_minutes * 120
                + app_config.schedule.display_startup_delay_seconds
            )
            health = "recent" if 0 <= age <= bound else "stale"
        return {
            "version": __version__,
            "service_ready": True,
            "approved_count": len(approved),
            "view_ready_count": len(entries),
            "discovery_state": work.discovery_status if work else "unknown",
            "panel_health": health,
        }

    async def dashboard_status(request: Request) -> JSONResponse:
        values = request.headers.getlist("authorization")
        if len(values) != 1 or not values[0].startswith("Bearer ") or request.url.query:
            raise AccessDeniedError(
                "Use a status credential in the Authorization header, never the URL"
            )
        return _result(await run_in_threadpool(integration_status, values[0][7:]))

    async def selected_art(request: Request) -> Response:
        destination: Literal["wall", "dashboard"] = (
            "wall" if request.url.path.startswith("/wall/") else "dashboard"
        )
        entries = await run_in_threadpool(presentation.selected, destination)
        relative = request.path_params.get("asset")
        if relative is not None:
            entry = next(
                (
                    item
                    for item in entries
                    if relative in {item["portrait_path"], item["display_path"]}
                ),
                None,
            )
            if entry is None:
                return _error(404, "not_found", "This asset is outside the selected view")
            kind = "portrait" if relative == entry["portrait_path"] else "display"
            return Response(
                await run_in_threadpool(journal.approved_asset, cast(int, entry["taxon_id"]), kind),
                media_type="image/png",
            )
        if destination == "wall":
            # Match the display-node catalog schema without altering /v1/catalog.
            return JSONResponse(
                {
                    "schema_version": 1,
                    "generated_at": datetime.now(UTC).isoformat(),
                    "species": entries,
                }
            )
        from urllib.parse import quote

        item = entries[int(datetime.now(UTC).timestamp() // 60) % len(entries)] if entries else None
        # A static, cookie-free art document. It cannot load owner state or scripts.
        style = (
            "html,body{margin:0;height:100%;background:transparent}"
            "main{height:100%;display:grid;place-items:center}"
            "img{display:block;max-width:100%;max-height:100%;object-fit:contain}"
            "p{font:16px system-ui}"
        )
        image = (
            '<img alt="Approved bird plate" src="/v1/dashboard/assets/'
            f'{quote(cast(str, item["portrait_path"]), safe="/")}">'
            if item
            else "<p>No approved plates in this view.</p>"
        )
        body = (
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="refresh" content="60"><title>Inky art</title>'
            f"<style>{style}</style><main>{image}</main></html>"
        )
        import base64

        digest = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
        origins = " ".join(("'self'", *config.embed_allowed_origins))
        return Response(
            body,
            media_type="text/html",
            headers={
                "Content-Security-Policy": "default-src 'none'; img-src 'self'; "
                f"style-src 'sha256-{digest}'; base-uri 'none'; form-action 'none'; "
                f"frame-ancestors {origins}"
            },
        )

    async def community_history(request: Request) -> JSONResponse:
        await authorized(request)
        if request.method == "GET":
            return _result(await run_in_threadpool(community.status))
        action = request.path_params.get("action")
        if action == "cancel":
            await _json_body(request, set())
            async with foreground():
                await run_in_threadpool(community.cancel)
            return _result({"cancelled": True})
        if action not in {"preview", "save"}:
            return _error(404, "not_found", "Choose a supported community-history action")
        body = await _json_body(
            request,
            {"key", "confirm", "choices" if action == "preview" else "preview_hash"},
            structured={"confirm", "choices"},
        )
        if (
            body["confirm"] is not True
            or action == "preview"
            and not isinstance(body["choices"], dict)
        ):
            raise JournalError("Confirm this community-history action")
        return _result(
            await run_in_threadpool(
                operations.submit,
                "community-preview" if action == "preview" else "community-save",
                key=cast(str, body["key"]),
                inputs={key: value for key, value in body.items() if key != "key"},
                execute=lambda: (
                    community.preview(cast(dict[str, object], body["choices"]))
                    if action == "preview"
                    else community.save(cast(str, body["preview_hash"]))
                ),
            )
        )

    async def birdbuddy_account(request: Request) -> JSONResponse:
        await authorized(request)
        secure = (
            request.url.scheme == "https"
            or _loopback(request.url.hostname)
            and request.client is not None
            and _loopback(request.client.host)
        )
        try:
            if request.method == "GET":
                initial = current_config(app_config) if app_config is not None else None
                state = await run_in_threadpool(
                    birdbuddy_status,
                    config.state_dir,
                    include_manual_sightings=initial.discovery.birdbuddy_include_manual_sightings
                    if initial
                    else False,
                )
                return _result({"state": state, "secure_bootstrap": secure})
            body = await _json_body(
                request,
                {"confirm"},
                optional={"email", "password", "feeder_id"},
                structured={"confirm"},
            )
            action = request.path_params["action"]
            if body["confirm"] is not True:
                raise AccessError(
                    "Confirm the Bird Buddy access statement before sending credentials"
                )
            async with foreground():
                if action == "logout":
                    if set(body) != {"confirm"}:
                        raise AccessError("Logout accepts no credentials")
                    return _result(await run_in_threadpool(logout_birdbuddy, config.state_dir))
                if action != "login":
                    return _error(404, "not_found", "Choose a supported Bird Buddy account action")
                if not secure:
                    raise AccessError(
                        "Use HTTPS or the controller's genuine loopback address for Bird Buddy "
                        "sign-in. Local-network HTTP cannot protect that password."
                    )
                email, password = body.get("email"), body.get("password")
                if (
                    not isinstance(email, str)
                    or not email.strip()
                    or len(email) > 320
                    or not isinstance(password, str)
                    or not password
                    or len(password) > MAX_PASSWORD_CHARACTERS
                ):
                    raise AccessError("Enter the dedicated guest account email and password")
                result = await run_in_threadpool(
                    login_birdbuddy,
                    config.state_dir,
                    email=email,
                    password=password,
                    authorization_confirmed=True,
                    feeder_id=cast(str | None, body.get("feeder_id")),
                )
                return _result(result)
        except BirdBuddyFeederSelectionRequiredError as exc:
            return _result(
                {"choose_feeder": True, "feeders": [asdict(feeder) for feeder in exc.feeders]}
            )
        except DataSourceError:
            return _error(
                400,
                "birdbuddy_account",
                "Bird Buddy could not complete this account action. Check the dedicated "
                "guest login, continued permission and connection. Existing history is preserved.",
            )

    async def import_action(request: Request) -> JSONResponse:
        secret = await authorized(request)
        imports = history_imports()
        action = request.path_params["action"]
        if action == "retained":
            await _json_body(request, set())
            async with foreground():
                return _result(await run_in_threadpool(imports.use_retained_history))
        if action == "trip-edit":
            body = await _json_body(request, {"upload_id"})
            async with foreground():
                await run_in_threadpool(imports.edit_trip, cast(str, body["upload_id"]))
            return _result({"editing": True})
        if action == "cancel":
            body = await _json_body(request, {"upload_id"})
            async with foreground():
                await run_in_threadpool(imports.cancel, cast(str, body["upload_id"]))
            return _result(
                {
                    "cancelled": True,
                    "message": "Staged file removed. "
                    "Confirmed history and saved collections remain.",
                }
            )
        if action == "checklists":
            body = await _json_body(request, {"upload_id", "start_date", "end_date"})
            async with foreground():
                return _result(
                    await run_in_threadpool(
                        imports.checklist_choices,
                        cast(str, body["upload_id"]),
                        start_date=body["start_date"],
                        end_date=body["end_date"],
                    )
                )
        if action not in {"import-preview", "import-commit", "trip-preview", "trip-save"}:
            return _error(404, "not_found", "Choose a supported import step")
        extra = (
            {"preview_hash", "allow_reduction"}
            if action == "import-commit"
            else {"choices"}
            if action == "trip-preview"
            else {"preview_hash"}
            if action == "trip-save"
            else set()
        )
        body = await _json_body(
            request,
            {"upload_id", "key", "revision", "confirm"} | extra,
            structured={"confirm", "allow_reduction", "choices"},
            maximum=MAX_JOURNAL_BYTES + MAX_OWNER_JSON_BYTES,
        )
        if (
            body["confirm"] is not True
            or "allow_reduction" in body
            and type(body["allow_reduction"]) is not bool
        ):
            raise OwnerOperationError("Review and confirm this import step")
        if action == "trip-preview" and not isinstance(body["choices"], dict):
            raise OwnerOperationError("Choose valid checklists for this trip")

        def execute() -> dict[str, object]:
            try:
                if action == "import-preview":
                    return imports.preview(
                        cast(str, body["upload_id"]), revision=cast(str, body["revision"])
                    )
                if action == "import-commit":
                    return imports.commit(
                        cast(str, body["upload_id"]),
                        revision=cast(str, body["revision"]),
                        preview_hash=cast(str, body["preview_hash"]),
                        key=cast(str, body["key"]),
                        allow_reduction=cast(bool, body["allow_reduction"]),
                    )
                if action == "trip-preview":
                    return imports.preview_trip(
                        cast(str, body["upload_id"]), **cast(dict[str, object], body["choices"])
                    )
                return imports.save_trip(
                    cast(str, body["upload_id"]), preview_hash=cast(str, body["preview_hash"])
                )
            finally:
                try:
                    store.authenticate(secret)
                except AccessError:
                    imports.cancel(uncommitted_only=True)

        return _result(
            await run_in_threadpool(
                operations.submit,
                cast(OperationKind, action),
                key=cast(str, body["key"]),
                inputs={key: value for key, value in body.items() if key not in {"key", "confirm"}},
                execute=execute,
            )
        )

    async def journal_bird(request: Request) -> Response:
        await authorized(request)
        taxon_id = request.path_params["taxon_id"]
        if request.path_params["kind"] == "portrait":
            return Response(
                await run_in_threadpool(journal.portrait, taxon_id), media_type="image/png"
            )
        if request.path_params["kind"] == "profile":
            return _result(await run_in_threadpool(journal.profile, taxon_id))
        return _error(404, "not_found", "This bird page does not exist")

    async def candidate_work(request: Request) -> Response:
        from .recovery import decide_pending_candidate, pending_candidate, pending_portrait

        await authorized(request)
        if app_config is None:
            raise JournalError("Start Inky with its configuration file")
        taxon_id = request.path_params["taxon_id"]
        action = request.path_params.get("action")
        if request.method == "GET":
            if action == "portrait":
                body = await run_in_threadpool(
                    pending_portrait,
                    app_config,
                    taxon_id,
                    revision=request.query_params.get("revision", ""),
                )
                return Response(body, media_type="image/png")
            return _result(await run_in_threadpool(pending_candidate, app_config, taxon_id))
        if action not in {"approve", "reject"}:
            return _error(404, "not_found", "Choose a supported candidate decision")
        fields = {"key", "configuration_revision", "candidate_revision", "confirm"}
        if action == "reject":
            fields.add("reason")
        body_json = await _json_body(request, fields, structured={"confirm"})
        if body_json["confirm"] is not True:
            raise JournalError("Inspect the full candidate and confirm this decision")

        def execute() -> dict[str, object]:
            captured = confirmed_config(
                app_config, cast(str, body_json["configuration_revision"]), load_secrets=False
            )
            return decide_pending_candidate(
                captured,
                taxon_id,
                revision=cast(str, body_json["candidate_revision"]),
                approve=action == "approve",
                reason=cast(str, body_json.get("reason", "")),
            )

        return _result(
            await run_in_threadpool(
                operations.submit,
                "approve-candidate" if action == "approve" else "reject-candidate",
                key=cast(str, body_json["key"]),
                inputs={
                    "taxon_id": taxon_id,
                    **{
                        key: value
                        for key, value in body_json.items()
                        if key not in {"key", "confirm"}
                    },
                },
                execute=execute,
            )
        )

    async def journal_collection(request: Request) -> JSONResponse:
        await authorized(request)
        action = request.path_params["action"]
        if action == "export" and request.method == "GET":
            return _result(
                await run_in_threadpool(
                    journal.export_collection, request.query_params.get("collection", "")
                )
            )
        if request.method != "POST":
            return _error(405, "method_not_allowed", "Use the documented collection operation")
        if action in {"queue-preview", "queue"}:
            if app_config is None:
                raise JournalError("Start Inky with its configuration file")
            extra = {"preview_hash", "key", "confirm"} if action == "queue" else set()
            body = await _json_body(
                request,
                {"collection_id", "revision", "configuration_revision"} | extra,
                structured={"revision", "confirm"},
            )

            def selection_service() -> JournalService:
                captured = confirmed_config(
                    app_config, cast(str, body["configuration_revision"]), load_secrets=False
                )
                return JournalService(captured.controller, app_config=captured)

            if action == "queue-preview":
                result_preview = await run_in_threadpool(
                    lambda: selection_service().queue_preview(
                        collection_id=cast(str, body["collection_id"]), revision=body["revision"]
                    )
                )
                return _result(result_preview)
            if body["confirm"] is not True:
                raise JournalError("Confirm the missing-plate request preview")
            return _result(
                await run_in_threadpool(
                    operations.submit,
                    "queue-plates",
                    key=cast(str, body["key"]),
                    inputs={
                        key: value for key, value in body.items() if key not in {"key", "confirm"}
                    },
                    execute=lambda: selection_service().queue_collection(
                        collection_id=cast(str, body["collection_id"]),
                        revision=body["revision"],
                        preview_hash=cast(str, body["preview_hash"]),
                    ),
                )
            )
        if action == "favorite":
            body = await _json_body(
                request,
                {"revision", "taxon_id", "favorite"},
                structured={"revision", "taxon_id", "favorite"},
            )
            result = await run_in_threadpool(journal.favorite, **body)
        elif action in {"import-preview", "import"}:
            fields = (
                {"document"}
                if action == "import-preview"
                else {"document", "revision", "preview_hash", "collection_id"}
            )
            body = await _json_body(
                request,
                fields,
                structured={"document", "revision"},
                maximum=MAX_JOURNAL_BYTES + MAX_OWNER_JSON_BYTES,
            )
            if action == "import-preview":
                return _result(
                    await run_in_threadpool(journal.collection_import_preview, body["document"])
                )
            result = await run_in_threadpool(journal.import_collection, **body)
        elif action == "save":
            body = await _json_body(
                request,
                {"revision", "label", "taxon_ids"},
                optional={"collection_id"},
                structured={"revision", "taxon_ids"},
            )
            result = await run_in_threadpool(journal.save_selection, **body)
        elif action == "remove":
            body = await _json_body(request, {"revision", "collection_id"}, structured={"revision"})
            result = await run_in_threadpool(remove_collection, config.state_dir, **body)
        else:
            return _error(404, "not_found", "This collection action does not exist")
        return _result(result.owner_dict())

    async def page(request: Request) -> Response:
        from starlette.responses import FileResponse

        filename = {
            "/app": "index.html",
            "/app/": "index.html",
            "/app/app.js": "app.js",
            "/app/app.css": "app.css",
            "/app/journal.js": "journal.js",
            "/app/settings.js": "settings.js",
            "/app/imports.js": "imports.js",
            "/app/viewer.js": "viewer.js",
            "/app/presentation.js": "presentation.js",
            "/app/frame-tools.js": "frame-tools.js",
            "/screen": "screen.html",
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
        Route("/app/journal.js", page),
        Route("/app/settings.js", page),
        Route("/app/imports.js", page),
        Route("/app/viewer.js", page),
        Route("/app/presentation.js", page),
        Route("/app/frame-tools.js", page),
        Route("/screen", page),
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
        Route("/v1/owner/journal", journal_snapshot),
        Route("/v1/owner/health", journal_health),
        Route("/v1/owner/settings", application_settings, methods=["GET", "POST"]),
        Route("/v1/owner/operations", owner_operations, methods=["GET", "POST"]),
        Route("/v1/owner/operations/{operation_id}", owner_operations),
        Route("/v1/owner/imports", import_status),
        Route("/v1/owner/community", community_history),
        Route("/v1/owner/community/{action}", community_history, methods=["POST"]),
        Route("/v1/owner/presentation", presentation_settings, methods=["GET", "POST"]),
        Route("/v1/owner/saved/{action}", saved_collection, methods=["POST"]),
        Route("/v1/screen/pair", screen_pair, methods=["POST"]),
        Route("/v1/screen/forget", screen_forget, methods=["POST"]),
        Route("/v1/screen/view", screen_data),
        Route("/v1/screen/plates/{taxon_id:int}", screen_data),
        Route("/v1/integrations/status", dashboard_status),
        Route("/v1/dashboard/art", selected_art),
        Route("/v1/dashboard/assets/{asset:path}", selected_art),
        Route("/wall/v1/catalog", selected_art),
        Route("/wall/v1/assets/{asset:path}", selected_art),
        Route("/wall/v1/display-fetch", catalog.telemetry_response, methods=["POST"]),
        Route("/wall/v1/display-success", catalog.telemetry_response, methods=["POST"]),
        Route("/v1/owner/birdbuddy", birdbuddy_account),
        Route("/v1/owner/birdbuddy/{action}", birdbuddy_account, methods=["POST"]),
        Route("/v1/owner/imports/upload/{kind}", upload_history, methods=["POST"]),
        Route("/v1/owner/imports/{action}", import_action, methods=["POST"]),
        Route("/v1/owner/journal/birds/{taxon_id:int}/{kind}", journal_bird),
        Route("/v1/owner/candidates/{taxon_id:int}", candidate_work),
        Route(
            "/v1/owner/candidates/{taxon_id:int}/{action}", candidate_work, methods=["GET", "POST"]
        ),
        Route(
            "/v1/owner/journal/collections/{action}", journal_collection, methods=["GET", "POST"]
        ),
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
        if isinstance(exc, AccessPasswordConfirmationError):
            return _error(403, "password_confirmation_failed", str(exc))
        if isinstance(exc, AccessDeniedError):
            return _error(401, "sign_in_required", str(exc))
        return _error(400, "access_error", str(exc))

    async def journal_error(request: Request, exc: Exception) -> Response:
        if isinstance(exc, OwnerOperationBusyError):
            return _error(409, "operation_busy", str(exc))
        if isinstance(exc, OwnerOperationError):
            return _error(400, "operation_error", str(exc))
        if isinstance(exc, (JournalConflictError, SettingsConflictError)):
            return _error(409, "journal_changed", str(exc))
        if isinstance(exc, (JournalError, SettingsError)):
            return _error(400, "journal_error", str(exc))
        return _error(
            503,
            "journal_unavailable",
            "The journal cannot read its local state. Check storage and the approved catalog; "
            "do not delete private history.",
        )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        stopped = asyncio.Event()

        async def cleanup_imports() -> None:
            warned = False
            while not stopped.is_set():
                try:
                    if app_config is not None:
                        async with foreground():
                            await run_in_threadpool(history_imports().cleanup_expired)
                    warned = False
                except OwnerOperationBusyError:
                    pass  # Never evict an upload or history operation in progress.
                except OwnerOperationError:
                    if not warned:
                        logging.getLogger(__name__).warning(
                            "Private import cleanup needs attention; "
                            "inspect imports in the owner app"
                        )
                        warned = True
                with suppress(TimeoutError):
                    # Preview retention has hour precision. Sweep once a minute
                    # while serving, and again on every service start.
                    await asyncio.wait_for(stopped.wait(), timeout=60)

        if app_config is not None:
            try:
                async with foreground():
                    await run_in_threadpool(history_imports().cleanup_expired)
            except (OwnerOperationBusyError, OwnerOperationError):
                pass  # Keep owner recovery reachable; the sweep reports the problem.
        cleaner = asyncio.create_task(cleanup_imports())
        try:
            yield
        finally:
            stopped.set()
            await cleaner
            operations.close()

    app: ASGIApp = OwnerBoundary(
        Starlette(
            routes=routes,
            lifespan=lifespan,
            exception_handlers={
                AccessError: access_error,
                JournalError: journal_error,
                SettingsError: journal_error,
                OwnerOperationError: journal_error,
                CatalogError: journal_error,
                OSError: journal_error,
            },
        ),
        config,
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
