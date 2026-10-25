"""Single-server flock on ``<socket>.lock``.

``connect()`` cannot tell a stale socket from a live one with a full listen
backlog (macOS reports ECONNREFUSED for both), so liveness is decided by a lock
the kernel drops when the owner dies. Clients never touch it.
"""

from __future__ import annotations

import errno
import os
import shutil
import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from stt_server import __main__ as cli
from stt_server.backend import EchoBackend
from stt_server.server import (
    ServerConfig,
    TranscriptionServer,
    acquire_socket_lock,
    socket_lock_held,
)


@pytest.fixture
def short_home(monkeypatch):
    """Private short $HOME: the socket-dir ancestor walk is rooted at it."""
    home = Path(tempfile.mkdtemp(prefix="stt.", dir="/tmp"))
    monkeypatch.setenv("HOME", str(home))
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_second_acquire_fails_until_first_released(tmp_path):
    sock = tmp_path / "a.sock"
    fd = acquire_socket_lock(sock)
    with pytest.raises(OSError) as exc:
        acquire_socket_lock(sock)
    assert exc.value.errno == errno.EADDRINUSE
    os.close(fd)  # what process death does
    os.close(acquire_socket_lock(sock))


def test_lock_file_is_private_and_left_in_place(tmp_path):
    sock = tmp_path / "a.sock"
    os.close(acquire_socket_lock(sock))
    lock = Path(f"{sock}.lock")
    assert lock.is_file() and (lock.stat().st_mode & 0o777) == 0o600


def test_lock_refuses_symlink(tmp_path):
    sock = tmp_path / "a.sock"
    target = tmp_path / "elsewhere"
    target.write_text("x")
    Path(f"{sock}.lock").symlink_to(target)
    with pytest.raises(OSError):
        acquire_socket_lock(sock)


def test_probe_never_creates_the_lock_file(tmp_path):
    sock = tmp_path / "a.sock"
    assert socket_lock_held(sock) is False
    assert not Path(f"{sock}.lock").exists()


def test_probe_reports_held_then_free(tmp_path):
    sock = tmp_path / "a.sock"
    fd = acquire_socket_lock(sock)
    assert socket_lock_held(sock) is True
    os.close(fd)
    assert socket_lock_held(sock) is False


def test_preflight_held_lock_beats_econnrefused(tmp_path, monkeypatch):
    """A live server with a full backlog looks stale to connect(); the lock wins."""
    sock = tmp_path / "live.sock"
    sock.write_bytes(b"socket inode sentinel")
    inode = sock.stat().st_ino
    real_is_socket = Path.is_socket
    monkeypatch.setattr(Path, "is_socket", lambda self: self == sock or real_is_socket(self))

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def settimeout(self, _timeout):
            pass

        def connect(self, _path):
            raise ConnectionRefusedError(errno.ECONNREFUSED, "backlog full")

    monkeypatch.setattr(socket, "socket", lambda *_a, **_k: Probe())
    args = SimpleNamespace(socket_path=str(sock), host=None, port=None)

    cli._preflight_listener(args)  # no lock holder: ECONNREFUSED is stale, as before
    fd = acquire_socket_lock(sock)
    try:
        with pytest.raises(OSError) as exc:
            cli._preflight_listener(args)
    finally:
        os.close(fd)
    assert exc.value.errno == errno.EADDRINUSE
    assert sock.stat().st_ino == inode


async def test_second_server_fails_before_backend_start_and_keeps_socket(short_home):
    sock = short_home / "s" / "stt.sock"
    first = TranscriptionServer(EchoBackend(), ServerConfig(socket_path=str(sock)))
    await first.start()
    try:
        inode = sock.stat().st_ino
        second_backend = EchoBackend()
        started = []

        async def _start():
            started.append(True)

        second_backend.start = _start
        second = TranscriptionServer(second_backend, ServerConfig(socket_path=str(sock)))
        with pytest.raises(OSError) as exc:
            await second.start()
        assert exc.value.errno == errno.EADDRINUSE
        assert not started  # failed before any model load
        assert sock.stat().st_ino == inode
    finally:
        await first.shutdown()


async def test_shutdown_releases_lock_for_restart(short_home):
    sock = short_home / "s" / "stt.sock"
    for _ in range(2):
        srv = TranscriptionServer(EchoBackend(), ServerConfig(socket_path=str(sock)))
        await srv.start()
        assert socket_lock_held(sock) is True
        await srv.shutdown()
        assert socket_lock_held(sock) is False


