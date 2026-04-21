"""Example: stream a WAV file (16 kHz mono PCM16) to the server.

Usage::

    python -m stt_server.examples.file_stream --host 127.0.0.1 --port 8765 path/to/file.wav
"""

from __future__ import annotations

import argparse
import asyncio
import wave

from ..client import TranscriptionClient
from ..protocol import (
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE_HZ,
    AUDIO_SAMPLE_WIDTH_BYTES,
    EVT_TRANSCRIPT_COMPLETED,
    EVT_SESSION_CLOSED,
)


async def run(path: str, host: str, port: int) -> None:
    with wave.open(path, "rb") as wf:
        if (
            wf.getframerate() != AUDIO_SAMPLE_RATE_HZ
            or wf.getnchannels() != AUDIO_CHANNELS
            or wf.getsampwidth() != AUDIO_SAMPLE_WIDTH_BYTES
        ):
            raise SystemExit("file must be 16 kHz mono PCM16")
        pcm = wf.readframes(wf.getnframes())

    async with TranscriptionClient(host=host, port=port) as client:
        await client.update_session(turn_detection=None)
        # Stream in 100 ms chunks so the server exercises its append path.
        chunk = AUDIO_SAMPLE_RATE_HZ * AUDIO_CHANNELS * AUDIO_SAMPLE_WIDTH_BYTES // 10
        for i in range(0, len(pcm), chunk):
            await client.send_audio(pcm[i : i + chunk])
        await client.commit()

        async for ev in client.events():
            t = ev.get("type")
            if t == EVT_TRANSCRIPT_COMPLETED:
                print("transcript:", ev.get("transcript"))
                await client.close_session()
            elif t == EVT_SESSION_CLOSED:
                return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    asyncio.run(run(args.path, args.host, args.port))


if __name__ == "__main__":
    main()
