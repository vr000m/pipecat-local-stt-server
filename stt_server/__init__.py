"""Standalone local transcription WebSocket server and client.

Repo-neutral package intended for later extraction into its own OSS repo.
Public API is exposed at the package level so importers can stay agnostic to
internal module layout.
"""

from __future__ import annotations

from .protocol import (
    PROTOCOL_VERSION,
    AUDIO_SAMPLE_RATE_HZ,
    AUDIO_CHANNELS,
    AUDIO_FORMAT,
    MAX_APPEND_BYTES,
    MAX_UNCOMMITTED_SECONDS,
    SEND_QUEUE_HIGH_WATER_BYTES,
    ErrorCode,
)
from .backend import TranscriptionBackend, BackendStream, TranscriptEvent, EchoBackend
from .server import serve, TranscriptionServer, ServerConfig
from .client import TranscriptionClient

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
    "serve",
    "TranscriptionServer",
    "ServerConfig",
    "TranscriptionClient",
]
