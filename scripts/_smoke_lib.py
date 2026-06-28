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
