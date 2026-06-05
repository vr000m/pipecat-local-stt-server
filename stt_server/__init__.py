"""Standalone local transcription WebSocket server and client.

Repo-neutral package intended for later extraction into its own OSS repo.

Import layout reflects the ``client`` vs ``mlx`` extras split:

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
    PROTOCOL_VERSION,
    ErrorCode,
)

# Client-facing surface only. Server-policy constants
# (``SEND_QUEUE_HIGH_WATER_BYTES``, ``MAX_UNCOMMITTED_SECONDS``, drain
# timeout, etc.) live in ``stt_server.protocol`` and are imported directly
# by the server runtime — a client-only install has no business reading
# them and exposing them here would grow the client API surface
# unnecessarily once the package is extracted.
__all__ = [
    "PROTOCOL_VERSION",
    "AUDIO_SAMPLE_RATE_HZ",
    "AUDIO_CHANNELS",
    "AUDIO_FORMAT",
    "MAX_APPEND_BYTES",
    "ErrorCode",
    "TranscriptionBackend",
    "BackendStream",
    "TranscriptEvent",
    "EchoBackend",
    "TranscriptionClient",
]
