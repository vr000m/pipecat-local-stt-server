"""Shared helpers for the smoke scripts: one definition of everything two or more
of them must agree on, so no copy can drift. Stdlib only, so it imports under
the venv driver and under any system python.

The module holds several separate concerns; each section below is used by:

* Smoke outcomes and the runner-harness contract (``smoke.py``,
  ``smoke_launchd.py``, ``smoke_peercred.py``): the outcome names ``PASS`` /
  ``FAIL`` / ``SKIPPED``; the harness's skip line (``LAUNCHD_SKIP_MARK``) and
  failure channel (``LAUNCHD_FAIL_MARK``, ``launchd_fail_lines``,
  ``launchd_failures``); the peer-cred skip lines; the launchd preconditions
  (``launchd_skip_reason``: the launchd checks are SKIPPED when ``CI`` is
  present with any value, the empty string included, Decision 8).
* Agent discovery (``smoke.py --agents``, the harness's guard and snapshot): the
  spare label, the installed-agent plist glob and directory, and the two homes.
  ``env_home()`` is ``$HOME``: where the installer puts the spare's plist, logs
  and socket. ``agents_home()`` is where the installed real agents are read
  from: the account's home from the password database, so an overridden
  ``HOME`` can neither empty the snapshot nor hide an agent from the port guard.
  No environment variable moves it; the stubbed test suites patch
  ``agents_home`` (in-process, or through ``tests/_with_agents_home.py`` for a
  subprocess) so they never read the developer's real ``~/Library/LaunchAgents``.
* Peer-cred traversal (``smoke_peercred.py``, ``verify_peercred_crossuid.py``):
  ``assert_chain_traversable``. A cross-uid probe proves that peer-cred (not
  the filesystem) rejects a foreign uid only if that uid can traverse the path
  to the socket; otherwise it fails at path resolution, before the peer-cred
  gate, and the verdict would reflect the filesystem boundary.
* Verified connections (``smoke.py --agents``' decode child, the token-file
  step, the harness's token check and its transport-recovery waits):
  ``connect_verified`` (a launchd job) and ``connect_verified_child`` (the
  token-file step's own child server), the one rule for
  sending a token, and ``launchd_identity`` (a job's loaded state, pid and
  spawn counter from one ``launchctl print``, a thin wrapper over
  ``squat_check``'s one reader). Contract at that section.
* Token gate (``smoke.py``'s token-file step, the harness's token check): the
  token-file fixture (``write_policy_token_file``), a raw websocket handshake
  (``ws_handshake_status``, which sends a token only over a socket the caller
  verified) and the 401/401/101 assertion (``assert_token_gate``, Decision 9),
  whose every request goes over a ``connect_verified`` socket.
* The harness's environment (``smoke_launchd.py``): ``harness_env`` and
  ``SAFE_PATH``, what every harness subprocess and the installer inherit.
* Owned process groups (``smoke.py``'s steps, the harness's installer calls):
  ``run_owned`` and ``OwnedGroup``, the one lifecycle for a child whose whole
  process group may have to be killed (invariant at that section), and
  ``held_signals``, the one helper that holds SIGINT / SIGTERM for a block.

The smoke scripts' four signal policies. Each is a distinct role, so they are
kept apart; only the hold is shared (``held_signals``):

==============================  ==================================  ==============================
policy                          where                               why
==============================  ==================================  ==============================
hold, then redeliver or drop    ``run_owned`` (redeliver),          nothing cuts the block short;
(``held_signals``)              ``smoke._token_check``'s cleanup    run_owned redelivers the first
                                (drop)                              held signal only after its
                                                                    GroupNotStopped verdict; the
                                                                    exiting child drops it
SIGTERM -> KeyboardInterrupt    ``smoke._term_as_interrupt``        the runner treats SIGTERM like
                                (``smoke.main``)                    Ctrl-C, so the active step is
                                                                    stopped through run_owned
SIGTERM -> SystemExit(128+n)    ``smoke._token_check``'s            the token-check child runs its
                                ``on_term``                         finally (server, token dir)
raise once, then ignore         ``smoke_launchd.signal_handler``,   the first signal stops the
                                ``begin_teardown``                  checks; no later one escapes
                                                                    main or cuts the teardown short
==============================  ==================================  ==============================
"""

from __future__ import annotations

import contextlib
import os
import pwd
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# squat_check sits beside this module (every importer has scripts/ on its
# path) and is stdlib-only; it never imports this one. It owns the one
# `launchctl print` reader and the UNKNOWN sentinel.
import squat_check

