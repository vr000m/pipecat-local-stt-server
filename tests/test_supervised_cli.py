"""``serve --supervised``: the contract a managing app relies on (echo backend, lean).

- stdout carries exactly one ``{"event": "ready", ...}`` JSON line once the
  listener is bound and warm, and nothing else (logs stay on stderr);
- closing stdin stops the server cleanly (exit 0), like SIGTERM;
- a busy TCP port or a live Unix socket fails fast with ``code: EADDRINUSE``;
- a missing backend extra fails with ``code: ModuleNotFoundError``;
- without ``--supervised`` stdout stays empty (behaviour unchanged).
"""

from __future__ import annotations

import errno
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from stt_server import __main__ as cli
from stt_server import supervise


def _env(**extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("STT_WS_", "PIPECAT_STT_", "KODA_STT_"))
    }
    env.update(extra)
    return env


def _serve(*args: str, env: dict[str, str] | None = None) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "stt_server", "serve", *args, "--supervised"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env or _env(),
    )


def _read_event(proc: subprocess.Popen, timeout: float = 20.0) -> dict:
    assert proc.stdout is not None
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        proc.kill()
        raise AssertionError("no stdout line in time; stderr:\n" + proc.stderr.read().decode())
    return json.loads(proc.stdout.readline())


def _finish(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)


def _wait_stderr_line(proc: subprocess.Popen, match, timeout: float = 10.0) -> str | None:
    """The first stderr line ``match`` accepts, or None on timeout or EOF.

    Reads the pipe's fd directly. ``select`` on the buffered file object cannot
    see lines already read into its buffer, so a marker that arrived in the same
    read as an earlier log line was never found on a slow runner."""
    assert proc.stderr is not None
    fd = proc.stderr.fileno()
    buf = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.25)
        if not ready:
            continue
        chunk = os.read(fd, 65536)
        if not chunk:
            return None
        buf += chunk
        for raw in buf.split(b"\n")[:-1]:
            line = raw.decode(errors="replace")
            if match(line):
                return line
    return None


def test_ready_line_then_stdin_eof_stops():
    proc = _serve("--backend", "echo", "--host", "127.0.0.1", "--port", "0")
    try:
        event = _read_event(proc)
        assert event["event"] == "ready"
        assert event["backend"] == "echo" and event["model"] is None
        assert event["pid"] == proc.pid
        assert event["rate"] == 16000
        assert event["host"] == "127.0.0.1" and event["socket_path"] is None
        port = event["port"]
        assert isinstance(port, int) and port > 0  # --port 0 reports the real port
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
        proc.stdin.close()  # the supervisor goes away
        assert proc.wait(timeout=15) == 0
        assert proc.stdout.read() == b""  # nothing after the ready line
    finally:
        _finish(proc)


