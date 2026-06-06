"""MLX Whisper backend for Apple Silicon.

V1 is commit-oriented: we accumulate PCM16LE audio until ``end()`` is called,
then run a single decode and emit one ``delta`` plus one ``completed`` event.
True streaming partials are deferred to future backends.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import AsyncGenerator

import numpy as np

from stt_server.env import env_bool_first, env_float_first
from stt_server.text_quality import dominant_unigram_ratio, is_degenerate

from ..backend import TranscriptEvent
from ._thread_util import run_in_daemon_thread

logger = logging.getLogger("stt_server.backends.mlx")


# Whisper hallucination-suppression knobs. Defaults match OpenAI's reference
# Whisper EXCEPT condition_on_previous_text, which we disable: feeding the
# previous chunk's emitted text back as a decoder prompt creates a
# self-amplifying loop on hallucinated tokens (e.g. "subscription" walls
# from YouTube outro training data).
#
# Resolved at call time (not import time) so tests can monkeypatch env vars.
_BOOL_DEFAULT_CONDITION = False
_FLOAT_DEFAULT_COMPRESSION = 2.4
_FLOAT_DEFAULT_LOGPROB = -1.0
_FLOAT_DEFAULT_NO_SPEECH = 0.6


def _normalize_language(language: str | None) -> str | None:
    """Recast the cross-backend ``"auto"`` sentinel (and blank) to ``None``.

    Clients connect to a *socket* and don't know which backend is behind it,
    so a uniform ``"auto"`` is the natural "detect the language" request. But
    Whisper has no ``"auto"`` token: ``mlx_whisper.transcribe(language="auto")``
    raises ``ValueError: Unsupported language: auto`` in its tokenizer (which
    accepts only real codes/names), and ``language=None`` is how Whisper itself
    asks for auto-detection. We translate here — server-side, in the one
    backend that needs it — rather than pushing the quirk onto every client.
    A real code (``"en"``, ``"es"``) passes through unchanged.

    The sibling backends need no such translation: ``parakeet`` ignores
    ``language`` entirely, and ``nemotron`` accepts ``"auto"`` as a first-class
    prompt key — so the sentinel is recast only here, not generically, to avoid
    coupling it to those backends' own defaults.
    """
    if language is None:
        return None
    return None if language.strip().lower() in ("", "auto") else language


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
            self._result = await run_in_daemon_thread(self._decode_sync, thread_name="mlx-decode")

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
                # Resolve suppression knobs at call time so tests / operators
                # can monkeypatch env vars without re-importing the module.
                # Canonical PIPECAT_STT_* name wins; the legacy KODA_STT_*
                # name is still honoured as a deprecated alias.
                condition_on_previous_text = env_bool_first(
                    "PIPECAT_STT_WHISPER_CONDITION_ON_PREVIOUS_TEXT",
                    "KODA_STT_WHISPER_CONDITION_ON_PREVIOUS_TEXT",
                    default=_BOOL_DEFAULT_CONDITION,
                )
                compression_ratio_threshold = env_float_first(
                    "PIPECAT_STT_WHISPER_COMPRESSION_RATIO_THRESHOLD",
                    "KODA_STT_WHISPER_COMPRESSION_RATIO_THRESHOLD",
                    default=_FLOAT_DEFAULT_COMPRESSION,
                )
                logprob_threshold = env_float_first(
                    "PIPECAT_STT_WHISPER_LOGPROB_THRESHOLD",
                    "KODA_STT_WHISPER_LOGPROB_THRESHOLD",
                    default=_FLOAT_DEFAULT_LOGPROB,
                )
                no_speech_threshold = env_float_first(
                    "PIPECAT_STT_WHISPER_NO_SPEECH_THRESHOLD",
                    "KODA_STT_WHISPER_NO_SPEECH_THRESHOLD",
                    default=_FLOAT_DEFAULT_NO_SPEECH,
                )
                result = mlx_whisper.transcribe(
                    audio,
                    path_or_hf_repo=self._model,
                    # "auto"/blank -> None so Whisper auto-detects instead of
                    # raising on an unknown language token (see _normalize_language).
                    language=_normalize_language(self._language),
                    fp16=True,
                    verbose=False,
                    condition_on_previous_text=condition_on_previous_text,
                    compression_ratio_threshold=compression_ratio_threshold,
                    logprob_threshold=logprob_threshold,
                    no_speech_threshold=no_speech_threshold,
                )
            # Post-decode degenerate-output filter. Whisper occasionally
            # emits a single segment that is one token repeated dozens of
            # times (e.g. "subscription subscription ...") even with the
            # decode-time suppression knobs in place. We catch those at the
            # segment level and replace the text with empty so the rest of
            # the pipeline treats the segment as silence.
            segments = result.get("segments") or []
            if segments:
                kept: list[str] = []
                for seg in segments:
                    seg_text = seg.get("text") or ""
                    if seg_text and is_degenerate(seg_text):
                        ratio, token, total = dominant_unigram_ratio(seg_text)
                        logger.warning(
                            "mlx_whisper.degenerate_dropped tokens=%d dominant=%r ratio=%.2f",
                            total,
                            token,
                            ratio,
                        )
                        continue
                    kept.append(seg_text)
                # Build the on-wire return value AND the safety-net check
                # text from the same ``kept`` list, side-by-side, so a future
                # refactor of the segment loop cannot let them diverge.
                #
                # ``joined_kept`` is the on-wire return value — unmodified
                # ``"".join(kept)`` so downstream readers see exactly what
                # mlx_whisper produced (segments carry a leading space).
                #
                # ``check_text`` is a whitespace-normalised view used only
                # by the post-join safety net below. Whisper sometimes splits
                # a wall into multiple short segments (5-8 tokens each),
                # each below MIN_TOKENS and individually passing the per-
                # segment check; re-running ``is_degenerate`` on the joined
                # survivors catches the reconstructed wall. Normalised
                # separately so the safety net is robust if a future backend
                # ships space-trimmed segments.
                joined_kept = "".join(kept).strip()
                check_text = " ".join(s.strip() for s in kept if s.strip()).strip()
                if check_text and is_degenerate(check_text):
                    ratio, token, total = dominant_unigram_ratio(check_text)
                    logger.warning(
                        "mlx_whisper.degenerate_dropped post-join tokens=%d dominant=%r ratio=%.2f",
                        total,
                        token,
                        ratio,
                    )
                    return ""
                return joined_kept
            # Fallback: backend returned no segments (older mlx_whisper, or
            # no-speech path). Apply the filter to the joined text directly
            # so a wholly-degenerate decode is still suppressed.
            joined = result.get("text") or ""
            if joined and is_degenerate(joined):
                ratio, token, total = dominant_unigram_ratio(joined)
                logger.warning(
                    "mlx_whisper.degenerate_dropped tokens=%d dominant=%r ratio=%.2f",
                    total,
                    token,
                    ratio,
                )
                return ""
            return joined.strip()
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
    backend_name = "mlx"

    def __init__(self, *, model: str = "mlx-community/whisper-large-v3-turbo") -> None:
        self._model = model
        # Public identity for the server.hello / server.status `backend` field.
        self.model = model
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
        # measured on 2026-04-22.
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