async def test_failed_start_releases_lock(short_home):
    sock = short_home / "s" / "stt.sock"
    backend = EchoBackend()

    async def _boom():
        raise RuntimeError("load failed")

    backend.start = _boom
    srv = TranscriptionServer(backend, ServerConfig(socket_path=str(sock)))
    with pytest.raises(RuntimeError):
        await srv.start()
    assert socket_lock_held(sock) is False


async def test_abort_sync_keeps_lock_until_process_exit(short_home):
    """``abort_sync`` precedes ``os._exit``; the kernel drops the lock with the
    process. Releasing it early would let a new server start loading while this
    one (and its model thread) still exists."""
    sock = short_home / "s" / "stt.sock"
    srv = TranscriptionServer(EchoBackend(), ServerConfig(socket_path=str(sock)))
    await srv.start()
    srv.abort_sync()
    assert socket_lock_held(sock) is True
    await srv.shutdown()
    assert socket_lock_held(sock) is False


def _hold(sock, kind):
    import fcntl

    fd = os.open(f"{sock}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, kind)
    return fd


@pytest.mark.parametrize("holder", [None, "shared", "exclusive"])
def test_probe_state_space(tmp_path, holder):
    """Probe vs. {no holder, another probe (shared), a server (exclusive)}: only
    a server reads as held; a concurrent probe does not."""
    import fcntl

    sock = tmp_path / "a.sock"
    os.close(acquire_socket_lock(sock))  # lock file exists, unheld
    fd = None
    if holder == "shared":
        fd = _hold(sock, fcntl.LOCK_SH)
    elif holder == "exclusive":
        fd = _hold(sock, fcntl.LOCK_EX)
    try:
        assert socket_lock_held(sock) is (holder == "exclusive")
    finally:
        if fd is not None:
            os.close(fd)


def test_acquire_survives_a_concurrent_probe(tmp_path):
    """A preflight probe briefly holds the lock (shared). A real starter racing
    it must not fail with EADDRINUSE when no server is running."""
    import fcntl
    import threading

    sock = tmp_path / "a.sock"
    os.close(acquire_socket_lock(sock))
    fd = _hold(sock, fcntl.LOCK_SH)  # the probe, mid-check
    threading.Timer(0.05, os.close, args=(fd,)).start()
    os.close(acquire_socket_lock(sock))


@pytest.mark.parametrize("mode", [0o666, 0o644, 0o620, 0o604])
def test_owned_lock_file_with_unsafe_mode_is_tightened(tmp_path, mode):
    sock = tmp_path / "a.sock"
    lock = Path(f"{sock}.lock")
    lock.write_text("")
    os.chmod(lock, mode)
    os.close(acquire_socket_lock(sock))
    assert (lock.stat().st_mode & 0o777) == 0o600


class _AbortCounting(EchoBackend):
    def __init__(self, start_exc=None):
        self.aborts = 0
        self.closes = 0
        self._start_exc = start_exc

    async def start(self):
        if self._start_exc is not None:
            raise self._start_exc

    def abort(self):
        self.aborts += 1

    async def close(self):
        self.closes += 1


async def test_failed_start_runs_backend_abort_in_every_failure_cell(short_home):
    """``close()`` never runs for a server that never started, so every failed
    start must run the backend's ``abort()`` (parakeet/nemotron temp dir)."""
    sock = short_home / "s" / "stt.sock"

    # Cell 1: the lock is held by a live server (fast EADDRINUSE path).
    first = TranscriptionServer(EchoBackend(), ServerConfig(socket_path=str(sock)))
    await first.start()
    try:
        b = _AbortCounting()
        with pytest.raises(OSError):
            await TranscriptionServer(b, ServerConfig(socket_path=str(sock))).start()
        assert b.aborts == 1
    finally:
        await first.shutdown()

    # Cell 2: backend.start() fails (e.g. missing extra).
    b = _AbortCounting(start_exc=ModuleNotFoundError("no extra"))
    srv = TranscriptionServer(b, ServerConfig(socket_path=str(sock)))
    with pytest.raises(ModuleNotFoundError):
        await srv.start()
    assert b.aborts == 1 and socket_lock_held(sock) is False

    # Cell 3: the socket directory fails enforcement (outside the trusted root).
    b = _AbortCounting()
    with pytest.raises(ValueError):
        await TranscriptionServer(b, ServerConfig(socket_path="/tmp/x/stt.sock")).start()
    assert b.aborts == 1

    # Cell 4: the TCP bind fails (port already taken).
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    try:
        b = _AbortCounting()
        port = taken.getsockname()[1]
        with pytest.raises(OSError):
            await TranscriptionServer(b, ServerConfig(host="127.0.0.1", port=port)).start()
        assert b.aborts == 1
    finally:
        taken.close()

    # Control: a successful start does not abort; shutdown closes.
    b = _AbortCounting()
    srv = TranscriptionServer(b, ServerConfig(socket_path=str(sock)))
    await srv.start()
    assert b.aborts == 0
    await srv.shutdown()
    assert b.closes == 1


async def test_serve_bad_config_aborts_backend():
    from stt_server.server import serve

    b = _AbortCounting()
    with pytest.raises(ValueError):
        await serve(b, host="10.0.0.1", port=1, install_signal_handlers=False)
    assert b.aborts == 1


async def test_failed_backend_close_still_releases_lock(short_home):
    sock = short_home / "s" / "stt.sock"

    class BadClose(EchoBackend):
        async def close(self):
            raise RuntimeError("metal teardown failed")

    srv = TranscriptionServer(BadClose(), ServerConfig(socket_path=str(sock)))
    await srv.start()
    with pytest.raises(RuntimeError):
        await srv.shutdown()
    assert socket_lock_held(sock) is False
    assert srv.state == "stopped"
    await srv.shutdown()  # a second call returns instead of spinning


async def test_cancelled_shutdown_keeps_the_lock_until_close_finishes(short_home):
    """Cancelling ``shutdown()`` during ``backend.close()`` never abandons the
    close: the lock is released once it has really finished."""
    import asyncio

    sock = short_home / "s" / "stt.sock"
    gate = asyncio.Event()
    closed = []

    class SlowClose(EchoBackend):
        async def close(self):
            await gate.wait()
            closed.append(True)

    srv = TranscriptionServer(SlowClose(), ServerConfig(socket_path=str(sock)))
    await srv.start()
    task = asyncio.ensure_future(srv.shutdown())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.wait({task})
    assert socket_lock_held(sock) is True and srv.state == "stopping"
    gate.set()
    await asyncio.wait_for(srv.wait_stopped(), 5)
    assert closed == [True]
    assert socket_lock_held(sock) is False and srv.state == "stopped"


async def test_shutdown_is_bounded_when_a_task_swallows_cancellation(monkeypatch):
    """``wait_for(gather(...))`` waited for cancellation to finish; a session
    task that swallows ``CancelledError`` must not hold shutdown forever."""
    import asyncio

    from stt_server import server as S

    monkeypatch.setattr(S, "_CANCEL_GRACE_SECONDS", 0.2)
    cfg = ServerConfig(host="127.0.0.1", port=0, drain_timeout_seconds=0.1)
    srv = TranscriptionServer(EchoBackend(), cfg)
    await srv.start()
    released = False

    async def stubborn():
        while True:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                if released:
                    raise

    task = asyncio.ensure_future(stubborn())
    srv._track(task, "session")
    try:
        await asyncio.wait_for(srv.shutdown(), timeout=3)
    finally:
        released = True
        task.cancel()
        await asyncio.wait({task}, timeout=1)


# --- start()/shutdown() lifecycle contract ------------------------------------
#
# Invariant: once ``shutdown()`` is entered, no ``start()`` (in flight or later)
# binds a listener or marks the server started, and the single-server lock is
# held until nothing of this server can still touch the socket: an in-flight
# start releases it when it unwinds, stuck session tasks when the last finishes.


async def test_shutdown_during_backend_start_never_binds(short_home):
    import asyncio

    sock = short_home / "s" / "stt.sock"
    entered, gate = asyncio.Event(), asyncio.Event()

    class SlowStart(_AbortCounting):
        async def start(self):
            entered.set()
            await gate.wait()

    backend = SlowStart()
    srv = TranscriptionServer(backend, ServerConfig(socket_path=str(sock)))
    task = asyncio.ensure_future(srv.start())
    await entered.wait()
    await srv.shutdown()
    # The start still owns the lock: releasing it here would let a new server
    # take the path while this start could still bind it.
    assert socket_lock_held(sock) is True
    gate.set()
    with pytest.raises(RuntimeError):
        await task
    assert srv.state == "stopped" and srv.sockets_bound == []
    assert not sock.exists()
    assert socket_lock_held(sock) is False
    assert backend.aborts == 1
    with pytest.raises(RuntimeError):
        await srv.start()  # a shut-down server never starts again


async def test_shutdown_during_bind_closes_the_bound_listener(short_home, monkeypatch):
    import asyncio

    from stt_server import server as S

    sock = short_home / "s" / "stt.sock"
    srv = TranscriptionServer(EchoBackend(), ServerConfig(socket_path=str(sock)))
    real_bind = S.ws_unix_serve

    async def bind_then_shutdown(*a, **k):
        listener = await real_bind(*a, **k)
        await srv.shutdown()  # lands between the bind and RUNNING
        return listener

    monkeypatch.setattr(S, "ws_unix_serve", bind_then_shutdown)
    with pytest.raises(RuntimeError):
        await srv.start()
    assert srv.state == "stopped" and srv.sockets_bound == []
    assert not sock.exists()
    assert socket_lock_held(sock) is False
    await asyncio.sleep(0)


async def test_cancel_during_bind_does_not_leak_the_listener(short_home, monkeypatch):
    import asyncio

    from stt_server import server as S

    sock = short_home / "s" / "stt.sock"
    srv = TranscriptionServer(EchoBackend(), ServerConfig(socket_path=str(sock)))
    real_bind = S.ws_unix_serve
    bound = asyncio.Event()
    listeners = []

    async def slow_tail_bind(*a, **k):
        listener = await real_bind(*a, **k)
        listeners.append(listener)
        bound.set()
        await asyncio.sleep(0.05)  # the await after the synchronous bind
        return listener

    monkeypatch.setattr(S, "ws_unix_serve", slow_tail_bind)
    task = asyncio.ensure_future(srv.start())
    await bound.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (listener,) = listeners
    assert not listener.is_serving()
    assert not sock.exists()
    assert socket_lock_held(sock) is False


async def test_serve_soft_stop_with_a_stubborn_start_never_binds(short_home, monkeypatch):
    """``serve(hard_exit_on_stop=False)``: a start that swallows the cancel
    outlives serve's bounded wait; it must not bind once serve has returned."""
    import asyncio
    import functools
    import signal

    from stt_server import server as S

    monkeypatch.setattr(
        S, "ServerConfig", functools.partial(S.ServerConfig, drain_timeout_seconds=0.1)
    )
    sock = short_home / "s" / "stt.sock"
    entered, gate = asyncio.Event(), asyncio.Event()

    class Stubborn(EchoBackend):
        async def start(self):
            entered.set()
            while not gate.is_set():
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    pass  # swallows the stop's cancel

    serving = asyncio.ensure_future(S.serve(Stubborn(), socket_path=str(sock)))
    try:
        await entered.wait()
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(serving, timeout=5)
        assert socket_lock_held(sock) is True  # the stuck start still owns it
        gate.set()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not socket_lock_held(sock):
                break
        assert socket_lock_held(sock) is False
        assert not sock.exists()
    finally:
        gate.set()
        asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)


