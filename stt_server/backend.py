"""Transcription backend interface and reference implementations.

The server depends on this abstraction rather than on MLX-specific objects so
that alternate backends (faster-whisper, CPU Whisper, remote providers) can
be swapped in later without touching protocol/session code.

V1 is commit-oriented: ``feed()`` accumulates audio, ``end()`` triggers one
decode, ``events()`` may yield a single large ``delta`` followed by
``completed``. True streaming partials are deferred.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


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
    # Declared as AsyncGenerator (not AsyncIterator) so the structural match
    # rejects implementations that return a plain coroutine.
    def events(self) -> AsyncGenerator[TranscriptEvent, None]: ...
    async def cancel(self) -> None: ...


@runtime_checkable
class TranscriptionBackend(Protocol):
    # Identity surfaced in the ``server.hello`` / ``server.status`` ``backend``
    # field so a client (the A/B benchmark, the bot) can verify which ASR is
    # actually behind a socket rather than trusting the socket path. ``model``
    # is ``None`` for backends with no model (echo).
    backend_name: str
    model: str | None

    async def start(self) -> None: ...
    async def open_stream(self, *, language: str | None = None) -> BackendStream: ...
    async def close(self) -> None: ...


# Optional backend hooks. None is part of ``TranscriptionBackend`` (a backend
# need not implement them), but each is a fixed contract:
#
# - ``abort()`` (instance method, :class:`AbortableBackend`): synchronous, cheap,
#   non-blocking cleanup -- no thread joins, no Metal drain -- e.g. removing a
#   private temp dir. ``TranscriptionServer`` calls it when its ``start()``
#   fails or is cancelled -- after ``close()`` if the backend's ``start()`` had
#   returned (that ``close()`` is bounded and may still be running), alone if
#   it had not -- and from ``abort_sync()`` before a hard process exit (no
#   ``close()``). It may run before ``start()``,
#   more than once, and while a decode / lazy model-load daemon thread is still
#   running (threads cannot be cancelled: a stop during warm-up leaves one
#   behind). So it must be idempotent and must not wait for or break that
#   thread: anything the thread still uses after ``abort()`` may simply fail
#   that decode (an error delivered to its awaiter, if any), and a later
#   ``start()`` of the same backend must recreate whatever ``abort()`` removed:
#   a ``TranscriptionServer`` whose failed ``start()`` returned it to ``new``
#   may be started again, and its backend may always be handed to a new server
#   (a server is single-use once ``shutdown()`` was called or a failed start
#   left work running, i.e. it is ``stopping`` or ``stopped``). ``close()``
#   may call it too, after its bounded drain.
# - ``idle()`` (instance method, :class:`IdleAwareBackend`): returns an
#   ``asyncio.Future`` that completes once no backend-owned worker thread (a
#   decode daemon thread) is still running. Must not block. Cancelling a decode
#   task does not stop its thread, so after every drain (``shutdown()``, or a
#   failed ``start()``) the server keeps the single-server lock until this
#   future completes: no new server may load a model while this one's decode
#   still runs (a failed start that waits on it becomes single-use). Without
#   the hook the server can only see its own asyncio tasks.
# - ``prefetch_specs(model: str | None) -> list[SnapshotSpec]`` (module-level
#   function in the backend's module, registered in ``backends.BACKENDS``): the
#   Hugging Face snapshots ``python -m stt_server prefetch`` fetches/checks so
#   the backend can start ``model`` (``None`` = its default) offline. See
#   ``stt_server.snapshot.SnapshotSpec``.


@runtime_checkable
class AbortableBackend(Protocol):
    """A backend implementing the optional ``abort()`` hook (contract above)."""

    def abort(self) -> None: ...


@runtime_checkable
class IdleAwareBackend(Protocol):
    """A backend implementing the optional ``idle()`` hook (contract above)."""

    def idle(self) -> "asyncio.Future[None]": ...


def abort_backend(backend: object, log) -> None:
    """Call ``backend.abort()`` if it is an :class:`AbortableBackend`; log failures."""
    if isinstance(backend, AbortableBackend):
        try:
            backend.abort()
        except Exception:  # noqa: BLE001 -- best-effort cleanup
            log.exception("stt_server: backend abort failed")


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

    async def events(self) -> AsyncGenerator[TranscriptEvent, None]:
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

    backend_name = "echo"
    model = None

    async def start(self) -> None:
        return None

    async def open_stream(self, *, language: str | None = None) -> BackendStream:
        return _EchoStream(language)

    async def close(self) -> None:
        return None
