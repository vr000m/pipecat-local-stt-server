"""Standalone local transcription WebSocket server and client.

Repo-neutral package intended for later extraction into its own OSS repo.

Import layout reflects the planned ``stt-server-client`` vs
``stt-server-mlx`` extras split once extracted:

- protocol / client / backend interfaces are re-exported here, so a
  client-only install (no MLX, no asyncio server runtime) can
  ``from stt_server import TranscriptionClient`` without pulling the
  server or its dependency on ``websockets.asyncio.server``.
- Server runtime (``TranscriptionServer`` / ``ServerConfig`` / ``serve``)
  must be imported explicitly from ``stt_server.server``.
- ``EchoBackend`` is re-exported because it's used in tests on both
  sides and has no heavy runtime dependencies.
"""

from __future__ import annotations

from .backend import BackendStream, EchoBackend, TranscriptEvent, TranscriptionBackend
from .client import TranscriptionClient
from .protocol import (
    AUDIO_CHANNELS,
    AUDIO_FORMAT,
    AUDIO_SAMPLE_RATE_HZ,
    MAX_APPEND_BYTES,
    MAX_UNCOMMITTED_SECONDS,
    PROTOCOL_VERSION,
    SEND_QUEUE_HIGH_WATER_BYTES,
    ErrorCode,
)

__all__ = [
    "PROTOCOL_VERSION",
    "AUDIO_SAMPLE_RATE_HZ",
    "AUDIO_CHANNELS",
    "AUDIO_FORMAT",
    "MAX_APPEND_BYTES",
    "MAX_UNCOMMITTED_SECONDS",
    "SEND_QUEUE_HIGH_WATER_BYTES",
    "ErrorCode",
    "TranscriptionBackend",
    "BackendStream",
    "TranscriptEvent",
    "EchoBackend",
    "TranscriptionClient",
]
