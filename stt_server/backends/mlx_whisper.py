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
        backend: "MLXWhisperBackend",
    ) -> None:
        self._model = model
        self._language = language
        self._buf = bytearray()
        self._ended = False
        self._cancelled = False
        self._result: str | None = None
        self._decode_lock = decode_lock
        self._thread_lock = thread_lock
        self._backend = backend

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
            # Mark the decode in-flight BEFORE spawning the daemon thread
            # so ``backend.close()`` can observe it immediately. The
            # daemon thread (``_decode_sync``) owns the decrement in a
            # finally block, so the marker survives asyncio cancellation
            # of this awaiter — the daemon thread is what shutdown must
            # actually drain, not this coroutine.
            self._backend._mark_inflight_start()
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
        # Decrement the backend-scope in-flight counter in a finally so
        # ``backend.close()``'s drain wait is correct across every exit
        # path (normal return, transcribe() raising, cancellation).
        try:
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
        finally:
            self._backend._mark_inflight_end()

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
        # Backend-scope in-flight counter. Incremented BEFORE we spawn
        # the decode thread (in the coroutine's event-loop context);
        # decremented by the daemon thread itself in a finally block.
        # ``close()`` waits on this reaching zero with a timeout. The
        # earlier "acquire _thread_lock once" implementation had a race:
        # between ``threading.Thread.start()`` and the daemon thread
        # reaching ``with self._thread_lock:``, the lock was free, and
        # ``close()`` could acquire-release and return while the decode
        # was about to enter ``mlx_whisper.transcribe()`` — the exact
        # Metal-assertion window the bound was meant to close.
        self._inflight_count = 0
        self._inflight_cond = threading.Condition()

    def _mark_inflight_start(self) -> None:
        with self._inflight_cond:
            self._inflight_count += 1

    def _mark_inflight_end(self) -> None:
        with self._inflight_cond:
            self._inflight_count -= 1
            if self._inflight_count == 0:
                self._inflight_cond.notify_all()

    def _wait_inflight_drained(self, timeout_s: float) -> bool:
        with self._inflight_cond:
            return self._inflight_cond.wait_for(
                lambda: self._inflight_count == 0,
                timeout=timeout_s,
            )

    async def start(self) -> None:
        # Eager import; fail fast if the extra isn't installed.
        import mlx_whisper  # type: ignore # noqa: F401

    async def open_stream(self, *, language: str | None = None) -> "_MLXStream":
        return _MLXStream(self._model, language, self._decode_lock, self._thread_lock, self)

    async def close(self) -> None:
        # Give any in-flight MLX decode a bounded window to finish flushing
        # Metal work before the process exits. Without this, SIGTERM during
        # a decode leaves the Metal command buffer mid-commit and the
        # process exit trips `-[IOGPUMetalCommandBuffer validate]: failed
        # assertion 'commit command buffer with uncommitted encoder'` —
        # measured on 2026-04-22, see the Tier B spike results in
        # docs/dev_plans/20260420-design-whisper-websocket-server.md.
        #
        # Waits on the in-flight counter reaching zero; the daemon thread
        # itself decrements in a finally block, so there is no race
        # between Thread.start() and the thread's first statement where
        # close() could observe "nothing in flight" and return early.
        timeout_s = 3.0
        drained = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._wait_inflight_drained(timeout_s)
        )
        if not drained:
            logger.warning(
                "mlx_whisper: in-flight decode did not finish within %.1fs; "
                "Metal assertion possible at process exit",
                timeout_s,
            )
