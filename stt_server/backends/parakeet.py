"""Parakeet (NVIDIA TDT) backend for Apple Silicon via ``parakeet-mlx``.

V1 is commit-oriented: we accumulate PCM16LE audio until ``end()`` is called,
then run a single decode and emit one ``delta`` plus one ``completed`` event.
True streaming partials are deferred — Parakeet runs after smart-turn, so it
always sees a complete utterance.

Mirrors ``stt_server/backends/mlx_whisper.py`` in structure: lazy import of the
optional ``parakeet_mlx`` extra, a backend-scoped asyncio + threading decode
lock pair, an in-flight drain in ``close()`` for SIGTERM-mid-decode crash
isolation, and the same empty-decode contract (``delta`` only on non-empty
text, ``completed`` always).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import AsyncGenerator, Callable, TypeVar

import numpy as np

from ..backend import TranscriptEvent

logger = logging.getLogger("stt_server.backends.parakeet")

# Default model for ``--backend parakeet``. Exported so Phase 2's
# backend-aware ``--model`` default imports it rather than hardcoding a second
# copy. ``parakeet-tdt-0.6b-v3`` is the package's own CLI default.
DEFAULT_PARAKEET_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"

# Parakeet's native attention window is ~24 s. ``parakeet_mlx``'s decode path
# implements chunk-and-concatenate (token-merge across overlapping chunks) when
# passed ``chunk_duration`` — so we hand it the chunking rather than capping or
# reimplementing the merge. 120 s window / 15 s overlap mirror the package's
# own CLI defaults; no audio is dropped at chunk boundaries. Utterances longer
# than the V1 wire ceiling (MAX_UNCOMMITTED_SECONDS = 300) are rejected by the
# server before they ever reach the backend, so chunking covers the full
# 24-300 s danger zone without truncation.
_CHUNK_DURATION_S = 120.0
_OVERLAP_DURATION_S = 15.0

_T = TypeVar("_T")


def _run_in_daemon_thread(func: Callable[[], _T]) -> "asyncio.Future[_T]":
    """Run ``func`` on a fresh daemon thread and return an asyncio Future.

    Same rationale as ``mlx_whisper._run_in_daemon_thread``: the thread is a
    daemon and is NOT registered with the ``concurrent.futures`` atexit
    handler, so a stuck ``parakeet_mlx`` decode cannot block process exit when
    ``session.cancel`` / ``shutdown()`` fires while a decode is running.
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

    threading.Thread(target=_runner, daemon=True, name="parakeet-decode").start()
    return fut


def _set_result_safe(fut: "asyncio.Future", value) -> None:
    if not fut.done():
        fut.set_result(value)


def _set_exception_safe(fut: "asyncio.Future", exc: BaseException) -> None:
    if not fut.done():
        fut.set_exception(exc)


class _ParakeetStream:
    def __init__(
        self,
        language: str | None,
        decode_lock: asyncio.Lock,
        thread_lock: threading.Lock,
        backend: "ParakeetBackend",
    ) -> None:
        # ``language`` is accepted to satisfy the structural protocol but is
        # NOT forwarded to the decoder: Parakeet TDT models are language-pinned
        # by the model id (e.g. the multilingual ``-v3`` checkpoint), and
        # ``parakeet_mlx.transcribe`` exposes no per-call language kwarg. The
        # client-supplied ``language`` is therefore accepted and ignored — the
        # documented behaviour, recorded so the protocol match does not hide a
        # semantic mismatch.
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
        # Serialize decodes across all sessions: parakeet-mlx holds one cached
        # model and MLX/Metal is not safe for concurrent calls against it. The
        # asyncio lock orders decodes event-loop side; the threading lock held
        # inside ``_decode_sync`` keeps a second decode thread blocked even
        # when the awaiter here is cancelled. Same pattern as ``_MLXStream``.
        async with self._decode_lock:
            if self._cancelled:
                return
            # Mark in-flight BEFORE spawning the daemon thread so
            # ``backend.close()`` observes it immediately; the daemon thread
            # owns the decrement in a finally block.
            self._backend._mark_inflight_start()
            self._result = await _run_in_daemon_thread(self._decode_sync)

    async def cancel(self) -> None:
        self._cancelled = True
        self._ended = True

    def _decode_sync(self) -> str:
        # Decrement the backend-scope in-flight counter in a finally so the
        # ``close()`` drain wait is correct across every exit path (normal
        # return, decode raising, cancellation).
        try:
            # First-decode model-load failure and per-utterance decode failure
            # both raise from inside this function; the exception is marshalled
            # back through the Future and propagates out of ``end()`` /
            # ``events()``. The backend never invents a ``failed`` event kind —
            # the server's ``except`` arm synthesises the wire
            # ``transcript.failed`` + ``BACKEND_ERROR``.
            # PCM16LE -> float32 in [-1, 1), the normalised form decoders
            # expect (mirrors ``_MLXStream._decode_sync``). The wire protocol
            # enforces AUDIO_SAMPLE_RATE_HZ (16 kHz) upstream.
            audio = np.frombuffer(bytes(self._buf), dtype=np.int16).astype(np.float32) / 32768.0
            if audio.size == 0:
                return ""
            model = self._backend._get_model()  # lazy first-decode load; may raise
            # Hold the backend-scope threading lock for the entire decode so a
            # second decode thread started after this stream's asyncio awaiter
            # was cancelled still blocks until this one completes.
            with self._thread_lock:
                # ``chunk_duration`` hands parakeet-mlx its chunk-and-concatenate
                # path (cross-chunk token merge) so a 24-300 s utterance is
                # chunked, never truncated.
                result = model.transcribe(
                    audio,
                    chunk_duration=_CHUNK_DURATION_S,
                    overlap_duration=_OVERLAP_DURATION_S,
                )
            text = getattr(result, "text", "") or ""
            return text.strip()
        finally:
            self._backend._mark_inflight_end()

    async def events(self) -> AsyncGenerator[TranscriptEvent, None]:
        if self._cancelled or self._result is None:
            return
        text = self._result
        # Match the MLX empty-decode contract: ``delta`` only when the
        # transcript text is non-empty (Parakeet on near-silence frequently
        # produces empty text); ``completed`` always.
        if text:
            yield TranscriptEvent(kind="delta", text=text)
        yield TranscriptEvent(kind="completed", text=text)


