"""Shared test hardening: no inherited git configuration, no outbound network."""

from __future__ import annotations

import os
import socket
from typing import Any

# Keep tests hermetic against the developer's git configuration; global
# url.<base>.insteadOf rewrites otherwise change `git remote get-url` output
# inside publisher tests. Subprocesses inherit this environment.
os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_SYSTEM"] = os.devnull

_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
_original_connect = socket.socket.connect


def _guarded_connect(self: socket.socket, address: Any) -> None:
    if isinstance(address, tuple) and address:
        host = address[0]
        if isinstance(host, str) and host not in _LOOPBACK_HOSTS and not host.startswith("127."):
            raise RuntimeError(f"Test attempted an outbound network connection to {host!r}")
    _original_connect(self, address)


socket.socket.connect = _guarded_connect  # type: ignore[method-assign, assignment]
