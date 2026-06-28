"""Unit tests for the cross-platform peer-credential resolver.

Covers Phase 2 of the UDS trust-boundary hardening plan: ``peer_uid(sock)``
in ``stt_server._peercred`` resolves the connecting peer's uid via
``getpeereid(2)`` (macOS) or ``SO_PEERCRED`` (Linux), and fails closed
(returns ``None``) on unknown platforms or syscall failure.

These are plain synchronous unit tests (no asyncio); they follow the
``monkeypatch`` idiom used by ``tests/test_env_helpers.py`` and do not rely on
a ``conftest.py``.
"""

from __future__ import annotations

import os
import socket
import struct
import sys

import pytest

from stt_server._peercred import peer_uid


# ---------------------------------------------------------------------------
# Host-platform acceptance gate
#
# The real gate that the ctypes binding (width + signature) and getpeereid
# semantics are correct on the dev host (macOS), and that SO_PEERCRED unpacking
# is correct on Linux. A connected AF_UNIX socketpair has both ends owned by
# this process, so the peer uid must equal our effective uid.
# ---------------------------------------------------------------------------


def test_peer_uid_getpeereid_on_real_socketpair_returns_geteuid():
    """The host-platform gate: real socketpair peer uid == os.geteuid()."""
    if sys.platform not in ("darwin", "linux"):
        pytest.skip(f"no peer-cred resolver path for platform {sys.platform!r}")
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert peer_uid(a) == os.geteuid()
        # Symmetric: both ends are this process, so both resolve identically.
        assert peer_uid(b) == os.geteuid()
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------------------
# Branch-selection coverage regardless of CI host
# ---------------------------------------------------------------------------


def test_linux_branch_unpacks_so_peercred_uid(monkeypatch):
    """Force the Linux branch; mock getsockopt to return a packed ucred."""
    monkeypatch.setattr(sys, "platform", "linux")
    # ``socket.SO_PEERCRED`` is Linux-only and absent on the macOS dev host;
    # provide it so the Linux dispatch branch can be exercised on any host.
    # The fake socket ignores the optname, so the concrete value is irrelevant.
    if not hasattr(socket, "SO_PEERCRED"):
        monkeypatch.setattr(socket, "SO_PEERCRED", 17, raising=False)

    pid, uid, gid = 4321, 1234, 20

    class FakeSocket:
        family = socket.AF_UNIX

        def fileno(self):
            return -1

        def getsockopt(self, level, optname, buflen):
            # SO_PEERCRED returns struct ucred { pid_t; uid_t; gid_t }. The
            # implementation reads it as unsigned ("3I") so a uid >= 2**31 stays
            # positive; mirror that exact format here rather than signed "3i".
            assert buflen == struct.calcsize("3I")
            return struct.pack("3I", pid, uid, gid)

    assert peer_uid(FakeSocket()) == uid


def test_darwin_branch_selected_on_real_socketpair(monkeypatch):
    """Force the darwin branch; on a darwin host this exercises real ctypes."""
    if sys.platform != "darwin":
        pytest.skip("darwin ctypes getpeereid path only runs on macOS host")
    monkeypatch.setattr(sys, "platform", "darwin")
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert peer_uid(a) == os.geteuid()
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------------------
# Fail-closed: None branch
# ---------------------------------------------------------------------------


def test_unknown_platform_returns_none(monkeypatch):
    """Unsupported platform must fail closed (None), never silently allow."""
    monkeypatch.setattr(sys, "platform", "sunos5")
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert peer_uid(a) is None
    finally:
        a.close()
        b.close()


def test_linux_branch_syscall_failure_returns_none(monkeypatch):
    """A raising getsockopt on the Linux branch must return None, not raise."""
    monkeypatch.setattr(sys, "platform", "linux")

    class RaisingSocket:
        family = socket.AF_UNIX

        def fileno(self):
            return -1

        def getsockopt(self, level, optname, buflen):
            raise OSError("getsockopt failed")

    assert peer_uid(RaisingSocket()) is None


def test_darwin_branch_call_failure_returns_none(monkeypatch):
    """getpeereid failure (bad fd / non-zero return) must fail closed to None."""
    if sys.platform != "darwin":
        pytest.skip("darwin ctypes getpeereid path only runs on macOS host")
    monkeypatch.setattr(sys, "platform", "darwin")

    class BadFdSocket:
        family = socket.AF_UNIX

        def fileno(self):
            # An invalid descriptor makes getpeereid(2) fail (EBADF / non-zero).
            return -1

    assert peer_uid(BadFdSocket()) is None
