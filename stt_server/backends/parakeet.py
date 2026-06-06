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
import contextlib
import logging
import os
import shutil
import tempfile
import threading
import wave
from typing import AsyncGenerator

from ..backend import TranscriptEvent
from ..protocol import (
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE_HZ,
    AUDIO_SAMPLE_WIDTH_BYTES,
)
from ._thread_util import run_in_daemon_thread

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
            self._result = await run_in_daemon_thread(
                self._decode_sync, thread_name="parakeet-decode"
            )

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
            pcm = bytes(self._buf)
            if not pcm:
                return ""
            # ``parakeet_mlx``'s ``transcribe()`` takes a file *path* (it runs
            # ``load_audio`` internally) — unlike ``mlx_whisper.transcribe`` it
            # does NOT accept a raw audio array. Materialise the buffered
            # PCM16LE audio as a temp WAV and hand transcribe() the path. The
            # wire protocol pins channels / sample width / rate upstream, so
            # the WAV header is fully determined by the protocol constants.
            # The WAV holds raw utterance audio (PII); it is written inside the
            # backend's private 0o700 temp dir, never the world-listable system
            # temp dir — see ``ParakeetBackend.__init__``.
            fd, wav_path = tempfile.mkstemp(suffix=".wav", dir=self._backend._tmpdir)
            os.close(fd)
            try:
                with wave.open(wav_path, "wb") as wav:
                    wav.setnchannels(AUDIO_CHANNELS)
                    wav.setsampwidth(AUDIO_SAMPLE_WIDTH_BYTES)
                    wav.setframerate(AUDIO_SAMPLE_RATE_HZ)
                    wav.writeframes(pcm)
                # Hold the backend-scope threading lock for the entire decode
                # so a second decode thread started after this stream's
                # asyncio awaiter was cancelled still blocks until this one
                # completes. ``_get_model()`` is acquired INSIDE the lock: a
                # first-ever ``from_pretrained`` does Metal work, and loading
                # under the lock keeps it from racing another thread's
                # ``transcribe()`` on the Metal device.
                with self._thread_lock:
                    model = self._backend._get_model()  # lazy load; may raise
                    # ``chunk_duration`` hands parakeet-mlx its
                    # chunk-and-concatenate path (cross-chunk token merge) so a
                    # 24-300 s utterance is chunked, never truncated.
                    result = model.transcribe(
                        wav_path,
                        chunk_duration=_CHUNK_DURATION_S,
                        overlap_duration=_OVERLAP_DURATION_S,
                    )
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(wav_path)
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

    backend_name = "parakeet"

    def __init__(self, *, model: str = DEFAULT_PARAKEET_MODEL) -> None:
        self._model_id = model
        # Public identity for the server.hello / server.status `backend` field.
        self.model = model
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
        # Private 0o700 temp dir for per-utterance decode WAVs. ``mkdtemp``
        # makes it owner-only-traversable, so raw utterance audio (PII) is
        # never written to the world-listable system temp dir and an orphaned
        # WAV (SIGKILL between mkstemp and unlink) stays unreadable by other
        # local users. Removed in ``close()``.
        self._tmpdir = tempfile.mkdtemp(prefix="pipecat-stt-parakeet-")

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
        # Eager import; fail fast if the ``parakeet`` extra is not
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
        # Remove the private decode-WAV temp dir. ``ignore_errors`` keeps
        # shutdown best-effort — a leftover dir is harmless (0o700, owner-only)
        # and a fresh one is created on the next process start.
        shutil.rmtree(self._tmpdir, ignore_errors=True)
