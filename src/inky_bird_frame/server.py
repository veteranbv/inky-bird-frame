"""HTTP service for approved catalog reads and display-node health reports."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import __version__
from .catalog import read_json, rebuild_catalog_index, utc_now
from .config import ControllerConfig
from .errors import CatalogError
from .http import USER_AGENT, write_json_atomic
from .images import slugify
from .timeutil import parse_utc_timestamp

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


class CatalogRequestHandler(BaseHTTPRequestHandler):
    catalog_dir: Path
    active_catalog_path: Path
    state_dir: Path
    cors_allowed_origins: tuple[str, ...] = ()
    embed_allowed_origins: tuple[str, ...] = ()

    def _has_origin(self) -> bool:
        return bool(self.headers.get_all("Origin", failobj=[]))

    def _is_legacy_display_request(self) -> bool:
        return not self._has_origin() and self.headers.get_all("User-Agent", failobj=[]) == [
            USER_AGENT
        ]

    def _load_active_catalog(self) -> tuple[dict[str, object], dict[str, str]]:
        return _project_active_catalog(read_json(self.active_catalog_path))

    def _send_browser_access_headers(self) -> None:
        if not self.cors_allowed_origins:
            return
        self.send_header("Vary", "Origin")
        origins = self.headers.get_all("Origin", failobj=[])
        if len(origins) == 1 and origins[0] in self.cors_allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origins[0])

    def _send_json(
        self,
        status: HTTPStatus,
        payload: object,
        *,
        allow_browser_access: bool = False,
        head_only: bool = False,
    ) -> None:
        body = json.dumps(payload, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if allow_browser_access:
            self._send_browser_access_headers()
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_featured_plate(
        self,
        status: HTTPStatus,
        entry: dict[str, object] | None,
        *,
        empty_message: str,
    ) -> None:
        body = _featured_plate_document(entry, empty_message=empty_message)
        frame_ancestors = " ".join(("'self'", *self.embed_allowed_origins))
        content_security_policy = "; ".join(
            (
                "default-src 'none'",
                "img-src 'self'",
                f"style-src 'sha256-{_FEATURED_PLATE_STYLE_HASH}'",
                "script-src 'none'",
                "object-src 'none'",
                "base-uri 'none'",
                "form-action 'none'",
                f"frame-ancestors {frame_ancestors}",
            )
        )
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", content_security_policy)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(
        self,
        path: Path,
        requested_sha256: list[str] | None,
        expected_sha256: str,
    ) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"ok": False, "error": "active asset unavailable", "schema_version": 1},
                allow_browser_access=True,
            )
            return
        actual_sha256 = hashlib.sha256(body).hexdigest()
        if actual_sha256 != expected_sha256:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"ok": False, "error": "active asset unavailable", "schema_version": 1},
                allow_browser_access=True,
            )
            return
        content_addressed = requested_sha256 == [expected_sha256]
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self._send_browser_access_headers()
        self.send_header(
            "Cache-Control",
            "public, max-age=86400, immutable" if content_addressed else "no-cache",
        )
        self.end_headers()
        self.wfile.write(body)

    def _record_display_event(self, filename: str, timestamp_field: str) -> bool:
        try:
            write_json_atomic(
                self.state_dir / filename,
                {"schema_version": 1, timestamp_field: utc_now()},
            )
        except OSError:
            print(
                json.dumps(
                    {
                        "event": "display_telemetry_write_failed",
                        "file": filename,
                    }
                )
            )
            return False
        return True

    def _send_telemetry_unavailable(self) -> None:
        self._send_json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"ok": False, "error": "display telemetry unavailable", "schema_version": 1},
        )

    def _read_telemetry_payload(self) -> bool:
        if self._has_origin():
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"ok": False, "error": "browser telemetry is not accepted", "schema_version": 1},
            )
            return False
        media_type = self.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if media_type != "application/json":
            self._send_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"ok": False, "error": "expected application/json", "schema_version": 1},
            )
            return False
        lengths = self.headers.get_all("Content-Length", failobj=[])
        if len(lengths) != 1:
            self._send_json(
                HTTPStatus.LENGTH_REQUIRED,
                {"ok": False, "error": "content length required", "schema_version": 1},
            )
            return False
        try:
            content_length = int(lengths[0])
        except ValueError:
            content_length = -1
        if content_length < 0:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid content length", "schema_version": 1},
            )
            return False
        if content_length > MAX_TELEMETRY_REQUEST_BYTES:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"ok": False, "error": "request body too large", "schema_version": 1},
            )
            return False
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid JSON", "schema_version": 1},
            )
            return False
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version"}
            or not isinstance(payload.get("schema_version"), int)
            or isinstance(payload.get("schema_version"), bool)
            or payload["schema_version"] != 1
        ):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid telemetry payload", "schema_version": 1},
            )
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        split = urlsplit(self.path)
        request_path = split.path
        if request_path == "/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "approved_species": _species_count(self.catalog_dir / "index.json"),
                    "active_species": _species_count(self.active_catalog_path),
                    "version": __version__,
                    "schema_version": 1,
                },
            )
            return
        if request_path == "/v1/catalog":
            try:
                payload, _asset_hashes = self._load_active_catalog()
            except (CatalogError, OSError):
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"ok": False, "error": "active catalog unavailable", "schema_version": 1},
                    allow_browser_access=True,
                )
                return
            # Compatibility bridge for one release. Only the fixed user agent
            # used by older display nodes can update physical-display health.
            if self._is_legacy_display_request() and parse_qs(split.query).get(
                "reports_success"
            ) == ["1"]:
                # Legacy telemetry is best effort: a state-write failure must
                # never withhold an otherwise valid catalog from the display.
                self._record_display_event("display-last-fetch.json", "fetched_at")
            self._send_json(HTTPStatus.OK, payload, allow_browser_access=True)
            return
        if request_path == "/v1/embed/featured-plate":
            try:
                payload, _asset_hashes = self._load_active_catalog()
                entry = _featured_plate_entry(payload)
            except (CatalogError, OSError):
                self._send_featured_plate(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    None,
                    empty_message="Featured plate unavailable",
                )
                return
            self._send_featured_plate(
                HTTPStatus.OK,
                entry,
                empty_message="No active plate yet",
            )
            return
        if request_path == "/v1/display-success":
            if not self._is_legacy_display_request():
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {
                        "ok": False,
                        "error": "browser telemetry is not accepted",
                        "schema_version": 1,
                    },
                )
                return
            # Compatibility bridge for one release. New display nodes use POST.
            if not self._record_display_event("display-last-success.json", "succeeded_at"):
                self._send_telemetry_unavailable()
                return
            self._send_json(HTTPStatus.OK, {"ok": True, "schema_version": 1})
            return
        prefix = "/v1/assets/"
        if request_path.startswith(prefix):
            relative_text = unquote(request_path.removeprefix(prefix))
            try:
                _payload, asset_hashes = self._load_active_catalog()
            except (CatalogError, OSError):
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"ok": False, "error": "active catalog unavailable", "schema_version": 1},
                    allow_browser_access=True,
                )
                return
            expected_sha256 = asset_hashes.get(relative_text)
            if expected_sha256 is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": "not found"},
                    allow_browser_access=True,
                )
                return
            try:
                relative = Path(relative_text)
                root = self.catalog_dir.resolve()
                candidate = root / relative
                resolved_candidate = candidate.resolve()
            except (OSError, RuntimeError, ValueError):
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": "not found"},
                    allow_browser_access=True,
                )
                return
            if (
                relative.suffix.lower() != ".png"
                or any(part.startswith(".") for part in relative.parts)
                or not resolved_candidate.is_relative_to(root)
                or resolved_candidate != candidate
                or not candidate.is_file()
            ):
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": "not found"},
                    allow_browser_access=True,
                )
                return
            self._send_file(
                candidate,
                parse_qs(split.query).get("sha256"),
                expected_sha256,
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        request_path = urlsplit(self.path).path
        if request_path != "/v1/catalog":
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "not found"},
                head_only=True,
            )
            return
        try:
            payload, _asset_hashes = self._load_active_catalog()
        except (CatalogError, OSError):
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"ok": False, "error": "active catalog unavailable", "schema_version": 1},
                allow_browser_access=True,
                head_only=True,
            )
            return
        self._send_json(
            HTTPStatus.OK,
            payload,
            allow_browser_access=True,
            head_only=True,
        )

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        request_path = urlsplit(self.path).path
        events = {
            "/v1/display-fetch": ("display-last-fetch.json", "fetched_at"),
            "/v1/display-success": ("display-last-success.json", "succeeded_at"),
        }
        event = events.get(request_path)
        if event is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        if not self._read_telemetry_payload():
            return
        if not self._record_display_event(*event):
            self._send_telemetry_unavailable()
            return
        self._send_json(HTTPStatus.OK, {"ok": True, "schema_version": 1})

    def log_message(self, message_format: str, *args: object) -> None:
        message = message_format % args
        print(
            json.dumps(
                {"event": "http_request", "client": self.client_address[0], "message": message}
            )
        )


def rebuild_index_logging_failures(catalog_dir: Path) -> None:
    try:
        rebuild_catalog_index(catalog_dir)
    except (CatalogError, OSError) as exc:
        print(json.dumps({"event": "catalog_index_rebuild_failed", "error": str(exc)}))


def serve_catalog(config: ControllerConfig) -> None:
    config.catalog_dir.mkdir(parents=True, exist_ok=True)
    rebuild_index_logging_failures(config.catalog_dir)
    handler = type(
        "ConfiguredCatalogRequestHandler",
        (CatalogRequestHandler,),
        {
            "catalog_dir": config.catalog_dir,
            "active_catalog_path": config.state_dir / "active-catalog.json",
            "state_dir": config.state_dir,
            "cors_allowed_origins": config.cors_allowed_origins,
            "embed_allowed_origins": config.embed_allowed_origins,
        },
    )
    server = ThreadingHTTPServer((config.bind_host, config.port), handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
