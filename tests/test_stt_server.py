"""Tests for the standalone ``stt_server`` package."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import websockets

from stt_server import EchoBackend, TranscriptionClient
from stt_server import protocol as P
from stt_server.backend import TranscriptEvent
from stt_server.client import (
    format_host_for_uri,
    is_cleartext_remote,
    resolve_endpoint_from_env,
)
from stt_server.server import ServerConfig, TranscriptionServer


def test_format_host_for_uri_brackets_ipv6():
    assert format_host_for_uri("::1") == "[::1]"
    assert format_host_for_uri("fe80::1") == "[fe80::1]"


def test_format_host_for_uri_passes_hostnames_through():
    assert format_host_for_uri("127.0.0.1") == "127.0.0.1"
    assert format_host_for_uri("localhost") == "localhost"
    assert format_host_for_uri("example.local") == "example.local"


def test_is_cleartext_remote_flags_non_loopback_ws():
    assert is_cleartext_remote("ws://example.com/") is True
    assert is_cleartext_remote("ws://8.8.8.8:9000/") is True


def test_is_cleartext_remote_allows_loopback_and_wss():
    assert is_cleartext_remote("ws://localhost:9000/") is False
    assert is_cleartext_remote("ws://127.0.0.1:9000/") is False
    assert is_cleartext_remote("ws://[::1]:9000/") is False
    assert is_cleartext_remote("wss://example.com/") is False
    assert is_cleartext_remote("") is False


def test_resolve_endpoint_from_env_precedence_uri_wins():
    env = {
        "STT_WS_URI": "ws://a/",
        "STT_WS_SOCKET": "/tmp/s",
        "STT_WS_HOST": "h",
        "STT_WS_PORT": "1",
    }
    r = resolve_endpoint_from_env(env)
    assert r == {"uri": "ws://a/", "socket_path": None, "host": None, "port": None}


def test_resolve_endpoint_from_env_precedence_socket_over_host_port():
    r = resolve_endpoint_from_env(
        {"STT_WS_SOCKET": "/tmp/s", "STT_WS_HOST": "h", "STT_WS_PORT": "1"}
    )
    assert r == {"uri": None, "socket_path": "/tmp/s", "host": None, "port": None}


def test_resolve_endpoint_from_env_empty_returns_nones():
    assert resolve_endpoint_from_env({}) == {
        "uri": None,
        "socket_path": None,
        "host": None,
        "port": None,
    }


def test_client_expanduser_on_socket_path():
    c = TranscriptionClient(socket_path="~/foo/bar.sock")
    assert c._socket_path == os.path.expanduser("~/foo/bar.sock")
    assert c._socket_path and not c._socket_path.startswith("~")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def server():
    """Run a TranscriptionServer on 127.0.0.1:<ephemeral> for the test."""
    srv = TranscriptionServer(
        EchoBackend(),
        ServerConfig(host="127.0.0.1", port=0, reject_browser_origins=False),
    )
    await srv.start()
    try:
        yield srv
    finally:
        await srv.shutdown()


@pytest.fixture
async def client(server: TranscriptionServer):
    port = server.listening_port()
    assert port is not None
    c = TranscriptionClient(host="127.0.0.1", port=port)
    await c.connect()
    try:
        yield c
    finally:
        await c.close()


async def _next_event_of_types(
    client: TranscriptionClient, types: set[str], *, timeout=2.0
) -> dict:
    async def _read():
        async for ev in client.events():
            if ev.get("type") in types:
                return ev
        raise RuntimeError(f"socket closed before receiving {types}")

    return await asyncio.wait_for(_read(), timeout=timeout)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_handshake_sends_hello_and_created():
    srv = TranscriptionServer(
        EchoBackend(),
        ServerConfig(host="127.0.0.1", port=0, reject_browser_origins=False),
    )
    await srv.start()
    try:
        port = srv.listening_port()
        c = TranscriptionClient(host="127.0.0.1", port=port)
        hello = await c.connect()
        assert hello["type"] == P.EVT_SERVER_HELLO
        assert hello["protocol_version"] == P.PROTOCOL_VERSION
        assert hello["capabilities"]["binary_audio"] is True
        # Backend identity — lets a client verify which ASR is behind a socket.
        assert hello["backend"] == {"name": "echo", "model": None}
        await c.close()
    finally:
        await srv.shutdown()


async def test_session_update_turn_detection_null_is_acknowledged(client):
    await client.update_session(turn_detection=None)
    updated = await _next_event_of_types(client, {P.EVT_SESSION_UPDATED, P.EVT_ERROR})
    assert updated["type"] == P.EVT_SESSION_UPDATED
    assert updated["session"]["audio"]["input"]["turn_detection"] is None


async def test_session_update_non_null_turn_detection_errors(client):
    import json as _j

    # Bypass the convenience helper to exercise the server VAD rejection path.
    await client._ws.send(  # type: ignore[attr-defined]
        _j.dumps(
            {
                "type": P.EVT_SESSION_UPDATE,
                "session": {"audio": {"input": {"turn_detection": {"type": "server_vad"}}}},
            }
        )
    )
    err = await _next_event_of_types(client, {P.EVT_SESSION_UPDATED, P.EVT_ERROR})
    assert err["type"] == P.EVT_ERROR
    assert err["error"]["code"] == P.ErrorCode.INVALID_CONFIG.value


async def test_session_update_invalid_sample_rate_errors(client):
    # Bypass the helper so we can pass an off-rate format explicitly.
    await client._ws.send(  # type: ignore[attr-defined]
        json.dumps(
            {
                "type": P.EVT_SESSION_UPDATE,
                "session": {
                    "audio": {
                        "input": {
                            "format": {
                                "encoding": P.AUDIO_FORMAT,
                                "rate": 48000,
                                "channels": P.AUDIO_CHANNELS,
                            },
                            "turn_detection": None,
                        }
                    }
                },
            }
        )
    )
    err = await _next_event_of_types(client, {P.EVT_SESSION_UPDATED, P.EVT_ERROR})
    assert err["type"] == P.EVT_ERROR
    assert err["error"]["code"] == P.ErrorCode.INVALID_CONFIG.value


async def test_unknown_event_returns_error(client):
    await client._ws.send(json.dumps({"type": "not.a.real.event"}))  # type: ignore[attr-defined]
    err = await _next_event_of_types(client, {P.EVT_ERROR})
    assert err["error"]["code"] == P.ErrorCode.UNSUPPORTED_EVENT.value


async def test_invalid_json_returns_error(client):
    await client._ws.send("this is not JSON{")  # type: ignore[attr-defined]
    err = await _next_event_of_types(client, {P.EVT_ERROR})
    assert err["error"]["code"] == P.ErrorCode.INVALID_JSON.value


# ---------------------------------------------------------------------------
# Commit-driven transcript flow
# ---------------------------------------------------------------------------


def _pcm(n_samples: int) -> bytes:
    return (0).to_bytes(2, "little") * n_samples


async def test_commit_binary_audio_produces_delta_and_completed(client):
    await client.update_session(turn_detection=None)
    await _next_event_of_types(client, {P.EVT_SESSION_UPDATED})
    await client.send_audio(_pcm(1600))  # 100 ms
    await client.commit()

    committed = await _next_event_of_types(client, {P.EVT_AUDIO_COMMITTED})
    item_id = committed["item_id"]

    delta = await _next_event_of_types(client, {P.EVT_TRANSCRIPT_DELTA})
    assert delta["item_id"] == item_id
    assert delta["delta"].startswith("echo:")

    completed = await _next_event_of_types(client, {P.EVT_TRANSCRIPT_COMPLETED})
    assert completed["item_id"] == item_id
    assert completed["transcript"].startswith("echo:")


async def test_long_turn_chunked_appends_commit_cleanly(client):
    """A >1 MiB turn split across multiple binary appends must commit.

    Regression: the Pipecat wrapper used to send the whole segment in one
    websocket frame, hitting ``payload_too_large`` at ~32 s of 16 kHz PCM16.
    """
    await client.update_session(turn_detection=None)
    await _next_event_of_types(client, {P.EVT_SESSION_UPDATED})

    # 40 seconds of silence: 40 * 16000 * 2 = 1.28 MiB, > MAX_APPEND_BYTES.
    total_samples = 40 * P.AUDIO_SAMPLE_RATE_HZ
    payload = _pcm(total_samples)
    assert len(payload) > P.MAX_APPEND_BYTES

    chunk = 512 * 1024
    for i in range(0, len(payload), chunk):
        await client.send_audio(payload[i : i + chunk])
    await client.commit()

    committed = await _next_event_of_types(client, {P.EVT_AUDIO_COMMITTED, P.EVT_ERROR})
    assert committed["type"] == P.EVT_AUDIO_COMMITTED
    completed = await _next_event_of_types(client, {P.EVT_TRANSCRIPT_COMPLETED}, timeout=5.0)
    assert completed["transcript"].startswith("echo:")


async def test_commit_empty_buffer_errors(client):
    await client.commit()
    err = await _next_event_of_types(client, {P.EVT_ERROR})
    assert err["error"]["code"] == P.ErrorCode.BUFFER_EMPTY.value


async def test_base64_json_append_compat_path(client):
    await client.send_audio_base64(_pcm(800))
    await client.commit()
    committed = await _next_event_of_types(client, {P.EVT_AUDIO_COMMITTED})
    assert "item_id" in committed
    completed = await _next_event_of_types(client, {P.EVT_TRANSCRIPT_COMPLETED})
    # EchoBackend reports the decoded byte count, not the base64 length.
    assert completed["transcript"] == "echo:1600"


# ---------------------------------------------------------------------------
# turn_detection: null must disable server VAD
# ---------------------------------------------------------------------------


async def test_turn_detection_null_never_emits_speech_events(client):
    await client.update_session(turn_detection=None)
    await _next_event_of_types(client, {P.EVT_SESSION_UPDATED})
    await client.send_audio(_pcm(1600))
    await client.commit()
    seen_types: list[str] = []
    async for ev in client.events():
        seen_types.append(ev["type"])
        if ev["type"] == P.EVT_TRANSCRIPT_COMPLETED:
            break
    assert "input_audio_buffer.speech_started" not in seen_types
    assert "input_audio_buffer.speech_stopped" not in seen_types


# ---------------------------------------------------------------------------
# close vs cancel
# ---------------------------------------------------------------------------


async def test_session_close_drains_and_closes(client):
    await client.send_audio(_pcm(1600))
    await client.commit()
    await _next_event_of_types(client, {P.EVT_AUDIO_COMMITTED})
    await client.close_session()
    closed = await _next_event_of_types(client, {P.EVT_SESSION_CLOSED})
    assert closed["reason"] == "client_close"


async def test_session_cancel_discards_and_closes(client):
    await client.send_audio(_pcm(1600))
    await client.cancel()
    closed = await _next_event_of_types(client, {P.EVT_SESSION_CLOSED})
    assert closed["reason"] == "client_cancel"


# ---------------------------------------------------------------------------
# server.status
# ---------------------------------------------------------------------------


async def test_server_status_reply(client):
    await client.send_audio(_pcm(800))
    await client.status()
    status = await _next_event_of_types(client, {P.EVT_SERVER_STATUS})
    assert status["uncommitted_bytes"] == 1600
    # pid / rss_bytes expose process-level health without requiring the
    # caller to discover the server by pgrep / cmdline pattern. Both are
    # always present; absolute RSS values are platform-dependent so the
    # test only asserts the shape.
    assert isinstance(status["pid"], int) and status["pid"] > 0
    assert isinstance(status["rss_bytes"], int) and status["rss_bytes"] > 0


# ---------------------------------------------------------------------------
# Shutdown — must not wedge even with an active session
# ---------------------------------------------------------------------------


async def test_shutdown_with_active_session_returns_promptly():
    srv = TranscriptionServer(
        EchoBackend(),
        ServerConfig(
            host="127.0.0.1", port=0, reject_browser_origins=False, drain_timeout_seconds=2.0
        ),
    )
    await srv.start()
    port = srv.listening_port()
    c = TranscriptionClient(host="127.0.0.1", port=port)
    await c.connect()
    # Leave the client connected and idle; the server must still shut down.
    await asyncio.wait_for(srv.shutdown(), timeout=5.0)
    await c.close()


# ---------------------------------------------------------------------------
# Unix domain socket transport
# ---------------------------------------------------------------------------


async def test_shutdown_with_idle_connection_does_not_stall(tmp_path):
    """Regression: shutdown must close open sockets, not wait for idle client."""
    srv = TranscriptionServer(
        EchoBackend(),
        ServerConfig(
            host="127.0.0.1",
            port=0,
            reject_browser_origins=False,
            drain_timeout_seconds=30.0,  # huge; test proves we don't hit it
        ),
    )
    await srv.start()
    port = srv.listening_port()
    c = TranscriptionClient(host="127.0.0.1", port=port)
    await c.connect()
    # Client is connected and idle; shutdown must still return quickly
    # (well under drain_timeout_seconds) by actually closing the socket.
    start = asyncio.get_event_loop().time()
    await asyncio.wait_for(srv.shutdown(), timeout=5.0)
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 5.0
    await c.close()


async def test_shutdown_drains_in_flight_decode(client):
    """Decode tasks spawned by commit must be in the drain set."""
    # Slow backend via monkey-patched EchoBackend sleep is overkill for the
    # echo case; just verify the public API exposes the tracking set.
    # Smoke: commit + shutdown (the fixture tears down afterward).
    await client.send_audio(_pcm(800))
    await client.commit()
    # EchoBackend finishes quickly; we just want no assertion errors.
    await _next_event_of_types(client, {P.EVT_TRANSCRIPT_COMPLETED})


async def test_audio_append_rejected_while_decode_in_flight():
    """Binary append between commit and completion must error, not merge.

    EchoBackend completes decode nearly instantly, so racing an append
    between commit and completion is inherently flaky. Use a slow backend
    that holds the decode open long enough to guarantee the append arrives
    mid-flight, so we can actually assert the INVALID_EVENT rejection.
    """

    class _SlowStream:
        def __init__(self) -> None:
            self._buf = bytearray()
            self._released = asyncio.Event()

        async def feed(self, chunk: bytes) -> None:
            self._buf.extend(chunk)

        async def end(self) -> None:
            # Block until the test signals the decode can finish.
            await self._released.wait()

        async def cancel(self) -> None:
            self._released.set()

        async def events(self):
            yield TranscriptEvent(kind="completed", text=f"slow:{len(self._buf)}")

        def release(self) -> None:
            self._released.set()

    class _SlowBackend:
        backend_name = "echo"
        model = None

        def __init__(self) -> None:
            self.last_stream: _SlowStream | None = None

        async def start(self) -> None:
            return None

        async def open_stream(self, *, language: str | None = None) -> _SlowStream:
            self.last_stream = _SlowStream()
            return self.last_stream

        async def close(self) -> None:
            return None

    backend = _SlowBackend()
    srv = TranscriptionServer(
        backend,
        ServerConfig(host="127.0.0.1", port=0, reject_browser_origins=False),
    )
    await srv.start()
    try:
        port = srv.listening_port()
        c = TranscriptionClient(host="127.0.0.1", port=port)
        await c.connect()
        await c.send_audio(_pcm(800))
        await c.commit()
        # Wait for input_audio_buffer.committed so we know decode is running.
        await _next_event_of_types(c, {P.EVT_AUDIO_COMMITTED})
        # Now append while the slow decode is still blocked.
        await c.send_audio(_pcm(400))
        err = await _next_event_of_types(c, {P.EVT_ERROR})
        assert err["error"]["code"] == P.ErrorCode.INVALID_EVENT.value
        # Release so shutdown can drain cleanly.
        if backend.last_stream is not None:
            backend.last_stream.release()
        await c.close()
    finally:
        await srv.shutdown()


async def test_double_close_is_idempotent(client):
    await client.send_audio(_pcm(800))
    await client.commit()
    await _next_event_of_types(client, {P.EVT_AUDIO_COMMITTED})
    await client.close_session()
    closed = await _next_event_of_types(client, {P.EVT_SESSION_CLOSED})
    assert closed["reason"] == "client_close"
    # Send a second close — socket is already closed from the server side,
    # so send() may raise; the test is that nothing in the server explodes.
    try:
        await client.close_session()
    except Exception:
        pass


async def test_bearer_auth_requires_token():
    srv = TranscriptionServer(
        EchoBackend(),
        ServerConfig(
            host="127.0.0.1",
            port=0,
            reject_browser_origins=False,
            auth_token="s3cret",
        ),
    )
    await srv.start()
    try:
        port = srv.listening_port()
        # Wrong token rejected with 401 (not any exception).
        bad = TranscriptionClient(host="127.0.0.1", port=port, auth_token="wrong")
        with pytest.raises(websockets.exceptions.InvalidStatus) as exc:
            await bad.connect()
        assert exc.value.response.status_code == 401
        # Correct token accepted
        good = TranscriptionClient(host="127.0.0.1", port=port, auth_token="s3cret")
        hello = await good.connect()
        assert hello["type"] == P.EVT_SERVER_HELLO
        await good.close()
    finally:
        await srv.shutdown()


async def test_unix_socket_has_owner_only_permissions(monkeypatch):
    import stat

    # Binds under /tmp (root-owned), which R1 dir-enforcement rejects. This
    # test exercises UDS perms, not ancestor-dir enforcement, so neutralise the
    # check via the Phase-4-sanctioned seam.
    monkeypatch.setattr("stt_server.server._enforce_socket_dir_secure", lambda *a, **k: a[0])
    with tempfile.TemporaryDirectory(prefix="stt.", dir="/tmp") as d:
        sock = Path(d) / "s"
        srv = TranscriptionServer(EchoBackend(), ServerConfig(socket_path=str(sock)))
        await srv.start()
        try:
            mode = stat.S_IMODE(sock.stat().st_mode)
            # Must not grant group/other access under the V1 trust model.
            assert mode & 0o077 == 0, f"UDS world/group-accessible: {oct(mode)}"
        finally:
            await srv.shutdown()


async def test_unix_socket_start_creates_parent_directory(monkeypatch):
    # Binds under /tmp (root-owned), which R1 ownership enforcement rejects.
    # This test exercises parent-dir *creation*, so the stub keeps the mkdir
    # behaviour but drops the ancestor ownership/perms check (the part /tmp
    # trips). Sanctioned by the Phase-4 test-only seam.
    def _mkdir_only(path, trusted_root):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path  # mirror the real helper's bind-path return contract

    monkeypatch.setattr("stt_server.server._enforce_socket_dir_secure", _mkdir_only)
    with tempfile.TemporaryDirectory(prefix="stt.", dir="/tmp") as d:
        sock = Path(d) / "nested" / "path" / "s"
        srv = TranscriptionServer(EchoBackend(), ServerConfig(socket_path=str(sock)))
        await srv.start()
        try:
            assert sock.parent.is_dir()
            assert sock.exists()
        finally:
            await srv.shutdown()


async def test_unix_socket_transport(monkeypatch):
    # AF_UNIX paths on macOS cap at ~104 bytes; use a short /tmp path.
    # /tmp is root-owned so R1 dir-enforcement rejects it; this test exercises
    # UDS transport, not ancestor-dir enforcement, so neutralise the check via
    # the Phase-4-sanctioned seam.
    monkeypatch.setattr("stt_server.server._enforce_socket_dir_secure", lambda *a, **k: a[0])
    with tempfile.TemporaryDirectory(prefix="stt.", dir="/tmp") as d:
        sock = Path(d) / "s"
        srv = TranscriptionServer(
            EchoBackend(),
            ServerConfig(socket_path=str(sock)),
        )
        await srv.start()
        try:
            c = TranscriptionClient(socket_path=str(sock))
            hello = await c.connect()
            assert hello["type"] == P.EVT_SERVER_HELLO
            await c.send_audio(_pcm(800))
            await c.commit()
            completed = await _next_event_of_types(c, {P.EVT_TRANSCRIPT_COMPLETED})
            assert completed["transcript"] == "echo:1600"
            await c.close()
        finally:
            await srv.shutdown()


async def test_client_uri_overrides_socket_path():
    srv = TranscriptionServer(
        EchoBackend(),
        ServerConfig(host="127.0.0.1", port=0, reject_browser_origins=False),
    )
    await srv.start()
    try:
        port = srv.listening_port()
        assert port is not None
        client = TranscriptionClient(
            socket_path="/tmp/does-not-exist-koda-stt.sock",
            uri=f"ws://127.0.0.1:{port}/",
        )
        hello = await client.connect()
        assert hello["type"] == P.EVT_SERVER_HELLO
        await client.close()
    finally:
        await srv.shutdown()


# ---------------------------------------------------------------------------
# Auth contract — pins doc claims in README / AGENTS.md / architecture.md:
#   "STT_WS_TOKEN is enforced only when the server was started with a token;
#    TCP without a token logs a warning at startup."
# Prevents silent drift between the documented trust model and behavior.
# ---------------------------------------------------------------------------


async def test_tcp_without_token_emits_startup_warning(caplog):
    caplog.set_level("WARNING", logger="stt_server.server")
    srv = TranscriptionServer(
        EchoBackend(),
        ServerConfig(host="127.0.0.1", port=0, reject_browser_origins=False),
    )
    await srv.start()
    try:
        assert any(
            "TCP listener is running without --auth-token" in rec.message for rec in caplog.records
        ), "TCP server without token must warn at startup"
    finally:
        await srv.shutdown()


async def test_uds_without_token_does_not_warn(caplog, monkeypatch):
    caplog.set_level("WARNING", logger="stt_server.server")
    # Binds under a system temp dir (root-owned ancestor) which R1 rejects;
    # this test exercises the UDS-vs-TCP token warning, not ancestor-dir
    # enforcement, so neutralise the check via the Phase-4-sanctioned seam.
    monkeypatch.setattr("stt_server.server._enforce_socket_dir_secure", lambda *a, **k: a[0])
    with tempfile.TemporaryDirectory() as tmp:
        sock = str(Path(tmp) / "stt.sock")
        srv = TranscriptionServer(
            EchoBackend(),
            ServerConfig(socket_path=sock, reject_browser_origins=False),
        )
        await srv.start()
        try:
            assert not any(
                "TCP listener is running without --auth-token" in rec.message
                for rec in caplog.records
            ), "UDS mode is protected by file perms; must not warn about missing token"
        finally:
            await srv.shutdown()


async def test_tcp_rejects_missing_bearer_when_token_required():
    srv = TranscriptionServer(
        EchoBackend(),
        ServerConfig(
            host="127.0.0.1",
            port=0,
            auth_token="s3cret",
            reject_browser_origins=False,
        ),
    )
    await srv.start()
    try:
        port = srv.listening_port()
        client = TranscriptionClient(host="127.0.0.1", port=port)  # no token
        with pytest.raises(websockets.exceptions.InvalidStatus) as exc:
            await client.connect()
        assert exc.value.response.status_code == 401
    finally:
        await srv.shutdown()


# ---------------------------------------------------------------------------
# CLI dispatch regression tests
# ---------------------------------------------------------------------------


def _run_module(
    *args: str,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "stt_server", *args],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=cwd,
        env=env,
    )


def test_cli_top_level_help_lists_subcommands():
    r = _run_module("--help")
    assert r.returncode == 0, r.stderr
    assert "serve" in r.stdout
    assert "status" in r.stdout


def test_cli_status_help_shows_status_flags():
    r = _run_module("status", "--help")
    assert r.returncode == 0, r.stderr
    assert "--timeout" in r.stdout
    assert "--json" in r.stdout
    assert "--socket-path" in r.stdout


def test_cli_serve_help_shows_serve_flags():
    r = _run_module("serve", "--help")
    assert r.returncode == 0, r.stderr
    assert "--backend" in r.stdout
    assert "--model" in r.stdout


def test_cli_status_against_missing_server_exits_nonzero(tmp_path: Path):
    # Point at a socket path that does not exist; probe must fail fast.
    missing = tmp_path / "does-not-exist.sock"
    r = _run_module("status", "--socket-path", str(missing), "--timeout", "1.0")
    assert r.returncode == 1
    assert "stt_server:" in r.stderr


# ---------------------------------------------------------------------------
# Probe auth surface — regressions for:
#   * P1: client-mode auth must NOT fall back to the server-side
#         KODA_STT_AUTH_TOKEN (could mask bot 401s and leak the server
#         secret to a remote STT_WS_URI).
#   * P2: dotenv must load before the probe reads STT_WS_TOKEN even when
#         the caller passes explicit endpoint flags.
# ---------------------------------------------------------------------------


def test_resolve_auth_token_client_uses_stt_ws_token(monkeypatch):
    from stt_server.__main__ import _resolve_auth_token

    monkeypatch.setenv("STT_WS_TOKEN", "abc")
    monkeypatch.delenv("KODA_STT_AUTH_TOKEN", raising=False)
    assert _resolve_auth_token(None, client=True) == "abc"


def test_resolve_auth_token_client_ignores_server_token(monkeypatch):
    from stt_server.__main__ import _resolve_auth_token

    monkeypatch.delenv("STT_WS_TOKEN", raising=False)
    monkeypatch.setenv("PIPECAT_STT_AUTH_TOKEN", "server-secret")
    assert _resolve_auth_token(None, client=True) is None


def test_resolve_auth_token_client_prefers_stt_ws_token_over_server(monkeypatch):
    from stt_server.__main__ import _resolve_auth_token

    monkeypatch.setenv("STT_WS_TOKEN", "client-secret")
    monkeypatch.setenv("PIPECAT_STT_AUTH_TOKEN", "server-secret")
    assert _resolve_auth_token(None, client=True) == "client-secret"


def test_resolve_auth_token_serve_reads_pipecat_env(monkeypatch):
    # Canonical PIPECAT_STT_AUTH_TOKEN alone authenticates the serve path.
    from stt_server.__main__ import _resolve_auth_token

    monkeypatch.delenv("STT_WS_TOKEN", raising=False)
    monkeypatch.delenv("KODA_STT_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("PIPECAT_STT_AUTH_TOKEN", "pipecat-secret")
    assert _resolve_auth_token(None, client=False) == "pipecat-secret"


def test_resolve_auth_token_serve_pipecat_wins_over_koda(monkeypatch):
    # When both are set, the canonical PIPECAT_STT_AUTH_TOKEN takes precedence.
    from stt_server.__main__ import _resolve_auth_token

    monkeypatch.delenv("STT_WS_TOKEN", raising=False)
    monkeypatch.setenv("PIPECAT_STT_AUTH_TOKEN", "pipecat-secret")
    monkeypatch.setenv("KODA_STT_AUTH_TOKEN", "koda-secret")
    assert _resolve_auth_token(None, client=False) == "pipecat-secret"


def test_resolve_auth_token_serve_koda_alias_still_works(monkeypatch):
    # Regression: legacy KODA_STT_AUTH_TOKEN alone still authenticates serve.
    from stt_server.__main__ import _resolve_auth_token

    monkeypatch.delenv("STT_WS_TOKEN", raising=False)
    monkeypatch.delenv("PIPECAT_STT_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("KODA_STT_AUTH_TOKEN", "koda-secret")
    assert _resolve_auth_token(None, client=False) == "koda-secret"


def test_resolve_auth_token_probe_ignores_pipecat_server_token(monkeypatch):
    # The probe path reads only STT_WS_TOKEN, never the serve-side bearer.
    from stt_server.__main__ import _resolve_auth_token

    monkeypatch.delenv("STT_WS_TOKEN", raising=False)
    monkeypatch.setenv("PIPECAT_STT_AUTH_TOKEN", "pipecat-secret")
    assert _resolve_auth_token(None, client=True) is None


def test_resolve_auth_token_file_wins_in_client_mode(monkeypatch, tmp_path: Path):
    from stt_server.__main__ import _resolve_auth_token

    tok = tmp_path / "tok"
    tok.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("STT_WS_TOKEN", "from-env")
    assert _resolve_auth_token(str(tok), client=True) == "from-file"


def test_resolve_auth_token_empty_and_whitespace_are_treated_as_unset(monkeypatch):
    from stt_server.__main__ import _resolve_auth_token

    monkeypatch.setenv("STT_WS_TOKEN", "   ")
    monkeypatch.delenv("KODA_STT_AUTH_TOKEN", raising=False)
    assert _resolve_auth_token(None, client=True) is None


def test_resolve_probe_endpoint_loads_dotenv_even_with_explicit_socket(monkeypatch, tmp_path: Path):
    pytest.importorskip("dotenv")
    import argparse

    from stt_server.__main__ import _resolve_auth_token, _resolve_probe_endpoint

    (tmp_path / ".env").write_text("STT_WS_TOKEN=from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # The dotenv loader also tries ~/.secrets/ai.env; neutralize it by
    # pointing HOME at an empty directory for the duration of the test.
    empty_home = tmp_path / "home"
    empty_home.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.delenv("STT_WS_TOKEN", raising=False)
    monkeypatch.delenv("KODA_STT_AUTH_TOKEN", raising=False)

    ns = argparse.Namespace(
        socket_path=str(tmp_path / "x.sock"),
        host=None,
        port=None,
        uri=None,
        auth_token_file=None,
    )
    endpoint = _resolve_probe_endpoint(ns)
    assert endpoint["socket_path"] == str(tmp_path / "x.sock")
    assert _resolve_auth_token(None, client=True) == "from-dotenv"


async def test_cli_status_with_explicit_socket_reads_token_from_dotenv(tmp_path: Path, monkeypatch):
    pytest.importorskip("dotenv")
    # Binds the in-process server's socket directly under /tmp (root-owned),
    # which R1 dir-enforcement rejects; this test exercises the CLI token-probe
    # path, not ancestor-dir enforcement, so neutralise via the Phase-4 seam.
    monkeypatch.setattr("stt_server.server._enforce_socket_dir_secure", lambda *a, **k: a[0])
    sock = Path("/tmp") / f"stt-preflight-{os.getpid()}.sock"
    sock.unlink(missing_ok=True)
    srv = TranscriptionServer(
        EchoBackend(),
        ServerConfig(socket_path=str(sock), auth_token="probe-token"),
    )
    await srv.start()
    try:
        (tmp_path / ".env").write_text("STT_WS_TOKEN=probe-token\n", encoding="utf-8")
        # Scrub both auth vars so the probe has to pick the token up from
        # dotenv — the whole point of the P2 regression test.
        env = {
            k: v for k, v in os.environ.items() if k not in {"STT_WS_TOKEN", "KODA_STT_AUTH_TOKEN"}
        }
        # Isolate HOME to avoid ~/.secrets/ai.env contributing another
        # value; keep PATH/PYTHONPATH/etc. so `python -m stt_server` runs.
        env["HOME"] = str(tmp_path / "home")
        # cwd is tmp_path so the in-tree ``stt_server`` package is not on
        # sys.path automatically — add the repo root explicitly.
        repo_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = os.pathsep.join([repo_root, env.get("PYTHONPATH", "")]).rstrip(
            os.pathsep
        )
        (tmp_path / "home").mkdir(exist_ok=True)
        # Probe runs in a subprocess; hand it off to a thread so the in-
        # process server's event loop stays free to accept the connection.
        r = await asyncio.to_thread(
            _run_module,
            "status",
            "--socket-path",
            str(sock),
            "--timeout",
            "3.0",
            cwd=str(tmp_path),
            env=env,
        )
        assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        assert "stt_server: ok" in r.stdout
        # The status text output names the ASR behind the socket.
        assert "backend: echo" in r.stdout
    finally:
        await srv.shutdown()
        sock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Parakeet backend selection wiring
#
# Two concerns:
#   * _make_backend / _resolve_model / argparse choices accept ``parakeet``;
#     the ``--model`` default is backend-aware; the MLX path is unregressed.
#   * a stubbed ``ParakeetBackend`` driven through the real ``client`` server
#     fixture is wire-identical to the MLX/echo path for non-empty decode,
#     empty-text decode, and a raising backend (server-synthesised
#     ``transcript.failed``), and the ``MAX_UNCOMMITTED_*`` protocol ceiling
#     composes with the backend regardless of backend choice.
#
# ``parakeet_mlx`` is stubbed via ``sys.modules`` injection so no model
# downloads in CI — same technique as ``tests/test_parakeet_backend.py``.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_parakeet_mlx(monkeypatch):
    """Install a permissive fake ``parakeet_mlx`` so the real backend module
    can be imported/constructed in CI without a model download.

    Phase 2 exercises the ``_make_backend`` ``parakeet`` arm, which lazily
    imports ``stt_server.backends.parakeet``; that module's own lazy
    ``import parakeet_mlx`` lives inside ``start()``/decode, so for pure
    construction tests the stub is not strictly needed — but installing it
    keeps the tests robust if the import discipline ever regresses.
    """
    import types as _types

    fake = _types.ModuleType("parakeet_mlx")

    def _from_pretrained(model_id, *a, **kw):  # pragma: no cover - not driven here
        raise AssertionError("real model load attempted in a wiring test")

    fake.from_pretrained = _from_pretrained
    monkeypatch.setitem(sys.modules, "parakeet_mlx", fake)
    monkeypatch.delitem(sys.modules, "stt_server.backends.parakeet", raising=False)
    return fake


@pytest.fixture
def fake_mlx_audio(monkeypatch):
    """Install a permissive fake ``mlx_audio`` so the real ``nemotron`` backend
    module can be imported/constructed in CI without a model download.

    Mirrors ``fake_parakeet_mlx``: ``nemotron.py``'s own ``import mlx_audio``
    lives inside ``start()``/decode, so for pure construction tests the stub is
    not strictly needed — but installing it keeps the tests robust if the import
    discipline ever regresses.
    """
    import types as _types

    fake = _types.ModuleType("mlx_audio")
    fake_stt = _types.ModuleType("mlx_audio.stt")

    def _load(model_id, *a, **kw):  # pragma: no cover - not driven here
        raise AssertionError("real model load attempted in a wiring test")

    fake_stt.load = _load
    fake.stt = fake_stt
    monkeypatch.setitem(sys.modules, "mlx_audio", fake)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", fake_stt)
    monkeypatch.delitem(sys.modules, "stt_server.backends.nemotron", raising=False)
    return fake


# --- _make_backend / _resolve_model / argparse choices --------------------


def test_make_backend_parakeet_constructs_parakeet_backend(fake_parakeet_mlx):
    from stt_server.__main__ import _make_backend
    from stt_server.backends.parakeet import ParakeetBackend

    backend = _make_backend("parakeet", "fake-parakeet-model")
    assert isinstance(backend, ParakeetBackend)


def test_make_backend_unknown_still_systemexits():
    from stt_server.__main__ import _make_backend

    with pytest.raises(SystemExit):
        _make_backend("not-a-backend", "whatever")


def test_argparse_backend_choices_include_parakeet():
    """``--backend parakeet`` must be an accepted argparse choice."""
    r = _run_module("serve", "--help")
    assert r.returncode == 0, r.stderr
    # argparse renders the choice tuple in the --backend metavar/help.
    assert "parakeet" in r.stdout


def test_argparse_rejects_unknown_backend():
    """A backend outside the choice tuple must exit non-zero (argparse=2)."""
    r = _run_module("serve", "--backend", "bogus")
    assert r.returncode != 0
    assert "parakeet" in r.stderr or "invalid choice" in r.stderr


def test_resolve_model_parakeet_unset_uses_default_parakeet_model(fake_parakeet_mlx):
    from stt_server.__main__ import _resolve_model
    from stt_server.backends.parakeet import DEFAULT_PARAKEET_MODEL

    assert _resolve_model("parakeet", None) == DEFAULT_PARAKEET_MODEL


def test_resolve_model_mlx_unset_still_defaults_to_whisper_repo():
    """Regression: the MLX default must not shift when the parakeet arm lands."""
    from stt_server.__main__ import _resolve_model

    assert _resolve_model("mlx", None) == "mlx-community/whisper-large-v3-turbo"


def test_resolve_model_explicit_override_wins_for_parakeet(fake_parakeet_mlx):
    from stt_server.__main__ import _resolve_model

    assert _resolve_model("parakeet", "my-org/custom-parakeet") == "my-org/custom-parakeet"


def test_resolve_model_explicit_override_wins_for_mlx():
    from stt_server.__main__ import _resolve_model

    assert _resolve_model("mlx", "my-org/custom-whisper") == "my-org/custom-whisper"


def test_resolve_model_parakeet_with_whisper_repo_passes_through(fake_parakeet_mlx):
    """Decided behaviour (``__main__._resolve_model`` docstring): an explicit
    ``--model`` is passed through verbatim for any backend — no reject, no
    string-heuristic reclassification. ``--backend`` is the trust anchor and a
    mismatched repo id fails fast later in ``start()``/decode."""
    from stt_server.__main__ import _resolve_model

    whisper_repo = "mlx-community/whisper-large-v3-turbo"
    assert _resolve_model("parakeet", whisper_repo) == whisper_repo


def test_make_backend_parakeet_arm_does_not_import_parakeet_mlx():
    """Lean-base invariant: importing/constructing the ``parakeet`` arm of
    ``_make_backend`` must not transitively import ``parakeet_mlx``.

    Run in a clean subprocess with the ``parakeet`` extra assumed
    absent — ``parakeet_mlx`` is removed from ``sys.modules`` and import is
    blocked, so a transitive pull would raise. The base install must still
    construct the backend; the missing-extra failure belongs in ``start()``.
    """
    code = (
        "import sys, builtins\n"
        "sys.modules.pop('parakeet_mlx', None)\n"
        "_real_import = builtins.__import__\n"
        "def _blocked(name, *a, **k):\n"
        "    if name == 'parakeet_mlx' or name.startswith('parakeet_mlx.'):\n"
        "        raise AssertionError('parakeet_mlx imported at _make_backend seam')\n"
        "    return _real_import(name, *a, **k)\n"
        "builtins.__import__ = _blocked\n"
        "from stt_server.__main__ import _make_backend\n"
        "b = _make_backend('parakeet', 'fake-model')\n"
        "assert type(b).__name__ == 'ParakeetBackend'\n"
        "assert 'parakeet_mlx' not in sys.modules\n"
        "print('OK')\n"
    )
    env = dict(os.environ)
    repo_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join([repo_root, env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "OK" in r.stdout


def test_make_backend_nemotron_constructs_nemotron_backend(fake_mlx_audio):
    from stt_server.__main__ import _make_backend
    from stt_server.backends.nemotron import NemotronBackend

    backend = _make_backend("nemotron", "fake-nemotron-model")
    assert isinstance(backend, NemotronBackend)


def test_argparse_backend_choices_include_nemotron():
    """``--backend nemotron`` must be an accepted argparse choice."""
    r = _run_module("serve", "--help")
    assert r.returncode == 0, r.stderr
    # argparse renders the choice tuple in the --backend metavar/help.
    assert "nemotron" in r.stdout


def test_argparse_rejects_unknown_backend_with_nemotron_present():
    """A backend outside the choice tuple must still exit non-zero now that
    ``nemotron`` is an accepted choice — parity with the parakeet reject test."""
    r = _run_module("serve", "--backend", "bogus")
    assert r.returncode != 0
    assert "nemotron" in r.stderr or "invalid choice" in r.stderr


def test_resolve_model_nemotron_unset_uses_default_nemotron_model(fake_mlx_audio):
    from stt_server.__main__ import _resolve_model
    from stt_server.backends.nemotron import DEFAULT_NEMOTRON_MODEL

    assert _resolve_model("nemotron", None) == DEFAULT_NEMOTRON_MODEL


def test_resolve_model_explicit_override_wins_for_nemotron(fake_mlx_audio):
    from stt_server.__main__ import _resolve_model

    assert _resolve_model("nemotron", "my-org/custom-nemotron") == "my-org/custom-nemotron"


def test_make_backend_and_resolve_model_nemotron_do_not_import_mlx_audio():
    """Lean-base invariant: the ``nemotron`` arm of ``_make_backend`` AND
    ``_resolve_model`` must not transitively import ``mlx_audio``.

    Run in a clean subprocess with the ``nemotron`` extra assumed absent —
    ``mlx_audio`` is removed from ``sys.modules`` and import is blocked, so a
    transitive pull would raise. The base install must still construct the
    backend and resolve the default model; the missing-extra failure belongs in
    ``start()``.
    """
    code = (
        "import sys, builtins\n"
        "sys.modules.pop('mlx_audio', None)\n"
        "_real_import = builtins.__import__\n"
        "def _blocked(name, *a, **k):\n"
        "    if name == 'mlx_audio' or name.startswith('mlx_audio.'):\n"
        "        raise AssertionError('mlx_audio imported at nemotron seam')\n"
        "    return _real_import(name, *a, **k)\n"
        "builtins.__import__ = _blocked\n"
        "from stt_server.__main__ import _make_backend, _resolve_model\n"
        "from stt_server.backends.nemotron import DEFAULT_NEMOTRON_MODEL\n"
        "m = _resolve_model('nemotron', None)\n"
        "assert m == DEFAULT_NEMOTRON_MODEL, m\n"
        "b = _make_backend('nemotron', m)\n"
        "assert type(b).__name__ == 'NemotronBackend'\n"
        "assert 'mlx_audio' not in sys.modules\n"
        "print('OK')\n"
    )
    env = dict(os.environ)
    repo_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join([repo_root, env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "OK" in r.stdout


# --- Wire-parity: stubbed ParakeetBackend through the server fixture -------
#
# These mirror ``_SlowBackend`` (above) — a minimal duck-typed backend driven
# through the real ``client`` server fixture so we assert the bytes on the
# wire, not just ``TranscriptEvent`` objects.


class _StubParakeetStream:
    """A ``BackendStream``-shaped stub standing in for ``_ParakeetStream``.

    ``text`` is the decoded transcript; an empty string exercises the
    near-silence path (``completed`` only, no ``delta``). ``raise_exc``, when
    set, makes ``end()`` raise — the server's ``except`` arm then synthesises
    ``transcript.failed``.
    """

    def __init__(self, *, text: str, raise_exc: BaseException | None) -> None:
        self._buf = bytearray()
        self._text = text
        self._raise_exc = raise_exc
        self._cancelled = False

    async def feed(self, chunk: bytes) -> None:
        self._buf.extend(chunk)

    async def end(self) -> None:
        if self._raise_exc is not None:
            raise self._raise_exc

    async def cancel(self) -> None:
        self._cancelled = True

    async def events(self):
        if self._cancelled:
            return
        # Match _MLXStream / _ParakeetStream: delta only for non-empty text.
        if self._text:
            yield TranscriptEvent(kind="delta", text=self._text)
        yield TranscriptEvent(kind="completed", text=self._text)


class _StubParakeetBackend:
    """A ``TranscriptionBackend``-shaped stub for the Parakeet backend."""

    backend_name = "parakeet"
    model = "stub-parakeet"

    def __init__(self, *, text: str = "parakeet decoded text", raise_exc=None) -> None:
        self._text = text
        self._raise_exc = raise_exc

    async def start(self) -> None:
        return None

    async def open_stream(self, *, language: str | None = None) -> _StubParakeetStream:
        return _StubParakeetStream(text=self._text, raise_exc=self._raise_exc)

    async def close(self) -> None:
        return None


async def _serve_with_backend(backend):
    """Start a server on an ephemeral port with the given backend; yield a
    connected client. Caller is responsible for nothing — teardown is here."""
    srv = TranscriptionServer(
        backend,
        ServerConfig(host="127.0.0.1", port=0, reject_browser_origins=False),
    )
    await srv.start()
    port = srv.listening_port()
    c = TranscriptionClient(host="127.0.0.1", port=port)
    await c.connect()
    return srv, c


async def test_parakeet_nonempty_decode_is_wire_identical_to_mlx_path():
    """Non-empty Parakeet decode -> ``delta`` + ``completed`` with the same
    event names, field set, and ordering as the echo/MLX path."""
    srv, c = await _serve_with_backend(_StubParakeetBackend(text="hello from parakeet"))
    try:
        await c.update_session(turn_detection=None)
        await _next_event_of_types(c, {P.EVT_SESSION_UPDATED})
        await c.send_audio(_pcm(1600))
        await c.commit()

        committed = await _next_event_of_types(c, {P.EVT_AUDIO_COMMITTED})
        item_id = committed["item_id"]

        delta = await _next_event_of_types(c, {P.EVT_TRANSCRIPT_DELTA})
        assert delta["type"] == P.EVT_TRANSCRIPT_DELTA
        assert delta["item_id"] == item_id
        assert delta["content_index"] == 0
        assert delta["delta"] == "hello from parakeet"
        assert set(delta) == {"type", "event_id", "item_id", "content_index", "delta"}

        completed = await _next_event_of_types(c, {P.EVT_TRANSCRIPT_COMPLETED})
        assert completed["type"] == P.EVT_TRANSCRIPT_COMPLETED
        assert completed["item_id"] == item_id
        assert completed["content_index"] == 0
        assert completed["transcript"] == "hello from parakeet"
        assert set(completed) == {"type", "event_id", "item_id", "content_index", "transcript"}
    finally:
        await c.close()
        await srv.shutdown()


async def test_backend_identity_surfaced_in_hello_and_status():
    """server.hello AND server.status carry the backend identity, so a client
    (the A/B benchmark, the bot) can verify which ASR is actually behind a
    socket instead of trusting the socket path."""
    srv, c = await _serve_with_backend(_StubParakeetBackend())
    try:
        # _serve_with_backend already consumed the hello; request status and
        # check the mirrored identity there.
        await c.status()
        status = await _next_event_of_types(c, {P.EVT_SERVER_STATUS})
        assert status["backend"] == {"name": "parakeet", "model": "stub-parakeet"}
    finally:
        await c.close()
        await srv.shutdown()


async def test_parakeet_empty_text_decode_emits_completed_only():
    """Near-silence: empty decode -> ``completed`` only, no ``delta`` —
    byte-identical to the MLX empty-text contract."""
    srv, c = await _serve_with_backend(_StubParakeetBackend(text=""))
    try:
        await c.update_session(turn_detection=None)
        await _next_event_of_types(c, {P.EVT_SESSION_UPDATED})
        await c.send_audio(_pcm(1600))
        await c.commit()

        seen: list[str] = []
        async for ev in c.events():
            seen.append(ev["type"])
            if ev["type"] == P.EVT_TRANSCRIPT_COMPLETED:
                assert ev["transcript"] == ""
                break
        assert P.EVT_TRANSCRIPT_DELTA not in seen, "empty-text decode must not emit a delta"
        assert P.EVT_TRANSCRIPT_COMPLETED in seen
    finally:
        await c.close()
        await srv.shutdown()


async def test_parakeet_raising_backend_synthesises_transcript_failed():
    """A raising Parakeet decode -> server synthesises ``transcript.failed``
    with ``error.code`` BACKEND_ERROR, ``error.type``, ``item_id``,
    ``content_index`` — byte-identical to the MLX raising path."""
    srv, c = await _serve_with_backend(
        _StubParakeetBackend(raise_exc=RuntimeError("metal OOM mid-decode"))
    )
    try:
        await c.update_session(turn_detection=None)
        await _next_event_of_types(c, {P.EVT_SESSION_UPDATED})
        await c.send_audio(_pcm(1600))
        await c.commit()

        committed = await _next_event_of_types(c, {P.EVT_AUDIO_COMMITTED})
        item_id = committed["item_id"]

        failed = await _next_event_of_types(c, {P.EVT_TRANSCRIPT_FAILED})
        assert failed["type"] == P.EVT_TRANSCRIPT_FAILED
        assert failed["item_id"] == item_id
        assert failed["content_index"] == 0
        assert failed["error"]["code"] == P.ErrorCode.BACKEND_ERROR.value
        # error.type is the coarse OpenAI-shaped group; present and non-empty.
        assert failed["error"]["type"]
        # The raw exception text must not leak to the wire.
        assert "metal OOM" not in json.dumps(failed)
    finally:
        await c.close()
        await srv.shutdown()


async def test_parakeet_protocol_ceiling_rejects_over_300s_commit():
    """A >300 s commit is rejected with ``BUFFER_OVERFLOW`` before reaching
    the backend, regardless of backend choice."""
    # A backend whose decode would raise if ever reached — proves the
    # rejection happens at the protocol layer, not in the backend.
    sentinel = _StubParakeetBackend(raise_exc=AssertionError("backend reached past the ceiling"))
    srv, c = await _serve_with_backend(sentinel)
    try:
        await c.update_session(turn_detection=None)
        await _next_event_of_types(c, {P.EVT_SESSION_UPDATED})

        # 301 s of 16 kHz PCM16 > MAX_UNCOMMITTED_BYTES (300 s ceiling).
        over_ceiling = (P.MAX_UNCOMMITTED_SECONDS + 1) * P.AUDIO_SAMPLE_RATE_HZ
        payload = _pcm(over_ceiling)
        assert len(payload) > P.MAX_UNCOMMITTED_BYTES

        # Send every append, then commit; the server rejects the session with
        # BUFFER_OVERFLOW once the uncommitted buffer crosses the ceiling. The
        # rejection lands at the protocol layer — the backend is never reached.
        chunk = 512 * 1024
        for i in range(0, len(payload), chunk):
            await c.send_audio(payload[i : i + chunk])
        await c.commit()

        err = await _next_event_of_types(c, {P.EVT_ERROR, P.EVT_AUDIO_COMMITTED}, timeout=10.0)
        assert err["type"] == P.EVT_ERROR, "over-ceiling commit must be rejected, not accepted"
        assert err["error"]["code"] == P.ErrorCode.BUFFER_OVERFLOW.value
    finally:
        await c.close()
        await srv.shutdown()


async def test_parakeet_24_to_300s_utterance_reaches_backend_intact():
    """A 24-300 s utterance (past Parakeet's ~24 s native chunk window, under
    the 300 s protocol ceiling) is *not* rejected — it reaches the backend and
    produces the normal ``delta`` + ``completed`` wire output."""
    srv, c = await _serve_with_backend(_StubParakeetBackend(text="long utterance decoded"))
    try:
        await c.update_session(turn_detection=None)
        await _next_event_of_types(c, {P.EVT_SESSION_UPDATED})

        # 60 s of 16 kHz PCM16 — well inside the 300 s ceiling, well past 24 s.
        payload = _pcm(60 * P.AUDIO_SAMPLE_RATE_HZ)
        assert len(payload) < P.MAX_UNCOMMITTED_BYTES
        chunk = 512 * 1024
        for i in range(0, len(payload), chunk):
            await c.send_audio(payload[i : i + chunk])
        await c.commit()

        committed = await _next_event_of_types(c, {P.EVT_AUDIO_COMMITTED, P.EVT_ERROR})
        assert committed["type"] == P.EVT_AUDIO_COMMITTED, "24-300s utterance must not be rejected"
        completed = await _next_event_of_types(c, {P.EVT_TRANSCRIPT_COMPLETED}, timeout=5.0)
        assert completed["transcript"] == "long utterance decoded"
    finally:
        await c.close()
        await srv.shutdown()


async def test_cli_status_client_does_not_use_server_only_token(tmp_path: Path, monkeypatch):
    """P1 regression at the CLI boundary: KODA_STT_AUTH_TOKEN alone must
    not authenticate the probe — otherwise a health check can report ok
    while the bot (which only reads STT_WS_TOKEN) still 401s."""
    # Binds the in-process server's socket directly under /tmp (root-owned),
    # which R1 dir-enforcement rejects; this test exercises the client-vs-server
    # token contract, not ancestor-dir enforcement, so neutralise via the
    # Phase-4-sanctioned seam.
    monkeypatch.setattr("stt_server.server._enforce_socket_dir_secure", lambda *a, **k: a[0])
    sock = Path("/tmp") / f"stt-preflight-p1-{os.getpid()}.sock"
    sock.unlink(missing_ok=True)
    srv = TranscriptionServer(
        EchoBackend(),
        ServerConfig(socket_path=str(sock), auth_token="probe-token"),
    )
    await srv.start()
    try:
        env = {
            k: v for k, v in os.environ.items() if k not in {"STT_WS_TOKEN", "KODA_STT_AUTH_TOKEN"}
        }
        env["KODA_STT_AUTH_TOKEN"] = "probe-token"
        env["HOME"] = str(tmp_path / "home")
        repo_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = os.pathsep.join([repo_root, env.get("PYTHONPATH", "")]).rstrip(
            os.pathsep
        )
        (tmp_path / "home").mkdir(exist_ok=True)
        r = await asyncio.to_thread(
            _run_module,
            "status",
            "--socket-path",
            str(sock),
            "--timeout",
            "3.0",
            cwd=str(tmp_path),
            env=env,
        )
        assert r.returncode == 1, (
            f"probe should reject server-only token in client mode; "
            f"stdout={r.stdout!r} stderr={r.stderr!r}"
        )
    finally:
        await srv.shutdown()
        sock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Phase 1 — Parent-directory enforcement (R1).
#
# Pins the contract of the private helper
#   _enforce_socket_dir_secure(path: Path, trusted_root: Path)
# which walks every directory component from the socket's bind directory up to
# and including ``trusted_root`` requiring ``st_uid == os.geteuid()`` and no
# group/other write bits (sticky-bit dirs excepted), creating any missing
# socket directories ``0700`` (then re-stat-verifying, since mkdir's mode is
# umask-masked). On any failure it raises a clear exception naming the offending
# component; it MUST NOT chmod/chown a pre-existing directory it does not own.
#
# These call the helper directly (no event loop / bind needed). ``tempfile``
# creates the trusted root via ``mkdtemp`` which is reliably ``0700``, so the
# root itself passes the walk on single-uid CI.
# ---------------------------------------------------------------------------


def _make_trusted_root() -> Path:
    """A 0700, owner-owned temp dir suitable as the trusted root of the walk."""
    return Path(tempfile.mkdtemp(prefix="stt-dirsec."))


def test_enforce_socket_dir_creates_missing_socket_dir_0700():
    # (a)+(c): an absent socket bind directory is created 0700 and the helper
    # succeeds (no exception). The walk through the trusted root must pass.
    import shutil
    import stat as _stat

    from stt_server.server import _enforce_socket_dir_secure

    root = _make_trusted_root()
    try:
        bind_dir = root / "pipecat-stt"
        sock = bind_dir / "s"
        assert not bind_dir.exists()

        _enforce_socket_dir_secure(sock, root)  # must not raise

        assert bind_dir.is_dir(), "helper must create the missing socket directory"
        mode = _stat.S_IMODE(bind_dir.stat().st_mode)
        assert mode == 0o700, f"socket dir created as {oct(mode)}, expected 0o700"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_enforce_socket_dir_created_modes_are_0700_under_loose_umask():
    # (c): even under a permissive process umask the created dirs end up 0700
    # (the helper must verify/repair, not trust the umask-masked mkdir mode).
    import shutil
    import stat as _stat

    from stt_server.server import _enforce_socket_dir_secure

    root = _make_trusted_root()
    prior = os.umask(0o022)
    try:
        bind_dir = root / "nested"
        sock = bind_dir / "s"

        _enforce_socket_dir_secure(sock, root)

        mode = _stat.S_IMODE(bind_dir.stat().st_mode)
        assert mode == 0o700, f"created dir is {oct(mode)} under umask 0o022, expected 0o700"
    finally:
        os.umask(prior)
        shutil.rmtree(root, ignore_errors=True)


def test_enforce_socket_dir_refuses_group_writable_parent_dir():
    # (b): a pre-existing group/other-writable ancestor in the walk must make
    # the helper refuse with an actionable error naming the offending path.
    #
    # NOTE on the fixture mode: R1 / the Acceptance Criteria define the failure
    # as "group/other-writable" (st_mode & 0o022 != 0). A plain 0o755 dir is
    # NOT group/other-writable, so we use 0o775 (group-writable) which refuses
    # under both the literal `& 0o022` rule and any stricter `& 0o077` reading.
    import shutil

    from stt_server.server import _enforce_socket_dir_secure

    root = _make_trusted_root()
    try:
        loose = root / "loose"
        loose.mkdir()
        os.chmod(loose, 0o775)  # group-writable: defeats the trust boundary
        sock = loose / "s"

        with pytest.raises((ValueError, OSError)) as exc:
            _enforce_socket_dir_secure(sock, root)

        msg = str(exc.value)
        assert str(loose) in msg, f"error must name the offending path; got: {msg!r}"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_cli_serve_refuses_group_writable_socket_dir_with_exit1():
    # (b) CLI surface: a group-writable socket parent dir must surface as
    # "stt_server: <msg>" on stderr + exit code 1, not a bare traceback.
    # Uses the echo backend so `serve` does not load a heavy model and fails
    # fast at the dir-enforcement check before binding.
    import shutil

    root = Path(tempfile.mkdtemp(prefix="stt-cli-dirsec."))
    try:
        loose = root / "loose"
        loose.mkdir()
        os.chmod(loose, 0o777)  # world+group-writable parent
        sock = loose / "s.sock"

        r = _run_module(
            "serve",
            "--backend",
            "echo",
            "--socket-path",
            str(sock),
        )
        assert r.returncode == 1, f"expected exit 1; stdout={r.stdout!r} stderr={r.stderr!r}"
        assert "stt_server:" in r.stderr, f"expected actionable error, got: {r.stderr!r}"
        assert "Traceback" not in r.stderr, f"must not leak a bare traceback: {r.stderr!r}"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_enforce_socket_dir_foreign_owner_ancestor_rejects_without_chmod_chown(monkeypatch):
    # (d): a grandparent dir reporting a foreign st_uid must be rejected, and
    # the helper MUST NOT attempt to chmod/chown a directory it does not own.
    import shutil

    from stt_server.server import _enforce_socket_dir_secure

    root = _make_trusted_root()
    try:
        # Pre-create a fully valid 0700 chain so no mkdir happens; the only
        # failure is the monkeypatched foreign ownership of `child`.
        child = root / "child"
        child.mkdir(mode=0o700)
        bind_dir = child / "gc"
        bind_dir.mkdir(mode=0o700)
        sock = bind_dir / "s"

        real_stat = os.stat
        foreign_uid = os.geteuid() + 1

        def fake_stat(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            try:
                target = os.path.realpath(os.fspath(path))
            except TypeError:
                return st  # fd-based stat: leave untouched
            if target == os.path.realpath(child):
                fields = list(st)  # the 10 canonical stat fields
                fields[4] = foreign_uid  # st_uid
                return os.stat_result(fields)
            return st

        chmod_calls: list = []
        chown_calls: list = []
        monkeypatch.setattr(os, "stat", fake_stat)
        monkeypatch.setattr(os, "chmod", lambda *a, **k: chmod_calls.append(a))
        monkeypatch.setattr(os, "chown", lambda *a, **k: chown_calls.append(a))

        with pytest.raises((ValueError, OSError)):
            _enforce_socket_dir_secure(sock, root)

        # Invariant: never touch ownership/mode of a dir we do not own.
        assert chmod_calls == [], f"helper chmod'd an unowned dir: {chmod_calls!r}"
        assert chown_calls == [], f"helper chown'd an unowned dir: {chown_calls!r}"
    finally:
        monkeypatch.undo()
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Phase 3 — UDS peer-credential gate in _process_request (CI seam)
#
# The server authenticates a UDS peer by its kernel-supplied uid before the
# WebSocket handshake completes (R2). Every fail-closed path — missing
# transport socket, resolver returning None, resolver raising, or a uid that
# mismatches os.geteuid() — must return ``connection.respond(403, "peer not
# permitted\n")`` rather than allow or leak an exception. A same-uid peer
# (the only legitimate case under the same-uid deployment precondition) must
# pass the gate untouched.
#
# Two layers, per the plan's Testing Notes:
#   * integration: bind a real UDS server (dir-enforcement neutralised via the
#     sanctioned seam so it can bind under /tmp) and connect with the client;
#   * unit: drive ``_process_request`` directly with fakes for the cases that
#     cannot be staged with a real same-uid socket (None transport socket,
#     resolver raising).
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Minimal asyncio-transport stand-in exposing only get_extra_info."""

    def __init__(self, sock):
        self._sock = sock

    def get_extra_info(self, name, default=None):
        if name == "socket":
            return self._sock
        return default


