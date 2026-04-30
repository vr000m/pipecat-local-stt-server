"""Tiny env-var coercion helpers.

Resolved at call time so tests / operators can monkeypatch without
re-importing. Returning the default on parse failure (with a warning)
lets a typo'd env value not crash a long-running process.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("shared.env")

_TRUTHY = {"1", "true", "yes", "on"}


def env_bool(name: str, default: bool) -> bool:
    """Truthy values: ``"1"``, ``"true"``, ``"yes"``, ``"on"`` (case- and
    whitespace-insensitive). Anything else that is set — including
    ``"False"``, ``"0"``, and the empty string — is False. Only ``unset``
    falls through to ``default``.
    """
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in _TRUTHY


def env_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    try:
        return float(val)
    except ValueError:
        logger.warning("invalid float for %s=%r; using default %s", name, val, default)
        return default


def env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    try:
        return int(val)
    except ValueError:
        logger.warning("invalid int for %s=%r; using default %s", name, val, default)
        return default


def env_first(*names: str, default: str | None = None) -> str | None:
    """Return the first set, non-empty env var among ``names``.

    Used for canonical-then-alias lookups, where a newer var name takes
    precedence but an older one is still honoured for backward compat.
    """
    for name in names:
        val = os.environ.get(name)
        if val is not None and val.strip() != "":
            return val
    return default
