from __future__ import annotations

import hashlib
import io
import json
import threading
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inky_bird_frame import __version__
from inky_bird_frame.http import USER_AGENT
from inky_bird_frame.server import (
    MAX_TELEMETRY_REQUEST_BYTES,
    CatalogRequestHandler,
    rebuild_index_logging_failures,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
GENERATED_AT = "2026-08-30T12:00:00+00:00"


def _catalog_entry(
    *,
    taxon_id: int = 1,
    directory: str = "species/1-robin",
    image: bytes = PNG_BYTES,
) -> dict[str, object]:
    digest = hashlib.sha256(image).hexdigest()
    return {
        "taxon_id": taxon_id,
        "common_name": "Robin",
        "scientific_name": "Turdus migratorius",
        "slug": "robin",
        "portrait_path": f"{directory}/portrait.png",
        "portrait_sha256": digest,
        "display_path": f"{directory}/display.png",
        "display_sha256": digest,
        "approved_at": "2026-07-10T00:00:00+00:00",
    }


def _active_catalog(*species: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_at": GENERATED_AT,
        "species": list(species),
    }


@contextmanager
def _serving(
    catalog_dir: Path,
    active_catalog_path: Path,
    state_dir: Path,
    *,
    cors_allowed_origins: tuple[str, ...] = (),
) -> Iterator[int]:
    handler = type(
        "TestCatalogRequestHandler",
        (CatalogRequestHandler,),
        {
            "catalog_dir": catalog_dir,
            "active_catalog_path": active_catalog_path,
            "state_dir": state_dir,
            "cors_allowed_origins": cors_allowed_origins,
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _get(
    port: int, path: str, *, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _post(
    port: int,
    path: str,
    *,
    body: bytes = b'{"schema_version": 1}',
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("POST", path, body=body, headers=request_headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _post_with_content_length(
    port: int,
    path: str,
    content_length: str | None,
) -> tuple[int, dict[str, str], bytes]:
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.putrequest("POST", path)
        connection.putheader("Content-Type", "application/json")
        if content_length is not None:
            connection.putheader("Content-Length", content_length)
        connection.endheaders()
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


class ServerTests(unittest.TestCase):
    @contextmanager
    def _environment(self) -> Iterator[tuple[Path, Path, Path]]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog_dir = root / "nested" / "catalog"
            catalog_dir.mkdir(parents=True)
            state_dir = root / "state"
            state_dir.mkdir()
            yield root, catalog_dir, state_dir

    def test_active_catalog_is_not_cached_as_an_immutable_asset(self) -> None:
        with self._environment() as (_, catalog_dir, state_dir):
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text(json.dumps(_active_catalog()))
            with _serving(catalog_dir, active_catalog_path, state_dir) as port:
                status, headers, _ = _get(port, "/v1/catalog")

        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_browser_access_is_disabled_by_default(self) -> None:
        with self._environment() as (_, catalog_dir, state_dir):
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text(json.dumps(_active_catalog()))
            with _serving(catalog_dir, active_catalog_path, state_dir) as port:
                status, headers, _ = _get(
                    port,
                    "/v1/catalog",
                    headers={"Origin": "https://display.example.test"},
                )

        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertNotIn("Vary", headers)

    def test_trusted_browser_origin_can_read_catalog_and_assets(self) -> None:
        trusted_origin = "https://display.example.test"
        with self._environment() as (_, catalog_dir, state_dir):
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text(json.dumps(_active_catalog(_catalog_entry())))
            asset = catalog_dir / "species" / "1-robin" / "portrait.png"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(PNG_BYTES)
            with _serving(
                catalog_dir,
                active_catalog_path,
                state_dir,
                cors_allowed_origins=(trusted_origin,),
            ) as port:
                catalog_status, catalog_headers, _ = _get(
                    port,
                    "/v1/catalog",
                    headers={"Origin": trusted_origin},
                )
                asset_status, asset_headers, _ = _get(
                    port,
                    "/v1/assets/species/1-robin/portrait.png",
                    headers={"Origin": trusted_origin},
                )

        self.assertEqual(catalog_status, 200)
        self.assertEqual(catalog_headers.get("Access-Control-Allow-Origin"), trusted_origin)
        self.assertEqual(catalog_headers.get("Vary"), "Origin")
        self.assertEqual(asset_status, 200)
        self.assertEqual(asset_headers.get("Access-Control-Allow-Origin"), trusted_origin)
        self.assertEqual(asset_headers.get("Vary"), "Origin")

    def test_untrusted_browser_origin_is_not_allowed(self) -> None:
        with self._environment() as (_, catalog_dir, state_dir):
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text(json.dumps(_active_catalog()))
            with _serving(
                catalog_dir,
                active_catalog_path,
                state_dir,
                cors_allowed_origins=("https://display.example.test",),
            ) as port:
                status, headers, _ = _get(
                    port,
                    "/v1/catalog",
                    headers={"Origin": "https://untrusted.example.test"},
                )

        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(headers.get("Vary"), "Origin")

    def test_browser_access_does_not_cover_health_or_display_telemetry(self) -> None:
        trusted_origin = "https://display.example.test"
        with (
            self._environment() as (_, catalog_dir, state_dir),
            _serving(
                catalog_dir,
                state_dir / "active-catalog.json",
                state_dir,
                cors_allowed_origins=(trusted_origin,),
            ) as port,
        ):
            health_status, health_headers, _ = _get(
                port,
                "/health",
                headers={"Origin": trusted_origin},
            )
            heartbeat_status, heartbeat_headers, _ = _post(
                port,
                "/v1/display-success",
                headers={"Origin": trusted_origin},
            )
            legacy_status, _, _ = _get(
                port,
                "/v1/display-success",
                headers={"Origin": trusted_origin},
            )
            heartbeat_written = (state_dir / "display-last-success.json").exists()

        self.assertEqual(health_status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", health_headers)
        self.assertEqual(heartbeat_status, 403)
        self.assertEqual(legacy_status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", heartbeat_headers)
        self.assertFalse(heartbeat_written)

    def test_catalog_projects_only_documented_v1_fields(self) -> None:
        entry = _catalog_entry()
        entry.update(
            {
                "observation_count": 7,
                "latest_detection_at": "2026-08-30T11:45:00+00:00",
                "provider": "private-provider",
            }
        )
        active = _active_catalog(entry)
        active["internal_state"] = "private"
        with self._environment() as (_, catalog_dir, state_dir):
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text(json.dumps(active))
            with _serving(catalog_dir, active_catalog_path, state_dir) as port:
                status, headers, body = _get(port, "/v1/catalog")

        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"schema_version", "generated_at", "species"})
        self.assertEqual(
            set(payload["species"][0]),
            {
                "taxon_id",
                "common_name",
                "scientific_name",
                "slug",
                "portrait_path",
                "portrait_sha256",
                "display_path",
                "display_sha256",
                "approved_at",
                "observation_count",
                "latest_detection_at",
            },
        )
        self.assertEqual(payload["species"][0]["observation_count"], 7)
        self.assertEqual(
            payload["species"][0]["latest_detection_at"],
            "2026-08-30T11:45:00+00:00",
        )
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_catalog_fails_closed_when_documented_fields_are_invalid(self) -> None:
        invalid = _catalog_entry()
        invalid["portrait_path"] = "species/1-robin/manifest.json"
        with self._environment() as (_, catalog_dir, state_dir):
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text(json.dumps(_active_catalog(invalid)))
            with _serving(catalog_dir, active_catalog_path, state_dir) as port:
                status, _, body = _get(port, "/v1/catalog")

        self.assertEqual(status, 503)
        self.assertEqual(
            json.loads(body),
            {"ok": False, "error": "active catalog unavailable", "schema_version": 1},
        )

    def test_non_utf8_state_files_degrade_gracefully(self) -> None:
        with self._environment() as (_, catalog_dir, state_dir):
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_bytes(b'{"schema_version": 1, "species": [\xff\xfe')
            (catalog_dir / "index.json").write_bytes(b'{"species": [\xff\xfe')
            with _serving(catalog_dir, active_catalog_path, state_dir) as port:
                catalog_status, _, _ = _get(port, "/v1/catalog")
                health_status, _, health_body = _get(port, "/health")

        self.assertEqual(catalog_status, 503)
        self.assertEqual(health_status, 200)
        payload = json.loads(health_body)
        self.assertEqual(payload["approved_species"], 0)
        self.assertEqual(payload["active_species"], 0)

    def test_startup_index_rebuild_survives_missing_assets(self) -> None:
        with self._environment() as (_, catalog_dir, _state_dir):
            species = catalog_dir / "species" / "1-robin"
            species.mkdir(parents=True)
            (species / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "approved",
                        "taxon_id": 1,
                        "common_name": "Robin",
                        "scientific_name": "Turdus migratorius",
                        "slug": "robin",
                        "approved_at": "2026-07-10T00:00:00+00:00",
                        "assets": {
                            "portrait": {"filename": "portrait.png", "sha256": "a" * 64},
                            "display": {"filename": "display.png", "sha256": "b" * 64},
                        },
                    }
                )
            )
            rebuild_index_logging_failures(catalog_dir)

    def test_staging_and_dot_paths_are_never_served(self) -> None:
        with self._environment() as (_, catalog_dir, state_dir):
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text(json.dumps(_active_catalog()))
            staged = catalog_dir / ".staging" / "1-robin"
            staged.mkdir(parents=True)
            (staged / "manifest.json").write_text("{}")
            hidden = catalog_dir / "species" / ".hidden.png"
            hidden.parent.mkdir(parents=True)
            hidden.write_bytes(b"secret")
            with _serving(catalog_dir, active_catalog_path, state_dir) as port:
                staging_status, _, staging_body = _get(
                    port, "/v1/assets/.staging/1-robin/manifest.json"
                )
                hidden_status, _, _ = _get(port, "/v1/assets/species/.hidden.png")

        self.assertEqual(staging_status, 404)
        self.assertEqual(hidden_status, 404)
        self.assertNotIn(b"{}", staging_body)

    def test_legacy_display_success_get_without_origin_is_recorded(self) -> None:
        with self._environment() as (_, catalog_dir, state_dir):
            with _serving(catalog_dir, state_dir / "active-catalog.json", state_dir) as port:
                status, _, body = _get(
                    port,
                    "/v1/display-success",
                    headers={"User-Agent": USER_AGENT},
                )

            recorded = json.loads((state_dir / "display-last-success.json").read_text())

        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(recorded["schema_version"], 1)
        self.assertIn("succeeded_at", recorded)

    def test_asset_is_served_with_png_content_type(self) -> None:
        with self._environment() as (_, catalog_dir, state_dir):
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text(json.dumps(_active_catalog(_catalog_entry())))
            asset = catalog_dir / "species" / "1-robin" / "portrait.png"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(PNG_BYTES)
            with _serving(catalog_dir, active_catalog_path, state_dir) as port:
                status, headers, body = _get(port, "/v1/assets/species/1-robin/portrait.png")

        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/png")
        self.assertEqual(headers.get("Cache-Control"), "no-cache")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(body, PNG_BYTES)

    def test_content_addressed_asset_is_immutable(self) -> None:
        with self._environment() as (_, catalog_dir, state_dir):
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text(json.dumps(_active_catalog(_catalog_entry())))
            asset = catalog_dir / "species" / "1-robin" / "portrait.png"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(PNG_BYTES)
            digest = hashlib.sha256(PNG_BYTES).hexdigest()
            with _serving(catalog_dir, active_catalog_path, state_dir) as port:
                status, headers, body = _get(
                    port,
                    f"/v1/assets/species/1-robin/portrait.png?sha256={digest}",
                )

        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "public, max-age=86400, immutable")
        self.assertEqual(body, PNG_BYTES)

    def test_unlisted_catalog_files_and_inactive_pngs_are_not_served(self) -> None:
        with self._environment() as (_, catalog_dir, state_dir):
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text(json.dumps(_active_catalog(_catalog_entry())))
            species_dir = catalog_dir / "species" / "1-robin"
            species_dir.mkdir(parents=True)
            (species_dir / "manifest.json").write_text('{"private": true}')
            (species_dir / "quality-review.json").write_text('{"private": true}')
            (species_dir / "inactive.png").write_bytes(PNG_BYTES)
            with _serving(catalog_dir, active_catalog_path, state_dir) as port:
                for path in (
                    "/v1/assets/species/1-robin/manifest.json",
                    "/v1/assets/species/1-robin/quality-review.json",
                    "/v1/assets/species/1-robin/inactive.png",
                ):
                    status, _, body = _get(port, path)
                    self.assertEqual(status, 404, path)
                    self.assertNotIn(b"private", body, path)

    def test_active_state_cannot_allowlist_a_noncanonical_png(self) -> None:
        entry = _catalog_entry()
        entry["portrait_path"] = "species/1-robin/inactive.png"
        with self._environment() as (_, catalog_dir, state_dir):
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text(json.dumps(_active_catalog(entry)))
            inactive = catalog_dir / "species" / "1-robin" / "inactive.png"
            inactive.parent.mkdir(parents=True)
            inactive.write_bytes(PNG_BYTES)
            with _serving(catalog_dir, active_catalog_path, state_dir) as port:
                catalog_status, _, _ = _get(port, "/v1/catalog")
                asset_status, _, asset_body = _get(
                    port,
                    "/v1/assets/species/1-robin/inactive.png",
                )

        self.assertEqual(catalog_status, 503)
        self.assertEqual(asset_status, 503)
        self.assertNotIn(PNG_BYTES, asset_body)

    def test_active_asset_checksum_mismatch_never_returns_file_bytes(self) -> None:
        corrupted = b"corrupted image bytes"
        with self._environment() as (_, catalog_dir, state_dir):
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text(json.dumps(_active_catalog(_catalog_entry())))
            asset = catalog_dir / "species" / "1-robin" / "portrait.png"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(corrupted)
            with _serving(catalog_dir, active_catalog_path, state_dir) as port:
                status, _, body = _get(port, "/v1/assets/species/1-robin/portrait.png")

        self.assertEqual(status, 503)
        self.assertNotIn(corrupted, body)
        self.assertEqual(
            json.loads(body),
            {"ok": False, "error": "active asset unavailable", "schema_version": 1},
        )

    def test_allowlisted_asset_symlink_cannot_escape_catalog_root(self) -> None:
        secret = b"private image outside the catalog"
        with self._environment() as (root, catalog_dir, state_dir):
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text(
                json.dumps(_active_catalog(_catalog_entry(image=secret)))
            )
            outside = root / "private.png"
            outside.write_bytes(secret)
            asset = catalog_dir / "species" / "1-robin" / "portrait.png"
            asset.parent.mkdir(parents=True)
            asset.symlink_to(outside)
            with _serving(catalog_dir, active_catalog_path, state_dir) as port:
                status, _, body = _get(port, "/v1/assets/species/1-robin/portrait.png")

        self.assertEqual(status, 404)
        self.assertNotIn(secret, body)

    def test_asset_path_traversal_is_rejected(self) -> None:
        secret = b"top secret"
        with self._environment() as (root, catalog_dir, state_dir):
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text(json.dumps(_active_catalog()))
            (root / "secret.txt").write_bytes(secret)
            with _serving(catalog_dir, active_catalog_path, state_dir) as port:
                for path in (
                    "/v1/assets/..%2f..%2fsecret.txt",
                    "/v1/assets/%2e%2e/%2e%2e/secret.txt",
                    "/v1/assets//etc/hostname",
                ):
                    status, _, body = _get(port, path)
                    self.assertEqual(status, 404, path)
                    self.assertNotIn(secret, body, path)
                    self.assertEqual(json.loads(body), {"ok": False, "error": "not found"})

    def test_unknown_route_returns_json_not_found(self) -> None:
        with (
            self._environment() as (_, catalog_dir, state_dir),
            _serving(catalog_dir, state_dir / "active-catalog.json", state_dir) as port,
        ):
            status, headers, body = _get(port, "/nope")

        self.assertEqual(status, 404)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(json.loads(body), {"ok": False, "error": "not found"})

    def test_catalog_returns_503_when_active_catalog_is_missing(self) -> None:
        with (
            self._environment() as (_, catalog_dir, state_dir),
            _serving(catalog_dir, state_dir / "active-catalog.json", state_dir) as port,
        ):
            status, _, body = _get(port, "/v1/catalog")

        self.assertEqual(status, 503)
        self.assertEqual(
            json.loads(body),
            {"ok": False, "error": "active catalog unavailable", "schema_version": 1},
        )

    def test_catalog_returns_503_when_active_catalog_is_corrupt(self) -> None:
        with self._environment() as (_, catalog_dir, state_dir):
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text('{"schema_version": 1, "species"')
            with _serving(catalog_dir, active_catalog_path, state_dir) as port:
                status, _, body = _get(port, "/v1/catalog")
            heartbeat = state_dir / "display-last-fetch.json"
            self.assertFalse(heartbeat.exists())

        self.assertEqual(status, 503)
        self.assertEqual(
            json.loads(body),
            {"ok": False, "error": "active catalog unavailable", "schema_version": 1},
        )

    def test_legacy_catalog_marker_without_origin_records_display_fetch(self) -> None:
        active = _active_catalog(_catalog_entry())
        with self._environment() as (_, catalog_dir, state_dir):
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text(json.dumps(active))
            with _serving(catalog_dir, active_catalog_path, state_dir) as port:
                plain_status, _, _ = _get(port, "/v1/catalog")
                _get(port, "/v1/catalog?not_reports_success=1")
                _get(port, "/v1/catalog?reports_success=10")
                plain_written = (state_dir / "display-last-fetch.json").exists()
                status, _, body = _get(
                    port,
                    "/v1/catalog?reports_success=1",
                    headers={"User-Agent": USER_AGENT},
                )
            heartbeat = json.loads((state_dir / "display-last-fetch.json").read_text())

        self.assertEqual(plain_status, 200)
        self.assertFalse(plain_written)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), active)
        self.assertEqual(heartbeat["schema_version"], 1)
        self.assertRegex(heartbeat["fetched_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

    def test_nonlegacy_gets_cannot_write_display_telemetry(self) -> None:
        with self._environment() as (_, catalog_dir, state_dir):
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text(json.dumps(_active_catalog()))
            with _serving(catalog_dir, active_catalog_path, state_dir) as port:
                catalog_status, _, _ = _get(port, "/v1/catalog?reports_success=1")
                success_status, _, _ = _get(port, "/v1/display-success")

        self.assertEqual(catalog_status, 200)
        self.assertEqual(success_status, 403)
        self.assertFalse((state_dir / "display-last-fetch.json").exists())
        self.assertFalse((state_dir / "display-last-success.json").exists())

    def test_post_display_telemetry_records_fetch_and_success_without_cors(self) -> None:
        with self._environment() as (_, catalog_dir, state_dir):
            with _serving(catalog_dir, state_dir / "active-catalog.json", state_dir) as port:
                fetch_status, fetch_headers, fetch_body = _post(port, "/v1/display-fetch")
                success_status, success_headers, success_body = _post(port, "/v1/display-success")
            fetched = json.loads((state_dir / "display-last-fetch.json").read_text())
            succeeded = json.loads((state_dir / "display-last-success.json").read_text())

        self.assertEqual(fetch_status, 200)
        self.assertEqual(success_status, 200)
        self.assertEqual(json.loads(fetch_body), {"ok": True, "schema_version": 1})
        self.assertEqual(json.loads(success_body), {"ok": True, "schema_version": 1})
        self.assertNotIn("Access-Control-Allow-Origin", fetch_headers)
        self.assertNotIn("Access-Control-Allow-Origin", success_headers)
        self.assertIn("fetched_at", fetched)
        self.assertIn("succeeded_at", succeeded)

    def test_post_display_telemetry_reports_persistence_failure(self) -> None:
        with (
            self._environment() as (_, catalog_dir, state_dir),
            patch(
                "inky_bird_frame.server.write_json_atomic",
                side_effect=OSError("state unavailable"),
            ),
            _serving(catalog_dir, state_dir / "active-catalog.json", state_dir) as port,
        ):
            status, _, body = _post(port, "/v1/display-success")

        self.assertEqual(status, 503)
        self.assertEqual(
            json.loads(body),
            {"ok": False, "error": "display telemetry unavailable", "schema_version": 1},
        )
        self.assertFalse((state_dir / "display-last-success.json").exists())

    def test_post_display_telemetry_rejects_unbounded_or_unexpected_data(self) -> None:
        with self._environment() as (_, catalog_dir, state_dir):
            with _serving(catalog_dir, state_dir / "active-catalog.json", state_dir) as port:
                cases: tuple[tuple[bytes, dict[str, str], int], ...] = (
                    (
                        b"x" * (MAX_TELEMETRY_REQUEST_BYTES + 1),
                        {"Content-Type": "application/json"},
                        413,
                    ),
                    (b"not-json", {"Content-Type": "application/json"}, 400),
                    (b'{"schema_version": 1, "client": "browser"}', {}, 400),
                    (b'{"schema_version": 1}', {"Content-Type": "text/plain"}, 415),
                )
                for body, headers, expected_status in cases:
                    status, _, _ = _post(
                        port,
                        "/v1/display-success",
                        body=body,
                        headers=headers,
                    )
                    self.assertEqual(status, expected_status)
            heartbeat_written = (state_dir / "display-last-success.json").exists()

        self.assertFalse(heartbeat_written)

    def test_post_display_telemetry_requires_one_valid_content_length(self) -> None:
        with self._environment() as (_, catalog_dir, state_dir):
            with _serving(catalog_dir, state_dir / "active-catalog.json", state_dir) as port:
                missing_status, _, _ = _post_with_content_length(port, "/v1/display-success", None)
                invalid_status, _, _ = _post_with_content_length(
                    port, "/v1/display-success", "not-a-number"
                )
            heartbeat_written = (state_dir / "display-last-success.json").exists()

        self.assertEqual(missing_status, 411)
        self.assertEqual(invalid_status, 400)
        self.assertFalse(heartbeat_written)

    def test_options_does_not_enable_private_network_or_cors_preflight(self) -> None:
        with (
            self._environment() as (_, catalog_dir, state_dir),
            _serving(
                catalog_dir,
                state_dir / "active-catalog.json",
                state_dir,
                cors_allowed_origins=("https://display.example.test",),
            ) as port,
        ):
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request(
                    "OPTIONS",
                    "/v1/catalog",
                    headers={
                        "Origin": "https://display.example.test",
                        "Access-Control-Request-Private-Network": "true",
                    },
                )
                response = connection.getresponse()
                status = response.status
                headers = dict(response.getheaders())
                response.read()
            finally:
                connection.close()

        self.assertEqual(status, 501)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertNotIn("Access-Control-Allow-Private-Network", headers)

    def test_request_log_is_local_and_header_free(self) -> None:
        output = io.StringIO()
        with (
            self._environment() as (_, catalog_dir, state_dir),
            redirect_stdout(output),
            _serving(catalog_dir, state_dir / "active-catalog.json", state_dir) as port,
        ):
            _get(
                port,
                "/health?probe=1",
                headers={
                    "Origin": "https://display.example.test",
                    "User-Agent": "private-browser-detail",
                },
            )

        log = json.loads(output.getvalue().strip())
        self.assertEqual(log["event"], "http_request")
        self.assertEqual(log["client"], "127.0.0.1")
        self.assertIn("GET /health?probe=1", log["message"])
        self.assertNotIn("display.example.test", output.getvalue())
        self.assertNotIn("private-browser-detail", output.getvalue())

    def test_browser_catalog_marker_never_records_display_fetch(self) -> None:
        trusted_origin = "https://display.example.test"
        with self._environment() as (_, catalog_dir, state_dir):
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text(json.dumps(_active_catalog()))
            with _serving(
                catalog_dir,
                active_catalog_path,
                state_dir,
                cors_allowed_origins=(trusted_origin,),
            ) as port:
                status, headers, _ = _get(
                    port,
                    "/v1/catalog?reports_success=1",
                    headers={"Origin": trusted_origin},
                )

        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), trusted_origin)
        self.assertFalse((state_dir / "display-last-fetch.json").exists())

    def test_health_reports_counts_without_touching_the_index(self) -> None:
        with self._environment() as (_, catalog_dir, state_dir):
            index_path = catalog_dir / "index.json"
            index_path.write_text(json.dumps({"schema_version": 1, "species": [{}, {}]}))
            index_stat = index_path.stat()
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text(json.dumps(_active_catalog(_catalog_entry())))
            with _serving(catalog_dir, active_catalog_path, state_dir) as port:
                status, _, body = _get(port, "/health")
            self.assertEqual(index_path.stat().st_mtime_ns, index_stat.st_mtime_ns)
            self.assertEqual(index_path.stat().st_size, index_stat.st_size)

        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {
                "ok": True,
                "approved_species": 2,
                "active_species": 1,
                "version": __version__,
                "schema_version": 1,
            },
        )

    def test_health_tolerates_missing_index_and_corrupt_active_catalog(self) -> None:
        with self._environment() as (_, catalog_dir, state_dir):
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text("not json")
            with _serving(catalog_dir, active_catalog_path, state_dir) as port:
                status, _, body = _get(port, "/health")
            self.assertFalse((catalog_dir / "index.json").exists())

        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {
                "ok": True,
                "approved_species": 0,
                "active_species": 0,
                "version": __version__,
                "schema_version": 1,
            },
        )


if __name__ == "__main__":
    unittest.main()
