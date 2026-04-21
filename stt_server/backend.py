"""Transcription backend interface and reference implementations.

The server depends on this abstraction rather than on MLX-specific objects so
that alternate backends (faster-whisper, CPU Whisper, remote providers) can
be swapped in later without touching protocol/session code.

V1 is commit-oriented: ``feed()`` accumulates audio, ``end()`` triggers one
decode, ``events()`` may yield a single large ``delta`` followed by
``completed``. True streaming partials are deferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol, runtime_checkable


@dataclass
class TranscriptEvent:
    """One transcript event yielded by a backend stream."""

    kind: str  # "delta" | "completed"
    text: str


@runtime_checkable
class BackendStream(Protocol):
    async def feed(self, chunk: bytes) -> None: ...
    async def end(self) -> None: ...
    # Async-generator function: implementations must be ``async def`` with
    # ``yield`` so callers can use ``async for ev in stream.events()``.
    def events(self) -> AsyncIterator[TranscriptEvent]: ...
    async def cancel(self) -> None: ...


@runtime_checkable
class TranscriptionBackend(Protocol):
    async def start(self) -> None: ...
    async def open_stream(self, *, language: str | None = None) -> BackendStream: ...
    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# EchoBackend — a trivial backend for tests and local smoke-checks.
# ---------------------------------------------------------------------------


class _EchoStream:
    def __init__(self, language: str | None) -> None:
        self._buf = bytearray()
        self._done = False
        self._language = language
        self._cancelled = False

    async def feed(self, chunk: bytes) -> None:
        self._buf.extend(chunk)

    async def end(self) -> None:
        self._done = True

    async def cancel(self) -> None:
        self._cancelled = True
        self._done = True

    async def events(self) -> AsyncIterator[TranscriptEvent]:
        # Block until end() or cancel() was called; server always awaits these
        # before consuming events, so we can yield synchronously here.
        if self._cancelled:
            return
        # Produce a single delta+completed derived from the byte count so
        # tests can assert deterministic output without a real model.
        text = f"echo:{len(self._buf)}"
        yield TranscriptEvent(kind="delta", text=text)
        yield TranscriptEvent(kind="completed", text=text)


class EchoBackend:
    """Reference backend that echoes audio length. Used by tests."""

    async def start(self) -> None:
        return None

    async def open_stream(self, *, language: str | None = None) -> BackendStream:
        return _EchoStream(language)

    async def close(self) -> None:
        return None
