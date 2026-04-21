"""Minimal async Python client for the transcription server.

Kept transport-generic: Koda (or any other consumer) is expected to wrap this
in a Pipecat ``STTService`` adapter in a separate module, so this client must
not bake in app-specific labels, frame types, or transcript storage.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import AsyncIterator

import websockets
from websockets.asyncio.client import (
    ClientConnection,
    connect as ws_connect,
    unix_connect as ws_unix_connect,
)

from . import protocol as P

logger = logging.getLogger("stt_server.client")


class TranscriptionClient:
    def __init__(
        self,
        *,
        socket_path: str | None = None,
        host: str | None = None,
        port: int | None = None,
        uri: str | None = None,
        auth_token: str | None = None,
    ) -> None:
        if uri is None and socket_path is None and (host is None or port is None):
            raise ValueError("Provide uri=, socket_path=, or host+port")
        self._socket_path = socket_path
        self._host = host
        self._port = port
        self._uri = uri
        self._auth_token = auth_token
        self._ws: ClientConnection | None = None
        self._closed = False

    # --- connection ---
    async def connect(self) -> dict:
        """Open websocket and return the ``server.hello`` message."""
        headers = {}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        if self._uri:
            self._ws = await ws_connect(
                self._uri,
                additional_headers=headers or None,
                max_size=P.MAX_APPEND_BYTES,
            )
        elif self._socket_path:
            self._ws = await ws_unix_connect(
                self._socket_path,
                "ws://localhost/",
                additional_headers=headers or None,
                max_size=P.MAX_APPEND_BYTES,
            )
        else:
            uri = f"ws://{self._host}:{self._port}/"
            self._ws = await ws_connect(
                uri,
                additional_headers=headers or None,
                max_size=P.MAX_APPEND_BYTES,
            )
        hello = await self._recv_json()
        if hello.get("type") != P.EVT_SERVER_HELLO:
            raise RuntimeError(f"expected server.hello, got {hello.get('type')}")
        created = await self._recv_json()
        if created.get("type") != P.EVT_SESSION_CREATED:
            raise RuntimeError(f"expected session.created, got {created.get('type')}")
        return hello

    async def _recv_json(self) -> dict:
        assert self._ws is not None
        raw = await self._ws.recv()
        if isinstance(raw, (bytes, bytearray)):
            raise RuntimeError("unexpected binary frame before handshake complete")
        return json.loads(raw)

    # --- control events ---
    async def update_session(
        self,
        *,
        turn_detection: str | None = None,
        language: str | None = None,
    ) -> None:
        assert self._ws is not None
        session: dict = {
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {
                        "encoding": P.AUDIO_FORMAT,
                        "rate": P.AUDIO_SAMPLE_RATE_HZ,
                        "channels": P.AUDIO_CHANNELS,
                    },
                    "turn_detection": turn_detection,
                }
            },
        }
        if language is not None:
            session["audio"]["input"]["language"] = language
        await self._ws.send(json.dumps({"type": P.EVT_SESSION_UPDATE, "session": session}))

    async def send_audio(self, pcm: bytes) -> None:
        """Send binary PCM16LE audio frame (V1 default transport)."""
        assert self._ws is not None
        await self._ws.send(pcm)

    async def send_audio_base64(self, pcm: bytes) -> None:
        """Optional JSON/base64 compatibility path for OpenAI-shaped clients."""
        assert self._ws is not None
        encoded = base64.b64encode(pcm).decode("ascii")
        await self._ws.send(json.dumps({"type": P.EVT_AUDIO_APPEND, "audio": encoded}))

    async def commit(self) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps({"type": P.EVT_AUDIO_COMMIT}))

    async def status(self) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps({"type": P.EVT_SERVER_STATUS_REQ}))

    async def close_session(self) -> None:
        """Send session.close; call ``close()`` to also tear down the socket."""
        assert self._ws is not None
        await self._ws.send(json.dumps({"type": P.EVT_SESSION_CLOSE}))

    async def cancel(self) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps({"type": P.EVT_SESSION_CANCEL}))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    # --- events iterator ---
    async def events(self) -> AsyncIterator[dict]:
        """Yield server events as dicts until the socket closes."""
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, (bytes, bytearray)):
                    # V1 server never emits binary frames; skip defensively.
                    continue
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("stt_server.client: dropping non-JSON text frame")
        except websockets.exceptions.ConnectionClosed:
            return

    # --- async context manager ---
    async def __aenter__(self) -> "TranscriptionClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()
