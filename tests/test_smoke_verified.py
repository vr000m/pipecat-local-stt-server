"""``_smoke_lib.connect_verified``: a token goes only over a connection whose
server end was verified to be the expected job (review round 6, Seam 1).

The state-space tests use real loopback sockets the test opens itself and stub
only the two answers that identify the server end (``lsof`` for TCP,
``LOCAL_PEERPID`` for AF_UNIX) and the launchd re-read. The macOS tests at the
end use the REAL ``lsof`` and ``LOCAL_PEERPID`` against a listener in a child
process (and a real echo ``stt_server``), never launchd.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"


@pytest.fixture(scope="module")
def lib():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("smoke_lib_verified", SCRIPTS / "_smoke_lib.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["smoke_lib_verified"] = mod
    spec.loader.exec_module(mod)
    return mod


class Listener:
    """A listener (TCP on 127.0.0.1 or AF_UNIX) that accepts every connection
    and records every byte it receives."""

    def __init__(self, transport: str):
        self.received: list[bytes] = []
        self.accepted = 0
        if transport == "tcp":
            self.srv = socket.socket()
            self.srv.bind(("127.0.0.1", 0))
            self.endpoint = ("tcp", "127.0.0.1", str(self.srv.getsockname()[1]))
        else:
            self.dir = tempfile.mkdtemp(dir="/tmp")
            path = os.path.join(self.dir, "s")
            self.srv = socket.socket(socket.AF_UNIX)
            self.srv.bind(path)
            self.endpoint = ("unix", path)
        self.srv.listen(8)
        self.srv.settimeout(0.2)
        self.stop = False
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        while not self.stop:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                continue
            self.accepted += 1
            threading.Thread(target=self._drain, args=(conn,), daemon=True).start()

    def _drain(self, conn):
        with conn:
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                self.received.append(chunk)

    def close(self):
        self.stop = True
        self.t.join(2)
        self.srv.close()


def _lsof_for(owners):
    """A ``run`` answering lsof as if OWNERS (list of pids; None: lsof fails)
    held the server end of whatever connection is asked about."""

    def run(argv, **_k):
        assert Path(argv[0]).name == "lsof", argv
        local = next(a for a in argv if a.startswith("-iTCP@"))[len("-iTCP@") :]
        if owners is None:
            return subprocess.CompletedProcess(argv, 1, "", "lsof: boom")
        # Our own end too: never counted (its name is local->remote).
        body = f"p{os.getpid()}\nn{local}->127.0.0.1:{RUN_PORT[0]}\n"
        body += "".join(f"p{p}\nn127.0.0.1:{RUN_PORT[0]}->{local}\n" for p in owners)
        return subprocess.CompletedProcess(argv, 0, body, "")

    return run


RUN_PORT = [""]  # the listener's port, set per test

_OWNERS = {
    "job": ["4242"],
    "foreign": ["777"],
    "both": ["4242", "777"],
    "invisible": [],
    "unreadable": None,
}
_AFTER = {
    "same": ("4242", "7"),
    "new-pid": ("5151", "8"),
    "same-pid-restarted": ("4242", "8"),
    "gone": None,
}


# transport x who holds the server end x the launchd re-read -> a socket is
# returned only for (job, same); otherwise EndpointRefused. In EVERY cell the
# listener receives zero bytes: connect_verified itself never sends.
@pytest.mark.parametrize("after", list(_AFTER))
@pytest.mark.parametrize("owners", list(_OWNERS))
@pytest.mark.parametrize("transport", ["tcp", "unix"])
def test_connect_verified_state_space(lib, monkeypatch, transport, owners, after):
    if transport == "unix" and owners in ("both", "invisible"):
        pytest.skip("LOCAL_PEERPID names one pid or none")
    srv = Listener(transport)
    try:
        if transport == "tcp":
            RUN_PORT[0] = srv.endpoint[2]
            run = _lsof_for(_OWNERS[owners])
        else:
            run = None
            peer = _OWNERS[owners]
            monkeypatch.setattr(lib, "unix_peer_pid", lambda s: peer[0] if peer else None)
        reads = []

        def identity():
            reads.append(1)
            return _AFTER[after]

        ok = owners == "job" and after == "same"
        kw = {"run": run} if run else {}
        if ok:
            sock = lib.connect_verified(srv.endpoint, "4242", "7", identity, settle=0.2, **kw)
            sock.close()
        else:
            with pytest.raises(lib.EndpointRefused):
                lib.connect_verified(srv.endpoint, "4242", "7", identity, settle=0.2, **kw)
        # The re-read happens only once the server end is the job's.
        assert len(reads) == (1 if owners == "job" else 0)
        time.sleep(0.2)
        assert srv.received == []
    finally:
        srv.close()


# Without a pid and a readable runs counter to verify against, nothing is even
# connected.
@pytest.mark.parametrize(("pid", "runs"), [(None, "7"), ("4242", None), ("4242", "UNKNOWN")])
def test_connect_verified_needs_an_identity(lib, pid, runs):
    runs = lib.UNKNOWN if runs == "UNKNOWN" else runs  # the one sentinel, not a copy
    srv = Listener("tcp")
    try:
        with pytest.raises(lib.EndpointRefused):
            lib.connect_verified(srv.endpoint, pid, runs, lambda: (pid, runs))
        time.sleep(0.3)
        assert srv.accepted == 0
    finally:
        srv.close()


# A token (or a wrong one) is never written to an address, only to a socket
# the caller verified: token x sock -> sent or ValueError.
@pytest.mark.parametrize("token", [None, "s3cret"])
@pytest.mark.parametrize("given_sock", [False, True])
def test_ws_handshake_sends_a_token_only_over_a_given_socket(lib, token, given_sock):
    srv = Listener("tcp")
    try:
        host, port = srv.endpoint[1], int(srv.endpoint[2])
        kw = {"sock": socket.create_connection((host, port))} if given_sock else {}
        if token and not given_sock:
            with pytest.raises(ValueError):
                lib.ws_handshake_status(host=host, port=port, token=token, timeout=0.5, **kw)
            time.sleep(0.3)
            assert srv.accepted == 0 and srv.received == []
        else:
            with pytest.raises(OSError):  # the listener never answers
                lib.ws_handshake_status(host=host, port=port, token=token, timeout=0.5, **kw)
    finally:
        srv.close()


# The status-line read has ONE deadline and a byte cap: peer behaviour ->
# outcome and how long it may take. A trickle (a byte every 50 ms, never a
# CRLF, for 4 s) must not extend the 0.5 s deadline; a flood with no CRLF stops
# at STATUS_LINE_MAX; a prompt status line is read as before.
@pytest.mark.parametrize(
    ("peer", "want", "within"),
    [
        ("status", 101, 2.0),
        ("trickle", "timeout", 1.5),
        ("flood", "cap", 1.5),
        ("silent", "timeout", 1.5),
    ],
)
def test_ws_handshake_status_line_has_one_deadline(lib, peer, want, within):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    stop = threading.Event()

    def serve():
        conn, _ = srv.accept()
        with conn:
            conn.recv(4096)
            if peer == "status":
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n\r\n")
            elif peer == "flood":
                with contextlib.suppress(OSError):
                    conn.sendall(b"x" * (4 * lib.STATUS_LINE_MAX))
            end = time.monotonic() + 4.0
            while not stop.is_set() and time.monotonic() < end:
                if peer == "trickle":
                    with contextlib.suppress(OSError):
                        conn.sendall(b"x")
                time.sleep(0.05)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        sock = socket.create_connection(srv.getsockname(), timeout=2)
        start = time.monotonic()
        if want == 101:
            assert lib.ws_handshake_status(sock=sock, timeout=0.5) == 101
        else:
            with pytest.raises(OSError) as exc:
                lib.ws_handshake_status(sock=sock, timeout=0.5)
            if want == "timeout":
                assert isinstance(exc.value, TimeoutError)
            else:
                assert f"first {lib.STATUS_LINE_MAX} bytes" in str(exc.value)
        assert time.monotonic() - start < within
    finally:
        stop.set()
        t.join(5)
        srv.close()


# --------------------------------------------------------------------------
# the real macOS facts (lsof, LOCAL_PEERPID) against a child-process listener
# --------------------------------------------------------------------------

darwin = pytest.mark.skipif(sys.platform != "darwin", reason="LOCAL_PEERPID and lsof: macOS")

_CHILD = """
import os, socket, sys, time
kind, where = sys.argv[1], sys.argv[2]
if kind == "tcp":
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    print(s.getsockname()[1], flush=True)
else:
    s = socket.socket(socket.AF_UNIX); s.bind(where); print(where, flush=True)
