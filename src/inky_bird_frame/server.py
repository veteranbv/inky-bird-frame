"""HTTP service for approved catalog reads and display-node health reports."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime
from html import escape
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import quote

from . import __version__
from .catalog import read_json, rebuild_catalog_index, utc_now
from .config import ControllerConfig
from .errors import CatalogError
from .http import write_json_atomic
from .images import slugify
from .timeutil import parse_utc_timestamp

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

CATALOG_SCHEMA_VERSION = 1
MAX_TELEMETRY_REQUEST_BYTES = 1024
FEATURED_PLATE_REFRESH_SECONDS = 15 * 60
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_CATALOG_STRING_FIELDS = (
    "common_name",
    "scientific_name",
    "slug",
    "portrait_path",
    "portrait_sha256",
    "display_path",
    "display_sha256",
    "approved_at",
)
_FEATURED_PLATE_STYLE = (
    "html,body{height:100%;margin:0;width:100%}"
    "body{background:transparent;color:#94a3b8;display:grid;font:500 14px "
    "system-ui,sans-serif;overflow:hidden;place-items:center}"
    "main{display:grid;inset:0;min-height:0;min-width:0;place-items:center;"
    "position:fixed}"
    "img{display:block;height:100vh;object-fit:contain;width:100vw}"
    "p{margin:1rem;text-align:center}"
)
_FEATURED_PLATE_STYLE_HASH = base64.b64encode(
    hashlib.sha256(_FEATURED_PLATE_STYLE.encode()).digest()
).decode("ascii")


def _catalog_asset_path(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise CatalogError(f"Active catalog entry has invalid {field}")
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or path.as_posix() != value
        or path.suffix.lower() != ".png"
        or any(part in {"", ".", ".."} or part.startswith(".") for part in path.parts)
    ):
        raise CatalogError(f"Active catalog entry has invalid {field}")
    return value


def _project_active_catalog(payload: object) -> tuple[dict[str, object], dict[str, str]]:
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("schema_version"), int)
        or payload.get("schema_version") != CATALOG_SCHEMA_VERSION
        or isinstance(payload.get("schema_version"), bool)
    ):
        raise CatalogError("Active catalog has an unsupported schema")
    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str) or parse_utc_timestamp(generated_at) is None:
        raise CatalogError("Active catalog has an invalid generated timestamp")
    raw_species = payload.get("species")
    if not isinstance(raw_species, list):
        raise CatalogError("Active catalog has no species list")

    species: list[dict[str, object]] = []
    asset_hashes: dict[str, str] = {}
    taxon_ids: set[int] = set()
    for raw in raw_species:
        if not isinstance(raw, dict):
            raise CatalogError("Active catalog entry must be an object")
        taxon_id = raw.get("taxon_id")
        if (
            not isinstance(taxon_id, int)
            or isinstance(taxon_id, bool)
            or taxon_id <= 0
            or taxon_id in taxon_ids
        ):
            raise CatalogError("Active catalog entry has an invalid or duplicate taxon ID")
        taxon_ids.add(taxon_id)

        strings = {field: raw.get(field) for field in _CATALOG_STRING_FIELDS}
        if any(not isinstance(value, str) or not value for value in strings.values()):
            raise CatalogError("Active catalog entry has invalid fields")
        slug = strings["slug"]
        if not isinstance(slug, str) or slugify(slug) != slug:
            raise CatalogError("Active catalog entry has an invalid slug")
        portrait_path = _catalog_asset_path(strings["portrait_path"], "portrait path")
        display_path = _catalog_asset_path(strings["display_path"], "display path")
        species_directory = f"species/{taxon_id}-{slug}"
        if (
            portrait_path != f"{species_directory}/portrait.png"
            or display_path != f"{species_directory}/display.png"
        ):
            raise CatalogError("Active catalog entry has noncanonical asset paths")
        portrait_sha256 = strings["portrait_sha256"]
        display_sha256 = strings["display_sha256"]
        if (
            not isinstance(portrait_sha256, str)
            or _SHA256_PATTERN.fullmatch(portrait_sha256) is None
            or not isinstance(display_sha256, str)
            or _SHA256_PATTERN.fullmatch(display_sha256) is None
        ):
            raise CatalogError("Active catalog entry has an invalid asset checksum")
        approved_at = strings["approved_at"]
        if not isinstance(approved_at, str) or parse_utc_timestamp(approved_at) is None:
            raise CatalogError("Active catalog entry has an invalid approval timestamp")

        entry: dict[str, object] = {"taxon_id": taxon_id}
        entry.update(strings)
        if "observation_count" in raw:
            observation_count = raw["observation_count"]
            if (
                not isinstance(observation_count, int)
                or isinstance(observation_count, bool)
                or observation_count < 0
            ):
                raise CatalogError("Active catalog entry has an invalid observation count")
            entry["observation_count"] = observation_count
        if "latest_detection_at" in raw:
            latest_detection_at = raw["latest_detection_at"]
            if (
                not isinstance(latest_detection_at, str)
                or parse_utc_timestamp(latest_detection_at) is None
            ):
                raise CatalogError("Active catalog entry has an invalid detection timestamp")
            entry["latest_detection_at"] = latest_detection_at

        for path, digest in (
            (portrait_path, portrait_sha256),
            (display_path, display_sha256),
        ):
            prior_digest = asset_hashes.setdefault(path, digest)
            if prior_digest != digest:
                raise CatalogError("Active catalog assigns conflicting checksums to one asset")
        species.append(entry)

    return (
        {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "generated_at": generated_at,
            "species": species,
        },
        asset_hashes,
    )


def _species_count(path: Path) -> int:
    try:
        value = read_json(path)
    except (CatalogError, OSError):
        return 0
    if isinstance(value, dict) and isinstance(value.get("species"), list):
        return len(value["species"])
    return 0


def _featured_plate_entry(payload: dict[str, object]) -> dict[str, object] | None:
    raw_species = payload.get("species")
    if not isinstance(raw_species, list):
        raise CatalogError("Active catalog has no species list")

    candidates: list[dict[str, object]] = []
    for entry in raw_species:
        if not isinstance(entry, dict):
            raise CatalogError("Active catalog entry must be an object")
        candidates.append(entry)

    def rank(entry: dict[str, object]) -> tuple[int, datetime, datetime, int]:
        detected_at = parse_utc_timestamp(entry.get("latest_detection_at"))
        approved_at = parse_utc_timestamp(entry.get("approved_at"))
        taxon_id = entry.get("taxon_id")
        if approved_at is None or not isinstance(taxon_id, int) or isinstance(taxon_id, bool):
            raise CatalogError("Active catalog entry has invalid featured-plate fields")
        return (
            1 if detected_at is not None else 0,
            detected_at or approved_at,
            approved_at,
            -taxon_id,
        )

    return max(candidates, key=rank, default=None)


def _featured_plate_document(
    entry: dict[str, object] | None,
    *,
    empty_message: str,
) -> bytes:
    if entry is None:
        title = "Inky Bird Frame"
        content = f'<p role="status">{escape(empty_message)}</p>'
    else:
        common_name = entry.get("common_name")
        portrait_path = entry.get("portrait_path")
        portrait_sha256 = entry.get("portrait_sha256")
        if not isinstance(common_name, str) or not common_name:
            raise CatalogError("Active catalog entry has invalid featured-plate fields")
        if not isinstance(portrait_path, str) or not portrait_path:
            raise CatalogError("Active catalog entry has invalid featured-plate fields")
        if (
            not isinstance(portrait_sha256, str)
            or _SHA256_PATTERN.fullmatch(portrait_sha256) is None
        ):
            raise CatalogError("Active catalog entry has invalid featured-plate checksum")
        title = f"{common_name} | Inky Bird Frame"
        portrait_url = f"../assets/{quote(portrait_path, safe='/')}?sha256={portrait_sha256}"
        content = (
            f'<img src="{escape(portrait_url, quote=True)}" '
            f'alt="Field-journal plate for {escape(common_name, quote=True)}">'
        )

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta http-equiv="refresh" content="{FEATURED_PLATE_REFRESH_SECONDS}">'
        f"<title>{escape(title)}</title><style>{_FEATURED_PLATE_STYLE}</style>"
        f"</head><body><main>{content}</main></body></html>"
    ).encode()


class CatalogService:
    """The existing display/catalog contract, also used by the browser service."""

    def __init__(self, config: ControllerConfig) -> None:
        self.config = config
        self.active_catalog_path = config.state_dir / "active-catalog.json"

    def _load(self) -> tuple[dict[str, object], dict[str, str]]:
        return _project_active_catalog(read_json(self.active_catalog_path))

    def _browser_headers(self, request: Request) -> dict[str, str]:
        if not self.config.cors_allowed_origins:
            return {}
        headers = {"Vary": "Origin"}
        origins = request.headers.getlist("origin")
        if len(origins) == 1 and origins[0] in self.config.cors_allowed_origins:
            headers["Access-Control-Allow-Origin"] = origins[0]
        return headers

    def _json(
        self, request: Request, status: int, payload: object, *, browser: bool = False
    ) -> Response:
        from starlette.responses import Response

        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if browser:
            headers.update(self._browser_headers(request))
        return Response(
            json.dumps(payload, sort_keys=True).encode(),
            status_code=status,
            media_type="application/json",
            headers=headers,
        )

    def read_response(self, request: Request) -> Response:
        from starlette.responses import Response

        path = request.url.path
        if request.method == "HEAD" and path != "/v1/catalog":
            response = self._json(request, 404, {"ok": False, "error": "not found"})
        elif path == "/health":
            response = self._json(
                request,
                200,
                {
                    "ok": True,
                    "approved_species": _species_count(self.config.catalog_dir / "index.json"),
                    "active_species": _species_count(self.active_catalog_path),
                    "version": __version__,
                    "schema_version": 1,
                },
            )
        elif path == "/v1/catalog":
            try:
                payload, _ = self._load()
            except (CatalogError, OSError):
                response = self._json(
                    request,
                    503,
                    {"ok": False, "error": "active catalog unavailable", "schema_version": 1},
                    browser=True,
                )
            else:
                response = self._json(request, 200, payload, browser=True)
        elif path == "/v1/embed/featured-plate":
            try:
                payload, _ = self._load()
                entry = _featured_plate_entry(payload)
            except (CatalogError, OSError):
                body = _featured_plate_document(None, empty_message="Featured plate unavailable")
                status = 503
            else:
                body = _featured_plate_document(entry, empty_message="No active plate yet")
                status = 200
            ancestors = " ".join(("'self'", *self.config.embed_allowed_origins))
            csp = "; ".join(
                (
                    "default-src 'none'",
                    "img-src 'self'",
                    f"style-src 'sha256-{_FEATURED_PLATE_STYLE_HASH}'",
                    "script-src 'none'",
                    "object-src 'none'",
                    "base-uri 'none'",
                    "form-action 'none'",
                    f"frame-ancestors {ancestors}",
                )
            )
            response = Response(
                body,
                status_code=status,
                media_type="text/html",
                headers={
                    "Cache-Control": "no-store",
                    "Content-Security-Policy": csp,
                    "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        elif path.startswith("/v1/assets/"):
            response = self.asset_response(request)
        else:
            response = self._json(request, 404, {"ok": False, "error": "not found"})
        if request.method == "HEAD":
            # Keep the content length of GET while suppressing the wire body.
            response.body = b""
        return response

    def asset_response(self, request: Request) -> Response:
        from starlette.responses import Response

        relative_text = request.url.path.removeprefix("/v1/assets/")
        try:
            _, asset_hashes = self._load()
        except (CatalogError, OSError):
            return self._json(
                request,
                503,
                {"ok": False, "error": "active catalog unavailable", "schema_version": 1},
                browser=True,
            )
        digest = asset_hashes.get(relative_text)
        if digest is None:
            return self._json(request, 404, {"ok": False, "error": "not found"}, browser=True)
        try:
            root = self.config.catalog_dir.resolve()
            relative = Path(relative_text)
            candidate = root / relative
            resolved = candidate.resolve()
            if (
                relative.suffix.lower() != ".png"
                or any(p.startswith(".") for p in relative.parts)
                or not resolved.is_relative_to(root)
                or resolved != candidate
                or not candidate.is_file()
            ):
                return self._json(request, 404, {"ok": False, "error": "not found"}, browser=True)
            body = candidate.read_bytes()
        except (OSError, RuntimeError, ValueError):
            return self._json(
                request,
                503,
                {"ok": False, "error": "active asset unavailable", "schema_version": 1},
                browser=True,
            )
        if hashlib.sha256(body).hexdigest() != digest:
            return self._json(
                request,
                503,
                {"ok": False, "error": "active asset unavailable", "schema_version": 1},
                browser=True,
            )
        addressed = request.query_params.getlist("sha256") == [digest]
        return Response(
            body,
            media_type="image/png",
            headers={
                "Cache-Control": "public, max-age=86400, immutable" if addressed else "no-cache",
                "X-Content-Type-Options": "nosniff",
                **self._browser_headers(request),
            },
        )

    def _record_event(self, filename: str, field: str) -> bool:
        try:
            write_json_atomic(
                self.config.state_dir / filename, {"schema_version": 1, field: utc_now()}
            )
        except OSError:
            print(json.dumps({"event": "display_telemetry_write_failed", "file": filename}))
            return False
        return True

    async def telemetry_response(self, request: Request) -> Response:
        from starlette.concurrency import run_in_threadpool

        events = {
            "/v1/display-fetch": ("display-last-fetch.json", "fetched_at"),
            "/v1/display-success": ("display-last-success.json", "succeeded_at"),
        }
        event = events.get(request.url.path)
        if event is None:
            return self._json(request, 404, {"ok": False, "error": "not found"})
        if request.headers.getlist("origin"):
            return self._json(
                request,
                403,
                {"ok": False, "error": "browser telemetry is not accepted", "schema_version": 1},
            )
        media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if media_type != "application/json":
            return self._json(
                request,
                415,
                {"ok": False, "error": "expected application/json", "schema_version": 1},
            )
        lengths = request.headers.getlist("content-length")
        if len(lengths) != 1:
            return self._json(
                request, 411, {"ok": False, "error": "content length required", "schema_version": 1}
            )
        try:
            length = int(lengths[0])
        except ValueError:
            length = -1
        if length < 0:
            return self._json(
                request, 400, {"ok": False, "error": "invalid content length", "schema_version": 1}
            )
        if length > MAX_TELEMETRY_REQUEST_BYTES:
            return self._json(
                request, 413, {"ok": False, "error": "request body too large", "schema_version": 1}
            )
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_TELEMETRY_REQUEST_BYTES:
                return self._json(
                    request,
                    413,
                    {"ok": False, "error": "request body too large", "schema_version": 1},
                )
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json(
                request, 400, {"ok": False, "error": "invalid JSON", "schema_version": 1}
            )
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version"}
            or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != 1
        ):
            return self._json(
                request,
                400,
                {"ok": False, "error": "invalid telemetry payload", "schema_version": 1},
            )
        if not await run_in_threadpool(self._record_event, *event):
            return self._json(
                request,
                503,
                {"ok": False, "error": "display telemetry unavailable", "schema_version": 1},
            )
        return self._json(request, 200, {"ok": True, "schema_version": 1})

    async def dispatch(self, request: Request) -> Response:
        from starlette.concurrency import run_in_threadpool

        if request.method in {"GET", "HEAD"}:
            response = await run_in_threadpool(self.read_response, request)
        elif request.method == "POST":
            response = await self.telemetry_response(request)
        else:
            response = self._json(request, 501, {"ok": False, "error": "method not supported"})
        # Never log a query, cookie, address, asset identity, or arbitrary path.
        path = request.url.path
        route = (
            path
            if path
            in {
                "/health",
                "/v1/catalog",
                "/v1/embed/featured-plate",
                "/v1/display-fetch",
                "/v1/display-success",
            }
            else "/v1/assets/{asset}"
            if path.startswith("/v1/assets/")
            else "/not-found"
        )
        print(
            json.dumps(
                {
                    "event": "http_request",
                    "method": request.method,
                    "route": route,
                    "status": response.status_code,
                }
            )
        )
        return response


def rebuild_index_logging_failures(catalog_dir: Path) -> None:
    try:
        rebuild_catalog_index(catalog_dir)
    except (CatalogError, OSError):
        print(json.dumps({"event": "catalog_index_rebuild_failed"}))


def serve_catalog(config: ControllerConfig) -> None:
    try:
        import uvicorn

        from .web import create_app
    except ImportError as exc:
        from .errors import MissingDependencyError

        raise MissingDependencyError("Install inky-bird-frame[controller] to serve Inky") from exc

    if config.tls_certificate is not None or config.tls_private_key is not None:
        import ssl
        import stat

        from .errors import ConfigurationError

        if config.tls_certificate is None or config.tls_private_key is None:
            raise ConfigurationError("Supply both the TLS certificate and private key")
        try:
            key_stat = config.tls_private_key.lstat()
            if not stat.S_ISREG(key_stat.st_mode) or stat.S_IMODE(key_stat.st_mode) & 0o077:
                raise ConfigurationError("TLS private key must be a regular file with mode 0600")
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            # Never let OpenSSL prompt on a supervised/headless service start.
            context.load_cert_chain(config.tls_certificate, config.tls_private_key, password="")
        except (OSError, ssl.SSLError) as exc:
            raise ConfigurationError(
                "Cannot load the TLS certificate/key; check files and their match"
            ) from exc

    config.catalog_dir.mkdir(parents=True, exist_ok=True)
    rebuild_index_logging_failures(config.catalog_dir)
    uvicorn.run(
        create_app(config),
        host=config.bind_host,
        port=config.port,
        access_log=False,
        server_header=False,
        proxy_headers=False,
        ssl_certfile=str(config.tls_certificate) if config.tls_certificate else None,
        ssl_keyfile=str(config.tls_private_key) if config.tls_private_key else None,
        ssl_keyfile_password="" if config.tls_private_key else None,
    )
