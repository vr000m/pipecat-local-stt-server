"""MLX Whisper backend for Apple Silicon.

V1 is commit-oriented: we accumulate PCM16LE audio until ``end()`` is called,
then run a single decode and emit one ``delta`` plus one ``completed`` event.
True streaming partials are deferred to future backends.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import AsyncGenerator, Callable, TypeVar

import numpy as np

from ..backend import TranscriptEvent

logger = logging.getLogger("stt_server.backends.mlx")

_T = TypeVar("_T")


def _run_in_daemon_thread(func: Callable[[], _T]) -> "asyncio.Future[_T]":
    """Run ``func`` on a fresh daemon thread and return an asyncio Future.

    Unlike ``loop.run_in_executor`` with ``ThreadPoolExecutor``, the thread
    is a daemon and is NOT registered with the ``concurrent.futures`` atexit
    handler — so a stuck ``mlx_whisper.transcribe()`` call cannot block
    process exit when ``session.cancel`` / ``shutdown()`` fires while a
    decode is running. MLX has no cooperative cancellation hook; the only
    honest way to bound shutdown is to let the OS reap the thread.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[_T] = loop.create_future()

    def _runner() -> None:
        try:
            result = func()
        except BaseException as exc:  # noqa: BLE001 — marshal across threads
            loop.call_soon_threadsafe(_set_exception_safe, fut, exc)
        else:
            loop.call_soon_threadsafe(_set_result_safe, fut, result)

    threading.Thread(target=_runner, daemon=True, name="mlx-decode").start()
    return fut


def _set_result_safe(fut: "asyncio.Future", value) -> None:
    if not fut.done():
        fut.set_result(value)


def _set_exception_safe(fut: "asyncio.Future", exc: BaseException) -> None:
    if not fut.done():
        fut.set_exception(exc)


class _MLXStream:
    def __init__(
        self,
        model: str,
        language: str | None,
        decode_lock: asyncio.Lock,
        thread_lock: threading.Lock,
    ) -> None:
        self._model = model
        self._language = language
        self._buf = bytearray()
        self._ended = False
        self._cancelled = False
        self._result: str | None = None
        self._decode_lock = decode_lock
        self._thread_lock = thread_lock

    async def feed(self, chunk: bytes) -> None:
        if self._cancelled:
            return
        self._buf.extend(chunk)

    async def end(self) -> None:
        if self._ended or self._cancelled:
            return
        self._ended = True
        # Serialize decodes across all sessions: MLX/Metal is not safe for
        # concurrent calls against the same cached model. The asyncio lock
        # orders decodes on the event-loop side, but cancelling the awaiter
        # releases it immediately while the daemon thread keeps running —
        # so we *also* hold a ``threading.Lock`` inside ``_decode_sync``.
        # That way a second decode thread will block until the first truly
        # finishes, even across cancel/reconnect/shutdown boundaries.
        async with self._decode_lock:
            if self._cancelled:
                return
            # If the caller's task is cancelled mid-decode, the await here
            # raises CancelledError and unwinds; the daemon thread keeps
            # running (but holds ``_thread_lock`` until it finishes) and is
            # reaped by the OS at process exit. Shutdown is bounded
            # regardless of MLX decode duration.
            self._result = await _run_in_daemon_thread(self._decode_sync)

    async def cancel(self) -> None:
        self._cancelled = True
        self._ended = True

    def _decode_sync(self) -> str:
        import mlx_whisper  # type: ignore

        audio = np.frombuffer(bytes(self._buf), dtype=np.int16).astype(np.float32) / 32768.0
        if audio.size == 0:
            return ""
        # Hold the backend-scope threading lock for the entire transcribe()
        # call so a second decode thread started after our asyncio awaiter
        # was cancelled still blocks until this one completes. Without this,
        # cancel-then-reopen can put two mlx-decode threads live at once.
        with self._thread_lock:
            # mlx_whisper.transcribe resamples internally using its own
            # constant; audio must already be at AUDIO_SAMPLE_RATE_HZ
            # (16 kHz), which the protocol enforces on the wire. Do not
            # pass sample_rate — it's not a valid DecodingOptions kwarg.
            result = mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=self._model,
                language=self._language,
                fp16=True,
                verbose=False,
            )
        return (result.get("text") or "").strip()

    async def events(self) -> AsyncGenerator[TranscriptEvent, None]:
        if self._cancelled or self._result is None:
            return
        text = self._result
        if text:
            yield TranscriptEvent(kind="delta", text=text)
        yield TranscriptEvent(kind="completed", text=text)


class MLXWhisperBackend:
    def __init__(self, *, model: str = "mlx-community/whisper-large-v3-turbo") -> None:
        self._model = model
        self._decode_lock = asyncio.Lock()
        # Backend-scope thread lock — see ``_MLXStream.end``. Shared across
        # every stream this backend opens so concurrent sessions truly
        # serialize on the MLX/Metal side.
        self._thread_lock = threading.Lock()

    async def start(self) -> None:
        # Eager import; fail fast if the extra isn't installed.
        import mlx_whisper  # type: ignore # noqa: F401

    async def open_stream(self, *, language: str | None = None) -> "_MLXStream":
        return _MLXStream(self._model, language, self._decode_lock, self._thread_lock)

    async def close(self) -> None:
        # No pool to drain: each decode runs on its own daemon thread that
        # the OS will reap at process exit.
        return None