s.listen(4)
conns = []
while True:
    c, _ = s.accept(); conns.append(c)
"""


@darwin
@pytest.mark.parametrize("expect", ["child", "other"])
@pytest.mark.parametrize("transport", ["tcp", "unix"])
def test_connect_verified_real_server_end(lib, transport, expect):
    d = tempfile.mkdtemp(dir="/tmp")
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, transport, os.path.join(d, "s")],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        first = child.stdout.readline().strip()
        endpoint = ("tcp", "127.0.0.1", first) if transport == "tcp" else ("unix", first)
        pid = str(child.pid) if expect == "child" else str(os.getpid())
        if expect == "child":
            sock = lib.connect_verified(endpoint, pid, "1", lambda: (pid, "1"))
            sock.close()
        else:
            with pytest.raises(lib.EndpointRefused, match=f"pid {child.pid}"):
                lib.connect_verified(endpoint, pid, "1", lambda: (pid, "1"))
    finally:
        child.kill()
        child.wait()


@darwin
@pytest.mark.skipif(not VENV_PYTHON.exists(), reason="needs the project venv")
@pytest.mark.parametrize("transport", ["tcp", "unix"])
def test_verified_client_handshakes_over_the_verified_socket(lib, transport):
    """smoke.py's decode client speaks over the verified socket: a real echo
    stt_server answers server.hello and session.created on it."""
    import asyncio

    sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location("smoke_runner_verified", SCRIPTS / "smoke.py")
    smoke = importlib.util.module_from_spec(spec)
    sys.modules["smoke_runner_verified"] = smoke
    spec.loader.exec_module(smoke)
    d = tempfile.mkdtemp(dir="/tmp")
    os.chmod(d, 0o700)
    if transport == "tcp":
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = str(s.getsockname()[1])
        where = ["--host", "127.0.0.1", "--port", port]
        endpoint, uri = ("tcp", "127.0.0.1", port), f"ws://127.0.0.1:{port}/"
    else:
        path = os.path.join(d, "stt.sock")
        where = ["--socket-path", path]
        endpoint, uri = ("unix", path), "ws://localhost/"
    server = subprocess.Popen(
        [sys.executable, "-m", "stt_server", "serve", "--backend", "echo", *where],
        cwd=str(REPO_ROOT),
        # The server trusts socket paths only under $HOME: a short private one.
        env={**os.environ, "HOME": d},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        me = (str(server.pid), "1")
        deadline = time.monotonic() + 30
        while True:
            try:
                sock = lib.connect_verified(endpoint, *me, lambda: me)
                break
            except lib.EndpointRefused:
                if time.monotonic() > deadline or server.poll() is not None:
                    raise
                time.sleep(0.3)

        async def go():
            async with smoke._verified_client(sock, uri, None) as c:
                return c._ws is not None

        assert asyncio.run(asyncio.wait_for(go(), 30))
    finally:
        server.terminate()
        server.wait(10)


@darwin
@pytest.mark.skipif(not VENV_PYTHON.exists(), reason="needs the project venv")
def test_token_check_passes_end_to_end_over_verified_connections(monkeypatch, tmp_path, capsys):
    """smoke.py's token-file child against a REAL echo server with a token file:
    401 / 401 / 101, every request over a connect_verified socket (real lsof)."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location("smoke_runner_token", SCRIPTS / "smoke.py")
    smoke = importlib.util.module_from_spec(spec)
    sys.modules["smoke_runner_token"] = smoke
    spec.loader.exec_module(smoke)
    calls = []
    real = smoke._smoke_lib.connect_verified_child
    monkeypatch.setattr(
        smoke._smoke_lib,
        "connect_verified_child",
        lambda *a, **k: calls.append(a) or real(*a, **k),
    )
    assert smoke._token_check() == 0, capsys.readouterr()
    assert "token gate OK" in capsys.readouterr().out
    assert len(calls) == 3
    assert not list(home.glob("stt-smoke-token.*"))