@pytest.mark.parametrize("stop_kind", ["stdin_eof", "sigterm"])
def test_supervised_stop_during_blocked_warmup_exits_without_ready(stop_kind):
    script = textwrap.dedent(
        """
        import asyncio
        import os
        import threading

        from stt_server.backend import EchoBackend
        from stt_server.server import serve

        # The real start(): readiness only ever runs on a ``running`` server
        # (a stubbed start would leave it ``new``, where adopt() is refused).
        async def blocked_ready(server):
            os.write(2, b"warmup-blocked\\n")
            await asyncio.to_thread(threading.Event().wait)
            os.write(1, b'{"event":"ready"}\\n')

        asyncio.run(
            serve(
                EchoBackend(),
                host="127.0.0.1",
                port=0,
                ready=blocked_ready,
                stop_on_stdin_eof=True,
                hard_exit_on_stop=True,
            )
        )
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(),
    )
    try:
        assert proc.stderr is not None
        if _wait_stderr_line(proc, lambda line: line == "warmup-blocked") is None:
            raise AssertionError("warm-up did not block in time")

        started = time.monotonic()
        if stop_kind == "stdin_eof":
            proc.stdin.close()
        else:
            proc.terminate()
        assert proc.wait(timeout=3) == 0
        assert time.monotonic() - started < 3
        assert proc.stdout.read() == b""
    finally:
        _finish(proc)


@pytest.mark.parametrize("backend_cls", ["ParakeetBackend", "NemotronBackend"])
def test_stop_during_blocked_warmup_removes_socket_and_tmpdir(short_home, backend_cls):
    """The hard exit skips shutdown()/close(); abort_sync must still unlink the
    bound socket and the backend's private temp dir."""
    module = backend_cls.replace("Backend", "").lower()
    sock = short_home / "s" / "stt.sock"
    script = textwrap.dedent(
        f"""
        import asyncio
        import os
        import threading

        from stt_server.backends.{module} import {backend_cls}
        from stt_server.server import serve

        class NoExtra({backend_cls}):
            async def start(self):
                return None

        backend = NoExtra()

        async def blocked_ready(server):
            os.write(2, ("warmup-blocked " + backend._tmpdir + "\\n").encode())
            await asyncio.to_thread(threading.Event().wait)

        asyncio.run(
            serve(
                backend,
                socket_path={str(sock)!r},
                ready=blocked_ready,
                stop_on_stdin_eof=True,
                hard_exit_on_stop=True,
            )
        )
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(HOME=str(short_home)),
    )
    try:
        assert proc.stderr is not None
        tmpdir = None
        line = _wait_stderr_line(proc, lambda line: line.startswith("warmup-blocked "))
        if line is not None:
            tmpdir = line.split(" ", 1)[1].strip()
        assert tmpdir, "warm-up did not block in time"
        assert sock.exists() and Path(tmpdir).is_dir()

        proc.stdin.close()
        assert proc.wait(timeout=5) == 0
        assert not sock.exists()
        assert not Path(tmpdir).exists()
    finally:
        _finish(proc)
        shutil.rmtree(tmpdir or "", ignore_errors=True)


def test_abort_sync_never_unlinks_a_socket_it_did_not_bind(short_home):
    import asyncio

    from stt_server.backend import EchoBackend
    from stt_server.server import ServerConfig, TranscriptionServer

    sock = short_home / "s" / "stt.sock"

    async def run():
        srv = TranscriptionServer(EchoBackend(), ServerConfig(socket_path=str(sock)))
        await srv.start()
        assert srv._bound_socket_id is not None
        # Replace our socket with someone else's at the same path.
        sock.unlink()
        other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        other.bind(str(sock))
        try:
            srv.abort_sync()
            assert sock.exists()  # foreign inode: left alone
        finally:
            other.close()
        sock.unlink()
        await srv.shutdown()

    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(short_home)
    try:
        asyncio.run(run())
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home


def test_abort_sync_without_bind_is_a_noop():
    from stt_server.backend import EchoBackend
    from stt_server.server import ServerConfig, TranscriptionServer

    TranscriptionServer(EchoBackend(), ServerConfig(host="127.0.0.1", port=0)).abort_sync()


def test_plain_serve_sigterm_during_slow_startup_exits_promptly():
    """Handlers are installed before ``start()``; a stop must not wait for it."""
    script = textwrap.dedent(
        """
        import asyncio
        import os

        from stt_server.backend import EchoBackend
        from stt_server.server import serve

        class SlowStart(EchoBackend):
            async def start(self):
                os.write(2, b"starting\\n")
                await asyncio.sleep(60)

        asyncio.run(serve(SlowStart(), host="127.0.0.1", port=0))
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(),
    )
    try:
        assert proc.stderr is not None
        if _wait_stderr_line(proc, lambda line: line == "starting") is None:
            raise AssertionError("startup did not begin in time")
        proc.terminate()
        assert proc.wait(timeout=5) == 0
    finally:
        _finish(proc)


@pytest.fixture
def short_home():
    """A private, short $HOME so the socket-dir ancestor walk passes and the
    socket path stays under the AF_UNIX length limit."""
    home = Path(tempfile.mkdtemp(prefix="stt.", dir="/tmp"))
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_ready_line_reports_socket_path(short_home):
    sock = short_home / "s" / "stt.sock"
    proc = _serve("--backend", "echo", "--socket-path", str(sock), env=_env(HOME=str(short_home)))
    try:
        event = _read_event(proc)
        assert event["event"] == "ready"
        assert event["socket_path"] == str(sock)
        assert event["port"] is None and event["host"] is None
        proc.stdin.close()
        assert proc.wait(timeout=15) == 0
    finally:
        _finish(proc)


def test_live_unix_socket_reports_eaddrinuse(short_home):
    sock = short_home / "s" / "stt.sock"
    env = _env(HOME=str(short_home))
    first = _serve("--backend", "echo", "--socket-path", str(sock), env=env)
    try:
        assert _read_event(first)["event"] == "ready"
        second = _serve("--backend", "echo", "--socket-path", str(sock), env=env)
        try:
            event = _read_event(second)
            assert second.wait(timeout=15) == 1
            err = second.stderr.read().decode()
        finally:
            _finish(second)
        assert event["event"] == "error" and event["code"] == "EADDRINUSE"
        # An expected failure is a one-line message, not a "fatal error" traceback.
        assert "Traceback" not in err and "stt_server: " in err
        # The first server still owns its socket (it was not replaced).
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.connect(str(sock))
    finally:
        _finish(first)


@pytest.mark.parametrize(
    "probe_error",
    [TimeoutError("timed out"), BlockingIOError(errno.EAGAIN, "backlog full")],
)
def test_unix_socket_ambiguous_probe_error_preserves_inode(short_home, monkeypatch, probe_error):
    path = short_home / "listener.sock"
    path.write_bytes(b"socket inode sentinel")
    inode = path.stat().st_ino
    real_is_socket = Path.is_socket
    monkeypatch.setattr(Path, "is_socket", lambda self: self == path or real_is_socket(self))

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def settimeout(self, _timeout):
            pass

        def connect(self, _path):
            raise probe_error

    monkeypatch.setattr(socket, "socket", lambda *_args, **_kwargs: Probe())
    with pytest.raises(OSError) as exc:
        cli._preflight_listener(SimpleNamespace(socket_path=str(path), host=None, port=None))

    assert exc.value.errno == errno.EADDRINUSE
    assert path.stat().st_ino == inode


@pytest.mark.parametrize("stale_errno", [errno.ECONNREFUSED, errno.ENOENT])
def test_unix_socket_narrow_stale_probe_errors_are_allowed(short_home, monkeypatch, stale_errno):
    path = short_home / "listener.sock"
    path.write_bytes(b"socket inode sentinel")
    real_is_socket = Path.is_socket
    monkeypatch.setattr(Path, "is_socket", lambda self: self == path or real_is_socket(self))

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def settimeout(self, _timeout):
            pass

        def connect(self, _path):
            raise OSError(stale_errno, "stale")

    monkeypatch.setattr(socket, "socket", lambda *_args, **_kwargs: Probe())
    cli._preflight_listener(SimpleNamespace(socket_path=str(path), host=None, port=None))


@pytest.mark.parametrize("fatal_errno", [errno.EACCES, errno.EPERM, errno.EIO])
def test_unix_socket_fatal_probe_error_preserves_inode(short_home, monkeypatch, fatal_errno):
    path = short_home / "listener.sock"
    path.write_bytes(b"socket inode sentinel")
    inode = path.stat().st_ino
    real_is_socket = Path.is_socket
    monkeypatch.setattr(Path, "is_socket", lambda self: self == path or real_is_socket(self))

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def settimeout(self, _timeout):
            pass

        def connect(self, _path):
            raise OSError(fatal_errno, "fatal")

    monkeypatch.setattr(socket, "socket", lambda *_args, **_kwargs: Probe())
    with pytest.raises(OSError) as exc:
        cli._preflight_listener(SimpleNamespace(socket_path=str(path), host=None, port=None))

    assert exc.value.errno == fatal_errno
    assert path.stat().st_ino == inode


def test_busy_tcp_port_reports_eaddrinuse_fast():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        started = time.monotonic()
        proc = _serve("--backend", "echo", "--host", "127.0.0.1", "--port", str(port))
        try:
            event = _read_event(proc)
            assert proc.wait(timeout=15) == 1
        finally:
            _finish(proc)
        assert event["event"] == "error" and event["code"] == "EADDRINUSE"
        assert time.monotonic() - started < 10


def test_missing_extra_reports_module_not_found(tmp_path):
    # A stand-in ``moshi_mlx`` that fails like an absent extra, whatever is
    # installed in this environment.
    fake = tmp_path / "moshi_mlx"
    fake.mkdir()
    (fake / "__init__.py").write_text(
        "raise ModuleNotFoundError(\"No module named 'moshi_mlx'\", name='moshi_mlx')\n"
    )
    proc = _serve(
        "--backend",
        "kyutai",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        env=_env(PYTHONPATH=str(tmp_path)),
    )
    try:
        event = _read_event(proc, timeout=60)
        assert proc.wait(timeout=15) == 1
    finally:
        _finish(proc)
    assert event["event"] == "error"
    assert event["code"] == "ModuleNotFoundError"
    assert "uv sync --extra kyutai" in event["message"]


def test_unsupervised_stdout_stays_empty():
    proc = subprocess.Popen(
        [sys.executable, "-m", "stt_server", "serve", "--backend", "echo"]
        + ["--host", "127.0.0.1", "--port", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(),
    )
    try:
        time.sleep(1.5)
        proc.terminate()
        out, _ = proc.communicate(timeout=15)
    finally:
        _finish(proc)
    assert out == b""


class _HubError(Exception):
    pass


_HubError.__module__ = "huggingface_hub.errors"


class HFValidationError(ValueError):
    """Stand-in for ``huggingface_hub.errors.HFValidationError`` (bad repo id)."""


HFValidationError.__module__ = "huggingface_hub.errors"
_HubValidationError = HFValidationError


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (
            ModuleNotFoundError("No module named 'moshi_mlx'", name="moshi_mlx"),
            "ModuleNotFoundError",
        ),
        (OSError(errno.EADDRINUSE, "Address already in use"), "EADDRINUSE"),
        (OSError(errno.EACCES, "Permission denied"), "EACCES"),
        (_HubError("404"), "ModelDownloadError"),
        # Bare builtins carry no download origin: not a model download.
        (ConnectionResetError("reset"), "BackendStartError"),
        (TimeoutError("timed out"), "BackendStartError"),
        (OSError(errno.EPERM, "Operation not permitted"), "BackendStartError"),
        (supervise.ConfigError("V1 only permits loopback binds"), "ConfigError"),
        # A bare ValueError (e.g. from a model library during warm-up) is not a
        # bad endpoint.
        (ValueError("bad tensor shape"), "BackendStartError"),
        (_HubValidationError("Repo id must be in the form 'repo_name'"), "ConfigError"),
        (RuntimeError("decoder exploded"), "BackendStartError"),
    ],
)
def test_classify_startup_error(exc, code):
    assert supervise.classify_startup_error(exc)[0] == code


