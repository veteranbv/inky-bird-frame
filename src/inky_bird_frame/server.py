"""Read-only HTTP service for approved catalog metadata and assets."""

from __future__ import annotations

import json
import mimetypes
from contextlib import suppress
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .catalog import read_json, rebuild_catalog_index, utc_now
from .config import ControllerConfig
from .errors import CatalogError
from .http import write_json_atomic


def _species_count(path: Path) -> int:
    try:
        value = read_json(path)
    except CatalogError:
        return 0
    if isinstance(value, dict) and isinstance(value.get("species"), list):
        return len(value["species"])
    return 0


class CatalogRequestHandler(BaseHTTPRequestHandler):
    catalog_dir: Path
    active_catalog_path: Path
    state_dir: Path

    def _send_json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400, immutable")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        request_path = urlsplit(self.path).path
        if request_path == "/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "approved_species": _species_count(self.catalog_dir / "index.json"),
                    "active_species": _species_count(self.active_catalog_path),
                    "schema_version": 1,
                },
            )
            return
        if request_path == "/v1/catalog":
            try:
                payload = read_json(self.active_catalog_path)
            except CatalogError:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"ok": False, "error": "active catalog unavailable", "schema_version": 1},
                )
                return
            self._send_json(HTTPStatus.OK, payload)
            with suppress(OSError):
                write_json_atomic(
                    self.state_dir / "display-last-fetch.json",
                    {"schema_version": 1, "fetched_at": utc_now()},
                )
            return
        prefix = "/v1/assets/"
        if request_path.startswith(prefix):
            relative = Path(unquote(request_path.removeprefix(prefix)))
            root = self.catalog_dir.resolve()
            candidate = (root / relative).resolve()
            if not candidate.is_relative_to(root) or not candidate.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
                return
            self._send_file(candidate)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def log_message(self, message_format: str, *args: object) -> None:
        message = message_format % args
        print(
            json.dumps(
                {"event": "http_request", "client": self.client_address[0], "message": message}
            )
        )


def serve_catalog(config: ControllerConfig) -> None:
    config.catalog_dir.mkdir(parents=True, exist_ok=True)
    try:
        rebuild_catalog_index(config.catalog_dir)
    except CatalogError as exc:
        print(json.dumps({"event": "catalog_index_rebuild_failed", "error": str(exc)}))
    handler = type(
        "ConfiguredCatalogRequestHandler",
        (CatalogRequestHandler,),
        {
            "catalog_dir": config.catalog_dir,
            "active_catalog_path": config.state_dir / "active-catalog.json",
            "state_dir": config.state_dir,
        },
    )
    server = ThreadingHTTPServer((config.bind_host, config.port), handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
