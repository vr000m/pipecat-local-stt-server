"""Tests for the standalone ``stt_server`` package."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from stt_server import (
    EchoBackend,
    ServerConfig,
    TranscriptionClient,
    TranscriptionServer,
)
from stt_server import protocol as P


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
