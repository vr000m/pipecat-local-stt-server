"""Pipecat integration example: a ``SegmentedSTTService`` backed by this server.

This is a *runnable reference*, not part of the shipped library — it imports
``pipecat`` (a dependency this package deliberately does not declare). Install
Pipecat alongside the client extra to try it::

    uv pip install "pipecat-ai" "pipecat-local-stt-server[client]"

Architecture recap (why there is no "pick a backend" knob here)
---------------------------------------------------------------
The **server** owns the ASR backend, pinned at launch:
``python -m stt_server serve --backend {mlx,parakeet} --model <repo>``. A
client — including this service — only points at an *endpoint* (a UDS socket,
or host+port) and transcribes against whatever backend that server was started
with. To switch Whisper <-> Parakeet you start a different server (or a second
one on its own socket) and point this service at it; you do not change client
code. ``server.hello`` reports ``backend.name`` / ``backend.model`` so you can
*assert* what you connected to (see ``_log_backend`` below), but never *choose*
it from the client.

``SegmentedSTTService`` hands ``run_stt`` one complete utterance (the audio
between VAD start/stop) as raw PCM16 bytes. That maps cleanly onto this
server's commit-oriented protocol: append the segment, ``commit``, drain to the
``...transcription.completed`` event, emit one ``TranscriptionFrame``.

Audio format: the server's wire format is pinned to 16 kHz mono PCM16. Run your
Pipecat transport / pipeline at ``sample_rate=16000`` so the bytes handed to
``run_stt`` already match; this example guards against a mismatch rather than
resampling.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

# Pipecat is an optional, example-only dependency — imported at module top so a
# misconfigured install fails loudly here rather than mid-pipeline. ``loguru``
# is Pipecat's logging convention and ships as one of its dependencies.
from loguru import logger
from pipecat.frames.frames import ErrorFrame, Frame, TranscriptionFrame
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.utils.time import time_now_iso8601

from ..client import TranscriptionClient
from ..protocol import (
    AUDIO_SAMPLE_RATE_HZ,
    EVT_SESSION_CLOSED,
    EVT_TRANSCRIPT_COMPLETED,
)


class LocalWebSocketSTTService(SegmentedSTTService):
    """Transcribe each VAD segment via a local ``stt_server``.

    Pass exactly one endpoint form:

    - ``socket_path="~/Library/Caches/pipecat-stt/stt.sock"`` (UDS, recommended
      for local use — no port, no token), or
    - ``host="127.0.0.1", port=8765`` (loopback TCP; pair with ``auth_token``
      if the server was started with one).
    """

    def __init__(
        self,
        *,
        socket_path: str | None = None,
        host: str | None = None,
        port: int | None = None,
        auth_token: str | None = None,
        sample_rate: int | None = None,
        **kwargs,
    ) -> None:
        super().__init__(sample_rate=sample_rate, **kwargs)
        self._endpoint = {
            "socket_path": socket_path,
            "host": host,
            "port": port,
            "auth_token": auth_token,
        }
        self._client: TranscriptionClient | None = None

    async def _ensure_connected(self) -> TranscriptionClient:
        if self._client is not None:
            return self._client
        client = TranscriptionClient(
            socket_path=self._endpoint["socket_path"],
            host=self._endpoint["host"],
            port=self._endpoint["port"],
            auth_token=self._endpoint["auth_token"],
        )
        hello = await client.connect()
        self._log_backend(hello)
        # Server-side VAD is not implemented (V1); segmentation is done here by
        # SegmentedSTTService, so the session must disable turn detection.
        await client.update_session(turn_detection=None)
        self._client = client
        return client

    def _log_backend(self, hello: dict) -> None:
        backend = hello.get("backend") or {}
        # Optional hard assert: uncomment to fail fast if you connected to the
        # wrong ASR (e.g. expected Parakeet, got Whisper on a stale socket).
        #   assert backend.get("name") == "parakeet", backend
        logger.info(
            "stt_server backend: {} (model: {})",
            backend.get("name"),
            backend.get("model"),
        )

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Transcribe one complete VAD segment.

        ``audio`` is the full utterance as 16 kHz mono PCM16 bytes.
        """
        if self.sample_rate != AUDIO_SAMPLE_RATE_HZ:
            yield ErrorFrame(
                f"stt_server requires {AUDIO_SAMPLE_RATE_HZ} Hz mono PCM16, "
                f"but the pipeline is running at {self.sample_rate} Hz. "
                "Configure the transport/pipeline sample_rate to 16000."
            )
            return

        try:
            client = await self._ensure_connected()
            await client.send_audio(audio)
            await client.commit()

            async for ev in client.events():
                etype = ev.get("type")
                if etype == EVT_TRANSCRIPT_COMPLETED:
                    text = ev.get("transcript") or ""
                    if text.strip():
                        yield TranscriptionFrame(
                            text,
                            "",  # user_id — single-speaker session
                            time_now_iso8601(),
                        )
                    return
                if etype == "error":
                    yield ErrorFrame(f"stt_server error: {ev.get('message')}")
                    return
                if etype == EVT_SESSION_CLOSED:
                    yield ErrorFrame("stt_server closed the session before completing")
                    return
        except Exception as exc:  # noqa: BLE001 — surface as a pipeline frame
            # Drop the dead client so the next segment reconnects.
            self._client = None
            yield ErrorFrame(f"stt_server transcription failed: {exc}")

    async def cleanup(self) -> None:
        if self._client is not None:
            try:
                await self._client.close_session()
            finally:
                await self._client.close()
                self._client = None
        await super().cleanup()
