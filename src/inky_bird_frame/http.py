"""Small stdlib JSON HTTP helper."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from http.client import HTTPMessage, HTTPResponse
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import IO, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import DataSourceError

MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_ASSET_BYTES = 64 * 1024 * 1024


def _read_capped(response: HTTPResponse, limit: int, display_url: str) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise DataSourceError(f"Response from {display_url} exceeds {limit} bytes")
    body = response.read(limit + 1)
    if len(body) > limit:
        raise DataSourceError(f"Response from {display_url} exceeds {limit} bytes")
    return body


def _checked_request(url: str, headers: Mapping[str, str]) -> Request:
    scheme = urlsplit(url).scheme
    if scheme not in ("http", "https"):
        raise DataSourceError(f"Refusing to fetch non-HTTP URL scheme: {scheme or 'none'}")
    return Request(url, headers=dict(headers))


class _HTTPOnlyRedirectHandler(HTTPRedirectHandler):
    # urllib otherwise follows redirects onto FTP, bypassing the scheme check.
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Request | None:
        if urlsplit(newurl).scheme not in ("http", "https"):
            raise DataSourceError(f"Refusing redirect to non-HTTP URL scheme: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = build_opener(_HTTPOnlyRedirectHandler)


def get_json(
    url: str,
    timeout_seconds: float = 10.0,
    *,
    headers: Mapping[str, str] | None = None,
    error_label: str | None = None,
) -> object:
    request_headers = {"User-Agent": "inky-bird-frame/0.1"}
    if headers is not None:
        request_headers.update(headers)
    display_url = error_label or url
    request = _checked_request(url, request_headers)
    try:
        with _OPENER.open(request, timeout=timeout_seconds) as response:
            body = _read_capped(cast(HTTPResponse, response), MAX_JSON_BYTES, display_url)
    except HTTPError as exc:
        raise DataSourceError(f"HTTP {exc.code} from {display_url}") from exc
    except URLError as exc:
        raise DataSourceError(f"Could not reach {display_url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise DataSourceError(f"Timed out reading {display_url}") from exc

    try:
        return cast(object, json.loads(body))
    except json.JSONDecodeError as exc:
        raise DataSourceError(f"Invalid JSON from {display_url}") from exc


def get_bytes(url: str, timeout_seconds: float = 30.0) -> bytes:
    request = _checked_request(url, {"User-Agent": "inky-bird-frame/0.1"})
    try:
        with _OPENER.open(request, timeout=timeout_seconds) as response:
            return _read_capped(cast(HTTPResponse, response), MAX_ASSET_BYTES, url)
    except HTTPError as exc:
        raise DataSourceError(f"HTTP {exc.code} from {url}") from exc
    except URLError as exc:
        raise DataSourceError(f"Could not reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise DataSourceError(f"Timed out reading {url}") from exc


def _fsync_directory(directory: Path) -> None:
    # Rename durability needs the parent directory synced; skip filesystems
    # that cannot fsync a directory handle rather than failing the write.
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        temporary.replace(path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def write_json_atomic(path: Path, value: object) -> None:
    write_bytes_atomic(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))
