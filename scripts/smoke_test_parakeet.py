#!/usr/bin/env python3
"""Throwaway smoke test: decode one WAV through a real Parakeet stt_server.

Not part of the test suite — a one-command end-to-end check that the
``ParakeetBackend`` actually decodes against the real ``parakeet-mlx`` package
(the unit tests stub it). It starts its own ``stt_server`` on a private temp
UDS socket, streams one file, prints the transcript, and tears the server down.
The running Whisper agent and the bot are never touched.

Usage::

    uv run python scripts/smoke_test_parakeet.py path/to/audio.wav
    uv run python scripts/smoke_test_parakeet.py audio.wav --model mlx-community/parakeet-tdt-0.6b-v3

The WAV must be 16 kHz mono PCM16 (the V1 wire format). The FIRST run downloads
the ~1.5 GB Parakeet model — allow several minutes; later runs hit the cache.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import subprocess
import sys
import tempfile
import time
import wave

# Run from the repo root so ``stt_server`` imports resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stt_server.client import TranscriptionClient  # noqa: E402
from stt_server.protocol import (  # noqa: E402
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE_HZ,
    AUDIO_SAMPLE_WIDTH_BYTES,
    EVT_ERROR,
    EVT_SESSION_CLOSED,
    EVT_TRANSCRIPT_COMPLETED,
    EVT_TRANSCRIPT_DELTA,
    EVT_TRANSCRIPT_FAILED,
)


def _load_wav(path: str) -> bytes:
    """Read a 16 kHz mono PCM16 WAV, or exit with a clear message."""
    try:
        with wave.open(path, "rb") as wf:
            rate, chans, width = (
                wf.getframerate(),
                wf.getnchannels(),
                wf.getsampwidth(),
            )
            if (
                rate != AUDIO_SAMPLE_RATE_HZ
                or chans != AUDIO_CHANNELS
                or width != AUDIO_SAMPLE_WIDTH_BYTES
            ):
                raise SystemExit(
                    f"{path}: must be {AUDIO_SAMPLE_RATE_HZ} Hz mono PCM16, "
                    f"got {rate} Hz / {chans}ch / {width * 8}-bit. "
                    f"Convert with: ffmpeg -i {path} -ar 16000 -ac 1 -c:a pcm_s16le out.wav"
                )
            pcm = wf.readframes(wf.getnframes())
    except wave.Error as exc:
        raise SystemExit(f"{path}: not a readable WAV file ({exc})") from exc
    if not pcm:
        raise SystemExit(f"{path}: contains no audio frames")
    return pcm


async def _wait_for_socket(sock: str, proc: subprocess.Popen, timeout_s: float) -> None:
    """Poll until the server is *accepting* on its UDS socket, or fail loudly.

    Polls with a real connect rather than ``os.path.exists``: the socket file
    appears at ``bind()`` time, a beat before ``listen()``/``accept()`` are
    ready, so an existence check alone leaves a TOCTOU window where the client
    connect can still be refused.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if os.path.exists(sock):
            try:
                _, writer = await asyncio.open_unix_connection(sock)
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                return
            except (ConnectionRefusedError, FileNotFoundError, OSError):
                pass  # socket exists but the server is not accepting yet
        if proc.poll() is not None:
            raise SystemExit(
                f"stt_server exited early (code {proc.returncode}) before "
                f"creating its socket — see the server log above."
            )
        await asyncio.sleep(0.1)
    raise SystemExit(f"stt_server did not accept on {sock} within {timeout_s:.0f}s")


async def _decode(sock: str, pcm: bytes, timeout_s: float) -> tuple[str, float]:
    """Stream ``pcm`` to the server on ``sock``; return (transcript, seconds)."""
    chunk = AUDIO_SAMPLE_RATE_HZ * AUDIO_CHANNELS * AUDIO_SAMPLE_WIDTH_BYTES // 10  # 100 ms
    transcript: str | None = None
    async with TranscriptionClient(socket_path=sock) as client:
        await client.update_session(turn_detection=None)
        for i in range(0, len(pcm), chunk):
            await client.send_audio(pcm[i : i + chunk])
        started = time.monotonic()
        await client.commit()

        async def _drain() -> None:
            nonlocal transcript
            async for ev in client.events():
                kind = ev.get("type")
                if kind == EVT_TRANSCRIPT_DELTA:
                    print(f"  delta: {ev.get('delta', '')!r}")
                elif kind == EVT_TRANSCRIPT_COMPLETED:
                    transcript = ev.get("transcript", "")
                    await client.close_session()
                elif kind == EVT_TRANSCRIPT_FAILED or kind == EVT_ERROR:
                    raise SystemExit(f"server reported failure: {ev}")
                elif kind == EVT_SESSION_CLOSED:
                    return

        await asyncio.wait_for(_drain(), timeout=timeout_s)
    if transcript is None:
        raise SystemExit("session closed without a transcript.completed event")
    return transcript, time.monotonic() - started


async def _run(args: argparse.Namespace) -> None:
    pcm = _load_wav(args.wav)
    audio_seconds = len(pcm) / (AUDIO_SAMPLE_RATE_HZ * AUDIO_CHANNELS * AUDIO_SAMPLE_WIDTH_BYTES)
    print(f"audio: {args.wav}  ({audio_seconds:.1f}s, {len(pcm)} bytes PCM16)")

    tmpdir = tempfile.mkdtemp(prefix="parakeet-smoke-")
    sock = os.path.join(tmpdir, "parakeet.sock")
    cmd = [
        sys.executable,
        "-m",
        "stt_server",
        "--socket-path",
        sock,
        "--backend",
        "parakeet",
    ]
    if args.model:
        cmd += ["--model", args.model]
    print(f"starting: {' '.join(cmd)}")
    print("(first run downloads the ~1.5 GB Parakeet model — be patient)\n")

    # Server log streams straight to this process's stderr so a failed start
    # or a decode error is visible inline.
    proc = subprocess.Popen(cmd)
    try:
        await _wait_for_socket(sock, proc, timeout_s=args.startup_timeout)
        transcript, decode_s = await _decode(sock, pcm, timeout_s=args.timeout)
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        if proc.poll() is None:
            proc.kill()
        with contextlib.suppress(OSError):
            os.unlink(sock)
        with contextlib.suppress(OSError):
            os.rmdir(tmpdir)

    rtf = decode_s / audio_seconds if audio_seconds else float("nan")
    print("\n=== Parakeet smoke test ===")
    print(f"  transcript : {transcript!r}")
    print(f"  decode time: {decode_s:.2f}s  (audio {audio_seconds:.1f}s, RTF {rtf:.2f})")
    print("  NOTE: first run includes the model download — re-run for a real RTF.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", help="path to a 16 kHz mono PCM16 WAV file")
    parser.add_argument(
        "--model",
        default=None,
        help="HF model id (default: the backend's DEFAULT_PARAKEET_MODEL)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="seconds to wait for the decode, incl. first-run model download (default: 600)",
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=30.0,
        help="seconds to wait for the server to open its socket (default: 30)",
    )
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
