"""MLX Whisper backend for Apple Silicon.

V1 is commit-oriented: we accumulate PCM16LE audio until ``end()`` is called,
then run a single decode and emit one ``delta`` plus one ``completed`` event.
True streaming partials are deferred to future backends.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import AsyncGenerator

import numpy as np

from ..backend import TranscriptEvent

logger = logging.getLogger("stt_server.backends.mlx")


class _MLXStream:
    def __init__(
        self,
        model: str,
        language: str | None,
        decode_lock: asyncio.Lock,
        executor: concurrent.futures.Executor,
    ) -> None:
        self._model = model
        self._language = language
        self._buf = bytearray()
        self._ended = False
        self._cancelled = False
        self._result: str | None = None
        self._decode_lock = decode_lock
        self._executor = executor

    async def feed(self, chunk: bytes) -> None:
        if self._cancelled:
            return
        self._buf.extend(chunk)

    async def end(self) -> None:
        if self._ended or self._cancelled:
            return
        self._ended = True
        loop = asyncio.get_running_loop()
        # Serialize decodes across all sessions: MLX/Metal is not safe for
        # concurrent calls against the same cached model from multiple
        # threads, and the dedicated single-worker executor guarantees only
        # one decode thread exists in the process.
        async with self._decode_lock:
            if self._cancelled:
                return
            self._result = await loop.run_in_executor(self._executor, self._decode_sync)

    async def cancel(self) -> None:
        self._cancelled = True
        self._ended = True

    def _decode_sync(self) -> str:
        import mlx_whisper  # type: ignore

        audio = np.frombuffer(bytes(self._buf), dtype=np.int16).astype(np.float32) / 32768.0
        if audio.size == 0:
            return ""
        # mlx_whisper.transcribe resamples internally using its own constant;
        # audio must already be at AUDIO_SAMPLE_RATE_HZ (16 kHz), which the
        # protocol enforces on the wire. Do not pass sample_rate — it's not a
        # valid DecodingOptions kwarg.
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
        # Dedicated single-worker executor — avoids the default pool where
        # other asyncio.run_in_executor users could end up sharing threads.
        # Exactly one thread can be inside mlx_whisper.transcribe at a time.
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="mlx-decode"
        )

    async def start(self) -> None:
        # Eager import; fail fast if the extra isn't installed.
        import mlx_whisper  # type: ignore # noqa: F401

    async def open_stream(self, *, language: str | None = None) -> "_MLXStream":
        return _MLXStream(self._model, language, self._decode_lock, self._executor)

    async def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
