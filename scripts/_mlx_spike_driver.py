"""Spike driver: hammer UDS with commit cycles until told to stop.

Run from ``scripts/mlx_teardown_spike.sh`` — not a general-purpose client.
Prints one JSON line to stdout on exit with a summary the harness parses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time

# Make sure repo-root imports work when invoked with just ``python``.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from stt_server import TranscriptionClient  # noqa: E402
from stt_server import protocol as P  # noqa: E402


async def _one_commit(sock: str, audio_bytes: bytes, budget_s: float) -> str:
    """Return "ok" | "error:<msg>" | "timeout". Never raises."""
    try:
        async with TranscriptionClient(socket_path=sock) as client:
            await client.update_session(turn_detection=None, language="en")
            # Drain the session.updated ack before committing audio.
            ready_deadline = asyncio.get_running_loop().time() + 5.0

            async def _await_updated():
                async for ev in client.events():
                    if ev.get("type") == P.EVT_SESSION_UPDATED:
                        return

            await asyncio.wait_for(
                _await_updated(),
                timeout=max(0.1, ready_deadline - asyncio.get_running_loop().time()),
            )
            await client.send_audio(audio_bytes)
            await client.commit()

            async def _await_completion():
                async for ev in client.events():
                    t = ev.get("type")
                    if t == P.EVT_TRANSCRIPT_COMPLETED:
                        return "ok"
                    if t == P.EVT_TRANSCRIPT_FAILED:
                        err = ev.get("error") or {}
                        return f"error:{err.get('message') or err.get('code') or 'failed'}"
                return "error:stream_closed"

            return await asyncio.wait_for(_await_completion(), timeout=budget_s)
    except asyncio.TimeoutError:
        return "timeout"
    except Exception as exc:  # noqa: BLE001
        return f"error:{type(exc).__name__}:{exc}"


async def _driver(sock: str, audio: bytes, budget_s: float, stop: asyncio.Event) -> dict:
    counts: dict[str, int] = {"ok": 0, "timeout": 0, "error": 0}
    first_error: str | None = None
    started = time.monotonic()
    while not stop.is_set():
        result = await _one_commit(sock, audio, budget_s)
        if result == "ok":
            counts["ok"] += 1
        elif result == "timeout":
            counts["timeout"] += 1
            if first_error is None:
                first_error = result
        else:
            counts["error"] += 1
            if first_error is None:
                first_error = result
        # Small gap so we don't starve the server's connection accept loop.
        await asyncio.sleep(0.05)
    return {
        "counts": counts,
        "first_error": first_error,
        "elapsed_s": round(time.monotonic() - started, 3),
    }


async def _main(args: argparse.Namespace) -> None:
    samples = int(P.AUDIO_SAMPLE_RATE_HZ * (args.audio_ms / 1000.0))
    audio = (b"\x00\x01") * samples

    if args.one_shot:
        # Used by the harness to probe "is the respawned server ready?"
        # Exit 0 iff one commit succeeds end-to-end; no looping, no signal
        # handlers (the harness runs us under a wall-clock poll).
        result = await _one_commit(args.socket, audio, args.budget_s)
        sys.exit(0 if result == "ok" else 1)

    stop = asyncio.Event()

    def _handle_signal(*_):
        stop.set()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, _handle_signal)
    loop.add_signal_handler(signal.SIGINT, _handle_signal)

    summary = await _driver(args.socket, audio, args.budget_s, stop)
    print(json.dumps(summary), flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True, help="path to UDS")
    ap.add_argument("--audio-ms", type=int, default=500)
    ap.add_argument("--budget-s", type=float, default=15.0)
    ap.add_argument(
        "--one-shot",
        action="store_true",
        help="attempt one commit, exit 0 on ok else 1 (harness probe)",
    )
    asyncio.run(_main(ap.parse_args()))


if __name__ == "__main__":
    main()