# Step outcomes, shared by smoke.py's table and smoke_launchd.py's.
PASS = "PASS"
FAIL = "FAIL"
SKIPPED = "SKIPPED"
# The first stdout line smoke_launchd.py prints (exit 0) when a precondition is
# absent; smoke.py maps it to SKIPPED, never PASS.
LAUNCHD_SKIP_MARK = "SKIPPED: launchd checks not run: "
# The failure channel: after its table smoke_launchd.py prints one stdout line
# per FAIL row, this mark + "<check>: <detail>"; smoke.py's launchd row takes its
# detail from these lines (never from whatever line happens to come last).
LAUNCHD_FAIL_MARK = "FAIL: launchd check failed: "


def launchd_fail_lines(rows) -> list[str]:
    """The failure-channel lines for ROWS ((name, outcome, detail) triples)."""
    return [
        f"{LAUNCHD_FAIL_MARK}{name}: {' '.join(detail.split())}"
        for name, outcome, detail in rows
        if outcome == FAIL
    ]


def launchd_failures(stdout: str) -> list[str]:
    """ "<check>: <detail>" for each failure-channel line in STDOUT."""
    return [
        ln[len(LAUNCHD_FAIL_MARK) :]
        for ln in (stdout or "").splitlines()
        if ln.startswith(LAUNCHD_FAIL_MARK)
    ]


SPARE_LABEL = "pipecat.stt-server.smoke"
AGENT_GLOB = "pipecat.stt-server*.plist"
# smoke_peercred.py's skip lines start with these (it exits 0 on both), and
# smoke.py maps them to SKIPPED; one definition so the two cannot drift.
PEERCRED_NO_UID_MARK = "skipped: needs a second local uid"
PEERCRED_PLATFORM_MARK = "skipped: peer-cred smoke needs"


def env_home() -> Path:
    """``$HOME`` (what the installer and the agents' own paths use)."""
    return Path(os.environ.get("HOME") or str(Path.home()))


