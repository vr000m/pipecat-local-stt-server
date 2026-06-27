"""Nemotron 3.5 ASR backend for Apple Silicon via ``mlx-audio``.

V1 is commit-oriented: we accumulate PCM16LE audio until ``end()`` is called,
then run a single offline ``generate()`` decode and emit one ``delta`` plus one
``completed`` event. True streaming partials are deferred — ``mlx-audio``
exposes a ``stream_generate()`` entrypoint, but it is intentionally unused
until a streaming wire protocol lands; Nemotron runs after smart-turn, so it
always sees a complete utterance and offline ``generate()`` is sufficient.

Mirrors ``stt_server/backends/parakeet.py`` in structure: lazy import of the
optional ``mlx-audio`` package, a backend-scoped asyncio + threading decode
lock pair, an in-flight drain in ``close()`` for SIGTERM-mid-decode crash
isolation, and the same empty-decode contract (``delta`` only on non-empty
text, ``completed`` always).

Three-way ``language`` contract across backends:
  * ``parakeet`` accepts-and-ignores ``language`` (``parakeet.py:62-68``) —
    its TDT models are language-pinned by model id and ``transcribe`` exposes
    no per-call language kwarg;
  * ``mlx_whisper`` forwards ``language`` to its decoder, but first recasts the
    cross-backend ``"auto"``/blank sentinel to ``None`` (Whisper's own
    auto-detect; it has no ``"auto"`` token and would raise on one) — see
    ``mlx_whisper._normalize_language``;
  * ``nemotron`` (this module) forwards ``language`` to ``generate()`` and,
    when the client sends ``None``, falls back to ``DEFAULT_NEMOTRON_LANGUAGE``
    (``"auto"`` — the model's own LID-over-40-locales default), so a client
    ``"auto"`` and a client ``None`` both reach the model's LID mode here.
There is deliberately no shared abstraction/registry — the per-call kwarg plus
the named ``DEFAULT_NEMOTRON_LANGUAGE`` constant is the minimal call. The net
effect across all three is that a uniform client ``"auto"`` (or ``None``) means
"auto-detect" everywhere, without the client needing to know the backend.
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

logger = logging.getLogger("stt_server.backends.nemotron")

# Default model for ``--backend nemotron``. Exported so Phase 2's
# backend-aware ``--model`` default imports it rather than hardcoding a second
# copy (single source of truth, mirrors ``DEFAULT_PARAKEET_MODEL``).
DEFAULT_NEMOTRON_MODEL = "mlx-community/nemotron-3.5-asr-streaming-0.6b"

# Default ``language`` forwarded to ``generate()`` when the client supplies no
# language. ``"auto"`` is a verified accepted prompt key AND the model's own
# default (language identification over 40+ locales). Named so the policy is a
# one-line swap (single source of truth, mirrors ``DEFAULT_NEMOTRON_MODEL``).
DEFAULT_NEMOTRON_LANGUAGE = "auto"


class _NemotronStream:
    def __init__(
        self,
        language: str | None,
        decode_lock: asyncio.Lock,
        thread_lock: threading.Lock,
        backend: "NemotronBackend",
    ) -> None:
        # ``language`` IS forwarded to the decoder (unlike Parakeet, which
        # accepts-and-ignores it). Nemotron 3.5 ASR does per-call language
        # selection / identification, so the client-supplied value is honoured.
        # ``None`` means "client did not specify" -> at decode time we fall back
        # to ``DEFAULT_NEMOTRON_LANGUAGE`` ("auto") rather than passing ``None``,
        # keeping the model's LID default explicit at the call site.
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
        # Serialize decodes across all sessions: mlx-audio holds one cached
        # model and MLX/Metal is not safe for concurrent calls against it. The
        # asyncio lock orders decodes event-loop side; the threading lock held
        # inside ``_decode_sync`` keeps a second decode thread blocked even
        # when the awaiter here is cancelled. Same pattern as ``_ParakeetStream``.
        async with self._decode_lock:
            if self._cancelled:
                return
            # Mark in-flight BEFORE spawning the daemon thread so
            # ``backend.close()`` observes it immediately; the daemon thread
            # owns the decrement in a finally block.
            self._backend._mark_inflight_start()
            self._result = await run_in_daemon_thread(
                self._decode_sync, thread_name="nemotron-decode"
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
            # mlx-audio's ``generate()`` takes a file *path* (it runs
            # ``load_audio`` internally) — exactly like parakeet's
            # ``transcribe(path)`` and unlike ``mlx_whisper.transcribe`` it does
            # NOT accept a raw audio array. Materialise the buffered PCM16LE
            # audio as a temp WAV and hand generate() the path. The wire
            # protocol pins channels / sample width / rate upstream, so the WAV
            # header is fully determined by the protocol constants. The WAV
            # holds raw utterance audio (PII); it is written inside the
            # backend's private 0o700 temp dir, never the world-listable system
            # temp dir — see ``NemotronBackend.__init__``.
            fd, wav_path = tempfile.mkstemp(suffix=".wav", dir=self._backend._tmpdir)
            os.close(fd)
            try:
                with wave.open(wav_path, "wb") as wav:
                    wav.setnchannels(AUDIO_CHANNELS)
                    wav.setsampwidth(AUDIO_SAMPLE_WIDTH_BYTES)
                    wav.setframerate(AUDIO_SAMPLE_RATE_HZ)
                    wav.writeframes(pcm)
                # Effective language: forward the client value, falling back to
                # the named "auto" default when the client did not specify one.
                lang = self._language if self._language is not None else DEFAULT_NEMOTRON_LANGUAGE
                # Hold the backend-scope threading lock for the entire decode
                # so a second decode thread started after this stream's
                # asyncio awaiter was cancelled still blocks until this one
                # completes. ``_get_model()`` is acquired INSIDE the lock: a
                # first-ever ``load`` does Metal work, and loading under the
                # lock keeps it from racing another thread's ``generate()`` on
                # the Metal device.
                with self._thread_lock:
                    model = self._backend._get_model()  # lazy load; may raise
                    # Nemotron's ``generate()`` decodes the full file offline.
                    # Unlike parakeet-mlx it takes no ``chunk_duration`` /
                    # ``overlap_duration`` kwargs — just the path and language.
                    result = model.generate(wav_path, language=lang)
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
        # Match the empty-decode contract: ``delta`` only when the transcript
        # text is non-empty (Nemotron on near-silence can produce empty text);
        # ``completed`` always.
        if text:
            yield TranscriptEvent(kind="delta", text=text)
        yield TranscriptEvent(kind="completed", text=text)


class NemotronBackend:
    """ASR backend backed by NVIDIA Nemotron 3.5 ASR models via ``mlx-audio``."""

    backend_name = "nemotron"

    def __init__(self, *, model: str = DEFAULT_NEMOTRON_MODEL) -> None:
        self._model_id = model
        # Public identity for the server.hello / server.status `backend` field.
        self.model = model
        # The loaded mlx-audio model. ``None`` until the first decode — ``load``
        # is eager and pulls a multi-hundred-MB checkpoint, so it is deferred
        # out of ``start()`` (which only fails fast on a missing package).
        # Guarded by ``_model_lock`` so two concurrent first decodes load it
        # exactly once.
        self._model = None
        self._model_lock = threading.Lock()
        self._decode_lock = asyncio.Lock()
        # Backend-scope thread lock — shared across every stream so concurrent
        # sessions truly serialize on the MLX/Metal side. mlx-audio is not
        # verified concurrency-safe (one cached model, Metal command buffers),
        # so the lock pair is kept exactly as ``ParakeetBackend`` does.
        self._thread_lock = threading.Lock()
        # Backend-scope in-flight counter for the ``close()`` drain. The
        # SIGTERM-mid-decode Metal command-buffer assertion class is
        # assumed-by-analogy here: it was observed with MLX Whisper / parakeet
        # and is plausible for mlx-audio (a different package, FastConformer-RNNT
        # rather than TDT), not verified identical. The in-flight drain is a
        # harmless no-op if the crash class does not in fact reach mlx-audio.
        self._inflight_count = 0
        self._inflight_cond = threading.Condition()
        # Private 0o700 temp dir for per-utterance decode WAVs. ``mkdtemp``
        # makes it owner-only-traversable, so raw utterance audio (PII) is
        # never written to the world-listable system temp dir and an orphaned
        # WAV (SIGKILL between mkstemp and unlink) stays unreadable by other
        # local users. Removed in ``close()``.
        self._tmpdir = tempfile.mkdtemp(prefix="pipecat-stt-nemotron-")

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
        """Lazily load the Nemotron model on first decode.

        Called from the decode daemon thread. ``load`` is eager and downloads a
        multi-hundred-MB checkpoint; deferring it here keeps ``start()`` cheap.
        A model-load failure raises here and propagates out of ``events()`` /
        ``end()`` exactly like a per-utterance decode failure — the server
        converts either into the wire ``transcript.failed``.
        """
        with self._model_lock:
            if self._model is None:
                from mlx_audio.stt import load  # type: ignore

                self._model = load(self._model_id)
            return self._model

    async def start(self) -> None:
        # Eager import; fail fast before the socket binds if the ``mlx-audio``
        # package is not installed. The model itself is NOT loaded here — see
        # ``_get_model``. Re-raise a missing module as an actionable message —
        # the bare ModuleNotFoundError is otherwise a cryptic crash-loop in the
        # LaunchAgent log. _cmd_serve turns this into ``stt_server: <msg>`` +
        # exit 1, and ``just stt-install nemotron`` self-heals it via _ensure-extra.
        try:
            from mlx_audio.stt import load  # type: ignore # noqa: F401
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                f"the 'nemotron' extra is not installed (missing module: {exc.name}) "
                "— run: uv sync --extra nemotron --inexact"
            ) from exc

    async def open_stream(self, *, language: str | None = None) -> "_NemotronStream":
        return _NemotronStream(language, self._decode_lock, self._thread_lock, self)

    async def close(self) -> None:
        # Give any in-flight Nemotron decode a bounded window to finish flushing
        # Metal work before the process exits. The SIGTERM-mid-decode Metal
        # command-buffer assertion class is assumed-by-analogy for mlx-audio
        # (observed with MLX Whisper / parakeet; plausible but not verified for
        # this package) — the drain is a harmless no-op if it never reaches
        # mlx-audio. Same bounded drain as ``ParakeetBackend.close()``.
        timeout_s = 3.0
        drained = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._wait_inflight_drained(timeout_s)
        )
        if not drained:
            logger.warning(
                "nemotron: in-flight decode did not finish within %.1fs; "
                "Metal assertion possible at process exit",
                timeout_s,
            )
        # Remove the private decode-WAV temp dir. ``ignore_errors`` keeps
        # shutdown best-effort — a leftover dir is harmless (0o700, owner-only)
        # and a fresh one is created on the next process start.
        shutil.rmtree(self._tmpdir, ignore_errors=True)