def test_classify_network_library_exception_in_chain_is_download_error():
    class _HttpxError(Exception):
        pass

    _HttpxError.__module__ = "httpx"
    try:
        try:
            raise _HttpxError("read timeout")
        except _HttpxError as inner:
            raise RuntimeError("model load failed") from inner
    except RuntimeError as outer:
        assert supervise.classify_startup_error(outer)[0] == "ModelDownloadError"


@pytest.mark.parametrize(
    "exc", [ConnectionResetError("reset"), ConnectionAbortedError("aborted"), TimeoutError("t")]
)
def test_classify_builtin_network_error_in_download_phase_is_download_error(exc):
    supervise.mark_download_phase(exc)
    assert supervise.classify_startup_error(exc)[0] == "ModelDownloadError"


def test_classify_download_phase_does_not_override_address_errors():
    exc = OSError(errno.EADDRINUSE, "Address already in use")
    supervise.mark_download_phase(exc)
    assert supervise.classify_startup_error(exc)[0] == "EADDRINUSE"
    exc = OSError(errno.EACCES, "Permission denied")
    supervise.mark_download_phase(exc)
    assert supervise.classify_startup_error(exc)[0] == "EACCES"


@pytest.mark.parametrize("probe_errno", [errno.ECONNRESET, errno.ECONNABORTED, errno.EPERM])
def test_supervised_preflight_oserror_is_not_reported_as_download_error(
    monkeypatch, capsys, probe_errno
):
    def broken_preflight(_args):
        raise OSError(probe_errno, "uds probe failed")

    monkeypatch.setattr(cli, "_preflight_listener", broken_preflight)
    args = SimpleNamespace(supervised=True, log_level="WARNING")
    with pytest.raises(SystemExit) as exc:
        cli._cmd_serve(args)
    assert exc.value.code == 1
    event = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert event["event"] == "error" and event["code"] == "BackendStartError"
    assert "download" not in event["message"]