class _Child:
    """A stand-in Popen: pid 4242; POLLS lists what successive poll() calls
    answer (None: still running and unreaped)."""

    def __init__(self, polls):
        self.pid = 4242
        self._polls = list(polls)

    def poll(self):
        return self._polls.pop(0) if len(self._polls) > 1 else self._polls[0]


# The token-file step's identity is its own unreaped child (no launchd runs
# value exists, none is faked): who holds the server end x the child's state
# before and after the check -> a socket only for (child, running, running);
# otherwise EndpointRefused, and the listener receives zero bytes in every cell.
@pytest.mark.parametrize(
    ("owners", "polls", "ok"),
    [
        ("job", [None, None], True),
        ("foreign", [None, None], False),
        ("invisible", [None, None], False),
        ("unreadable", [None, None], False),
        ("job", [0], False),  # exited before: nothing is even connected
        ("job", [None, 0], False),  # exited during the check
    ],
)
def test_connect_verified_child_state_space(lib, owners, polls, ok):
    srv = Listener("tcp")
    try:
        RUN_PORT[0] = srv.endpoint[2]
        kw = {"run": _lsof_for(_OWNERS[owners]), "settle": 0.2}
        child = _Child(polls)
        if ok:
            lib.connect_verified_child(srv.endpoint, child, **kw).close()
        else:
            with pytest.raises(lib.EndpointRefused):
                lib.connect_verified_child(srv.endpoint, child, **kw)
        if polls == [0]:
            time.sleep(0.3)
            assert srv.accepted == 0
        time.sleep(0.2)
        assert srv.received == []
    finally:
        srv.close()


