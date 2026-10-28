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

It also holds what ``smoke.py`` and ``smoke_launchd.py`` must agree on: the
outcome names and the harness's skip line (``LAUNCHD_SKIP_MARK``), the spare
label, the installed-agent plist glob and directory, the home the installed real
agents are read from, the launchd preconditions, the token-file fixture
(``write_policy_token_file``), the websocket token-gate assertion
(``assert_token_gate``), and the owned-process-group lifecycle (``run_owned``)
both use for children whose whole group they may kill.

Two homes. ``env_home()`` is ``$HOME``: where the installer puts the spare's
plist, logs and socket. ``agents_home()`` is where both scripts look for the
installed real agents (``smoke.py --agents`` and the harness's port guard and
before/after snapshot): the account's home from the password database, so an
overridden ``HOME`` can neither empty the snapshot nor hide an agent from the
port guard. The only way to move ``agents_home()`` is the test-only variable
``PIPECAT_STT_SMOKE_TEST_AGENTS_HOME`` (``TEST_AGENTS_HOME_ENV``), which the
stubbed test suites set to a temp directory so they never read the developer's
real ``~/Library/LaunchAgents``. It is honoured only while the ``launchctl`` on
PATH is not the system ``/bin/launchctl`` (the suites' stub), so a stale export
can never hide a real agent from a run that reaches real launchd: there it is
ignored, ``smoke.py --agents`` says so on stderr and ``smoke_launchd.py``
refuses to run. ``smoke.py`` drops it from the harness's environment, the
installer's environment is scrubbed of it, and both scripts print the same
``agents_home_notice()`` line whenever it is set.

The launchd checks are SKIPPED when ``CI`` is present in the environment with
any value, the empty string included (Decision 8: they run only when ``CI`` is
unset).
"""

from __future__ import annotations

import contextlib
import os
import pwd
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

# Step outcomes, shared by smoke.py's table and smoke_launchd.py's.
PASS = "PASS"
FAIL = "FAIL"
SKIPPED = "SKIPPED"
# The first stdout line smoke_launchd.py prints (exit 0) when a precondition is
# absent; smoke.py maps it to SKIPPED, never PASS.
LAUNCHD_SKIP_MARK = "SKIPPED: launchd checks not run: "

SPARE_LABEL = "pipecat.stt-server.smoke"
AGENT_GLOB = "pipecat.stt-server*.plist"
TEST_AGENTS_HOME_ENV = "PIPECAT_STT_SMOKE_TEST_AGENTS_HOME"
SYSTEM_LAUNCHCTL = "/bin/launchctl"
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


def launchctl_stubbed(env=None) -> bool:
    """True when the ``launchctl`` on PATH is not the system ``/bin/launchctl``
    (a test's stub). No ``launchctl`` at all is not a stub."""
    env = os.environ if env is None else env
    found = shutil.which("launchctl", path=env.get("PATH") or os.defpath)
    return found is not None and os.path.realpath(found) != os.path.realpath(SYSTEM_LAUNCHCTL)


def agents_home_override(env=None) -> Path | None:
    """The test-only agents home, or None. Honoured only while ``launchctl`` is
    stubbed, when no real launchd state can be reached; otherwise it is ignored
    (``ignored_agents_home_override``)."""
    env = os.environ if env is None else env
    val = env.get(TEST_AGENTS_HOME_ENV)
    return Path(val) if val and launchctl_stubbed(env) else None


def ignored_agents_home_override(env=None) -> str | None:
    """The override's value when it is set but not honoured (a stale export
    with the real ``launchctl``), else None."""
    env = os.environ if env is None else env
    val = env.get(TEST_AGENTS_HOME_ENV)
    return val if val and not launchctl_stubbed(env) else None


def agents_home_notice(env=None) -> str | None:
    """The stderr line both scripts print about the override, or None."""
    override = agents_home_override(env)
    if override is not None:
        return (
            f"warning: {TEST_AGENTS_HOME_ENV}={override} is a test-only override "
            "(launchctl is stubbed); installed agent plists are read from there"
        )
    ignored = ignored_agents_home_override(env)
    if ignored is not None:
        return (
            f"warning: {TEST_AGENTS_HOME_ENV}={ignored} is set but ignored: it is "
            "test-only and launchctl is not stubbed; unset it"
        )
    return None


def agents_home(env=None) -> Path:
    """Where the installed real agents are read from: the account home, or the
    test-only override while launchctl is stubbed. ``$HOME`` never moves it."""
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


@contextlib.contextmanager
def held_signals():
    """Record SIGINT and SIGTERM instead of acting on them for the block (main
    thread only; elsewhere nothing is held). Yields ``(got, old)``: the signals
    that arrived and the handlers that were in place, restored on exit."""
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


def redeliver(sig: int, handler) -> None:
    """Hand a held SIG to HANDLER (what was in place before it was held): a
    function is called, SIG_IGN drops it, anything else is KeyboardInterrupt."""
    if callable(handler):
        handler(sig, None)
    elif handler != signal.SIG_IGN:
        raise KeyboardInterrupt(f"signal {sig}")


def _drain(fd: int, sink: list[bytes]) -> None:
    with contextlib.suppress(OSError):
        while chunk := os.read(fd, 65536):
            sink.append(chunk)


class OwnedGroup:
    """A child started in its own session (so it leads its own process group),
    signalled only while this process owns that group (see the invariant
    above). Its stdout and stderr are drained by two daemon threads."""

    def __init__(self, cmd, *, env=None, cwd=None):
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
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
            for pipe, sink in ((self.proc.stdout, self._out), (self.proc.stderr, self._err)):
                t = threading.Thread(target=_drain, args=(pipe.fileno(), sink), daemon=True)
                t.start()
                self._readers.append(t)
        except BaseException:
            self.kill_group()
            self.reap()
            raise

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

    def kill_group(self) -> None:
        """SIGKILL the whole group, the leader included (a session leader cannot
        leave its group). macOS answers EPERM for a group that holds only the
        zombie leader: nothing is left to kill."""
        self._require_owned()
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.pid, signal.SIGKILL)

    def reap(self, drain: float = GROUP_DRAIN) -> None:
        """Reap the leader (the last use of its pid), then give the pipe readers
        DRAIN seconds; a holder that left the group cannot stall this."""
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
) -> tuple[subprocess.CompletedProcess, bool]:
    """Run CMD in its own process group; return (completed, timed_out).

    One lifecycle for every way the run can end. SIGINT and SIGTERM to this
    process are held throughout. The leader's exit is awaited without reaping
    it. On TIMEOUT the leader gets TIMEOUT_SIGNAL; on a held signal (when
    STOP_ON_SIGNAL) INTERRUPT_SIGNAL; either way it then has GRACE seconds to
    exit, and further signals change nothing. Then, on every path, the whole
    group is SIGKILLed while the leader is still unreaped, the leader is reaped
    and the pipes are drained. Last, the first held signal is handed to the
    handler that was in place (``redeliver``)."""
    with held_signals() as (got, old):
        group = OwnedGroup(cmd, env=env, cwd=cwd)
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
            group.kill_group()
            group.reap()
    if got:
        redeliver(got[0], old.get(got[0]))
    out, err = group.output()
    return subprocess.CompletedProcess(cmd, group.proc.returncode, out, err), timed_out
