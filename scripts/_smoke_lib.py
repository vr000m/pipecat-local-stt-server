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

It also holds what ``smoke.py`` and ``smoke_launchd.py`` must agree on: the spare
label, the installed-agent plist glob and directory, the home the installed real
agents are read from, the launchd preconditions, and the websocket token-gate
assertion (``assert_token_gate``).

Two homes. ``env_home()`` is ``$HOME``: where the installer puts the spare's
plist, logs and socket. ``agents_home()`` is where both scripts look for the
installed real agents (``smoke.py --agents`` and the harness's port guard and
before/after snapshot): the account's home from the password database, so an
overridden ``HOME`` can neither empty the snapshot nor hide an agent from the
port guard. The only way to move ``agents_home()`` is the test-only variable
``PIPECAT_STT_SMOKE_TEST_AGENTS_HOME`` (``TEST_AGENTS_HOME_ENV``), which the
stubbed test suites set to a temp directory so they never read the developer's
real ``~/Library/LaunchAgents``. Nothing else reads it, the installer's
environment is scrubbed of it, and whoever honours it says so on stderr.

The launchd checks are SKIPPED when ``CI`` is present in the environment with
any value, the empty string included (Decision 8: they run only when ``CI`` is
unset).
"""

from __future__ import annotations

import os
import pwd
import stat
from pathlib import Path

SPARE_LABEL = "pipecat.stt-server.smoke"
AGENT_GLOB = "pipecat.stt-server*.plist"
TEST_AGENTS_HOME_ENV = "PIPECAT_STT_SMOKE_TEST_AGENTS_HOME"


def env_home() -> Path:
    """``$HOME`` (what the installer and the agents' own paths use)."""
    return Path(os.environ.get("HOME") or str(Path.home()))


def account_home() -> Path:
    """The account's home from the password database; ``$HOME`` cannot move it."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def agents_home_override(env=None) -> Path | None:
    """The test-only agents home override, or None when it is not set."""
    val = (os.environ if env is None else env).get(TEST_AGENTS_HOME_ENV)
    return Path(val) if val else None


def agents_home(env=None) -> Path:
    """Where the installed real agents are read from: the account home, or the
    test-only override. ``$HOME`` never moves it."""
    return agents_home_override(env) or account_home()


def launch_agents_dir(home: Path) -> Path:
    return home / "Library" / "LaunchAgents"


def agent_plists(home: Path) -> list[Path]:
    """Installed ``pipecat.stt-server*`` plists under HOME, the spare excluded."""
    found = sorted(launch_agents_dir(home).glob(AGENT_GLOB))
    return [p for p in found if p.name != f"{SPARE_LABEL}.plist"]


def launchd_skip_reason(env, platform: str) -> str | None:
    """Why the launchd checks cannot run on this machine (SKIPPED), or None."""
    if platform != "darwin":
        return f"launchd checks need macOS, not {platform}"
    if "CI" in env:  # set at all, even empty: Decision 8 runs them only when unset
        return "CI is set; the launchd checks never run in CI"
    return None


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