# connect_verified keeps its launchd precondition: no caller passes a made-up
# runs value, and the token-file step has none to pass.
def test_no_caller_fakes_a_launchd_runs_value():
    text = (SCRIPTS / "smoke.py").read_text()
    assert '"self"' not in text
    assert "connect_verified_child(" in text


# --------------------------------------------------------------------------
# the --agents decode contract, end to end (Cause C)
# --------------------------------------------------------------------------
#
# smoke.py's decode child runs as a real subprocess (``decode_cmd``'s argv, the
# token on stdin) against a fake agent this process serves with the
# websockets library over a real TCP or AF_UNIX socket. Its launchd identity
# comes from a stub ``launchctl`` on PATH through squat_check's one reader, the
# TCP server end from a stub ``lsof``, a socket's from the real LOCAL_PEERPID
# (this process). Nothing is stubbed inside the child: connect_verified, the
# stdin token and the production TranscriptionClient connect tail all run.

_DECODE_LAUNCHCTL = """#!/bin/bash
if [[ "$1" == print ]]; then
    [[ -e {st}/print-fails ]] && {{ echo "launchctl: boom" >&2; exit 5; }}
    read -r pid runs < {st}/job
    printf 'gui/x = {{\\n\\tstate = running\\n\\tpid = %s\\n\\truns = %s\\n}}\\n' "$pid" "$runs"
    exit 0
fi
exit 1
"""
_DECODE_LSOF = """#!/bin/bash
for a in "$@"; do
    case "$a" in
    -iTCP@*) printf 'p%s\\nn127.0.0.1:%s->%s\\n' "$(cat {st}/owner)" "$(cat {st}/port)" "${{a#-iTCP@}}"; exit 0;;
    esac
done
exit 1
"""


