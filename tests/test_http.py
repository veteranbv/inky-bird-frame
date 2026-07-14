from __future__ import annotations

import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch
from urllib.error import HTTPError

import inky_bird_frame.http
from inky_bird_frame.errors import DataSourceError
from inky_bird_frame.http import get_bytes, get_json


class _RedirectingHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/ftp-redirect":
            self.send_response(302)
            self.send_header("Location", "ftp://127.0.0.1/pub/file.bin")
            self.end_headers()
            return
        if self.path == "/http-redirect":
            self.send_response(302)
            self.send_header("Location", "/payload")
            self.end_headers()
            return
        body = b"payload-bytes"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, message_format: str, *args: object) -> None:
        pass


@contextmanager
def _serving() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectingHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


class JsonHttpTests(unittest.TestCase):
    def test_error_label_redacts_sensitive_url(self) -> None:
        url = "https://example.test/stations/private-token/species"
        error = HTTPError(url, 401, "Unauthorized", Message(), None)

        with (
            patch.object(inky_bird_frame.http._OPENER, "open", side_effect=error),
            self.assertRaises(DataSourceError) as raised,
        ):
            get_json(url, error_label="BirdWeather API")

        self.assertEqual(str(raised.exception), "HTTP 401 from BirdWeather API")
        self.assertNotIn("private-token", str(raised.exception))

    def test_rejects_non_http_url_scheme(self) -> None:
        with self.assertRaisesRegex(DataSourceError, "non-HTTP URL scheme"):
            get_bytes("file:///etc/hostname")

    def test_rejects_redirect_to_non_http_scheme(self) -> None:
        with (
            _serving() as port,
            self.assertRaisesRegex(DataSourceError, "Refusing redirect"),
        ):
            get_bytes(f"http://127.0.0.1:{port}/ftp-redirect")

    def test_follows_same_scheme_redirects(self) -> None:
        with _serving() as port:
            body = get_bytes(f"http://127.0.0.1:{port}/http-redirect")

        self.assertEqual(body, b"payload-bytes")


if __name__ == "__main__":
    unittest.main()
