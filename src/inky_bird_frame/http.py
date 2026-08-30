"""Small stdlib JSON HTTP helper."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from http.client import HTTPException, HTTPMessage, HTTPResponse
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import IO, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import DataSourceError

MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_ASSET_BYTES = 64 * 1024 * 1024
USER_AGENT = "inky-bird-frame/0.1"


def _read_capped(response: HTTPResponse, limit: int, display_url: str) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise DataSourceError(f"Response from {display_url} exceeds {limit} bytes")
    body = response.read(limit + 1)
    if len(body) > limit:
        raise DataSourceError(f"Response from {display_url} exceeds {limit} bytes")
    return body


def _checked_request(
    url: str,
    headers: Mapping[str, str],
    *,
    data: bytes | None = None,
    method: str | None = None,
) -> Request:
    scheme = urlsplit(url).scheme
    if scheme not in ("http", "https"):
        raise DataSourceError(f"Refusing to fetch non-HTTP URL scheme: {scheme or 'none'}")
    return Request(url, data=data, headers=dict(headers), method=method)


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
        scheme = urlsplit(newurl).scheme
        if scheme not in ("http", "https"):
            raise DataSourceError(f"Refusing redirect to non-HTTP URL scheme: {scheme or 'none'}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = build_opener(_HTTPOnlyRedirectHandler)


class _SameOriginRedirectHandler(_HTTPOnlyRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Request | None:
        original = urlsplit(req.full_url)
        redirected = urlsplit(newurl)
        if (original.scheme, original.hostname, original.port) != (
            redirected.scheme,
            redirected.hostname,
            redirected.port,
        ):
            raise DataSourceError("Refusing cross-origin JSON POST redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_POST_OPENER = build_opener(_SameOriginRedirectHandler)


def get_json(
    url: str,
    timeout_seconds: float = 10.0,
    *,
    headers: Mapping[str, str] | None = None,
    error_label: str | None = None,
) -> object:
    request_headers = {"User-Agent": USER_AGENT}
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
    except HTTPException as exc:
        raise DataSourceError(f"Invalid HTTP response from {display_url}") from exc
    except OSError as exc:
        raise DataSourceError(f"Could not read response from {display_url}") from exc

    try:
        return cast(object, json.loads(body))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DataSourceError(f"Invalid JSON from {display_url}") from exc


def post_json(
    url: str,
    value: object,
    timeout_seconds: float = 10.0,
    *,
    headers: Mapping[str, str] | None = None,
    error_label: str | None = None,
) -> object:
    request_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    if headers is not None:
        request_headers.update(headers)
    display_url = error_label or url
    try:
        encoded = json.dumps(value).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("JSON request body was not serializable") from exc
    request = _checked_request(url, request_headers, data=encoded, method="POST")
    try:
        with _POST_OPENER.open(request, timeout=timeout_seconds) as response:
            body = _read_capped(cast(HTTPResponse, response), MAX_JSON_BYTES, display_url)
    except HTTPError as exc:
        raise DataSourceError(f"HTTP {exc.code} from {display_url}") from exc
    except URLError as exc:
        raise DataSourceError(f"Could not reach {display_url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise DataSourceError(f"Timed out reading {display_url}") from exc
    except HTTPException as exc:
        raise DataSourceError(f"Invalid HTTP response from {display_url}") from exc
    except OSError as exc:
        raise DataSourceError(f"Could not read response from {display_url}") from exc

    try:
        return cast(object, json.loads(body))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DataSourceError(f"Invalid JSON from {display_url}") from exc


def get_bytes(url: str, timeout_seconds: float = 30.0) -> bytes:
    request = _checked_request(url, {"User-Agent": USER_AGENT})
    try:
        with _OPENER.open(request, timeout=timeout_seconds) as response:
            return _read_capped(cast(HTTPResponse, response), MAX_ASSET_BYTES, url)
    except HTTPError as exc:
        raise DataSourceError(f"HTTP {exc.code} from {url}") from exc
    except URLError as exc:
        raise DataSourceError(f"Could not reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise DataSourceError(f"Timed out reading {url}") from exc
    except HTTPException as exc:
        raise DataSourceError(f"Invalid HTTP response from {url}") from exc
    except OSError as exc:
        raise DataSourceError(f"Could not read response from {url}") from exc


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


def write_bytes_atomic(path: Path, content: bytes, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        if mode is not None:
            os.fchmod(handle.fileno(), mode)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        temporary.replace(path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def write_json_atomic(path: Path, value: object, *, mode: int | None = None) -> None:
    write_bytes_atomic(
        path,
        (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        mode=mode,
    )