class ParakeetBackend:
    """ASR backend backed by NVIDIA Parakeet TDT models via ``parakeet-mlx``."""

    def __init__(self, *, model: str = DEFAULT_PARAKEET_MODEL) -> None:
        self._model_id = model
        # The loaded parakeet-mlx model. ``None`` until the first decode —
        # ``from_pretrained`` is eager and pulls a ~1.5 GB checkpoint, so it is
        # deferred out of ``start()`` (which only fails fast on a missing
        # extra). Guarded by ``_model_lock`` so two concurrent first decodes
        # load it exactly once.
        self._model = None
        self._model_lock = threading.Lock()
        self._decode_lock = asyncio.Lock()
        # Backend-scope thread lock — shared across every stream so concurrent
        # sessions truly serialize on the MLX/Metal side. parakeet-mlx is not
        # verified concurrency-safe (one cached model, Metal command buffers),
        # so the lock pair is kept exactly as ``MLXWhisperBackend`` does.
        self._thread_lock = threading.Lock()
        # Backend-scope in-flight counter for the ``close()`` drain. parakeet-mlx
        # is Metal-backed and exposed to the same SIGTERM-mid-decode Metal
        # command-buffer assertion class as MLX Whisper, so the in-flight drain
        # is kept, not just the locks.
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

    def _get_model(self):
        """Lazily load the Parakeet model on first decode.

        Called from the decode daemon thread. ``from_pretrained`` is eager and
        downloads a ~1.5 GB checkpoint; deferring it here keeps ``start()``
        cheap. A model-load failure raises here and propagates out of
        ``events()`` / ``end()`` exactly like a per-utterance decode failure —
        the server converts either into the wire ``transcript.failed``.
        """
        with self._model_lock:
            if self._model is None:
                from parakeet_mlx import from_pretrained  # type: ignore

                self._model = from_pretrained(self._model_id)
            return self._model

    async def start(self) -> None:
        # Eager import; fail fast if the ``stt-server-parakeet`` extra is not
        # installed. The model itself is NOT loaded here — see ``_get_model``.
        import parakeet_mlx  # type: ignore # noqa: F401

    async def open_stream(self, *, language: str | None = None) -> "_ParakeetStream":
        return _ParakeetStream(language, self._decode_lock, self._thread_lock, self)

    async def close(self) -> None:
        # Give any in-flight Parakeet decode a bounded window to finish
        # flushing Metal work before the process exits. Without this, SIGTERM
        # during a decode can leave a Metal command buffer mid-commit and trip
        # ``-[IOGPUMetalCommandBuffer validate]`` at process exit. Same crash
        # class and same drain as ``MLXWhisperBackend.close()``.
        timeout_s = 3.0
        drained = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._wait_inflight_drained(timeout_s)
        )
        if not drained:
            logger.warning(
                "parakeet: in-flight decode did not finish within %.1fs; "
                "Metal assertion possible at process exit",
                timeout_s,
            )
