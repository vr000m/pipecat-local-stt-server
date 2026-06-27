"""Cross-platform peer-credential resolution for AF_UNIX sockets.

The server authenticates a connecting UDS client by the **kernel-supplied**
peer uid rather than anything the client sends. The uid the kernel captures at
``connect()`` time is unforgeable, which is why peer-cred auth is the
kernel-authoritative defense-in-depth backstop behind the owner-only filesystem
ancestor chain (see R3 / R4 in the dev plan).

``peer_uid(sock)`` returns the connecting peer's uid, or ``None`` when it cannot
be resolved (unknown platform, missing socket, or any syscall failure). The
contract is **fail-closed**: a ``None`` return tells the caller to reject the
connection. This module deliberately raises nothing on the resolution paths —
the caller treats ``None`` as "reject," so a leaked exception would be a worse
failure mode than an explicit ``None``.

Platform notes:

- **Linux** uses ``SO_PEERCRED``: ``getsockopt`` returns a ``struct ucred {
  pid_t pid; uid_t uid; gid_t gid; }``, unpacked as ``"3I"`` (unsigned).
- **macOS** has no ``SO_PEERCRED``; we call ``getpeereid(2)`` through
  ``ctypes``. The wrinkle worth flagging for future maintainers: ``uid_t`` and
  ``gid_t`` are ``c_uint32`` on Darwin, and we set ``argtypes``/``restype`` on
  the libc function **explicitly**. A wrong width or signature could fail *open*
  (e.g. an uninitialized output buffer that happens to compare equal to the
  server uid), so the binding is pinned and the ``socketpair()`` unit test that
  asserts ``peer_uid() == os.geteuid()`` is the gate against a width/signature
  mistake. We also load libc with ``use_errno=True`` for correct errno
  propagation.

The same-uid precondition (R4): the client and server are assumed to run as the
same uid (per the Koda cross-repo contract, both are per-user LaunchAgents). A
uid mismatch is rejected, never warn-and-allowed.

Kept import-light and side-effect-free so it is unit-testable without binding a
server (mirrors the existing single ``sys.platform == "darwin"`` precedent in
``server.py``; no new abstraction framework).
"""

from __future__ import annotations

import functools
import logging
import socket
import struct
import sys
from typing import Protocol, runtime_checkable

logger = logging.getLogger("stt_server")


@runtime_checkable
class PeerCredSocket(Protocol):
    """Minimal structural socket contract the resolver depends on.

    Only the members ``peer_uid`` actually touches are declared, so callers can
    pass the concrete ``socket.socket`` and tests can pass a lightweight mock —
    the resolver is decoupled from the concrete socket class.
    """

    family: int

    def fileno(self) -> int: ...

    def getsockopt(self, level: int, optname: int, buflen: int) -> bytes: ...


def peer_uid(sock: PeerCredSocket) -> int | None:
    """Return the connecting peer's uid, or ``None`` if it cannot be resolved.

    ``None`` is the fail-closed signal: the caller rejects the connection. This
    covers unknown platforms and any syscall failure. The actual syscall paths
    are wrapped so an unexpected error yields ``None`` rather than propagating.
    """
    try:
        if sys.platform.startswith("linux"):
            return _peer_uid_linux(sock)
        if sys.platform == "darwin":
            return _peer_uid_darwin(sock)
    except Exception:  # noqa: BLE001 - fail closed on any resolution failure
        logger.warning("peer_uid: failed to resolve peer credentials on %s", sys.platform)
        return None

    logger.warning(
        "peer_uid: no peer-credential mechanism for platform %s; failing closed",
        sys.platform,
    )
    return None


def _peer_uid_linux(sock: PeerCredSocket) -> int | None:
    """Resolve the peer uid via ``SO_PEERCRED`` (``struct ucred``)."""
    # "3I": pid_t/uid_t/gid_t are unsigned in ``struct ucred``. Unpacking as
    # signed ("3i") would turn a uid >= 2**31 negative, so it would never equal
    # os.geteuid() and a legitimate same-uid peer would be wrongly rejected.
    buf = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3I"))
    _pid, uid, _gid = struct.unpack("3I", buf)
    return uid


@functools.lru_cache(maxsize=1)
def _darwin_getpeereid():
    """Load libc and pin the ``getpeereid`` binding ONCE per process.

    ``_process_request`` runs per UDS connection; re-doing ``CDLL(None)`` (a libc
    re-dlopen) plus re-resolving the symbol and re-setting ``argtypes``/``restype``
    on every call is wasted work. The configured callable never changes between
    calls, so build it once and cache it. ``uid_t``/``gid_t`` are ``c_uint32`` on
    Darwin; libc is loaded with ``use_errno=True`` (see module docstring).
    """
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    fn = libc.getpeereid
    fn.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    fn.restype = ctypes.c_int
    return fn


def _peer_uid_darwin(sock: PeerCredSocket) -> int | None:
    """Resolve the peer uid via ``getpeereid(2)`` through ``ctypes``."""
    import ctypes

    getpeereid = _darwin_getpeereid()
    uid = ctypes.c_uint32()
    gid = ctypes.c_uint32()
    rc = getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid))
    if rc != 0:
        errno = ctypes.get_errno()
        logger.warning(
            "peer_uid: getpeereid failed (rc=%d, errno=%d); failing closed",
            rc,
            errno,
        )
        return None
    return uid.value
