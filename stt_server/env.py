"""Tiny env-var coercion helpers.

Resolved at call time so tests / operators can monkeypatch without
re-importing. Returning the default on parse failure (with a warning)
lets a typo'd env value not crash a long-running process.

Canonical-then-alias precedence (the ``*_first`` helpers) deliberately uses
two different "which name wins" rules by value type, because what an empty
value *means* differs by type:

- ``env_first`` (string) picks the first **non-empty** name. An empty string
  is not a meaningful label/token, so a blank canonical falls through to the
  alias (then the default) — you would not want ``PIPECAT_STT_LABEL=""`` to
  win and blank out a set ``KODA_STT_LABEL``.
- ``env_bool_first`` / ``env_float_first`` / ``env_int_first`` (typed) pick the
  first **present** name (via :func:`_first_present_name`). There is no
  meaningful "empty number"/"empty bool", so a present-but-empty canonical
  wins and resolves to the default — blanking the canonical reliably overrides
  a set alias rather than silently deferring to it.

This split is intentional; keep it in mind before unifying the resolvers.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("stt_server.env")

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


def _first_present_name(names: tuple[str, ...]) -> str | None:
    """Return the first env var in ``names`` that is *present* (set to any
    value, including empty/whitespace), else ``None``.

    Unlike :func:`env_first` (which skips empty values), this reports mere
    presence so :func:`env_bool_first` can honour an explicit empty/``"0"``
    as a meaningful "False" rather than falling through to the default.
    """
    for name in names:
        if os.environ.get(name) is not None:
            return name
    return None


def env_bool_first(*names: str, default: bool) -> bool:
    """:func:`env_bool` with canonical-then-alias precedence across ``names``:
    the first *present* name wins. Pass the canonical name first."""
    name = _first_present_name(names)
    if name is None:
        return default
    return env_bool(name, default)


def env_float_first(*names: str, default: float) -> float:
    """:func:`env_float` with canonical-then-alias precedence across ``names``:
    the first *present* name wins (same presence rule as
    :func:`env_bool_first`). Pass the canonical name first.

    Presence — not non-emptiness — decides the winner, so a present-but-empty
    canonical resolves to ``default`` (via :func:`env_float`) and still
    overrides a set alias. Parsing/warning is delegated to :func:`env_float`
    so coercion stays single-sourced.
    """
    name = _first_present_name(names)
    if name is None:
        return default
    return env_float(name, default)


def env_int_first(*names: str, default: int) -> int:
    """:func:`env_int` with canonical-then-alias precedence across ``names``:
    the first *present* name wins (same presence rule as
    :func:`env_bool_first`). Pass the canonical name first.

    Presence — not non-emptiness — decides the winner, so a present-but-empty
    canonical resolves to ``default`` (via :func:`env_int`) and still overrides
    a set alias. Parsing/warning is delegated to :func:`env_int` so coercion
    stays single-sourced.
    """
    name = _first_present_name(names)
    if name is None:
        return default
    return env_int(name, default)
