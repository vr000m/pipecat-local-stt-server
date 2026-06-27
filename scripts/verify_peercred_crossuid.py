#!/usr/bin/env python3
"""Local cross-uid peer-cred verification (the leg `just smoke-peercred` skips
without a real second uid).

Runs an in-process permissive TranscriptionServer (0711 parent under /tmp +
0o666 socket, dir-enforcement monkeypatched off — the same test-only seam the
smoke uses), then drives a STDLIB-ONLY probe against the SAME socket twice:

  * as the owning uid  -> expect 101 Switching Protocols (peer-cred allows)
  * as `nobody`        -> expect 403 'peer not permitted' (peer-cred rejects)

Same socket, same perms, only the uid differs — so a 101-vs-403 split proves the
peer-credential gate (not the filesystem boundary) is what discriminates. The
probe needs no venv/websockets, so `nobody` (who cannot read this repo under a
0750 home) can still run it from /tmp via the system /usr/bin/python3.

Run from the repo root:  uv run python <this file>
The `nobody` leg uses `sudo -u nobody`; sudo will prompt for your password once
(or run `sudo -v` first to pre-cache).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Resolve repo root robustly even when run from scratchpad: rely on `uv run`'s
# project env for the import below.
import stt_server.server as server_module  # noqa: E402
from stt_server.backend import EchoBackend  # noqa: E402

SYS_PY = "/usr/bin/python3"  # world-executable; reachable by nobody

# Stdlib-only probe: open the UDS, send a minimal WebSocket upgrade, read the
# pre-handshake HTTP status. Written to /tmp (world-readable) at runtime.
PROBE_SRC = r"""
import socket, sys
sock_path = sys.argv[1]
req = (
    "GET / HTTP/1.1\r\n"
    "Host: localhost\r\n"
    "Upgrade: websocket\r\n"
    "Connection: Upgrade\r\n"
    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
    "Sec-WebSocket-Version: 13\r\n"
    "\r\n"
)
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(10)
try:
    s.connect(sock_path)
except OSError as e:
    # Failed before reaching the gate => filesystem boundary, NOT peer-cred.
    print("CONNECT_ERROR:%s:%s" % (type(e).__name__, e))
    sys.exit(3)
data = b""
try:
    s.sendall(req.encode())
    while b"\r\n\r\n" not in data and len(data) < 65536:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    try:
        data += s.recv(4096)
    except OSError:
        pass
except OSError as e:
    print("IO_ERROR:%s:%s" % (type(e).__name__, e))
    sys.exit(3)
finally:
    s.close()
