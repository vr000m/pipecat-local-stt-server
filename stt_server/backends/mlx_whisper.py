"""MLX Whisper backend for Apple Silicon.

V1 is commit-oriented: we accumulate PCM16LE audio until ``end()`` is called,
then run a single decode and emit one ``delta`` plus one ``completed`` event.
True streaming partials are deferred to future backends.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

import numpy as np

from ..backend import BackendStream, TranscriptEvent
from ..protocol import AUDIO_SAMPLE_RATE_HZ

logger = logging.getLogger("stt_server.backends.mlx")


class _MLXStream:
    def __init__(self, model: str, language: str | None) -> None:
        self._model = model
        self._language = language
        self._buf = bytearray()
        self._ended = False
        self._cancelled = False
        self._result: str | None = None

    async def feed(self, chunk: bytes) -> None:
        if self._cancelled:
            return
        self._buf.extend(chunk)

    async def end(self) -> None:
        if self._ended or self._cancelled:
            return
        self._ended = True
        loop = asyncio.get_running_loop()
        self._result = await loop.run_in_executor(None, self._decode_sync)

    async def cancel(self) -> None:
        self._cancelled = True
        self._ended = True

    def _decode_sync(self) -> str:
        import mlx_whisper  # type: ignore

        audio = np.frombuffer(bytes(self._buf), dtype=np.int16).astype(np.float32) / 32768.0
        if audio.size == 0:
            return ""
        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self._model,
            language=self._language,
            fp16=True,
            verbose=False,
            sample_rate=AUDIO_SAMPLE_RATE_HZ,
        )
        return (result.get("text") or "").strip()

    async def events(self) -> AsyncIterator[TranscriptEvent]:
        if self._cancelled or self._result is None:
            return
        text = self._result
        if text:
            yield TranscriptEvent(kind="delta", text=text)
        yield TranscriptEvent(kind="completed", text=text)


class MLXWhisperBackend:
    def __init__(self, *, model: str = "mlx-community/whisper-large-v3-turbo") -> None:
        self._model = model

    async def start(self) -> None:
        # Eager import; fail fast if the extra isn't installed.
        import mlx_whisper  # type: ignore # noqa: F401

    async def open_stream(self, *, language: str | None = None) -> BackendStream:
        return _MLXStream(self._model, language)

    async def close(self) -> None:
        return None
