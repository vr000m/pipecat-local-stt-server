#!/usr/bin/env python3
"""Local end-to-end smoke for the UDS peer-credential trust boundary.

Not part of the pytest suite — a one-command check of the two things a
single-uid CI run structurally cannot exercise:

1. **Cross-uid rejection (the real test, local-only).** A connection from a
   *foreign* local uid must be rejected by peer-cred (#3) with a pre-handshake
   HTTP ``403`` and body ``peer not permitted\\n``. To prove peer-cred is what
   rejects (and not the filesystem boundary), the server is built so BOTH
   filesystem layers permit the peer: a deliberately-traversable ``0711`` parent
   dir AND a ``0o666`` socket mode. That combination would normally be refused at
   startup by ``_enforce_socket_dir_secure`` (R1), so this script replaces that
   helper through a **test-only monkeypatch** that is *not* reachable from
   ``serve()`` or normal ``TranscriptionServer`` construction — there is no
   ``ServerConfig`` field or public flag for it. The foreign uid is driven via
   ``sudo -u <user>``; with no second uid / passwordless sudo available the path
   skips cleanly (exit 0), it does not fail.

2. **Same-uid multi-connection (CI-safe).** N concurrent ``TranscriptionClient``
   sessions as the owning uid must all complete the handshake and stream — a
   regression guard that peer-cred did not break the normal path under
   concurrency. It also resolves the peer uid through the **real**
   ``stt_server._peercred.peer_uid`` on a live socket and asserts it equals
   ``os.geteuid()``, so a silently-``None`` transport is caught rather than
   masked.

Usage::

    uv run python scripts/smoke_peercred.py
    uv run python scripts/smoke_peercred.py --connections 8

Run via ``just smoke-peercred``. The server runs IN-PROCESS (not as a
subprocess) so the test-only helper replacement and ``unix_socket_mode=0o666``
can be applied directly — the public ``serve()`` exposes neither.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import pwd
import socket
import subprocess
import sys
import tempfile

# Run from the repo root so ``stt_server`` imports resolve (also required when
# this file is re-invoked under ``sudo -u`` for the foreign-uid connect role).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets.exceptions  # noqa: E402

from stt_server import _peercred  # noqa: E402
from stt_server.backend import EchoBackend  # noqa: E402
from stt_server.client import TranscriptionClient  # noqa: E402
from stt_server.protocol import (  # noqa: E402
    AUDIO_CHANNELS,
    AUDIO_FORMAT,
    AUDIO_SAMPLE_RATE_HZ,
    EVT_SERVER_HELLO,
    EVT_TRANSCRIPT_COMPLETED,
    PROTOCOL_VERSION,
)

REJECT_BODY = "peer not permitted\n"
# Tokens the foreign-uid child prints on stdout so the parent can classify the
# outcome without parsing a traceback.
CHILD_REJECTED = "PEERCRED_REJECTED_403"
CHILD_ACCEPTED = "PEERCRED_ACCEPTED"
CHILD_OTHER = "PEERCRED_OTHER"


# ---------------------------------------------------------------------------
# Child role: connect under whatever uid this process is running as.
# ---------------------------------------------------------------------------
async def _connect_as_peer(sock: str) -> int:
    """Attempt one handshake against ``sock``; print a classification token.

    Exit code mirrors the token so a ``sudo`` caller can branch on either. A
    peer-cred reject is the SUCCESS case for the cross-uid test, so it exits 0.
    """
    client = TranscriptionClient(socket_path=sock)
    try:
        await client.connect()
    except websockets.exceptions.InvalidStatus as exc:
        response = exc.response
        body = response.body.decode("utf-8", "replace") if response.body else ""
        if response.status_code == 403 and body == REJECT_BODY:
            print(f"{CHILD_REJECTED} status={response.status_code} body={body!r}")
            return 0
        print(f"{CHILD_OTHER} status={response.status_code} body={body!r}")
        return 2
    except Exception as exc:  # noqa: BLE001 - surface any unexpected failure
        print(f"{CHILD_OTHER} exception={type(exc).__name__}: {exc}")
        return 2
    else:
        with contextlib.suppress(Exception):
            await client.close()
        print(CHILD_ACCEPTED)
        return 1


# ---------------------------------------------------------------------------
# Same-uid multi-connection path (CI-safe).
# ---------------------------------------------------------------------------
def _assert_hello(hello: dict) -> None:
    """Assert ``server.hello`` matches what ``server.py`` actually emits."""
    assert hello["type"] == EVT_SERVER_HELLO, hello
    assert hello["protocol_version"] == PROTOCOL_VERSION, hello
    assert hello["capabilities"] == {
        "binary_audio": True,
        "base64_audio_append": True,
        "server_vad": False,
    }, hello
    assert hello["audio"] == {
        "format": AUDIO_FORMAT,
        "rate": AUDIO_SAMPLE_RATE_HZ,
        "channels": AUDIO_CHANNELS,
    }, hello
    # EchoBackend identity: name "echo", model None.
    assert hello["backend"]["name"] == "echo", hello


async def _one_session(sock: str, index: int) -> str:
    """Run one full connect → stream → completed session; return transcript."""
    async with TranscriptionClient(socket_path=sock) as client:
        hello = await client.connect()
        _assert_hello(hello)
        await client.update_session(turn_detection=None)
        # 50 ms of silence is enough for EchoBackend (it echoes the byte count).
        pcm = b"\x00" * (AUDIO_SAMPLE_RATE_HZ * AUDIO_CHANNELS * 2 // 20)
        await client.send_audio(pcm)
        await client.commit()
        async for ev in client.events():
            if ev.get("type") == EVT_TRANSCRIPT_COMPLETED:
                transcript = ev.get("transcript", "")
                await client.close_session()
                return transcript
    raise SystemExit(f"session {index}: closed without a transcript.completed event")


def _assert_real_resolver(sock: str) -> None:
    """Resolve the peer uid on a live socket via the REAL resolver.

    Connecting a raw AF_UNIX socket to the server gives us a socket whose *peer*
    is the server process; ``peer_uid`` on it returns the server's uid. Since the
    server runs as us, it must equal ``os.geteuid()``. A ``None`` here would mean
    the transport silently yielded no creds — exactly the masked failure this
    guards against.
    """
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        raw.connect(sock)
        resolved = _peercred.peer_uid(raw)
    finally:
        raw.close()
    expected = os.geteuid()
    if resolved is None:
        raise SystemExit(
            "real peer_uid resolver returned None on a live socket — the "
            "transport is not exposing peer credentials (silent fail-open risk)"
        )
    if resolved != expected:
        raise SystemExit(f"real peer_uid resolver returned {resolved}, expected {expected}")
    print(f"  real resolver: peer_uid == os.geteuid() == {expected}  OK")


async def _run_same_uid(sock: str, connections: int) -> None:
    print(f"\n=== same-uid multi-connection ({connections} concurrent) ===")
    results = await asyncio.gather(*(_one_session(sock, i) for i in range(connections)))
    assert all(r.startswith("echo:") for r in results), results
    print(f"  {len(results)} concurrent sessions all completed: {sorted(set(results))}")
    _assert_real_resolver(sock)


# ---------------------------------------------------------------------------
# Cross-uid path (local-only).
# ---------------------------------------------------------------------------
def _find_second_uid() -> tuple[str, int] | None:
    """Return (username, uid) of a real local user reachable via passwordless
    ``sudo``, or ``None`` if none is available (the usual CI / dev case)."""
    if sys.platform not in ("darwin", "linux"):
        return None
    if not _have("sudo"):
        return None
    me = os.geteuid()
    # macOS normal users start at 501; Linux at 1000. Keep it conservative and
    # skip system accounts and ourselves.
    min_uid = 501 if sys.platform == "darwin" else 1000
    for entry in pwd.getpwall():
        if entry.pw_uid == me or entry.pw_uid < min_uid:
            continue
        if entry.pw_name.startswith("_"):  # macOS service accounts
            continue
        # Passwordless sudo to this user, non-interactive: succeeds silently or
        # we move on. ``-n`` never prompts.
        try:
            probe = subprocess.run(
                ["sudo", "-n", "-u", entry.pw_name, "true"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return entry.pw_name, entry.pw_uid
    return None


def _have(cmd: str) -> bool:
    return subprocess.run(
        ["command", "-v", cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0 or os.path.exists(f"/usr/bin/{cmd}")


async def _run_cross_uid(sock: str) -> bool:
    """Drive the example client under a second uid; assert peer-cred rejects.

    Returns ``True`` if the cross-uid assertion ran, ``False`` if it was skipped.
    """
    second = _find_second_uid()
    if second is None:
        print("\n=== cross-uid rejection ===")
        print("  skipped: needs a second local uid reachable via passwordless sudo")
        return False

    username, uid = second
    print(f"\n=== cross-uid rejection (peer uid {uid} / {username}) ===")
    # Re-invoke THIS file under the second uid in its connect-as-peer role.
    cmd = [
        "sudo",
        "-n",
        "-u",
        username,
        sys.executable,
        os.path.abspath(__file__),
        "--connect-as-peer",
        sock,
    ]
    proc = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60
    )
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if err:
        print(f"  child stderr: {err}")
    print(f"  child stdout: {out}")
    if CHILD_REJECTED not in out:
        raise SystemExit(
            f"cross-uid connect was NOT rejected with 403 '{REJECT_BODY.strip()}' "
            f"(rc={proc.returncode}, stdout={out!r}) — peer-cred boundary FAILED"
        )
    print("  cross-uid connection rejected with 403 'peer not permitted' — peer-cred OK")
    return True


# ---------------------------------------------------------------------------
# Server lifecycle (in-process).
# ---------------------------------------------------------------------------
@contextlib.asynccontextmanager
async def _running_server():
    """Spin up a TranscriptionServer on a temp UDS with both filesystem layers
    deliberately permissive, so peer-cred is the only boundary left.

    The dir-enforcement helper is replaced by a test-only no-op monkeypatch that
    is unreachable from ``serve()`` / normal construction (no ServerConfig flag).
    """
    import stt_server.server as server_module

    # TEST-ONLY seam: defeat R1's ancestor-chain enforcement so a 0711/0o666
    # socket can be bound. NOT reachable from serve() or public construction.
    original_enforce = server_module._enforce_socket_dir_secure
    server_module._enforce_socket_dir_secure = lambda *a, **k: None

    tmpdir = tempfile.mkdtemp(prefix="peercred-smoke-")
    # Make the parent dir traversable by *other* uids (0711): +x for group/other
    # lets a foreign uid path-resolve to the socket. R1 would normally refuse
    # this; the monkeypatch above is why it binds.
    os.chmod(tmpdir, 0o711)
    sock = os.path.join(tmpdir, "p.sock")

    config = server_module.ServerConfig(
        socket_path=sock,
        # 0o666: socket is connectable by any uid — the other half of defeating
        # the filesystem boundary so peer-cred is provably what rejects.
        unix_socket_mode=0o666,
        reject_browser_origins=False,
    )
    srv = server_module.TranscriptionServer(EchoBackend(), config)
    await srv.start()
    try:
        yield sock
    finally:
        await srv.shutdown()
        server_module._enforce_socket_dir_secure = original_enforce
        with contextlib.suppress(OSError):
            os.unlink(sock)
        with contextlib.suppress(OSError):
            os.rmdir(tmpdir)


async def _run(args: argparse.Namespace) -> int:
    if sys.platform not in ("darwin", "linux"):
        print(f"skipped: peer-cred smoke needs macOS/Linux, not {sys.platform}")
        return 0

    async with _running_server() as sock:
        # Verify socket/parent perms are actually permissive — the whole point is
        # that the filesystem does NOT reject, so peer-cred provably does.
        import stat as _stat

        sock_mode = _stat.S_IMODE(os.stat(sock).st_mode)
        parent_mode = _stat.S_IMODE(os.stat(os.path.dirname(sock)).st_mode)
        print("=== UDS peer-cred smoke ===")
        print(f"  socket : {sock}  mode={oct(sock_mode)}")
        print(f"  parent : {os.path.dirname(sock)}  mode={oct(parent_mode)}")
        assert sock_mode & 0o006, f"socket not other-accessible: {oct(sock_mode)}"
        assert parent_mode & 0o001, f"parent not other-traversable: {oct(parent_mode)}"

        await _run_same_uid(sock, args.connections)
        ran_cross = await _run_cross_uid(sock)

    print("\n=== summary ===")
    print("  same-uid multi-connection: PASS")
    print(f"  cross-uid rejection: {'PASS' if ran_cross else 'SKIPPED (no second uid)'}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--connections",
        type=int,
        default=5,
        help="number of concurrent same-uid sessions (default: 5)",
    )
    parser.add_argument(
        "--connect-as-peer",
        metavar="SOCK",
        default=None,
        help=argparse.SUPPRESS,  # internal: foreign-uid child role (via sudo -u)
    )
    args = parser.parse_args()

    if args.connect_as_peer is not None:
        raise SystemExit(asyncio.run(_connect_as_peer(args.connect_as_peer)))

    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
