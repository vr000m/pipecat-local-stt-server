"""Shared helpers for the peer-cred verification scripts.

``smoke_peercred.py`` and ``verify_peercred_crossuid.py`` both drive a cross-uid
probe against a deliberately permissive socket to prove that *peer-cred* (not the
filesystem) is what rejects a foreign uid. That assertion is only meaningful if
the foreign uid can actually traverse the path to the socket — otherwise it fails
at PATH RESOLUTION before reaching ``_process_request`` and the verdict reflects
the filesystem boundary, not the peer-cred gate. This module holds the single
canonical traversability check both scripts use so they cannot diverge.

Pure stdlib (``os``/``stat``) so it imports under both the venv driver and any
system-python context.
"""

from __future__ import annotations

import os
import stat


def assert_chain_traversable(sock: str) -> None:
    """Require every directory from the socket's parent up to ``/`` to be
    other-traversable (``mode & 0o001``).

    Raises ``SystemExit`` with a targeted diagnostic if any ancestor is not
    other-traversable: a foreign uid would fail at path resolution BEFORE the
    peer-cred gate, so the cross-uid result would be meaningless rather than a
    real peer-cred verdict.
    """
    d = os.path.dirname(os.path.realpath(sock))
    while True:
        mode = stat.S_IMODE(os.stat(d).st_mode)
        if not (mode & 0o001):
            raise SystemExit(
                f"socket ancestor {d} is not other-traversable (mode={oct(mode)}): a "
                "foreign uid would fail at path resolution BEFORE the peer-cred gate, "
                "so the cross-uid result would be meaningless. Bind the socket under a "
                "world-traversable root (e.g. /tmp)."
            )
        parent = os.path.dirname(d)
        if parent == d:  # reached '/'
            break
        d = parent


def ws_handshake_status(
    *,
    host: str | None = None,
    port: int | None = None,
    unix_path: str | None = None,
    token: str | None = None,
    timeout: float = 5.0,
) -> int:
    """Send a websocket upgrade request and return the HTTP status code.

    Connects over TCP (``host`` + ``port``) or a Unix socket (``unix_path``) and
    reads only the response status line, then closes. The server runs its bearer
    check (``_process_request``) before it validates the upgrade, so a missing or
    wrong token is a 401 and an accepted request is a 101. Stdlib only.
    """
    import base64
    import socket

    if unix_path is not None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(unix_path)
    else:
        if host is None or port is None:
            raise ValueError("provide host+port or unix_path")
        sock = socket.create_connection((host, port), timeout=timeout)
    try:
        lines = [
            "GET / HTTP/1.1",
            "Host: localhost",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}",
            "Sec-WebSocket-Version: 13",
        ]
        if token:
            lines.append(f"Authorization: Bearer {token}")
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        buf = b""
        while b"\r\n" not in buf:
            chunk = sock.recv(1024)
            if not chunk:
                break
            buf += chunk
        parts = buf.split(b"\r\n", 1)[0].decode("latin-1").split()
        if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
            raise OSError(f"no HTTP status line in the reply: {buf[:80]!r}")
        return int(parts[1])
    finally:
        sock.close()


def assert_token_gate(host: str, port: int, token_file: str) -> None:
    """Require the server at HOST:PORT to enforce the bearer token in TOKEN_FILE.

    A websocket upgrade without a token must be refused with ``401`` (the
    server's reply to a missing or wrong ``Authorization: Bearer`` header), and
    the same request carrying the token read from ``token_file`` must be accepted
    (``101 Switching Protocols``). Raises ``AssertionError`` otherwise.
    """
    with open(token_file, encoding="utf-8") as fh:
        token = fh.read().strip()
    if not token:
        raise AssertionError(f"token file {token_file} holds no token")
    without = ws_handshake_status(host=host, port=port)
    if without != 401:
        raise AssertionError(
            f"{host}:{port} answered {without} to a request with no token; expected 401"
        )
    wrong = ws_handshake_status(host=host, port=port, token=token + "x")
    if wrong != 401:
        raise AssertionError(
            f"{host}:{port} answered {wrong} to a request with a wrong token; expected 401"
        )
    accepted = ws_handshake_status(host=host, port=port, token=token)
    if accepted != 101:
        raise AssertionError(
            f"{host}:{port} answered {accepted} to a request with the token from "
            f"{token_file}; expected 101"
        )
