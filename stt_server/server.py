"""WebSocket server runtime for the local transcription service.

Lifecycle summary:

- TCP or Unix-socket listener, accepting one WebSocket per transcription
  session.
- On connect: send ``server.hello``, mint ``session_id``, send
  ``session.created``.
- Text messages parsed as JSON control events.
- Binary messages treated as raw PCM16LE audio append (V1 default path).
- ``input_audio_buffer.commit`` drains the uncommitted buffer and runs one
  backend decode to completion, streaming delta/completed events back.
- ``session.close`` drains any in-flight decode, sends ``session.closed``,
  then closes the socket. ``session.cancel`` discards uncommitted audio,
  cancels the in-flight decode, sends ``session.closed`` and closes.
- ``serve()`` installs SIGINT/SIGTERM handlers for a bounded graceful drain
  that covers both connection handlers and their decode tasks.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import signal
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import websockets
from websockets.asyncio.server import (
    Server,
    ServerConnection,
    serve as ws_serve,
    unix_serve as ws_unix_serve,
)

from . import protocol as P
from .backend import BackendStream, EchoBackend, TranscriptionBackend

logger = logging.getLogger("stt_server")


def _event_id() -> str:
    return f"evt_{uuid.uuid4().hex[:16]}"


def _session_id() -> str:
    return f"session_{uuid.uuid4().hex[:16]}"


def _item_id() -> str:
    return f"item_{uuid.uuid4().hex[:16]}"


@dataclass
class ServerConfig:
    """Transport and policy configuration for ``TranscriptionServer``."""

    socket_path: str | None = None
    host: str | None = None
    port: int | None = None
    auth_token: str | None = None
    reject_browser_origins: bool = True
    max_append_bytes: int = P.MAX_APPEND_BYTES
    max_uncommitted_bytes: int = P.MAX_UNCOMMITTED_BYTES
    # Bounded write-buffer per session: if the socket's pending-send buffer
    # exceeds this many bytes, the server closes the session with
    # ``send_queue_overflow`` rather than blocking the decode loop on a slow
    # consumer.
    send_queue_high_water_bytes: int = P.SEND_QUEUE_HIGH_WATER_BYTES
    drain_timeout_seconds: float = P.SHUTDOWN_DRAIN_TIMEOUT_SECONDS
    # chmod applied to UDS after bind. 0o600 restricts connect to the owning
    # user; set to None to inherit the process umask (not recommended on
    # shared hosts — the UDS is the V1 trust boundary).
    unix_socket_mode: int | None = 0o600

    def __post_init__(self) -> None:
        if self.socket_path is None and (self.host is None or self.port is None):
            raise ValueError("ServerConfig requires socket_path or host+port")
        if self.host is not None and self.host not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("V1 only permits loopback binds (127.0.0.1/::1/localhost)")


@dataclass
class _SessionState:
    session_id: str
    config: dict = field(default_factory=lambda: {"turn_detection": None})
    buffer: bytearray = field(default_factory=bytearray)
    # ``asyncio.Task`` here reflects the V1 in-process topology. A future
    # worker-process backend would replace this with a cross-process handle.
    in_flight_task: asyncio.Task | None = None
    current_stream: BackendStream | None = None
    closed: bool = False
    started_monotonic: float = field(default_factory=time.monotonic)


class TranscriptionServer:
    """Owns the listener and lifecycle for in-process WebSocket sessions."""

    def __init__(self, backend: TranscriptionBackend, config: ServerConfig) -> None:
        self._backend = backend
        self._config = config
        self._server: Server | None = None
        self._active_handlers: set[asyncio.Task] = set()
        self._active_decodes: set[asyncio.Task] = set()
        self._shutdown_event = asyncio.Event()
        self._started = False

    # --- lifecycle ---
    async def start(self) -> None:
        if self._started:
            return
        await self._backend.start()
        if self._config.socket_path:
            # Restrict the socket file to owner-only before bind so the UDS
            # trust boundary actually holds on multi-user hosts.
            prior_umask = os.umask(0o077)
            try:
                self._server = await ws_unix_serve(
                    self._handle_connection,
                    path=self._config.socket_path,
                    max_size=self._config.max_append_bytes,
                    process_request=self._process_request,
                )
            finally:
                os.umask(prior_umask)
            if self._config.unix_socket_mode is not None:
                try:
                    os.chmod(self._config.socket_path, self._config.unix_socket_mode)
                except OSError as exc:
                    logger.warning("stt_server: chmod on UDS failed: %s", exc)
        else:
            self._server = await ws_serve(
                self._handle_connection,
                host=self._config.host,
                port=self._config.port,
                max_size=self._config.max_append_bytes,
                process_request=self._process_request,
            )
        self._started = True
        logger.info(
            "stt_server listening on %s",
            self._config.socket_path or f"{self._config.host}:{self._config.port}",
        )

    @property
    def sockets_bound(self) -> list:
        if self._server is None:
            return []
        return list(self._server.sockets or [])

    def listening_port(self) -> int | None:
        """For TCP mode, return the actual port (helpful with port=0)."""
        for s in self.sockets_bound:
            try:
                return s.getsockname()[1]
            except Exception:
                continue
        return None

    async def wait_closed(self) -> None:
        if self._server is not None:
            await self._server.wait_closed()

    async def shutdown(self) -> None:
        """Stop accepting new connections and drain active sessions.

        Drains both connection handlers and their spawned decode tasks under
        a single bounded timeout. Force-cancels anything still running so the
        process never wedges on MLX/Metal teardown.
        """
        if not self._started:
            return
        self._shutdown_event.set()
        if self._server is not None:
            # ``close_connections=True`` sends a close frame to every open
            # socket so handler recv loops exit instead of blocking forever
            # on an idle client.
            self._server.close(close_connections=True)
        pending = list(self._active_handlers | self._active_decodes)
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=self._config.drain_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "stt_server: drain timeout expired with %d tasks in flight",
                    sum(1 for t in pending if not t.done()),
                )
                for t in pending:
                    if not t.done():
                        t.cancel()
                # Give the cancellations a short window to unwind before
                # backend.close() touches shared MLX/Metal state.
                await asyncio.gather(*pending, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
        await self._backend.close()
        self._started = False

    # --- connection handling ---
    async def _process_request(self, connection, request):
        # Reject unexpected browser Origin headers for non-browser-focused V1,
        # and enforce optional bearer auth in one place.
        headers = request.headers
        origin = headers.get("Origin")
        if self._config.reject_browser_origins and origin:
            return connection.respond(403, "origin not permitted\n")
        if self._config.auth_token:
            provided = headers.get("Authorization", "") or ""
            expected = f"Bearer {self._config.auth_token}"
            # Constant-time compare so a loopback attacker cannot recover the
            # token byte-by-byte via response-timing signals.
            if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
                return connection.respond(401, "unauthorized\n")
        return None

    async def _handle_connection(self, ws: ServerConnection) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._active_handlers.add(task)
        state = _SessionState(session_id=_session_id())
        try:
            await self._send(
                ws,
                state,
                {
                    "type": P.EVT_SERVER_HELLO,
                    "event_id": _event_id(),
                    "protocol_version": P.PROTOCOL_VERSION,
                    "capabilities": {
                        "binary_audio": True,
                        "base64_audio_append": True,
                        "server_vad": False,
                    },
                    "audio": {
                        "format": P.AUDIO_FORMAT,
                        "sample_rate_hz": P.AUDIO_SAMPLE_RATE_HZ,
                        "channels": P.AUDIO_CHANNELS,
                    },
                },
            )
            await self._send(
                ws,
                state,
                {
                    "type": P.EVT_SESSION_CREATED,
                    "event_id": _event_id(),
                    "session": {"id": state.session_id, "type": "transcription"},
                },
            )
            async for raw in ws:
                if state.closed:
                    break
                try:
                    if isinstance(raw, (bytes, bytearray)):
                        await self._handle_binary_audio(ws, state, bytes(raw))
                    else:
                        await self._handle_text(ws, state, raw)
                except Exception as exc:
                    logger.exception("stt_server: error handling message")
                    await self._error(ws, state, P.ErrorCode.INTERNAL_ERROR, str(exc))
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            await self._teardown_session(ws, state)
            self._active_handlers.discard(task)

    # --- message handlers ---
    async def _handle_text(self, ws: ServerConnection, state: _SessionState, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            await self._error(ws, state, P.ErrorCode.INVALID_JSON, str(exc))
            return
        if not isinstance(msg, dict) or "type" not in msg:
            await self._error(ws, state, P.ErrorCode.INVALID_EVENT, "missing type")
            return
        t = msg["type"]
        if t not in P.CLIENT_EVENT_TYPES:
            await self._error(ws, state, P.ErrorCode.UNSUPPORTED_EVENT, f"unknown event: {t}")
            return

        if t == P.EVT_SESSION_UPDATE:
            await self._on_session_update(ws, state, msg)
        elif t == P.EVT_AUDIO_APPEND:
            await self._on_audio_append_json(ws, state, msg)
        elif t == P.EVT_AUDIO_COMMIT:
            await self._on_commit(ws, state)
        elif t == P.EVT_SERVER_STATUS_REQ:
            await self._on_status(ws, state)
        elif t == P.EVT_SESSION_CLOSE:
            await self._on_close(ws, state)
        elif t == P.EVT_SESSION_CANCEL:
            await self._on_cancel(ws, state)

    async def _handle_binary_audio(
        self, ws: ServerConnection, state: _SessionState, data: bytes
    ) -> None:
        # Reject appends while a decode is in flight rather than silently
        # merging new audio into the next turn's buffer, which would lose the
        # commit boundary the client just signalled.
        if state.in_flight_task is not None and not state.in_flight_task.done():
            await self._error(
                ws,
                state,
                P.ErrorCode.INVALID_EVENT,
                "audio append while previous decode in flight",
            )
            return
        if len(data) > self._config.max_append_bytes:
            await self._error(
                ws, state, P.ErrorCode.PAYLOAD_TOO_LARGE, "binary audio exceeds max_append_bytes"
            )
            return
        if len(state.buffer) + len(data) > self._config.max_uncommitted_bytes:
            await self._error(
                ws,
                state,
                P.ErrorCode.BUFFER_OVERFLOW,
                "uncommitted audio exceeds per-session cap",
            )
            return
        state.buffer.extend(data)

    async def _on_session_update(
        self, ws: ServerConnection, state: _SessionState, msg: dict
    ) -> None:
        session = msg.get("session") or {}
        audio_in = (session.get("audio") or {}).get("input") or {}
        fmt = audio_in.get("format") or {}
        if fmt:
            if fmt.get("encoding", P.AUDIO_FORMAT) != P.AUDIO_FORMAT:
                await self._error(
                    ws, state, P.ErrorCode.INVALID_CONFIG, "only pcm16 encoding is supported"
                )
                return
            if fmt.get("sample_rate_hz", P.AUDIO_SAMPLE_RATE_HZ) != P.AUDIO_SAMPLE_RATE_HZ:
                await self._error(
                    ws, state, P.ErrorCode.INVALID_CONFIG, "only 16000 Hz sample rate is supported"
                )
                return
            if fmt.get("channels", P.AUDIO_CHANNELS) != P.AUDIO_CHANNELS:
                await self._error(
                    ws, state, P.ErrorCode.INVALID_CONFIG, "only mono audio is supported"
                )
                return
        if "turn_detection" in audio_in:
            td = audio_in["turn_detection"]
            if td is not None:
                await self._error(
                    ws,
                    state,
                    P.ErrorCode.INVALID_CONFIG,
                    "server VAD is not implemented; use turn_detection: null",
                )
                return
            state.config["turn_detection"] = None
        if "language" in audio_in:
            state.config["language"] = audio_in["language"]

        await self._send(
            ws,
            state,
            {
                "type": P.EVT_SESSION_UPDATED,
                "event_id": _event_id(),
                "session": {
                    "id": state.session_id,
                    "type": "transcription",
                    "audio": {
                        "input": {
                            "format": {
                                "type": "audio/pcm",
                                "encoding": P.AUDIO_FORMAT,
                                "sample_rate_hz": P.AUDIO_SAMPLE_RATE_HZ,
                                "channels": P.AUDIO_CHANNELS,
                            },
                            "turn_detection": state.config.get("turn_detection"),
                            "language": state.config.get("language"),
                        }
                    },
                },
            },
        )

    async def _on_audio_append_json(
        self, ws: ServerConnection, state: _SessionState, msg: dict
    ) -> None:
        import base64

        audio_b64 = msg.get("audio")
        if not isinstance(audio_b64, str):
            await self._error(
                ws, state, P.ErrorCode.INVALID_EVENT, "input_audio_buffer.append missing audio"
            )
            return
        try:
            data = base64.b64decode(audio_b64, validate=True)
        except Exception as exc:
            await self._error(
                ws, state, P.ErrorCode.INVALID_EVENT, f"audio base64 decode failed: {exc}"
            )
            return
        await self._handle_binary_audio(ws, state, data)

    async def _on_commit(self, ws: ServerConnection, state: _SessionState) -> None:
        if len(state.buffer) == 0:
            await self._error(ws, state, P.ErrorCode.BUFFER_EMPTY, "commit on empty buffer")
            return
        if state.in_flight_task is not None and not state.in_flight_task.done():
            await self._error(
                ws, state, P.ErrorCode.INVALID_EVENT, "commit while previous decode in flight"
            )
            return

        audio = bytes(state.buffer)
        state.buffer.clear()
        item = _item_id()
        await self._send(
            ws,
            state,
            {
                "type": P.EVT_AUDIO_COMMITTED,
                "event_id": _event_id(),
                "item_id": item,
            },
        )
        decode_task = asyncio.create_task(self._run_decode(ws, state, audio, item))
        state.in_flight_task = decode_task
        self._active_decodes.add(decode_task)
        decode_task.add_done_callback(self._active_decodes.discard)

    async def _run_decode(
        self,
        ws: ServerConnection,
        state: _SessionState,
        audio: bytes,
        item_id: str,
    ) -> None:
        stream: BackendStream | None = None
        try:
            stream = await self._backend.open_stream(language=state.config.get("language"))
            state.current_stream = stream
            await stream.feed(audio)
            await stream.end()
            final_text = ""
            async for ev in stream.events():
                if ev.kind == "delta":
                    await self._send(
                        ws,
                        state,
                        {
                            "type": P.EVT_TRANSCRIPT_DELTA,
                            "event_id": _event_id(),
                            "item_id": item_id,
                            "content_index": 0,
                            "delta": ev.text,
                        },
                    )
                elif ev.kind == "completed":
                    final_text = ev.text
            await self._send(
                ws,
                state,
                {
                    "type": P.EVT_TRANSCRIPT_COMPLETED,
                    "event_id": _event_id(),
                    "item_id": item_id,
                    "content_index": 0,
                    "transcript": final_text,
                },
            )
        except asyncio.CancelledError:
            if stream is not None:
                try:
                    await stream.cancel()
                except Exception:
                    pass
            raise
        except Exception as exc:
            logger.exception("stt_server: backend decode failed")
            await self._error(ws, state, P.ErrorCode.BACKEND_ERROR, str(exc), item_id=item_id)
        finally:
            state.current_stream = None

    async def _on_status(self, ws: ServerConnection, state: _SessionState) -> None:
        await self._send(
            ws,
            state,
            {
                "type": P.EVT_SERVER_STATUS,
                "event_id": _event_id(),
                "session_id": state.session_id,
                "queue_depth": 1 if state.in_flight_task and not state.in_flight_task.done() else 0,
                "uncommitted_bytes": len(state.buffer),
                "uptime_seconds": time.monotonic() - state.started_monotonic,
            },
        )

    async def _on_close(self, ws: ServerConnection, state: _SessionState) -> None:
        if state.closed:
            return
        if state.in_flight_task and not state.in_flight_task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(state.in_flight_task),
                    timeout=self._config.drain_timeout_seconds,
                )
            except asyncio.TimeoutError:
                # Force-cancel and wait for unwind so the decode can't outlive
                # the session (and the surrounding shutdown, if this is it).
                state.in_flight_task.cancel()
                try:
                    await state.in_flight_task
                except (asyncio.CancelledError, Exception):
                    pass
        state.closed = True
        await self._send(
            ws,
            state,
            {
                "type": P.EVT_SESSION_CLOSED,
                "event_id": _event_id(),
                "session_id": state.session_id,
                "reason": "client_close",
            },
            force=True,
        )
        await ws.close()

    async def _on_cancel(self, ws: ServerConnection, state: _SessionState) -> None:
        if state.closed:
            return
        state.buffer.clear()
        if state.current_stream is not None:
            try:
                await state.current_stream.cancel()
            except Exception:
                pass
        if state.in_flight_task and not state.in_flight_task.done():
            state.in_flight_task.cancel()
            try:
                await state.in_flight_task
            except (asyncio.CancelledError, Exception):
                pass
        state.closed = True
        await self._send(
            ws,
            state,
            {
                "type": P.EVT_SESSION_CLOSED,
                "event_id": _event_id(),
                "session_id": state.session_id,
                "reason": "client_cancel",
            },
            force=True,
        )
        await ws.close()

    async def _teardown_session(self, ws: ServerConnection, state: _SessionState) -> None:
        if state.in_flight_task and not state.in_flight_task.done():
            state.in_flight_task.cancel()
            try:
                await state.in_flight_task
            except (asyncio.CancelledError, Exception):
                pass
        state.closed = True

    # --- send helpers ---
    async def _send(
        self,
        ws: ServerConnection,
        state: _SessionState,
        payload: dict,
        *,
        force: bool = False,
    ) -> None:
        # Backpressure: if the socket's pending write buffer is larger than
        # the per-session high-water mark, the consumer isn't keeping up. We
        # surface an explicit error and close the socket rather than block
        # the decode loop indefinitely. ``force=True`` bypasses the check for
        # terminal events (session.closed, error) so teardown can still flush.
        if not force and not state.closed:
            pending = _pending_write_bytes(ws)
            if pending is not None and pending > self._config.send_queue_high_water_bytes:
                logger.warning(
                    "stt_server: send-queue overflow (%d bytes pending), closing session %s",
                    pending,
                    state.session_id,
                )
                state.closed = True
                try:
                    await ws.close(code=1011, reason="send_queue_overflow")
                except Exception:
                    pass
                return
        try:
            await ws.send(json.dumps(payload))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def _error(
        self,
        ws: ServerConnection,
        state: _SessionState,
        code: P.ErrorCode,
        message: str,
        *,
        item_id: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "type": P.EVT_ERROR,
            "event_id": _event_id(),
            "error": {"code": code.value, "message": message},
        }
        if item_id:
            payload["item_id"] = item_id
        await self._send(ws, state, payload, force=True)


def _pending_write_bytes(ws: ServerConnection) -> int | None:
    """Best-effort read of the socket's pending outbound byte count.

    websockets 15.x exposes the underlying asyncio transport; we look up the
    write-buffer size through it. Returns None if we cannot determine it,
    which disables the backpressure guard for that send.
    """
    try:
        transport = ws.transport  # type: ignore[attr-defined]
    except AttributeError:
        return None
    if transport is None:
        return None
    try:
        return transport.get_write_buffer_size()
    except Exception:
        return None


async def serve(
    backend: TranscriptionBackend | None = None,
    *,
    socket_path: str | None = None,
    host: str | None = None,
    port: int | None = None,
    auth_token: str | None = None,
    install_signal_handlers: bool = True,
    ready: Callable[[TranscriptionServer], Awaitable[None]] | None = None,
) -> None:
    """Start the server, wait for shutdown signal, then drain and exit.

    Kept as the one public entrypoint intended for ``python -m stt_server``;
    tests drive ``TranscriptionServer`` directly.
    """
    cfg = ServerConfig(socket_path=socket_path, host=host, port=port, auth_token=auth_token)
    server = TranscriptionServer(backend or EchoBackend(), cfg)
    await server.start()
    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    if install_signal_handlers:

        def _request_stop() -> None:
            if not stop.done():
                stop.set_result(None)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _request_stop)
            except NotImplementedError:
                pass
    if ready is not None:
        await ready(server)
    try:
        await stop
    finally:
        await server.shutdown()
