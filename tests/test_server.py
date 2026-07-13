from __future__ import annotations

import json
import threading
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from inky_bird_frame.server import CatalogRequestHandler, rebuild_index_logging_failures

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@contextmanager
def _serving(catalog_dir: Path, active_catalog_path: Path, state_dir: Path) -> Iterator[int]:
    handler = type(
        "TestCatalogRequestHandler",
        (CatalogRequestHandler,),
        {
            "catalog_dir": catalog_dir,
            "active_catalog_path": active_catalog_path,
            "state_dir": state_dir,
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


def _get(port: int, path: str) -> tuple[int, dict[str, str], bytes]:
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
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
            active_catalog_path.write_text(json.dumps({"schema_version": 1, "species": []}))
            with _serving(catalog_dir, active_catalog_path, state_dir) as port:
                status, headers, _ = _get(port, "/v1/catalog")

        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")

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
            staged = catalog_dir / ".staging" / "1-robin"
            staged.mkdir(parents=True)
            (staged / "manifest.json").write_text("{}")
            hidden = catalog_dir / "species" / ".hidden.png"
            hidden.parent.mkdir(parents=True)
            hidden.write_bytes(b"secret")
            with _serving(catalog_dir, state_dir / "active-catalog.json", state_dir) as port:
                staging_status, _, staging_body = _get(
                    port, "/v1/assets/.staging/1-robin/manifest.json"
                )
                hidden_status, _, _ = _get(port, "/v1/assets/species/.hidden.png")

        self.assertEqual(staging_status, 404)
        self.assertEqual(hidden_status, 404)
        self.assertNotIn(b"{}", staging_body)

    def test_display_success_report_is_recorded(self) -> None:
        with self._environment() as (_, catalog_dir, state_dir):
            with _serving(catalog_dir, state_dir / "active-catalog.json", state_dir) as port:
                status, _, body = _get(port, "/v1/display-success")

            recorded = json.loads((state_dir / "display-last-success.json").read_text())

        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(recorded["schema_version"], 1)
        self.assertIn("succeeded_at", recorded)

    def test_asset_is_served_with_png_content_type(self) -> None:
        with self._environment() as (_, catalog_dir, state_dir):
            asset = catalog_dir / "species" / "1-robin" / "portrait.png"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(PNG_BYTES)
            with _serving(catalog_dir, state_dir / "active-catalog.json", state_dir) as port:
                status, headers, body = _get(port, "/v1/assets/species/1-robin/portrait.png")

        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/png")
        self.assertEqual(body, PNG_BYTES)

    def test_asset_path_traversal_is_rejected(self) -> None:
        secret = b"top secret"
        with self._environment() as (root, catalog_dir, state_dir):
            (root / "secret.txt").write_bytes(secret)
            with _serving(catalog_dir, state_dir / "active-catalog.json", state_dir) as port:
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

    def test_catalog_success_records_display_fetch_heartbeat(self) -> None:
        active = {"schema_version": 1, "species": [{"taxon_id": 1}]}
        with self._environment() as (_, catalog_dir, state_dir):
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text(json.dumps(active))
            with _serving(catalog_dir, active_catalog_path, state_dir) as port:
                plain_status, _, _ = _get(port, "/v1/catalog")
                plain_written = (state_dir / "display-last-fetch.json").exists()
                status, _, body = _get(port, "/v1/catalog?reports_success=1")
            heartbeat = json.loads((state_dir / "display-last-fetch.json").read_text())

        self.assertEqual(plain_status, 200)
        self.assertFalse(plain_written)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), active)
        self.assertEqual(heartbeat["schema_version"], 1)
        self.assertRegex(heartbeat["fetched_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

    def test_health_reports_counts_without_touching_the_index(self) -> None:
        with self._environment() as (_, catalog_dir, state_dir):
            index_path = catalog_dir / "index.json"
            index_path.write_text(json.dumps({"schema_version": 1, "species": [{}, {}]}))
            index_stat = index_path.stat()
            active_catalog_path = state_dir / "active-catalog.json"
            active_catalog_path.write_text(json.dumps({"schema_version": 1, "species": [{}]}))
            with _serving(catalog_dir, active_catalog_path, state_dir) as port:
                status, _, body = _get(port, "/health")
            self.assertEqual(index_path.stat().st_mtime_ns, index_stat.st_mtime_ns)
            self.assertEqual(index_path.stat().st_size, index_stat.st_size)

        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {"ok": True, "approved_species": 2, "active_species": 1, "schema_version": 1},
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
            {"ok": True, "approved_species": 0, "active_species": 0, "schema_version": 1},
        )


if __name__ == "__main__":
    unittest.main()
