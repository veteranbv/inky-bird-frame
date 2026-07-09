from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from inky_bird_frame.server import CatalogRequestHandler


class ServerTests(unittest.TestCase):
    def test_active_catalog_is_not_cached_as_an_immutable_asset(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog_dir = root / "catalog"
            catalog_dir.mkdir()
            active_catalog_path = root / "active-catalog.json"
            active_catalog_path.write_text(json.dumps({"schema_version": 1, "species": []}))
            handler = type(
                "TestCatalogRequestHandler",
                (CatalogRequestHandler,),
                {
                    "catalog_dir": catalog_dir,
                    "active_catalog_path": active_catalog_path,
                },
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                connection.request("GET", "/v1/catalog")
                response = connection.getresponse()
                response.read()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")


if __name__ == "__main__":
    unittest.main()