def account_home() -> Path:
    """The account's home from the password database; ``$HOME`` cannot move it."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def agents_home() -> Path:
    """Where the installed real agents are read from: the account home.
    ``$HOME`` never moves it, and no environment variable does; the stubbed
    test suites patch this function (``tests/_with_agents_home.py``)."""
    return account_home()


def launch_agents_dir(home: Path) -> Path:
    return home / "Library" / "LaunchAgents"


def agent_plists(home: Path) -> list[Path]:
    """Installed ``pipecat.stt-server*`` plists under HOME, the spare excluded."""
    found = sorted(launch_agents_dir(home).glob(AGENT_GLOB))
    return [p for p in found if p.name != f"{SPARE_LABEL}.plist"]


# The harness's environment. Every subprocess the launchd harness runs, the
# installer included, inherits only these, so nothing the caller's shell
# exported steers it: no BASH_ENV / ENV (sourced by bash before the
# installer reads its label), no exported BASH_FUNC_* (a `launchctl`
# function), no PYTHON*, DYLD_* or LD_*, and a fixed PATH instead of the
# caller's. Kept: HOME, USER, TMPDIR, and of the PIPECAT_STT_* / KODA_STT_*
# names only the two the harness reads (the spare label, which the guard
# checks, and the log dir); install_env sets the installer's own per call.
SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
_HARNESS_KEEP = (
    "HOME", "USER", "TMPDIR",
    "PIPECAT_STT_LABEL", "KODA_STT_LABEL", "PIPECAT_STT_LOG_DIR", "KODA_STT_LOG_DIR",
)  # fmt: skip


def safe_path() -> str:
    """The harness's PATH: ``SAFE_PATH``. No environment variable moves it;
    the stubbed test suites patch this function (in-process, or through
    ``tests/_with_agents_home.py`` for a subprocess) to put their stub
    ``launchctl`` / ``lsof`` / ``id`` first."""
    return SAFE_PATH


def harness_env(base) -> dict[str, str]:
    """The kept names from BASE (see above) plus ``PATH=safe_path()``."""
    env = {k: v for k, v in base.items() if k in _HARNESS_KEEP}
    env["PATH"] = safe_path()
    return env


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


# --- verified connections -------------------------------------------------------
#
# The contract for sending a token (or anything secret) to an agent endpoint:
# the token goes only over a connection whose SERVER END was verified to belong
# to the job, on that same connection; a check on one connection and a send on
# another leaves a window in which a squatter can take the endpoint.
#
#   1. The caller reads the job's pid P and spawn counter R from ONE
#      `launchctl print` (launchd_identity, or the harness's read_job).
#   2. connect_verified opens the connection and sends NOTHING.
#   3. It identifies the process holding the server end of THAT connection:
#      AF_UNIX -> getsockopt(SOL_LOCAL, LOCAL_PEERPID) on the connected socket
#      (macOS; anything else cannot tell); TCP -> lsof for the socket whose
#      endpoints are (server address -> our ephemeral address), polled briefly
#      because a connection is not in any process until the server accepts it.
#      The pid set must be exactly [P]; unreadable, invisible (lsof cannot see
#      another uid's sockets) or different -> EndpointRefused, socket closed.
#   4. A second `launchctl print` (READ_IDENTITY) must still give (P, R); only
#      then is the socket returned, for the caller to write the token on.
#   5. The caller requires a FRESH read to give (P, R) after the exchange
#      before it reports a PASS.
#
# Residuals: a same-uid process cannot hold the job's pid, so a lookalike fails
# step 3; a root process (which can do anything) is out of scope; LOCAL_PEERPID
# and lsof's view of an accepted socket are macOS facts the stubbed tests
# cannot prove; the live run does.

# A value the reader could not read (a running job's runs counter, a snapshot
# field): never equal to anything, and never an identity to verify against.
# Defined once, in squat_check (which cannot import this module);
# smoke_launchd.UNKNOWN is this same object too.
UNKNOWN = squat_check.UNKNOWN

SOL_LOCAL = 0  # <sys/un.h>, macOS
LOCAL_PEERPID = 2  # <sys/un.h>, macOS: pid of the peer at connect time
SERVER_END_SETTLE = 3.0  # how long lsof gets to see the accepted server end
# The bound on the connect, and (separately) on the whole request-and-status-
# line exchange after it: one deadline, never reset by a byte that arrives.
HANDSHAKE_TIMEOUT = 5.0
STATUS_LINE_MAX = 4096  # bytes read for the status line before it is "no status line"


class EndpointRefused(Exception):
    """The endpoint's server end is not provably the expected job's; nothing
    was sent on the connection (it is closed)."""


def launchd_identity(label: str, uid=None, run=subprocess.run):
    """(loaded, pid, runs) from ONE read-only ``launchctl print gui/<uid>/LABEL``
    through ``squat_check.read_job_print`` (the one reader and its rules);
    None when the answer cannot be read. A running job whose answer has no
    ``runs =`` line has runs UNKNOWN (never None)."""
    job = squat_check.read_job_print(label, run, uid)
    return None if job is None else (job.loaded, job.pid, job.runs)


def _lsof_addr(addr) -> str:
    """A socket address as ``lsof -nP`` prints it: ``host:port``, ``[v6]:port``."""
    host, port = addr[0], addr[1]
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def unix_peer_pid(sock) -> str | None:
    """The pid at the other end of a connected AF_UNIX socket (macOS
    ``LOCAL_PEERPID``), or None when it cannot be read (fail closed)."""
    if sys.platform != "darwin":
        return None
    try:
        raw = sock.getsockopt(SOL_LOCAL, LOCAL_PEERPID, struct.calcsize("i"))
        pid = struct.unpack("i", raw)[0]
    except (OSError, struct.error):
        return None
    return str(pid) if pid > 0 else None


def tcp_server_end_pids(
    sock, run=subprocess.run, settle: float = SERVER_END_SETTLE, sleep=time.sleep
) -> list[str] | None:
    """Pids holding the server end of the connected TCP socket SOCK: the lsof
    entries whose name is ``<server address>-><our address>``. [] when none is
    visible within SETTLE seconds, None when lsof cannot tell."""
    local, remote = _lsof_addr(sock.getsockname()), _lsof_addr(sock.getpeername())
    want = f"{remote}->{local}"
    deadline = time.monotonic() + settle
    while True:
        out = squat_check.lsof_output([f"-iTCP@{local}", "-Fpn"], run)
        if out is None:
            return None
        pids: set[str] = set()
        cur = None
        for line in out.splitlines():
            if line.startswith("p"):
                cur = line[1:]
            elif line.startswith("n") and line[1:] == want and cur:
                pids.add(cur)
        if pids or time.monotonic() >= deadline:
            return sorted(pids)
        sleep(0.1)


def connect_verified(
    endpoint: tuple,
    pid: str | None,
    runs: str | None,
    read_identity,
    *,
    timeout: float = HANDSHAKE_TIMEOUT,
    run=subprocess.run,
    settle: float = SERVER_END_SETTLE,
    sleep=time.sleep,
):
    """A connected socket to ENDPOINT (``("unix", path)`` or ``("tcp", host,
    port)``) whose server end belongs to the launchd job's pid PID, with
    nothing sent on it (steps 2-4 of the contract above). READ_IDENTITY()
    re-reads the job and must return ``(PID, RUNS)``. Raises EndpointRefused
    otherwise."""
    if not pid or not runs or runs == UNKNOWN:
        raise EndpointRefused(f"no launchd pid and runs to verify against (pid {pid}, runs {runs})")

    def still_same(where: str) -> str | None:
        again = read_identity()
        if again == (pid, runs):
            return None
        return f"the job changed while {where} was verified (pid {pid}, runs {runs} -> {again})"

    return _connect_held_by(
        endpoint, pid, still_same, f"the launchd job (pid {pid})",
        timeout=timeout, run=run, settle=settle, sleep=sleep,
    )  # fmt: skip


def connect_verified_child(
    endpoint: tuple,
    child: subprocess.Popen,
    *,
    timeout: float = HANDSHAKE_TIMEOUT,
    run=subprocess.run,
    settle: float = SERVER_END_SETTLE,
    sleep=time.sleep,
):
    """``connect_verified`` for a server that is this process's OWN child
    (smoke.py's token-file step), not a launchd job: the identity is CHILD's
    pid. While CHILD is unreaped (``poll()`` is None) its pid cannot be handed
    to another process, so "still running" stands in for launchd's
    ``(pid, runs)`` re-read in step 4. Raises EndpointRefused when CHILD has
    exited, before or during the check."""
    pid = str(child.pid)
    if child.poll() is not None:
        raise EndpointRefused(f"the server (pid {pid}) has exited")

    def still_same(where: str) -> str | None:
        if child.poll() is None:
            return None
        return f"the server (pid {pid}) exited while {where} was verified"

    return _connect_held_by(
        endpoint, pid, still_same, f"the server this process started (pid {pid})",
        timeout=timeout, run=run, settle=settle, sleep=sleep,
    )  # fmt: skip


def _connect_held_by(endpoint, pid, still_same, owner, *, timeout, run, settle, sleep):
    """Steps 2-4: connect to ENDPOINT, send nothing, require the server end's
    pid set to be exactly [PID], then STILL_SAME(where) (None, or why the
    identity no longer holds). OWNER names PID in a refusal."""
    try:
        if endpoint[0] == "unix":
            where = endpoint[1]
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.settimeout(timeout)
                sock.connect(endpoint[1])
            except BaseException:
                sock.close()
                raise
        else:
            host, port = endpoint[1], endpoint[2]
            if not host:
                raise EndpointRefused(f"the TCP endpoint has no host (port {port})")
            where = f"{host}:{port}"
            sock = socket.create_connection((host, int(port)), timeout=timeout)
    except OSError as exc:
        raise EndpointRefused(f"cannot connect to {where}: {exc}") from exc
    try:
        if endpoint[0] == "unix":
            peer = unix_peer_pid(sock)
            owners = None if peer is None else [peer]
            how = "LOCAL_PEERPID"
        else:
            owners = tcp_server_end_pids(sock, run, settle, sleep)
            how = "lsof"
        if owners is None:
            raise EndpointRefused(f"cannot identify the server end of {where} ({how} failed)")
        if not owners:
            raise EndpointRefused(
                f"the server end of {where} is not visible ({how}; another user's socket?)"
            )
        if owners != [pid]:
            raise EndpointRefused(
                f"the server end of {where} is held by pid {', '.join(owners)}, not {owner}"
            )
        changed = still_same(where)
        if changed:
            raise EndpointRefused(changed)
    except BaseException:
        sock.close()
        raise
    return sock


def ws_handshake_status(
    *,
    host: str | None = None,
    port: int | None = None,
    unix_path: str | None = None,
    sock=None,
    token: str | None = None,
    timeout: float = HANDSHAKE_TIMEOUT,
) -> int:
    """Send a websocket upgrade request and return the HTTP status code.

    Uses SOCK (a connected socket, closed afterwards) or connects over TCP
    (``host`` + ``port``) or a Unix socket (``unix_path``), and reads only the
    response status line, then closes. The server runs its bearer check
    (``_process_request``) before it validates the upgrade, so a missing or
    wrong token is a 401 and an accepted request is a 101. A TOKEN (or a wrong
    one, which is nearly the token) is sent only over SOCK, which the caller
    got from ``connect_verified``; with an address it is a ValueError.

    Bounds: the connect gets TIMEOUT; then sending the request and reading
    the status line share ONE deadline of TIMEOUT (a peer that trickles bytes
    without a CRLF cannot extend it), and at most STATUS_LINE_MAX bytes are
    read. Past either, OSError (TimeoutError for the deadline).
    """
    import base64

    if token is not None and sock is None:
        raise ValueError("a token is sent only over a verified socket (sock=connect_verified())")
    if sock is not None:
        sock.settimeout(timeout)
    elif unix_path is not None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(unix_path)
    else:
        if host is None or port is None:
            raise ValueError("provide host+port or unix_path")
        sock = socket.create_connection((host, port), timeout=timeout)
    deadline = time.monotonic() + timeout

    def left() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"no HTTP status line within {timeout:g}s")
        return remaining

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
        sock.settimeout(left())
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        buf = b""
        while b"\r\n" not in buf:
            if len(buf) >= STATUS_LINE_MAX:
                raise OSError(f"no HTTP status line in the first {STATUS_LINE_MAX} bytes")
            sock.settimeout(left())
            chunk = sock.recv(min(1024, STATUS_LINE_MAX - len(buf)))
            if not chunk:
                break
            buf += chunk
        parts = buf.split(b"\r\n", 1)[0].decode("latin-1").split()
        if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
            raise OSError(f"no HTTP status line in the reply: {buf[:80]!r}")
        return int(parts[1])
    finally:
        sock.close()


def assert_token_gate(host: str, port: int, token_file: str, *, connect) -> None:
    """Require the server at HOST:PORT to enforce the bearer token in TOKEN_FILE.

    A websocket upgrade without a token must be refused with ``401`` (the
    server's reply to a missing or wrong ``Authorization: Bearer`` header), and
    the same request carrying the token read from ``token_file`` must be accepted
    (``101 Switching Protocols``). Raises ``AssertionError`` otherwise. Each of
    the three requests goes over a fresh ``CONNECT()`` socket (a
    ``connect_verified`` one: required, so no caller can send the token to an
    unverified listener); its EndpointRefused propagates.
    """
    with open(token_file, encoding="utf-8") as fh:
        token = fh.read().strip()
    if not token:
        raise AssertionError(f"token file {token_file} holds no token")
    without = ws_handshake_status(sock=connect())
    if without != 401:
        raise AssertionError(
            f"{host}:{port} answered {without} to a request with no token; expected 401"
        )
    wrong = ws_handshake_status(sock=connect(), token=token + "x")
    if wrong != 401:
        raise AssertionError(
            f"{host}:{port} answered {wrong} to a request with a wrong token; expected 401"
        )
    accepted = ws_handshake_status(sock=connect(), token=token)
    if accepted != 101:
        raise AssertionError(
            f"{host}:{port} answered {accepted} to a request with the token from "
            f"{token_file}; expected 101"
        )


def write_policy_token_file(home: Path, prefix: str) -> tuple[Path, Path]:
    """Create (directory, token file) that pass ``stt_server.auth``'s token-file
    policy: a fresh 0700 directory directly under HOME (``/tmp`` fails the
    ancestor-directory check) holding a 0600 file created with ``O_EXCL``. The
    caller removes the directory."""
    import secrets

    directory = Path(tempfile.mkdtemp(prefix=prefix, dir=str(home)))
    try:
        os.chmod(directory, 0o700)
        token_file = directory / "token"
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_urlsafe(24) + "\n")
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return directory, token_file


# --- owned process groups ------------------------------------------------------
#
# One mechanism for every child whose whole process group a smoke script may
# have to kill: smoke.py's steps and smoke_launchd.py's installer calls.
#
# Invariant: the group is signalled only while its leader is this process's
# own, not yet reaped, child. An exited child stays a zombie until it is
# reaped, and while it exists neither its pid nor the process group it leads
# can be handed to another process; so a signal sent then reaches this group
# and nothing else. Exit is therefore watched WITHOUT reaping (waitid(WNOWAIT)
# where the platform has it, else a kqueue NOTE_EXIT), the group is SIGKILLed
# while the leader is still unreaped, and only then is the leader reaped, once.
# After the reap both signal methods refuse.

GROUP_POLL = 0.1  # how often the leader's exit, the clock and held signals are checked
GROUP_DRAIN = 5.0  # pipe drain after the group is killed (a holder may have left it)
READER_STOP = 1.0  # then the stopped readers' join (each notices STOP within GROUP_POLL)


def reap_bound() -> float:
    """OwnedGroup.reap's worst case: the drain, then the stopped readers' join."""
    return GROUP_DRAIN + READER_STOP


# held_signals is the one hold-then-act helper; the other signal policies are
# distinct roles (the table in the module docstring).


@contextlib.contextmanager
def held_signals(*, reraise: bool = False):
    """Record SIGINT and SIGTERM instead of acting on them for the block (main
    thread only; elsewhere nothing is held and nothing is raised). Yields
    ``(got, old)``: the signals that arrived and the handlers that were in
    place, restored on exit (a handler Python did not install, None, is left).

    Without RERAISE a held signal is the caller's: ``run_owned`` redelivers
    ``got[0]`` to ``old`` itself (``redeliver``), after its GroupNotStopped
    verdict; a caller that ignores ``got`` drops it. With RERAISE, once the
    handlers are back and only when the block finished normally, a held
    signal is raised as KeyboardInterrupt; an exception from the block wins.
    Nested, the inner block holds what arrives in it and restores the outer
    hold; what the inner one holds never reaches the outer ``got``."""
    got: list[int] = []
    if threading.current_thread() is not threading.main_thread():
        yield got, {}
        return

    def hold(signum, _frame):
        got.append(signum)

    old = {s: signal.signal(s, hold) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield got, old
    finally:
        for s, h in old.items():
            if h is not None:
                signal.signal(s, h)
    if reraise and got:
        raise KeyboardInterrupt(f"signal {got[0]}")


def redeliver(sig: int, handler) -> None:
    """Hand a held SIG to HANDLER (what was in place before it was held): a
    function is called, SIG_IGN drops it, anything else is KeyboardInterrupt."""
    if callable(handler):
        handler(sig, None)
    elif handler != signal.SIG_IGN:
        raise KeyboardInterrupt(f"signal {sig}")


def _drain(pipe, sink: list[bytes], stop: threading.Event) -> None:
    """Read PIPE (the Popen's pipe file object, which this thread's reference
    keeps alive, so GC cannot close it) into SINK until EOF or STOP. It reads
    only when the pipe is readable (polled every GROUP_POLL), so a read never
    blocks and the thread notices STOP within one poll: the owner sets STOP,
    joins the thread, and only then closes the pipe, so no read can ever land
    on an fd number the owner closed (and the system may have reused)."""
    import selectors

    fd = pipe.fileno()
    with contextlib.suppress(OSError, ValueError), selectors.DefaultSelector() as sel:
        sel.register(fd, selectors.EVENT_READ)
        while not stop.is_set():
            if not sel.select(GROUP_POLL):
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                return
            sink.append(chunk)


class GroupNotStopped(RuntimeError):
    """``run_owned`` could not prove the child's group stopped: the SIGKILL
    failed, or the leader was still not reaped after it. Something the child
    started may still be running; the caller must treat this as a failure."""


def group_not_stopped(exc: BaseException) -> GroupNotStopped | None:
    """The GroupNotStopped EXC is or carries: a held signal's exception
    (e.g. KeyboardInterrupt) that ``run_owned`` raised after a group it could
    not show stopped carries it as ``group_not_stopped`` (and as its
    ``__cause__``); None when the group was not left running."""
    if isinstance(exc, GroupNotStopped):
        return exc
    found = getattr(exc, "group_not_stopped", None)
    return found if isinstance(found, GroupNotStopped) else None


def _feed(pipe, data: bytes, stop: threading.Event) -> None:
    """Write DATA to PIPE (the child's stdin) and close it, on its own thread,
    so the caller's timeout loop runs while it writes. The fd is non-blocking
    and written only when writable (polled every GROUP_POLL), so a child that
    never reads cannot block this thread past STOP, which the owner sets on
    the reap, after the group is killed. A child that closed its stdin or
    exited first (EPIPE) is the child's own problem: the rest is dropped.
    PIPE is closed on every path (the child's EOF); no other thread uses it."""
    import selectors

    try:
        fd = pipe.fileno()
        os.set_blocking(fd, False)
        view = memoryview(data)
        with selectors.DefaultSelector() as sel:
            sel.register(fd, selectors.EVENT_WRITE)
            while view and not stop.is_set():
                if not sel.select(GROUP_POLL):
                    continue
                with contextlib.suppress(BlockingIOError):
                    view = view[os.write(fd, view) :]
    except (OSError, ValueError):
        pass  # BrokenPipeError: the child is not reading any more
    finally:
        with contextlib.suppress(OSError, ValueError):
            pipe.close()


class OwnedGroup:
    """A child started in its own session (so it leads its own process group),
    signalled only while this process owns that group (see the invariant
    above). Its stdout and stderr are drained by two daemon threads; INPUT, when
    given, is written to its stdin by a third (``_feed``), which then closes it.

    A failure after the child started (the kqueue registration, a thread
    start) kills and reaps the group before it propagates; when the group
    cannot be shown stopped (``stop_verdict``) the constructor raises
    GroupNotStopped, chained to that failure, never the bare failure."""

    def __init__(self, cmd, *, env=None, cwd=None, input: bytes | None = None):
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL if input is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
        self.pid = self.proc.pid
        self.reaped = False
        self._exited = False
        self._kq = None
        self._out: list[bytes] = []
        self._err: list[bytes] = []
        self._readers: list[threading.Thread] = []
        self._stop_reading = threading.Event()
        try:
            if not hasattr(os, "waitid"):
                import select

                self._kq = select.kqueue()
                ev = select.kevent(
                    self.pid,
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                    fflags=select.KQ_NOTE_EXIT,
                )
                try:
                    self._kq.control([ev], 0, 0)
                except ProcessLookupError:
                    # Already exited: still a zombie, since only reap() reaps it.
                    self._exited = True
            # The pipe threads: the two readers, then the stdin writer. reap()
            # stops and joins every one before it closes the output pipes.
            targets = [
                (_drain, (self.proc.stdout, self._out, self._stop_reading)),
                (_drain, (self.proc.stderr, self._err, self._stop_reading)),
            ]
            if input is not None:
                targets.append((_feed, (self.proc.stdin, input, self._stop_reading)))
            for target, args in targets:
                t = threading.Thread(target=target, args=args, daemon=True)
                t.start()
                self._readers.append(t)
        except BaseException as exc:
            # The group exists (Popen succeeded): kill and reap it, and when it
            # cannot be shown stopped that verdict is what propagates.
            kill_error = self.kill_group()
            self.reap()
            not_stopped = self.stop_verdict(kill_error)
            if not_stopped is not None:
                raise not_stopped from exc
            raise

    def stop_verdict(self, kill_error: str | None) -> GroupNotStopped | None:
        """After ``kill_group()`` (its result, KILL_ERROR) and ``reap()``:
        None when the group is shown stopped (the kill landed AND the leader
        was reaped), else the GroupNotStopped to raise. The one rule for
        ``run_owned`` and for a constructor that failed after the spawn."""
        if kill_error is None and self.reaped:
            return None
        why = kill_error or f"pid {self.pid} was still running after SIGKILL and the reap wait"
        return GroupNotStopped(f"{why}; its process group may still be running")

    def exited(self) -> bool:
        """True once the leader has exited; it is NOT reaped here."""
        if not (self._exited or self.reaped):
            if self._kq is not None:
                self._exited = bool(self._kq.control(None, 1, 0))
            else:
                flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
                self._exited = os.waitid(os.P_PID, self.pid, flags) is not None
        return self._exited or self.reaped

    def _require_owned(self) -> None:
        if self.reaped:
            raise RuntimeError(
                f"pid {self.pid} was reaped; its process group is no longer ours to signal"
            )

    def signal_leader(self, sig: int) -> None:
        self._require_owned()
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(self.pid, sig)

    def kill_group(self) -> str | None:
        """SIGKILL the whole group, the leader included (a session leader cannot
        leave its group). None when the group is gone or was killed; otherwise
        why it could not be (the caller must treat that as a failure).

        ESRCH: nothing is left. EPERM: macOS's answer for a group that holds
        only the zombie leader, so it is accepted only once the leader has
        exited; EPERM while the leader is still running, or any other error,
        is a failure."""
        self._require_owned()
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            return None
        except PermissionError:
            if self.exited():
                return None
            return (
                f"cannot SIGKILL process group {self.pid}: permission denied, leader still running"
            )
        except OSError as exc:
            return f"cannot SIGKILL process group {self.pid}: {exc}"
        return None

    def reap(self, drain: float = GROUP_DRAIN) -> None:
        """Reap the leader (the last use of its pid), then give the pipe readers
        DRAIN seconds; a holder that left the group cannot stall this. Then
        stop the readers, join them, and close the pipes (owner's path): a
        pipe whose reader is still alive is never closed under it."""
        end = time.monotonic() + drain
        if not self.reaped:
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(drain)
            self.reaped = self.proc.returncode is not None
            if self._kq is not None:
                self._kq.close()
                self._kq = None
        for t in self._readers:
            t.join(max(0.0, end - time.monotonic()))
        self._stop_reading.set()
        stop_end = time.monotonic() + READER_STOP
        for t in self._readers:
            t.join(max(0.0, stop_end - time.monotonic()))
        if not any(t.is_alive() for t in self._readers):
            for pipe in (self.proc.stdout, self.proc.stderr):
                if pipe is not None:
                    with contextlib.suppress(OSError):
                        pipe.close()

    def output(self) -> tuple[str, str]:
        dec = lambda chunks: b"".join(chunks).decode(errors="replace")  # noqa: E731
        return dec(list(self._out)), dec(list(self._err))


def run_owned(
    cmd,
    timeout: float,
    *,
    env=None,
    cwd=None,
    grace: float = 0.0,
    stop_on_signal: bool = True,
    interrupt_signal: int | None = signal.SIGINT,
    timeout_signal: int | None = signal.SIGTERM,
    input: bytes | None = None,
) -> tuple[subprocess.CompletedProcess, bool]:
    """Run CMD in its own process group; return (completed, timed_out).
    INPUT (bytes) is written to its stdin by a writer thread, which then
    closes it; without it stdin is ``/dev/null``. A secret goes this way,
    never on argv. The write runs under the same TIMEOUT and kill as the rest
    (a child that never reads its stdin times out; it never blocks this
    process with the signals held), and the writer is stopped on the reap.

    One lifecycle for every way the run can end. SIGINT and SIGTERM to this
    process are held throughout. The leader's exit is awaited without reaping
    it. On TIMEOUT the leader gets TIMEOUT_SIGNAL; on a held signal (when
    STOP_ON_SIGNAL) INTERRUPT_SIGNAL; either way it then has GRACE seconds to
    exit, and further signals change nothing. Then, on every path, the whole
    group is SIGKILLed while the leader is still unreaped, the leader is reaped
    and the pipes are drained. Last, the first held signal is handed to the
    handler that was in place (``redeliver``), on EVERY path: also when the
    group cannot be started or the wait raises (then after the kill and reap,
    and the handler's own exception, e.g. KeyboardInterrupt, takes over from
    the one in flight). A held signal is never dropped.

    Raises GroupNotStopped when the group could not be shown stopped: the
    SIGKILL failed (anything but ESRCH, or EPERM once the leader has exited,
    macOS's answer for a zombie-only group) or the leader was not reaped
    after it; also when OwnedGroup's constructor failed after the child
    started and its group could not be shown stopped (``stop_verdict``, one
    rule). No result is returned as success then. That verdict is decided
    BEFORE a held signal is redelivered, so the handler's exception cannot
    hide it: when the handler raises, its exception propagates carrying the
    GroupNotStopped (``group_not_stopped(exc)``, also its ``__cause__``);
    otherwise GroupNotStopped is raised, also over an exception in flight."""
    held: tuple[list[int], dict] = ([], {})
    group = None
    kill_error = None
    not_stopped = None
    try:
        with held_signals() as held:
            got, _old = held
            try:
                group = OwnedGroup(cmd, env=env, cwd=cwd, input=input)
            except GroupNotStopped as exc:
                # The child started and the constructor then failed: its
                # group could not be shown stopped, the same verdict as below.
                not_stopped = exc
                raise
            timed_out = False
            try:
                deadline = time.monotonic() + timeout
                stop_at = None
                while not group.exited():
                    now = time.monotonic()
                    if stop_at is None:
                        sig = None
                        if got and stop_on_signal:
                            sig = interrupt_signal
                        elif now >= deadline:
                            timed_out, sig = True, timeout_signal
                        else:
                            time.sleep(GROUP_POLL)
                            continue
                        if sig is not None:
                            group.signal_leader(sig)
                        stop_at = now + grace
                    if now >= stop_at:
                        break  # grace over: the kill below takes the leader too
                    time.sleep(GROUP_POLL)
            finally:
                kill_error = group.kill_group()
                group.reap()
    finally:
        if group is not None:
            not_stopped = group.stop_verdict(kill_error)
        got, old = held
        if got:
            try:
                redeliver(got[0], old.get(got[0]))
            except BaseException as sig_exc:
                if not_stopped is not None:
                    sig_exc.group_not_stopped = not_stopped
                    raise sig_exc from not_stopped
                raise
        if not_stopped is not None:
            raise not_stopped
    out, err = group.output()
    return subprocess.CompletedProcess(cmd, group.proc.returncode, out, err), timed_out
