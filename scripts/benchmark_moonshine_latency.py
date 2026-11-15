#!/usr/bin/env python3
"""Paced latency benchmark for the ``moonshine`` backend (single endpoint).

Feeds the go/no-go for the streaming-mode follow-up plan. For utterances of 3, 5
and 10 s (cut from the ``moonshine-voice`` package's ``two_cities.wav`` and
resampled to 16 kHz PCM16) it reports two p50s:

- **one-shot stream decode (commit-to-completed) over the wire**: audio is sent to a running
  ``stt_server --backend moonshine`` in real-time-paced 100 ms frames, then the
  clock runs from the ``commit`` to the terminal ``transcription.completed``;
- **paced streamed feed, library ``stop()`` flush**: the same audio is fed in real-time-paced 100 ms
  chunks to a streaming ``Transcriber`` stream and the clock runs over
  ``stream.stop()`` (the tail flush a streaming mode would pay at commit). This is
  the paced streamed-feed case; the backend's own decode is a one-shot stream
  (whole utterance in one ``add_audio``, then ``stop()``).

Each (length, measurement) is run ``--runs`` times (default 10) and the first,
cold, run is discarded. This is not an extension of ``benchmark_asr_ab.py``
(two endpoints, unpaced audio). Nothing is written to disk and no transcript text
is printed or reported; audio never leaves memory.

The pure helpers (``cut_utterance``, ``resample_pcm16``, ``p50``,
``discard_cold``, ``summarise``, ``format_report``) import without
``moonshine-voice``; only ``load_two_cities`` and ``measure_stop_flush`` need it.

Usage:
    uv run --inexact python scripts/benchmark_moonshine_latency.py \\
        --uri ws://127.0.0.1:9965/
    uv run --inexact python scripts/benchmark_moonshine_latency.py \\
        --socket ~/Library/Caches/pipecat-stt/moonshine.sock --no-pace --skip-flush
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave
from collections.abc import Callable, Sequence
from pathlib import Path
from statistics import median

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from stt_server import protocol as P  # noqa: E402

DEFAULT_LENGTHS_S = (3, 5, 10)
DEFAULT_RUNS = 10
FRAME_MS = 100
_BYTES_PER_SAMPLE = 2


# ---------------------------------------------------------------------------
# Pure helpers (importable without moonshine-voice)
# ---------------------------------------------------------------------------


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int = P.AUDIO_SAMPLE_RATE_HZ) -> bytes:
    """Mono PCM16LE ``pcm`` from ``src_rate`` to ``dst_rate``.

    An integer downsampling factor box-averages blocks (a cheap low-pass, so
    48 kHz -> 16 kHz does not alias); anything else is linear interpolation.
    """
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError("sample rates must be positive")
    pcm = pcm[: len(pcm) // _BYTES_PER_SAMPLE * _BYTES_PER_SAMPLE]
    if src_rate == dst_rate or not pcm:
        return pcm
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    if src_rate % dst_rate == 0:
        k = src_rate // dst_rate
        usable = len(x) // k * k
        y = x[:usable].reshape(-1, k).mean(axis=1)
    else:
        n_out = int(len(x) * dst_rate / src_rate)
        y = np.interp(np.arange(n_out) * src_rate / dst_rate, np.arange(len(x)), x)
    return np.clip(np.rint(y), -32768, 32767).astype("<i2").tobytes()


def cut_utterance(pcm16: bytes, seconds: float, rate: int = P.AUDIO_SAMPLE_RATE_HZ) -> bytes:
    """The first ``seconds`` of 16 kHz mono PCM16LE ``pcm16``; ``ValueError`` if too short."""
    n_bytes = int(seconds * rate) * _BYTES_PER_SAMPLE
    if len(pcm16) < n_bytes:
        have = len(pcm16) / (rate * _BYTES_PER_SAMPLE)
        raise ValueError(f"source audio is {have:.1f}s, need {seconds}s")
    return pcm16[:n_bytes]


def pcm16_to_float_list(pcm16: bytes) -> list[float]:
    """PCM16LE -> floats in ``[-1, 1]``, the conversion the backend uses."""
    pcm16 = pcm16[: len(pcm16) // _BYTES_PER_SAMPLE * _BYTES_PER_SAMPLE]
    return (np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0).tolist()


def frame_bytes(frame_ms: int = FRAME_MS, rate: int = P.AUDIO_SAMPLE_RATE_HZ) -> int:
    return int(rate * frame_ms / 1000) * _BYTES_PER_SAMPLE


def p50(values: Sequence[float]) -> float:
    """Median; ``ValueError`` on an empty sequence."""
    if not values:
        raise ValueError("p50 of no samples")
    return float(median(values))


def discard_cold(runs: Sequence[float]) -> list[float]:
    """Drop the first (cold) run; ``ValueError`` if there is nothing left."""
    warm = list(runs)[1:]
    if not warm:
        raise ValueError("need at least 2 runs (the first, cold, run is discarded)")
    return warm


def summarise(samples_by_length: dict[int, Sequence[float]]) -> dict[int, float]:
    """Per utterance length: p50 of the warm runs, in seconds."""
    return {length: p50(discard_cold(runs)) for length, runs in sorted(samples_by_length.items())}


def format_report(
    one_shot_s: dict[int, float],
    flush_s: dict[int, float] | None,
    *,
    backend: str = "moonshine",
    runs: int = DEFAULT_RUNS,
) -> str:
    """A fixed-width table of p50s in milliseconds (flush column ``n/a`` when not measured)."""
    lines = [
        f"{backend} latency, p50 of {runs - 1} warm runs (1 cold run discarded), 16 kHz PCM16",
        f"{'utterance':>10}  {'one-shot commit->completed':>27}  {'stop() flush':>14}  {'flush/one-shot':>14}",
    ]
    for length in sorted(one_shot_s):
        one = one_shot_s[length]
        flush = (flush_s or {}).get(length)
        ratio = f"{flush / one:.2f}" if flush is not None and one > 0 else "n/a"
        flush_txt = f"{flush * 1000:.0f} ms" if flush is not None else "n/a"
        lines.append(f"{length:>8} s  {one * 1000:>24.0f} ms  {flush_txt:>14}  {ratio:>14}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------


async def paced_send(
    send: Callable[[bytes], asyncio.Future | object],
    pcm: bytes,
    *,
    pace: bool,
    frame_ms: int = FRAME_MS,
) -> None:
    """Send ``pcm`` in ``frame_ms`` frames; with ``pace`` each frame leaves at its
    real-time offset (deadline-based, so sleep jitter does not accumulate)."""
    step = frame_bytes(frame_ms)
    start = time.perf_counter()
    for i, off in enumerate(range(0, len(pcm), step)):
        result = send(pcm[off : off + step])
        if asyncio.iscoroutine(result):
            await result
        if pace:
            delay = start + (i + 1) * frame_ms / 1000 - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)


async def measure_one_shot_wire(
    make_client: Callable[[], object], pcm: bytes, *, pace: bool
) -> float:
    """Seconds from ``commit`` to ``transcription.completed`` for one utterance.

    Raises ``RuntimeError`` on ``transcription.failed`` or a socket closed early.
    """
    client = make_client()
    try:
        await client.connect()
        await client.update_session(turn_detection=None)
        await paced_send(client.send_audio, pcm, pace=pace)
        t0 = time.perf_counter()
        await client.commit()
        async for ev in client.events():
            etype = ev.get("type")
            if etype == P.EVT_TRANSCRIPT_COMPLETED:
                return time.perf_counter() - t0
            if etype == P.EVT_TRANSCRIPT_FAILED:
                raise RuntimeError("backend reported transcript.failed")
        raise RuntimeError("socket closed before a terminal event")
    finally:
        await client.close()


async def run_one_shot_wire(
    make_client: Callable[[], object],
    pcm_by_length: dict[int, bytes],
    *,
    runs: int,
    pace: bool,
) -> dict[int, list[float]]:
    out: dict[int, list[float]] = {}
    for length, pcm in pcm_by_length.items():
        out[length] = [
            await measure_one_shot_wire(make_client, pcm, pace=pace) for _ in range(runs)
        ]
    return out


def measure_stop_flush(transcriber, pcm: bytes, *, pace: bool) -> float:
    """Seconds spent in ``stream.stop()`` after a (paced) streamed feed of ``pcm``.

    ``transcriber`` is a ``moonshine_voice.Transcriber`` on a streaming model.
    ``stop()`` swallows a failed final transcription and emits an ``Error`` event
    instead, so a listener records those (as the backend's decode does) and the
    run raises ``RuntimeError`` rather than reporting a failed flush as a fast one.
    """
    from moonshine_voice.transcriber import Error  # type: ignore

    errors: list[object] = []
    stream = transcriber.create_stream(update_interval=0.3)
    step = frame_bytes()
    try:
        stream.add_listener(lambda ev: errors.append(ev) if isinstance(ev, Error) else None)
        stream.start()
        start = time.perf_counter()
        for i, off in enumerate(range(0, len(pcm), step)):
            stream.add_audio(pcm16_to_float_list(pcm[off : off + step]), P.AUDIO_SAMPLE_RATE_HZ)
            if pace:
                delay = start + (i + 1) * FRAME_MS / 1000 - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
        t0 = time.perf_counter()
        stream.stop()
        elapsed = time.perf_counter() - t0
    finally:
        stream.close()
    if errors:
        cause = getattr(errors[0], "error", None)
        if cause is not None and not isinstance(cause, BaseException):
            cause = RuntimeError(str(cause))
        raise RuntimeError("library stop() flush reported an error") from cause
    return elapsed


def run_stop_flush(
    transcriber, pcm_by_length: dict[int, bytes], *, runs: int, pace: bool
) -> dict[int, list[float]]:
    return {
        length: [measure_stop_flush(transcriber, pcm, pace=pace) for _ in range(runs)]
        for length, pcm in pcm_by_length.items()
    }


# ---------------------------------------------------------------------------
# Audio source and CLI (thin)
# ---------------------------------------------------------------------------


def read_wav_mono_pcm16(path: Path) -> tuple[bytes, int]:
    """``(mono PCM16LE bytes, sample rate)`` of a 16-bit WAV (first channel only)."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != _BYTES_PER_SAMPLE:
            raise ValueError(f"{path}: need 16-bit PCM, got {w.getsampwidth() * 8}-bit")
        channels, rate = w.getnchannels(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if channels > 1:
        raw = np.frombuffer(raw, dtype="<i2").reshape(-1, channels)[:, 0].tobytes()
    return raw, rate


def load_two_cities() -> tuple[bytes, int]:
    """The package's ``two_cities.wav`` as ``(mono PCM16LE, rate)`` (needs moonshine-voice)."""
    from moonshine_voice.utils import get_assets_path  # type: ignore

    return read_wav_mono_pcm16(get_assets_path() / "two_cities.wav")


def build_utterances(
    pcm: bytes, rate: int, lengths: Sequence[int] = DEFAULT_LENGTHS_S
) -> dict[int, bytes]:
    """16 kHz PCM16 utterances of each length in ``lengths`` seconds, cut from ``pcm``."""
    pcm16k = resample_pcm16(pcm, rate)
    return {n: cut_utterance(pcm16k, n) for n in lengths}


def resolve_auth_token(token_file: str | None) -> str | None:
    """The bearer token the way ``stt_server status`` resolves it (the client probe
    policy): ``--auth-token-file``, else ``STT_WS_TOKEN``. Never taken from argv."""
    from stt_server.__main__ import _resolve_auth_token

    return _resolve_auth_token(token_file, client=True)


def _load_streaming_transcriber(arch_name: str):
    from moonshine_voice import Transcriber  # type: ignore
    from moonshine_voice.download import get_model_for_language  # type: ignore
    from moonshine_voice.moonshine_api import ModelArch  # type: ignore

    path, arch = get_model_for_language("en", ModelArch[arch_name.upper().replace("-", "_")])
    return Transcriber(path, arch)


def main(argv: list[str] | None = None) -> int:
    from stt_server.backends import DEFAULT_MOONSHINE_MODEL
    from stt_server.client import TranscriptionClient

    default_arch = DEFAULT_MOONSHINE_MODEL.partition("@")[0]
    # allow_abbrev=False: the old ``--auth-token <token>`` must fail, not be read as
    # ``--auth-token-file`` naming a file called ``<token>``.
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--uri", help="ws:// URI of a running moonshine server.")
    target.add_argument("--socket", help="UDS path of a running moonshine server.")
    parser.add_argument(
        "--auth-token-file",
        default=None,
        help="file holding the bearer token (default: the STT_WS_TOKEN environment variable)",
    )
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help="runs per length (>= 2)")
    parser.add_argument(
        "--lengths", default=",".join(map(str, DEFAULT_LENGTHS_S)), help="seconds, comma-separated"
    )
    parser.add_argument("--arch", default=default_arch, help="streaming arch for the flush test")
    parser.add_argument(
        "--wav", type=Path, default=None, help="source WAV (default two_cities.wav)"
    )
    parser.add_argument("--no-pace", action="store_true", help="send audio unpaced")
    parser.add_argument("--skip-flush", action="store_true", help="skip the library stop() test")
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.runs < 2:
        parser.error("--runs must be at least 2")
    lengths = [int(x) for x in args.lengths.split(",") if x.strip()]

    pcm, rate = read_wav_mono_pcm16(args.wav) if args.wav else load_two_cities()
    utterances = build_utterances(pcm, rate, lengths)
    pace = not args.no_pace
    auth_token = resolve_auth_token(args.auth_token_file)

    def make_client():
        return TranscriptionClient(socket_path=args.socket, uri=args.uri, auth_token=auth_token)

    one_shot_runs = asyncio.run(
        run_one_shot_wire(make_client, utterances, runs=args.runs, pace=pace)
    )
    one_shot = summarise(one_shot_runs)
    flush = None
    if not args.skip_flush:
        transcriber = _load_streaming_transcriber(args.arch)
        try:
            flush = summarise(run_stop_flush(transcriber, utterances, runs=args.runs, pace=pace))
        finally:
            transcriber.close()
    print(format_report(one_shot, flush, runs=args.runs))
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(
                {
                    "runs": args.runs,
                    "paced": pace,
                    "one_shot_p50_s": one_shot,
                    "stop_flush_p50_s": flush,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
