"""Kyutai streaming STT backend for Apple Silicon via ``moshi_mlx``.

Kyutai's delayed-streams models (``kyutai/stt-1b-en_fr-mlx``: English and
French, CC-BY-4.0) run frame by frame: 24 kHz audio is encoded by the Mimi codec
into 80 ms frames, and the language model emits a text token per frame about
0.5 s behind the audio. V1 of this server is commit-oriented, so on ``end()``
the whole buffered utterance is resampled, padded as the model's ``stt_config``
asks (leading silence; trailing silence to flush the text delay), run through
Mimi + the LM, and returned as one ``delta`` plus one ``completed`` (the same
contract as the other backends). The LM's KV cache is a rotating window, so an
utterance can be any length. ``kyutai/stt-2.6b-en-mlx`` (English, larger and
slower; 1 s prefix, 2.5 s delay) loads the same way via ``--model``. The
reference loop is Kyutai's ``delayed-streams-modeling/scripts/stt_from_file_mlx.py``.

Mirrors ``parakeet.py`` in structure: a lazy import of the optional ``kyutai``
extra, a backend-scoped asyncio + threading decode-lock pair, daemon-thread
decodes, and an in-flight drain in ``close()`` (Metal work must not be torn
down mid-commit at SIGTERM).

``moshi_mlx`` pins ``mlx<0.27`` and ``huggingface-hub<0.29``, which conflict
with the other MLX extras, so ``kyutai`` is declared as a conflicting extra in
``pyproject.toml``: install it in its own environment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import AsyncGenerator

from ..backend import TranscriptEvent
from ._thread_util import run_in_daemon_thread

logger = logging.getLogger("stt_server.backends.kyutai")

DEFAULT_KYUTAI_MODEL = "kyutai/stt-1b-en_fr-mlx"

# Model frame: 80 ms at Mimi's 24 kHz.
_MODEL_RATE_HZ = 24_000
_FRAME_SAMPLES = 1920
# Trailing silence flushes the text the model emits ``audio_delay_seconds`` behind
# the audio: that delay plus a second, and never less than the reference's 2 s.
_MIN_FLUSH_SECONDS = 2.0
# Text tokens that carry no text (padding / end of word markers).
_SKIP_TOKENS = (0, 3)


def resample_16k_to_24k(audio):
    """Polyphase 2:3 resample of a float32 array: zero-stuff x3, windowed-sinc
    low-pass at the source Nyquist, keep every second sample."""
    import numpy as np

    if audio.size == 0:
        return audio.astype(np.float32)
    up = np.zeros(audio.size * 3, dtype=np.float32)
    up[::3] = audio
    n = np.arange(-48, 49, dtype=np.float64)
    fc = 8000 / 48000  # 8 kHz source Nyquist at the 48 kHz intermediate rate
    taps = 2 * fc * np.sinc(2 * fc * n) * np.hamming(n.size) * 3
    return np.convolve(up, taps.astype(np.float32), mode="same")[::2]


class _KyutaiStream:
    def __init__(
        self,
        language: str | None,
        decode_lock: asyncio.Lock,
        thread_lock: threading.Lock,
        backend: "KyutaiBackend",
    ) -> None:
        # ``language`` is accepted for the protocol and ignored: the model
        # transcribes English and French without a language prompt.
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
        # Serialise decodes across sessions: one model, one Metal device, and
        # stateful Mimi / LM caches that are reset per utterance.
        async with self._decode_lock:
            if self._cancelled:
                return
            self._backend._mark_inflight_start()
            self._result = await run_in_daemon_thread(
                self._decode_sync, thread_name="kyutai-decode"
            )

    async def cancel(self) -> None:
        self._cancelled = True
        self._ended = True

    def _decode_sync(self) -> str:
        try:
            pcm = bytes(self._buf)
            if not pcm:
                return ""
            with self._thread_lock:
                return self._backend._transcribe(pcm)
        finally:
            self._backend._mark_inflight_end()

    async def events(self) -> AsyncGenerator[TranscriptEvent, None]:
        if self._cancelled or self._result is None:
            return
        text = self._result
        if text:
            yield TranscriptEvent(kind="delta", text=text)
        yield TranscriptEvent(kind="completed", text=text)


class KyutaiBackend:
    """ASR backend backed by Kyutai's streaming STT models via ``moshi_mlx``."""

    backend_name = "kyutai"

    def __init__(self, *, model: str = DEFAULT_KYUTAI_MODEL) -> None:
        self._model_id = model
        self.model = model
        # Loaded on the first decode (a ~2.4 GB checkpoint), like parakeet.
        self._loaded: dict | None = None
        self._model_lock = threading.Lock()
        self._decode_lock = asyncio.Lock()
        self._thread_lock = threading.Lock()
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
                lambda: self._inflight_count == 0, timeout=timeout_s
            )

    def _get_model(self) -> dict:
        """Load config, Mimi, the LM and the tokenizer once (decode thread)."""
        with self._model_lock:
            if self._loaded is not None:
                return self._loaded
            import mlx.core as mx  # type: ignore
            import mlx.nn as nn  # type: ignore
            import sentencepiece  # type: ignore
            from huggingface_hub import hf_hub_download  # type: ignore
            from moshi_mlx import models, utils  # type: ignore

            raw = json.loads(Path(hf_hub_download(self._model_id, "config.json")).read_text())
            mimi_weights = hf_hub_download(self._model_id, raw["mimi_name"])
            lm_weights = hf_hub_download(self._model_id, raw.get("moshi_name", "model.safetensors"))
            tokenizer = hf_hub_download(self._model_id, raw["tokenizer_name"])

            lm = models.Lm(models.LmConfig.from_config_dict(raw))
            lm.set_dtype(mx.bfloat16)
            if lm_weights.endswith(".q4.safetensors"):
                nn.quantize(lm, bits=4, group_size=32)
            elif lm_weights.endswith(".q8.safetensors"):
                nn.quantize(lm, bits=8, group_size=64)
            lm.load_weights(lm_weights, strict=True)
            mimi = models.mimi.Mimi(models.mimi_202407(32))
            mimi.load_pytorch_weights(str(mimi_weights), strict=True)
            lm.warmup()
            stt = raw.get("stt_config") or {}
            delay = float(stt.get("audio_delay_seconds", 0.5))
            self._loaded = {
                "prefix_samples": int(
                    float(stt.get("audio_silence_prefix_seconds", 0.0)) * _MODEL_RATE_HZ
                ),
                "flush_samples": int(max(_MIN_FLUSH_SECONDS, delay + 1.0) * _MODEL_RATE_HZ),
                "mx": mx,
                "models": models,
                "utils": utils,
                "lm": lm,
                "mimi": mimi,
                "tokenizer": sentencepiece.SentencePieceProcessor(tokenizer),  # type: ignore
            }
            logger.info("kyutai: loaded %s", self._model_id)
            return self._loaded

    def _transcribe(self, pcm16: bytes) -> str:
        """One utterance of pcm16 LE at 16 kHz -> text. Caller holds the thread lock."""
        import numpy as np

        m = self._get_model()
        mx, models, utils = m["mx"], m["models"], m["utils"]
        audio = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        audio = np.concatenate(
            [
                np.zeros(m["prefix_samples"], dtype=np.float32),
                resample_16k_to_24k(audio),
                np.zeros(m["flush_samples"], dtype=np.float32),
            ]
        )
        frames = audio.size // _FRAME_SAMPLES
        x = mx.array(audio[None, : frames * _FRAME_SAMPLES])
        # Fresh streaming state per utterance: Mimi's encoder state and the LM's
        # KV cache (``LmGen`` does not reset it).
        m["mimi"].reset_all()
        for cache in m["lm"].transformer_cache:
            cache.reset()
        gen = models.LmGen(
            model=m["lm"],
            max_steps=frames + 8,
            text_sampler=utils.Sampler(top_k=25, temp=0),
            audio_sampler=utils.Sampler(top_k=250, temp=0.8),
            check=False,
        )
        pieces: list[str] = []
        for start in range(0, frames * _FRAME_SAMPLES, _FRAME_SAMPLES):
            tokens = (
                m["mimi"].encode_step(x[:, None, start : start + _FRAME_SAMPLES]).transpose(0, 2, 1)
            )
            token = gen.step(tokens[0])[0].item()
            if token not in _SKIP_TOKENS:
                pieces.append(m["tokenizer"].id_to_piece(token).replace("▁", " "))
        return "".join(pieces).strip()

    async def start(self) -> None:
        # Fail fast if the extra is missing; the model loads on the first decode.
        try:
            import moshi_mlx  # type: ignore # noqa: F401
            import sentencepiece  # type: ignore # noqa: F401
        except ImportError as exc:
            missing = getattr(exc, "name", None) or "moshi_mlx"
            raise ModuleNotFoundError(
                f"the 'kyutai' extra is not installed or failed to import "
                f"({missing}) — run: uv sync --extra kyutai --inexact"
            ) from exc

    async def open_stream(self, *, language: str | None = None) -> "_KyutaiStream":
        return _KyutaiStream(language, self._decode_lock, self._thread_lock, self)

    async def close(self) -> None:
        timeout_s = 3.0
        drained = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._wait_inflight_drained(timeout_s)
        )
        if not drained:
            logger.warning(
                "kyutai: in-flight decode did not finish within %.1fs; "
                "Metal assertion possible at process exit",
                timeout_s,
            )


def prefetch_specs(model: str | None) -> list:
    """Snapshots ``python -m stt_server prefetch --backend kyutai`` fetches: the
    config, Mimi and LM weights and the tokenizer that ``_get_model`` loads."""
    from ..prefetch import SnapshotSpec

    return [
        SnapshotSpec(
            model or DEFAULT_KYUTAI_MODEL,
            allow_patterns=("*.json", "*.safetensors", "*.model"),
            required=("config.json", "model.safetensors", "mimi*.safetensors", "*.model"),
        )
    ]