async def test_stuck_session_task_keeps_lock_until_it_finishes(short_home, monkeypatch):
    """Embedding mode: ``shutdown()`` returns on its bound, but a task that
    swallowed cancellation keeps the lock held until it actually finishes."""
    import asyncio

    from stt_server import server as S

    monkeypatch.setattr(S, "_CANCEL_GRACE_SECONDS", 0.1)
    sock = short_home / "s" / "stt.sock"
    cfg = ServerConfig(socket_path=str(sock), drain_timeout_seconds=0.1)
    srv = TranscriptionServer(EchoBackend(), cfg)
    await srv.start()
    release = asyncio.Event()

    async def stubborn():
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    task = asyncio.ensure_future(stubborn())
    srv._track(task, "session")
    try:
        await asyncio.wait_for(srv.shutdown(), timeout=3)
        assert srv._stuck_work_count == 1
        assert socket_lock_held(sock) is True
    finally:
        release.set()
        await asyncio.wait({task}, timeout=1)
    await asyncio.sleep(0)
    assert srv._stuck_work_count == 0
    assert socket_lock_held(sock) is False


def test_cli_exits_when_a_session_task_outlives_shutdown():
    """``hard_exit_on_stop=True``: ``asyncio.run`` teardown re-gathers leftover
    tasks with no timeout, so a stuck session task would wedge the process
    after SIGTERM. serve exits the process instead."""
    import subprocess
    import sys
    import textwrap
    import time

    script = textwrap.dedent(
        """
        import asyncio, functools, os
        from stt_server import server as S
        from stt_server.backend import EchoBackend

        S._CANCEL_GRACE_SECONDS = 0.1
        S.ServerConfig = functools.partial(S.ServerConfig, drain_timeout_seconds=0.1)

        async def stubborn():
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    pass

        async def ready(server):
            server._track(asyncio.ensure_future(stubborn()), "session")

        def publish(server):
            os.write(1, b"ready\\n")

        asyncio.run(S.serve(EchoBackend(), host="127.0.0.1", port=0, ready=ready,
                            publish_ready=publish, hard_exit_on_stop=True))
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        import select

        assert proc.stdout is not None
        readable, _, _ = select.select([proc.stdout], [], [], 10)
        assert readable and proc.stdout.readline() == b"ready\n"
        start = time.monotonic()
        proc.terminate()
        assert proc.wait(timeout=10) == 0
        assert time.monotonic() - start < 10
        assert b"outlived shutdown" in proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_lock_mode_does_not_depend_on_umask(tmp_path):
    """A ``0o777`` umask masks the create mode to 000; a later start must still
    open the lock file, so the mode is set on the fd, not by ``open``."""
    sock = tmp_path / "a.sock"
    prior = os.umask(0o777)
    try:
        os.close(acquire_socket_lock(sock))
    finally:
        os.umask(prior)
    assert (Path(f"{sock}.lock").stat().st_mode & 0o777) == 0o600
    os.close(acquire_socket_lock(sock))  # reopenable by the next start


def test_unopenable_existing_lock_file_reports_a_clear_eacces(tmp_path):
    sock = tmp_path / "a.sock"
    lock = Path(f"{sock}.lock")
    lock.write_text("")
    os.chmod(lock, 0)
    try:
        if os.access(lock, os.R_OK):
            pytest.skip("running with privileges that bypass file modes")
        with pytest.raises(OSError) as exc:
            acquire_socket_lock(sock)
        assert exc.value.errno == errno.EACCES
        assert "chmod 600" in str(exc.value)
    finally:
        os.chmod(lock, 0o600)


def _returns_within(fn, timeout):
    import threading

    box = {}
    t = threading.Thread(target=lambda: box.setdefault("v", fn()), daemon=True)
    t.start()
    t.join(timeout)
    return (not t.is_alive()), box.get("v")


def test_probe_does_not_hang_on_a_planted_fifo(tmp_path):
    sock = tmp_path / "a.sock"
    fifo = Path(f"{sock}.lock")
    os.mkfifo(fifo)
    try:
        finished, held = _returns_within(lambda: socket_lock_held(sock), 2)
        assert finished, "socket_lock_held blocked opening a FIFO"
        assert held is False
        finished, _ = _returns_within(lambda: pytest.raises(OSError, acquire_socket_lock, sock), 2)
        assert finished, "acquire_socket_lock blocked opening a FIFO"
    finally:
        # Unblock a mutant's blocked reader, if any.
        try:
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass


def test_probe_treats_a_foreign_owned_lock_as_not_held(tmp_path, monkeypatch):
    sock = tmp_path / "a.sock"
    fd = acquire_socket_lock(sock)  # a held, regular lock file
    try:
        real = os.geteuid()
        monkeypatch.setattr(os, "geteuid", lambda: real + 1)
        assert socket_lock_held(sock) is False
    finally:
        os.close(fd)


async def test_hard_exit_right_after_bind_unlinks_the_socket(short_home, monkeypatch):
    """A stop that wins the loop turn right after the bind must still remove
    the socket file: the inode is recorded in the bind's own step, not only
    once ``start()`` resumes, so ``abort_sync`` can prove it is ours."""
    sock = short_home / "s" / "stt.sock"
    srv = TranscriptionServer(EchoBackend(), ServerConfig(socket_path=str(sock)))
    adopt = TranscriptionServer._adopt_listener
    seen = {}

    def _exit_before_adoption(self, server):
        # What serve(hard_exit_on_stop=True) does the moment the stop wins.
        assert self._bound_socket_id is not None
        seen["existed"] = sock.exists()
        self.abort_sync()
        seen["left"] = sock.exists()
        adopt(self, server)

    monkeypatch.setattr(TranscriptionServer, "_adopt_listener", _exit_before_adoption)
    try:
        await srv.start()
    finally:
        await srv.shutdown()
    assert seen == {"existed": True, "left": False}