text = data.decode("latin-1", "replace")
status = text.split("\r\n", 1)[0] if text else ""
print("STATUS:%s" % status)
print("PEER_NOT_PERMITTED:%s" % ("peer not permitted" in text))
"""


def _classify(out: str) -> str:
    if "CONNECT_ERROR" in out or "IO_ERROR" in out:
        return "INCONCLUSIVE"  # never reached the gate (filesystem boundary)
    if "STATUS:" in out and " 403 " in out and "PEER_NOT_PERMITTED:True" in out:
        return "REJECTED_403"
    if "STATUS:" in out and " 101 " in out:
        return "ACCEPTED_101"
    return "OTHER"


async def _run_probe(sock: str, probe_path: str, as_nobody: bool) -> tuple[str, str, int]:
    # MUST be async (not subprocess.run): the server runs in THIS process's event
    # loop, so a blocking subprocess.run would freeze the loop and the probe's
    # connection would never be serviced (it would time out). create_subprocess_exec
    # keeps the loop live so the in-process server can respond. stdin is inherited
    # so `sudo` can still prompt on the tty when run interactively.
    cmd = [SYS_PY, probe_path, sock]
    if as_nobody:
        cmd = ["sudo", "-u", "nobody", *cmd]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return "", "timeout", 124
    return out_b.decode().strip(), err_b.decode().strip(), proc.returncode or 0


def _assert_chain_traversable(sock: str) -> None:
    d = os.path.dirname(os.path.realpath(sock))
    while True:
        mode = stat.S_IMODE(os.stat(d).st_mode)
        if not (mode & 0o001):
            raise SystemExit(
                f"ancestor {d} not other-traversable (mode={oct(mode)}); "
                "a foreign uid would fail before the peer-cred gate"
            )
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent


async def main() -> int:
    if sys.platform != "darwin" and sys.platform != "linux":
        print(f"skipped: needs macOS/Linux, not {sys.platform}")
        return 0
    if not os.path.exists(SYS_PY):
        raise SystemExit(f"{SYS_PY} not found; need a system python3 reachable by nobody")

    original = server_module._enforce_socket_dir_secure
    server_module._enforce_socket_dir_secure = lambda *a, **k: None
    tmpdir = tempfile.mkdtemp(prefix="peercred-crossuid-", dir="/tmp")
    os.chmod(tmpdir, 0o711)
    sock = os.path.join(tmpdir, "p.sock")
    probe_path = os.path.join(tmpdir, "probe.py")
    with open(probe_path, "w") as f:
        f.write(PROBE_SRC)
    os.chmod(probe_path, 0o644)

    config = server_module.ServerConfig(
        socket_path=sock, unix_socket_mode=0o666, reject_browser_origins=False
    )
    srv = server_module.TranscriptionServer(EchoBackend(), config)
    await srv.start()
    try:
        _assert_chain_traversable(sock)
        print("=== cross-uid peer-cred verification ===")
        print(f"  socket : {sock}  mode={oct(stat.S_IMODE(os.stat(sock).st_mode))}")
        print(f"  euid   : {os.geteuid()}\n")

        self_out, self_err, _ = await _run_probe(sock, probe_path, as_nobody=False)
        self_v = _classify(self_out)
        print(f"  [self uid={os.geteuid()}] {self_v}  ({self_out!r})")
        if self_err:
            print(f"      stderr: {self_err}")

        print("\n  driving probe as 'nobody' (sudo will prompt if not cached)…")
        nob_out, nob_err, nob_rc = await _run_probe(sock, probe_path, as_nobody=True)
        # If sudo itself couldn't run the child (no tty / password required /
        # not cached), the probe never executed — that's a setup gap, NOT a
        # gate result. Detect it (empty probe output but non-zero exit).
        sudo_blocked = not nob_out and nob_rc != 0
        nob_v = "SUDO_UNAVAILABLE" if sudo_blocked else _classify(nob_out)
        print(f"  [nobody]          {nob_v}  (rc={nob_rc}, out={nob_out!r})")
        if nob_err:
            print(f"      stderr: {nob_err}")

        print("\n=== verdict ===")
        if self_v == "ACCEPTED_101" and nob_v == "REJECTED_403":
            print("  PASS: same socket+perms — same-uid accepted (101), foreign uid")
            print("        rejected (403). Peer-cred is provably the discriminator.")
            return 0
        if nob_v == "SUDO_UNAVAILABLE":
            print("  NOT RUN: could not launch the child as 'nobody' — sudo needs a")
            print("        password/tty here. Run this in your terminal (sudo will")
            print(f"        prompt), or `sudo -v` first. Same-uid leg verified: {self_v}.")
            return 2
        if nob_v == "INCONCLUSIVE":
            print("  INCONCLUSIVE: the nobody probe never reached the gate (filesystem")
            print("        boundary). Not proof the gate is broken.")
            return 2
        print(f"  FAIL: self={self_v}, nobody={nob_v} — expected ACCEPTED_101 / REJECTED_403.")
        return 1
    finally:
        await srv.shutdown()
        server_module._enforce_socket_dir_secure = original
        with contextlib.suppress(OSError):
            os.unlink(sock)
        with contextlib.suppress(OSError):
            os.unlink(probe_path)
        with contextlib.suppress(OSError):
            os.rmdir(tmpdir)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
