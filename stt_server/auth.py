"""Bearer-token rules and the TCP bind-host allowlist (server, CLI, install scripts).

- ``LOOPBACK_HOSTS``: the only hosts a TCP listener may bind. Loopback TCP has
  no peer-uid check (any local process can connect); a bearer token is the
  optional guard there. The server's ``ServerConfig`` and the plist renderer
  (``scripts/render_stt_plist.py``) both use this one set.
- ``read_token_file`` / ``check_token_chars``: the bearer-token rules. The serve
  path, the probe client and the installer's preflight
  (``scripts/install_stt_agent.sh``) all call these, so a token one accepts can
  never be one another refuses.

This module itself uses only the stdlib (plus ``stt_server.errors``), but
``import stt_server.auth`` first runs the package ``__init__``, which imports
``stt_server.client`` and so ``websockets``: callers (the renderer, the
installer's preflight) need the project venv, not a bare system python3. It does
not load the server runtime or any backend implementation.
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import ConfigError

LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})


def check_token_chars(token: str, what: str) -> str:
    """Refuse a token that can never match an ``Authorization`` header.

    ``token`` is already stripped and non-empty. Raises ``ConfigError`` naming
    ``what`` (the source, never the value) for internal whitespace, a control
    character or a non-ASCII character. Shared by the token file and env paths.
    """
    if any(not 0x21 <= ord(c) <= 0x7E for c in token):
        raise ConfigError(
            f"{what} must hold one token of visible ASCII characters "
            "(no internal whitespace, newlines or non-ASCII); anything else can never "
            "match an Authorization header"
        )
    return token


def read_token_file(path: str | os.PathLike[str], *, strict: bool = False) -> str | None:
    """Read a bearer-token file: UTF-8, surrounding whitespace stripped.

    The server compares ``Authorization: Bearer <token>`` byte-for-byte, so the
    token must survive an HTTP header. ``strict`` (the serve path, and the
    installer's preflight, which imports this) refuses what would otherwise
    silently misbehave: an empty/whitespace-only file (the token would be
    ``None`` and auth disabled), a token with internal whitespace or a non-ASCII
    / control character (it can never match a header), and a non-UTF-8 file.
    Non-strict (the probe client) keeps the lenient ``None`` for an empty file.
    Messages never include the token.
    """
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ConfigError(f"auth token file {path} is not valid UTF-8") from exc
    if not strict:
        return token or None
    if not token:
        raise ConfigError(f"auth token file {path} is empty; an empty token would disable auth")
    return check_token_chars(token, f"auth token file {path}")