def test_warmup_decoder_connection_error_is_not_a_download_error():
    """Warm-up runs a decode as well as the lazy load; a builtin network error
    from the decoder (not a fetch) must not be reported as a model download."""
    import asyncio

    class FailingBackend:
        async def open_stream(self, *, language=None):
            raise ConnectionResetError("decoder socket hung up")

    server = SimpleNamespace(backend=FailingBackend())
    with pytest.raises(ConnectionResetError) as exc:
        asyncio.run(cli._prepare_ready(server))
    assert supervise.classify_startup_error(exc.value)[0] == "BackendStartError"


def _fake_modules(monkeypatch, **modules):
    for name, attrs in modules.items():
        monkeypatch.setitem(sys.modules, name, SimpleNamespace(**attrs))


def _reset(*_a, **_k):
    raise ConnectionResetError("model host hung up")


@pytest.mark.parametrize("backend_name", ["parakeet", "nemotron", "kyutai"])
def test_backend_model_fetch_connection_error_is_download_error(monkeypatch, backend_name):
    """Only the backend's fetch-and-load call is tagged as the download phase."""
    if backend_name == "parakeet":
        from stt_server.backends.parakeet import ParakeetBackend as cls

        _fake_modules(monkeypatch, parakeet_mlx={"from_pretrained": _reset})
    elif backend_name == "nemotron":
        from stt_server.backends.nemotron import NemotronBackend as cls

        _fake_modules(monkeypatch, mlx_audio={}, **{"mlx_audio.stt": {"load": _reset}})
    else:
        from stt_server.backends.kyutai import KyutaiBackend as cls

        core, nn = SimpleNamespace(), SimpleNamespace()
        _fake_modules(
            monkeypatch,
            mlx={"core": core, "nn": nn},
            sentencepiece={},
            moshi_mlx={"models": None, "utils": None},
            huggingface_hub={"hf_hub_download": _reset},
        )
        monkeypatch.setitem(sys.modules, "mlx.core", core)
        monkeypatch.setitem(sys.modules, "mlx.nn", nn)
    backend = cls(model="x/y")
    try:
        with pytest.raises(ConnectionResetError) as exc:
            backend._get_model()
    finally:
        getattr(backend, "abort", lambda: None)()
    assert supervise.classify_startup_error(exc.value)[0] == "ModelDownloadError"


