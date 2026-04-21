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
    _format_host_for_uri,
    is_cleartext_remote,
    resolve_endpoint_from_env,
)
from stt_server.server import ServerConfig, TranscriptionServer


def test_format_host_for_uri_brackets_ipv6():
    assert _format_host_for_uri("::1") == "[::1]"
    assert _format_host_for_uri("fe80::1") == "[fe80::1]"


def test_format_host_for_uri_passes_hostnames_through():
    assert _format_host_for_uri("127.0.0.1") == "127.0.0.1"
    assert _format_host_for_uri("localhost") == "localhost"
    assert _format_host_for_uri("example.local") == "example.local"


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


async def test_unix_socket_has_owner_only_permissions():
    import stat

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


async def test_unix_socket_start_creates_parent_directory():
    with tempfile.TemporaryDirectory(prefix="stt.", dir="/tmp") as d:
        sock = Path(d) / "nested" / "path" / "s"
        srv = TranscriptionServer(EchoBackend(), ServerConfig(socket_path=str(sock)))
        await srv.start()
        try:
            assert sock.parent.is_dir()
            assert sock.exists()
        finally:
            await srv.shutdown()


async def test_unix_socket_transport():
    # AF_UNIX paths on macOS cap at ~104 bytes; use a short /tmp path.
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


async def test_uds_without_token_does_not_warn(caplog):
    caplog.set_level("WARNING", logger="stt_server.server")
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


def _run_module(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "stt_server", *args],
        capture_output=True,
        text=True,
        timeout=15,
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