class _FakeConnection:
    """Minimal ``ServerConnection`` stand-in for _process_request unit tests.

    ``respond(status, body)`` records the call and returns a sentinel so the
    test can assert _process_request returned the reject response (the
    websockets contract: returning a respond() result rejects the handshake).
    """

    def __init__(self, sock):
        self.transport = _FakeTransport(sock)
        self.responses: list[tuple[int, str]] = []

    def respond(self, status, body):
        self.responses.append((status, body))
        return ("RESPOND", status, body)


class _FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}


def _uds_server_for_unit_test() -> TranscriptionServer:
    """A TranscriptionServer in UDS mode WITHOUT binding.

    ``_process_request`` only reads ``self._config.socket_path`` to decide the
    gate runs, so a config with any socket_path is enough — no start() needed.
    """
    return TranscriptionServer(
        EchoBackend(),
        ServerConfig(socket_path="/tmp/peercred-unit-test.sock"),
    )


async def test_uds_auth_foreign_uid_rejected_with_403(monkeypatch):
    """A UDS peer whose resolved uid != os.geteuid() is rejected pre-handshake.

    Bind a real UDS server (dir-enforcement neutralised via the sanctioned
    seam so it can bind under /tmp), force the resolver to report a foreign
    uid, and assert the client connect raises InvalidStatus(403). Mirrors the
    existing 401 bearer-auth test's InvalidStatus handling.
    """
    monkeypatch.setattr("stt_server.server._enforce_socket_dir_secure", lambda *a, **k: a[0])
    # Foreign uid: resolver claims the peer is someone other than us.
    monkeypatch.setattr("stt_server.server.peer_uid", lambda sock: os.geteuid() + 1)
    with tempfile.TemporaryDirectory(prefix="stt.", dir="/tmp") as d:
        sock = Path(d) / "s"
        srv = TranscriptionServer(EchoBackend(), ServerConfig(socket_path=str(sock)))
        await srv.start()
        try:
            c = TranscriptionClient(socket_path=str(sock))
            with pytest.raises(websockets.exceptions.InvalidStatus) as exc:
                await c.connect()
            assert exc.value.response.status_code == 403
            # Body assertion is best-effort: InvalidStatus exposes the response,
            # whose body may be bytes or absent depending on websockets version.
            # status_code == 403 is the load-bearing assertion (per the plan).
            body = getattr(exc.value.response, "body", None)
            if body:
                body_bytes = body if isinstance(body, (bytes, bytearray)) else body.encode()
                assert b"peer not permitted" in bytes(body_bytes)
        finally:
            await srv.shutdown()