class _FakeSttAgent:
    """The server half of the wire protocol, served from a thread's own event
    loop: checks the bearer token in the upgrade request (401 otherwise),
    sends server.hello and session.created, answers a commit with a
    transcript. Records every upgrade request's headers."""

    def __init__(self, transport: str, where, token: str | None):
        import asyncio

        self.requests: list[dict] = []
        self.token = token
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.where = where
        self.transport = transport
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()
        assert self.ready.wait(10)

    def _run(self):
        import asyncio

        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._start())
        self.ready.set()
        self.loop.run_forever()

    async def _start(self):
        import json

        from websockets.asyncio.server import serve, unix_serve

        from stt_server import protocol as P

        def process_request(connection, request):
            self.requests.append({"Authorization": request.headers.get("Authorization")})
            want = f"Bearer {self.token}" if self.token else None
            if request.headers.get("Authorization") != want:
                return connection.respond(401, "unauthorized\n")
            return None

        async def handler(ws):
            await ws.send(json.dumps({"type": P.EVT_SERVER_HELLO}))
            await ws.send(json.dumps({"type": P.EVT_SESSION_CREATED}))
            async for msg in ws:
                if isinstance(msg, str) and json.loads(msg).get("type") == P.EVT_AUDIO_COMMIT:
                    await ws.send(
                        json.dumps(
                            {"type": P.EVT_TRANSCRIPT_COMPLETED, "transcript": "Hello smoke"}
                        )
                    )

        if self.transport == "tcp":
            self.server = await serve(handler, "127.0.0.1", 0, process_request=process_request)
            self.port = self.server.sockets[0].getsockname()[1]
        else:
            self.server = await unix_serve(handler, self.where, process_request=process_request)

    def close(self):
        import asyncio

        async def stop():
            self.server.close()
            await self.server.wait_closed()

        asyncio.run_coroutine_threadsafe(stop(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.t.join(5)


# the decode child's identity gate x transport -> exit status, and what the
# agent received: only the "ok" cell decodes; a refused gate (server end not
# the job's, the job restarted between the two reads, launchd unreadable)
# exits DECODE_REFUSED with no request at all (no token); a token the agent
# refuses is a decode failure (exit 1) after the gate passed.
_DECODE_CELLS = {
    "ok": "pass",
    "foreign-listener": "refused",
    "pid-runs-change": "refused",
    "identity-unreadable": "refused",
    "token-refused": "fail",
}


@darwin
@pytest.mark.skipif(not VENV_PYTHON.exists(), reason="needs the project venv")
@pytest.mark.parametrize("cell", list(_DECODE_CELLS))
@pytest.mark.parametrize("transport", ["tcp", "unix"])
def test_agents_decode_contract_end_to_end(transport, cell, tmp_path):
    import wave

    sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location("smoke_runner_decode", SCRIPTS / "smoke.py")
    smoke = importlib.util.module_from_spec(spec)
    sys.modules["smoke_runner_decode"] = smoke
    spec.loader.exec_module(smoke)

    st = tmp_path / "state"
    st.mkdir()
    stub = tmp_path / "bin"
    stub.mkdir()
    for name, body in (("launchctl", _DECODE_LAUNCHCTL), ("lsof", _DECODE_LSOF)):
        (stub / name).write_text(body.format(st=st))
        (stub / name).chmod(0o755)
    wav = tmp_path / "clip.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 3200)

    token = "s3cret-token"
    d = tempfile.mkdtemp(dir="/tmp")
    agent = _FakeSttAgent(
        transport, os.path.join(d, "a.sock"), token + "-other" if cell == "token-refused" else token
    )
    me = str(os.getpid())  # this process holds the server end
    job_pid = "4242" if cell == "foreign-listener" else me
    (st / "owner").write_text(me)
    (st / "port").write_text(str(getattr(agent, "port", 0)))
    # The child's argv says (job_pid, 7); the re-read must still give it.
    (st / "job").write_text(f"{job_pid} {8 if cell == 'pid-runs-change' else 7}")
    if cell == "identity-unreadable":
        (st / "print-fails").write_text("1")
    if transport == "tcp":
        endpoint = {"host": "127.0.0.1", "port": str(agent.port)}
    else:
        endpoint = {"socket-path": agent.where}
    try:
        res = subprocess.run(
            smoke.decode_cmd("pipecat.stt-server", endpoint, str(wav), job_pid, "7"),
            input=token,
            capture_output=True,
            text=True,
            timeout=90,
            env={**os.environ, "PATH": f"{stub}:/usr/bin:/bin:/usr/sbin:/sbin"},
            cwd=str(REPO_ROOT),
        )
    finally:
        agent.close()
    want = _DECODE_CELLS[cell]
    assert token not in " ".join(smoke.decode_cmd("x", endpoint, str(wav), job_pid, "7"))
    if want == "pass":
        assert res.returncode == 0, res.stderr
        assert "Hello smoke" in res.stdout
        assert [r.get("Authorization") for r in agent.requests] == [f"Bearer {token}"]
    elif want == "refused":
        assert res.returncode == smoke.DECODE_REFUSED, (res.returncode, res.stderr)
        assert "refused:" in res.stderr
        assert agent.requests == []  # nothing, the token least of all, was sent
    else:
        assert res.returncode == 1, res.stderr
        assert len(agent.requests) == 1  # the gate passed; the agent refused the token


# smoke.py's verified-socket client runs the PRODUCTION connect (unchanged
# stt_server/client.py, the surface Koda pins): only its transport call is
# swapped for the verified socket. What the server sends first -> hello
# returned or the production client's own error, the bearer header sent on
# the verified socket, and the client module's ws_connect restored after.
_TAILS = {
    "ok": (["hello", "created"], None),
    "no-hello": (["bogus", "created"], "expected server.hello, got bogus"),
    "no-created": (["hello", "x"], "expected session.created, got x"),
}


@pytest.mark.parametrize("tail", list(_TAILS))
def test_verified_client_runs_the_production_connect_tail(tail):
    import asyncio
    import json

    from websockets.asyncio.server import serve

    import stt_server.client as client_mod
    from stt_server import protocol as P

    sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location("smoke_runner_tail", SCRIPTS / "smoke.py")
    smoke = importlib.util.module_from_spec(spec)
    sys.modules["smoke_runner_tail"] = smoke
    spec.loader.exec_module(smoke)
    frames, error = _TAILS[tail]
    names = {"hello": P.EVT_SERVER_HELLO, "created": P.EVT_SESSION_CREATED}
    seen = []
    real = client_mod.ws_connect

    async def go():
        async def handler(ws):
            seen.append(ws.request.headers.get("Authorization"))
            for f in frames:
                await ws.send(json.dumps({"type": names.get(f, f)}))
            await asyncio.sleep(1)

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            sock = socket.create_connection(("127.0.0.1", port))
            c = smoke._verified_client(sock, f"ws://127.0.0.1:{port}/", "tok")
            try:
                return await c.connect()
            finally:
                await c.close()

    if error is None:
        assert asyncio.run(go())["type"] == P.EVT_SERVER_HELLO
    else:
        with pytest.raises(RuntimeError, match=error):
            asyncio.run(go())
    assert seen == ["Bearer tok"]
    assert client_mod.ws_connect is real


# The swap admits only the verified socket: a connect for another uri (or one
# that already names a socket) is refused, never opened.
def test_verified_client_refuses_any_other_transport(monkeypatch):
    import asyncio

    import stt_server.client as client_mod

    sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location("smoke_runner_swap", SCRIPTS / "smoke.py")
    smoke = importlib.util.module_from_spec(spec)
    sys.modules["smoke_runner_swap"] = smoke
    spec.loader.exec_module(smoke)
    opened = []
    monkeypatch.setattr(client_mod, "ws_connect", lambda *a, **k: opened.append(a))
    c = smoke._verified_client(object(), "ws://127.0.0.1:1/", None)
    c._uri = "ws://elsewhere:2/"
    with pytest.raises(RuntimeError, match="refusing a transport"):
        asyncio.run(c.connect())
    assert opened == []