def test_classify_follows_cause_chain():
    try:
        try:
            raise OSError(errno.EADDRINUSE, "Address already in use")
        except OSError as inner:
            raise RuntimeError("bind failed") from inner
    except RuntimeError as outer:
        assert supervise.classify_startup_error(outer)[0] == "EADDRINUSE"


def test_stdin_watch_treats_nonblocking_empty_pipe_as_not_eof(monkeypatch):
    r, w = os.pipe()
    os.set_blocking(r, False)  # a parent that left the shared pipe non-blocking

    class FakeStdin:
        def fileno(self):
            return r

    class Loop:
        def call_soon_threadsafe(self, callback):
            callback()

    fired = threading.Event()
    monkeypatch.setattr(sys, "stdin", FakeStdin())
    try:
        supervise.watch_stdin_eof(Loop(), fired.set)
        assert not fired.wait(0.5)  # nothing to read yet is not EOF
        os.write(w, b"ignored")
        assert not fired.wait(0.3)  # data is discarded, still not EOF
        os.close(w)
        w = -1
        assert fired.wait(5)  # hang-up is EOF
    finally:
        if w != -1:
            os.close(w)
        os.close(r)


def test_json_channel_routes_prints_to_stderr(capsys):
    with supervise.json_channel():
        print("library noise")
        supervise.emit({"event": "x", "n": 1})
    out, err = capsys.readouterr()
    assert out == '{"event":"x","n":1}\n'
    assert "library noise" in err