async def test_uds_auth_peercred_missing_transport_socket_rejects_with_403():
    """A UDS handshake whose transport has no underlying socket must 403.

    Unit-drives _process_request with a fake connection whose
    ``transport.get_extra_info("socket")`` returns None. The gate must return
    the 403 respond() result (fail closed) and NOT raise (it must not call
    peer_uid(None), which would AttributeError).
    """
    srv = _uds_server_for_unit_test()
    conn = _FakeConnection(sock=None)
    result = await srv._process_request(conn, _FakeRequest())
    assert conn.responses == [(403, "peer not permitted\n")]
    assert result == ("RESPOND", 403, "peer not permitted\n")


async def test_uds_auth_peer_uid_resolver_raises_rejects_with_403(monkeypatch):
    """If the peer_uid resolver raises (e.g. OSError), the gate must 403, not
    leak the exception. Provide a non-None AF_UNIX socket so the gate reaches
    the resolver call, then force the resolver to raise.
    """
    import socket as _socket

    def _raise(_sock):
        raise OSError("getpeereid failed")

    monkeypatch.setattr("stt_server.server.peer_uid", _raise)
    srv = _uds_server_for_unit_test()

    class _FakeUnixSock:
        family = _socket.AF_UNIX

    conn = _FakeConnection(sock=_FakeUnixSock())
    result = await srv._process_request(conn, _FakeRequest())
    assert conn.responses == [(403, "peer not permitted\n")]
    assert result == ("RESPOND", 403, "peer not permitted\n")


