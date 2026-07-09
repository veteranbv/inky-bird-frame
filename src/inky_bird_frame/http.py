"""Small stdlib JSON HTTP helper."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import DataSourceError


def get_json(url: str, timeout_seconds: float = 10.0) -> object:
    request = Request(url, headers={"User-Agent": "inky-bird-frame/0.1"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read()
    except HTTPError as exc:
        raise DataSourceError(f"HTTP {exc.code} from {url}") from exc
    except URLError as exc:
        raise DataSourceError(f"Could not reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise DataSourceError(f"Timed out reading {url}") from exc

    try:
        return cast(object, json.loads(body))
    except json.JSONDecodeError as exc:
        raise DataSourceError(f"Invalid JSON from {url}") from exc


def get_bytes(url: str, timeout_seconds: float = 30.0) -> bytes:
    request = Request(url, headers={"User-Agent": "inky-bird-frame/0.1"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return cast(bytes, response.read())
    except HTTPError as exc:
        raise DataSourceError(f"HTTP {exc.code} from {url}") from exc
    except URLError as exc:
        raise DataSourceError(f"Could not reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise DataSourceError(f"Timed out reading {url}") from exc


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
        temporary = Path(handle.name)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