@pytest.mark.parametrize("hard", [True, False])
def test_stop_during_warmup_hard_exit_is_opt_in(hard):
    """``hard_exit_on_stop=True`` (the CLI): abort_sync + os._exit, no shutdown.
    Default (embedders): the warm-up is cancelled, shutdown()/close() run and
    ``serve`` returns to its caller."""
    script = textwrap.dedent(
        """
        import asyncio
        import os
        import sys

        from stt_server.backend import EchoBackend
        from stt_server.server import serve

        class B(EchoBackend):
            def abort(self):
                os.write(2, b"abort\\n")

            async def close(self):
                os.write(2, b"close\\n")

        async def blocked_ready(server):
            os.write(2, b"warmup-blocked\\n")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                os.write(2, b"warmup-cancelled\\n")
                raise

        asyncio.run(
            serve(
                B(),
                host="127.0.0.1",
                port=0,
                ready=blocked_ready,
                hard_exit_on_stop=sys.argv[1] == "1",
            )
        )
        os.write(2, b"returned\\n")
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script, "1" if hard else "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(),
    )
    try:
        assert proc.stderr is not None
        if _wait_stderr_line(proc, lambda line: line == "warmup-blocked") is None:
            raise AssertionError("warm-up did not block in time")
        proc.terminate()
        assert proc.wait(timeout=5) == 0
        lines = set(proc.stderr.read().decode().split())
    finally:
        _finish(proc)
    if hard:
        assert "abort" in lines and "close" not in lines and "returned" not in lines
    else:
        assert {"warmup-cancelled", "close", "returned"} <= lines and "abort" not in lines


def test_cli_run_serve_opts_into_hard_exit(monkeypatch):
    seen = {}

    async def fake_serve(backend, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(cli, "serve", fake_serve)
    args = SimpleNamespace(
        backend="echo", model=None, socket_path=None, host="127.0.0.1", port=0, auth_token_file=None
    )
    cli._run_serve(args)
    assert seen["hard_exit_on_stop"] is True


def test_run_serve_failure_chains_the_cause_explicitly(monkeypatch, capsys):
    """``_cmd_serve`` classifies the supervised error event from ``__cause__``."""
    original = OSError(errno.EADDRINUSE, "Address already in use")

    async def failing_serve(backend, **kwargs):
        raise original

    monkeypatch.setattr(cli, "serve", failing_serve)
    args = SimpleNamespace(
        backend="echo", model=None, socket_path=None, host="127.0.0.1", port=0, auth_token_file=None
    )
    with pytest.raises(SystemExit) as exc:
        cli._run_serve(args)
    assert exc.value.code == 1 and exc.value.__cause__ is original


@pytest.mark.parametrize("backend_name", ["parakeet", "nemotron"])
def test_start_after_abort_recreates_private_tmpdir(monkeypatch, backend_name):
    """A failed start runs ``abort()`` (removing the temp dir); a retried
    ``start()`` on the same backend must still have somewhere to write WAVs."""
    import asyncio

    if backend_name == "parakeet":
        from stt_server.backends.parakeet import ParakeetBackend as cls

        _fake_modules(monkeypatch, parakeet_mlx={})
    else:
        from stt_server.backends.nemotron import NemotronBackend as cls

        _fake_modules(monkeypatch, mlx_audio={}, **{"mlx_audio.stt": {"load": _reset}})
    backend = cls(model="x/y")
    try:
        backend.abort()
        assert not os.path.isdir(backend._tmpdir)
        asyncio.run(backend.start())
        assert os.path.isdir(backend._tmpdir)
        assert (os.stat(backend._tmpdir).st_mode & 0o777) == 0o700
    finally:
        backend.abort()


def test_endpoint_rejections_are_config_errors():
    """Every ServerConfig / socket-directory rejection still classifies as
    ConfigError now that a bare ValueError no longer does."""
    from stt_server.server import ServerConfig, _enforce_socket_dir_secure

    cases = []
    for kwargs in ({}, {"host": "10.0.0.1", "port": 1}):
        with pytest.raises(ValueError) as exc:
            ServerConfig(**kwargs)
        cases.append(exc.value)
    for path in ("relative/stt.sock", "/tmp/../x/stt.sock", "/opt/elsewhere/stt.sock"):
        with pytest.raises(ValueError) as exc:
            _enforce_socket_dir_secure(Path(path), Path.home())
        cases.append(exc.value)
    assert [supervise.classify_startup_error(e)[0] for e in cases] == ["ConfigError"] * 5


def _fake_kyutai_modules(monkeypatch, hf_hub_download):
    core, nn = SimpleNamespace(), SimpleNamespace()
    _fake_modules(
        monkeypatch,
        mlx={"core": core, "nn": nn},
        sentencepiece={},
        moshi_mlx={"models": None, "utils": None},
        huggingface_hub={"hf_hub_download": hf_hub_download},
    )
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    monkeypatch.setitem(sys.modules, "mlx.nn", nn)


@pytest.mark.parametrize(
    "config_text",
    [
        "{not json",
        "null",
        "[]",
        '{"mimi_name": "m.safetensors"}',
        '{"mimi_name": "../m", "tokenizer_name": "t"}',
    ],
)
def test_kyutai_serve_path_unusable_config_is_model_invalid(monkeypatch, tmp_path, config_text):
    """Serve-path ``_get_model`` agrees with prefetch: a fetched config.json that
    does not parse or name its files is ModelInvalidError, not ConfigError (a
    bare ValueError) or BackendStartError (KeyError/TypeError)."""
    from stt_server.backends.kyutai import KyutaiBackend

    cfg = tmp_path / "config.json"
    cfg.write_text(config_text)
    _fake_kyutai_modules(monkeypatch, lambda *_a, **_k: str(cfg))
    with pytest.raises(Exception) as exc:
        KyutaiBackend(model="x/y")._get_model()
    assert supervise.classify_startup_error(exc.value)[0] == "ModelInvalidError"


@pytest.mark.parametrize("backend_name", ["parakeet", "nemotron"])
def test_abort_while_decode_thread_runs_is_safe(monkeypatch, backend_name):
    """``abort()`` contract: it may run while a decode / lazy-load daemon thread
    is still going (a stop during warm-up leaves one behind). Cells: abort while
    the thread is inside the model call, abort again (idempotent), the thread
    then finishes without hanging, and a later ``start()`` recreates the temp
    dir so decoding works again; abort before a decode makes that decode fail
    cleanly, never hang."""
    import asyncio
    import threading

    if backend_name == "parakeet":
        from stt_server.backends.parakeet import ParakeetBackend as cls

        _fake_modules(monkeypatch, parakeet_mlx={})
    else:
        from stt_server.backends.nemotron import NemotronBackend as cls

        _fake_modules(monkeypatch, mlx_audio={}, **{"mlx_audio.stt": {"load": _reset}})
    entered, release = threading.Event(), threading.Event()

    class FakeModel:
        def _run(self, path, **_kwargs):
            entered.set()
            release.wait(5)
            return SimpleNamespace(text="hi")

        transcribe = generate = _run

    async def decode(backend):
        stream = await backend.open_stream()
        await stream.feed(b"\x00\x00" * 160)
        await stream.end()
        return [ev.text async for ev in stream.events()]

    async def body():
        backend = cls(model="x/y")
        backend._get_model = lambda: FakeModel()
        try:
            task = asyncio.ensure_future(decode(backend))
            assert await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)
            backend.abort()  # the decode thread is mid-call
            backend.abort()  # idempotent
            assert not os.path.isdir(backend._tmpdir)
            release.set()
            await asyncio.wait_for(asyncio.wait({task}), timeout=5)
            assert task.done()
            # Abort before a decode: the decode fails cleanly (no hang)...
            with pytest.raises(OSError):
                await asyncio.wait_for(decode(backend), timeout=5)
            # ...and a later start() recreates what abort() removed.
            await backend.start()
            assert await asyncio.wait_for(decode(backend), timeout=5) == ["hi", "hi"]
        finally:
            release.set()
            backend.abort()

    asyncio.run(body())