async def test_uds_auth_peer_uid_same_uid_real_resolver_completes_handshake(monkeypatch):
    """Same-uid regression with the REAL resolver (no peer_uid stub).

    Bind a real UDS server (dir-enforcement neutralised) and connect as the
    owning uid. The handshake must complete and a normal session work — this
    catches a silently-None transport socket that would otherwise fail closed.
    Kept to a single connection; the N-concurrent case is Phase 4.
    """
    monkeypatch.setattr("stt_server.server._enforce_socket_dir_secure", lambda *a, **k: a[0])
    with tempfile.TemporaryDirectory(prefix="stt.", dir="/tmp") as d:
        sock = Path(d) / "s"
        srv = TranscriptionServer(EchoBackend(), ServerConfig(socket_path=str(sock)))
        await srv.start()
        try:
            c = TranscriptionClient(socket_path=str(sock))
            hello = await c.connect()
            assert hello["type"] == P.EVT_SERVER_HELLO
            # A real session works end-to-end through the gate.
            await c.send_audio(_pcm(800))
            await c.commit()
            completed = await _next_event_of_types(c, {P.EVT_TRANSCRIPT_COMPLETED})
            assert completed["transcript"] == "echo:1600"
            await c.close()
        finally:
            await srv.shutdown()


async def test_uds_multi_connection_same_uid_all_handshake(monkeypatch):
    """Phase 4 CI-safe mirror: N concurrent same-uid sessions all pass the gate.

    Opens N concurrent ``TranscriptionClient`` sessions as the owning uid against
    a single real UDS server (dir-enforcement neutralised via the sanctioned seam
    so it binds under /tmp). All N must complete the handshake (``server.hello``)
    and stream a normal echo exchange — a regression guard that the peer-cred gate
    (Phase 3) did not break the normal path under concurrency.

    The REAL resolver runs: ``peer_uid`` is deliberately NOT stubbed, so each
    accepted connection had its uid resolved by the kernel-backed resolver and
    compared equal to ``os.geteuid()``; a silently-``None`` transport would fail
    closed (403) and the handshake would raise rather than succeed. To pin the
    "real resolver == geteuid()" invariant directly (not just transitively via a
    successful handshake), we also assert ``peer_uid`` on a live AF_UNIX
    ``socketpair()`` end returns ``os.geteuid()`` — the same assertion the
    resolver unit test uses, exercised here against the production import path.
    """
    import socket as _socket

    from stt_server.server import peer_uid as _server_peer_uid

    # Direct real-resolver assertion on a connected AF_UNIX socketpair (both ends
    # are this process -> same uid). Catches a width/signature regression or a
    # silently-None resolver before we even look at the server.
    a, b = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        assert _server_peer_uid(a) == os.geteuid()
        assert _server_peer_uid(b) == os.geteuid()
    finally:
        a.close()
        b.close()

    # Bind one real UDS server; do NOT stub peer_uid so the real gate runs for
    # every connection.
    monkeypatch.setattr("stt_server.server._enforce_socket_dir_secure", lambda *a, **k: a[0])
    n = 4
    with tempfile.TemporaryDirectory(prefix="stt.", dir="/tmp") as d:
        sock = Path(d) / "s"
        srv = TranscriptionServer(EchoBackend(), ServerConfig(socket_path=str(sock)))
        await srv.start()
        try:

            async def _one_session(idx: int) -> None:
                c = TranscriptionClient(socket_path=str(sock))
                hello = await c.connect()
                assert hello["type"] == P.EVT_SERVER_HELLO
                assert hello["protocol_version"] == P.PROTOCOL_VERSION
                try:
                    # A full echo exchange proves the session works post-handshake.
                    await c.send_audio(_pcm(800))
                    await c.commit()
                    completed = await _next_event_of_types(c, {P.EVT_TRANSCRIPT_COMPLETED})
                    assert completed["transcript"] == "echo:1600"
                finally:
                    await c.close()

            # All N concurrently — surfaces any per-connection gate races.
            await asyncio.gather(*(_one_session(i) for i in range(n)))
        finally:
            await srv.shutdown()
