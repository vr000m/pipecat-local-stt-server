#!/usr/bin/env python3
"""A/B benchmark: Whisper vs Parakeet over two running stt_server instances.

Replays a corpus of audio utterances against TWO concurrently-running
``stt_server`` processes — typically the Whisper (`mlx`) agent and the
Parakeet agent — and reports per-utterance Word Error Rate (WER) and decode
latency, plus aggregates. Both servers must speak the frozen V1 wire protocol
and accept 16 kHz PCM16LE mono audio; the benchmark is a pure client and adds
no protocol surface.

This is a one-off operator tool. It has no REST counterpart and is not a CI
gate — run it by hand after both LaunchAgents are installed and socket-live
(see ``stt_server/README.md`` → "Two-agent install").

The two-endpoint shape (one Whisper, one Parakeet) is baked into
``run_benchmark`` and ``PerUtterance``; adding a third ASR means changing
those signatures, not just passing another ``Endpoint``.

Usage:
    # Default: whisper on stt.sock, parakeet on parakeet.sock.
    uv run python scripts/benchmark_asr_ab.py --corpus path/to/corpus

    # Explicit endpoints (UDS).
    uv run python scripts/benchmark_asr_ab.py --corpus path/to/corpus \\
        --whisper-socket ~/Library/Caches/koda-stt/stt.sock \\
        --parakeet-socket ~/Library/Caches/koda-stt/parakeet.sock

    # Loopback TCP instead of a socket (per ASR).
    uv run python scripts/benchmark_asr_ab.py --corpus path/to/corpus \\
        --whisper-uri ws://127.0.0.1:8765/ \\
        --parakeet-uri ws://127.0.0.1:8766/

    # Write the per-utterance + aggregate report as JSON.
    uv run python scripts/benchmark_asr_ab.py --corpus path/to/corpus \\
        --json-out benchmarks/results/asr_ab_20260518.json

Corpus layout
-------------
``--corpus`` points at a directory of utterances. Each utterance is a pair:

    <stem>.wav   16 kHz mono PCM16 audio (mono, 16-bit; resampled if needed)
    <stem>.txt   reference transcript (one utterance of plain text)

A ``.txt`` with no matching ``.wav`` (or vice versa) is skipped with a
warning. WER is computed only for utterances that carry a reference; an
utterance with audio but no ``.txt`` still contributes a latency sample.

PII note
--------
The corpus is named explicitly on the command line and is **never** baked
into this script. Koda's ``docs/benchmarks`` / ``~/koda-data`` JSON corpora
contain real names, companies, and financials — do NOT point ``--corpus`` at
those and do NOT commit a derived audio corpus. Use a synthetic or
consented-recording corpus, and keep it outside the repo. As a guard, the
script refuses a ``--corpus`` directory that sits under ``docs/benchmarks``
or ``~/koda-data`` unless ``--allow-pii-corpus`` is passed.

Prerequisite
------------
Both ``stt_server`` instances must already be running and reachable. The
benchmark **fails fast** if only one of the two endpoints answers — it never
silently benchmarks a single ASR.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, median

# Ensure the project root is importable so `stt_server` resolves when this
# script is run directly (mirrors scripts/benchmark_llm.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from stt_server import protocol as P  # noqa: E402
from stt_server.client import TranscriptionClient  # noqa: E402

# Directories known to hold PII-bearing corpora — refuse these by default.
# ``docs/benchmarks`` is anchored to the repo root (derived from this file's
# location), NOT the process cwd: the script is runnable by absolute path from
# anywhere, and a cwd-relative guard would silently fail to refuse the real
# PII corpus whenever the caller is outside the repo root.
_PII_CORPUS_ROOTS = (
    (_REPO_ROOT / "docs" / "benchmarks").resolve(),
    Path(os.path.expanduser("~/koda-data")).resolve(),
)

# Audio is streamed to the server in ~1 s frames.
_FRAME_BYTES = (
    P.AUDIO_SAMPLE_RATE_HZ * P.AUDIO_CHANNELS * 2  # 16-bit => 2 bytes/sample
)


# ---------------------------------------------------------------------------
# WER
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[^\w']+")


def _normalize(text: str) -> list[str]:
    """Lowercase, strip punctuation, collapse whitespace into a word list."""
    return [w for w in _WORD_RE.split(text.lower()) if w]


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Levenshtein-based WER = (S + D + I) / N over whitespace-ish tokens.

    Returns 0.0 when the reference is empty and the hypothesis is also empty;
    returns 1.0 when the reference is empty but the hypothesis is not (pure
    insertions, undefined denominator — treated as a full miss).
    """
    ref = _normalize(reference)
    hyp = _normalize(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    # Classic edit-distance DP over the two token sequences.
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        curr = [i]
        for j, h in enumerate(hyp, start=1):
            cost = 0 if r == h else 1
            curr.append(
                min(
                    prev[j] + 1,  # deletion
                    curr[j - 1] + 1,  # insertion
                    prev[j - 1] + cost,  # substitution / match
                )
            )
        prev = curr
    return prev[-1] / len(ref)


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------


@dataclass
class Utterance:
    stem: str
    audio_path: Path
    reference: str | None  # None when no .txt sidecar exists


def _read_wav_pcm16(path: Path) -> bytes:
    """Read a 16 kHz mono 16-bit PCM WAV and return its raw PCM bytes.

    Raises ``ValueError`` on any format mismatch — the wire protocol is
    pinned to 16 kHz PCM16LE mono and silent resampling would corrupt the
    benchmark.
    """
    with wave.open(str(path), "rb") as wf:
        if wf.getnchannels() != P.AUDIO_CHANNELS:
            raise ValueError(f"{path}: expected mono, got {wf.getnchannels()} ch")
        if wf.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16-bit PCM, got {wf.getsampwidth() * 8}-bit")
        if wf.getframerate() != P.AUDIO_SAMPLE_RATE_HZ:
            raise ValueError(
                f"{path}: expected {P.AUDIO_SAMPLE_RATE_HZ} Hz, got {wf.getframerate()} Hz"
            )
        return wf.readframes(wf.getnframes())


def load_corpus(corpus_dir: Path, *, allow_pii: bool) -> list[Utterance]:
    """Discover ``<stem>.wav`` / ``<stem>.txt`` pairs under ``corpus_dir``."""
    corpus_dir = corpus_dir.resolve()
    if not corpus_dir.is_dir():
        raise SystemExit(f"corpus directory not found: {corpus_dir}")
    if not allow_pii:
        for root in _PII_CORPUS_ROOTS:
            if corpus_dir == root or root in corpus_dir.parents:
                raise SystemExit(
                    f"refusing PII-bearing corpus root {corpus_dir} "
                    f"(under {root}); pass --allow-pii-corpus to override"
                )
    utterances: list[Utterance] = []
    for wav in sorted(corpus_dir.glob("*.wav")):
        txt = wav.with_suffix(".txt")
        reference = txt.read_text(encoding="utf-8").strip() if txt.is_file() else None
        if reference is None:
            print(f"warning: {wav.name} has no .txt reference — latency-only", file=sys.stderr)
        utterances.append(Utterance(stem=wav.stem, audio_path=wav, reference=reference))
    # Flag orphan references (a .txt with no audio).
    for txt in sorted(corpus_dir.glob("*.txt")):
        if not txt.with_suffix(".wav").is_file():
            print(f"warning: {txt.name} has no .wav audio — skipped", file=sys.stderr)
    if not utterances:
        raise SystemExit(f"no .wav utterances found in {corpus_dir}")
    return utterances


# ---------------------------------------------------------------------------
# Endpoint config
# ---------------------------------------------------------------------------


@dataclass
class Endpoint:
    name: str
    socket_path: str | None = None
    uri: str | None = None
    auth_token: str | None = None

    def make_client(self) -> TranscriptionClient:
        return TranscriptionClient(
            socket_path=self.socket_path,
            uri=self.uri,
            auth_token=self.auth_token,
        )

    def describe(self) -> str:
        return self.uri or self.socket_path or "<unset>"


async def _probe(endpoint: Endpoint, expected_backend: str) -> str | None:
    """Return None if the endpoint answers a connect handshake AND reports the
    expected backend identity; else an error string.

    The backend-identity check is fail-closed: an A/B comparison is worthless
    if a socket is actually running a different ASR than its CLI label claims
    (stale LaunchAgent, swapped socket path, mis-exported ``KODA_STT_*`` env).
    A server too old to emit the ``backend`` field in ``server.hello`` reports
    ``None`` here and is correctly rejected.
    """
    client = endpoint.make_client()
    try:
        hello = await asyncio.wait_for(client.connect(), timeout=5.0)
    except Exception as exc:  # noqa: BLE001 — probe surfaces any failure as text
        return f"{type(exc).__name__}: {exc}"
    finally:
        await client.close()
    reported = (hello.get("backend") or {}).get("name")
    if reported != expected_backend:
        return (
            f"endpoint reports backend {reported!r}, expected {expected_backend!r} "
            f"— this socket is not running the ASR its label claims"
        )
    return None


# ---------------------------------------------------------------------------
# Decode one utterance against one server
# ---------------------------------------------------------------------------


@dataclass
class DecodeResult:
    transcript: str
    latency_s: float
    failed: bool = False
    error: str | None = None


async def decode_utterance(endpoint: Endpoint, pcm: bytes) -> DecodeResult:
    """Stream one utterance to a server and collect its transcript + latency.

    Latency is measured from the moment the commit is sent to the arrival of
    the terminal ``...transcription.completed`` (or ``.failed``) event — i.e.
    server-side decode time, excluding audio upload.
    """
    client = endpoint.make_client()
    transcript_parts: list[str] = []
    try:
        await client.connect()
        # Koda always drives commits from its own VAD — turn_detection: null.
        await client.update_session(turn_detection=None)
        for off in range(0, len(pcm), _FRAME_BYTES):
            await client.send_audio(pcm[off : off + _FRAME_BYTES])
        commit_t = time.perf_counter()
        await client.commit()
        async for ev in client.events():
            etype = ev.get("type")
            if etype == P.EVT_TRANSCRIPT_DELTA:
                transcript_parts.append(ev.get("delta", ""))
            elif etype == P.EVT_TRANSCRIPT_COMPLETED:
                latency = time.perf_counter() - commit_t
                # The completed event carries the authoritative final text.
                final = ev.get("transcript", "") or "".join(transcript_parts)
                return DecodeResult(transcript=final, latency_s=latency)
            elif etype == P.EVT_TRANSCRIPT_FAILED:
                latency = time.perf_counter() - commit_t
                err = (ev.get("error") or {}).get("message", "backend decode failed")
                return DecodeResult(transcript="", latency_s=latency, failed=True, error=err)
        # Socket closed before a terminal event. Report the real elapsed time
        # (consistent with the .failed path above) rather than a misleading 0.0
        # in the per-utterance JSON — _aggregate excludes failures from the
        # latency stats regardless.
        return DecodeResult(
            transcript="",
            latency_s=time.perf_counter() - commit_t,
            failed=True,
            error="no terminal event",
        )
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass
class PerUtterance:
    stem: str
    reference: str | None
    whisper: dict
    parakeet: dict


@dataclass
class AsrAggregate:
    name: str
    utterances: int = 0
    failures: int = 0
    mean_wer: float | None = None
    median_wer: float | None = None
    mean_latency_s: float | None = None
    median_latency_s: float | None = None


def _aggregate(name: str, results: list[tuple[DecodeResult, str | None]]) -> AsrAggregate:
    agg = AsrAggregate(name=name, utterances=len(results))
    wers: list[float] = []
    latencies: list[float] = []
    for res, ref in results:
        if res.failed:
            agg.failures += 1
            continue
        latencies.append(res.latency_s)
        if ref is not None:
            wers.append(word_error_rate(ref, res.transcript))
    if wers:
        agg.mean_wer = mean(wers)
        agg.median_wer = median(wers)
    if latencies:
        agg.mean_latency_s = mean(latencies)
        agg.median_latency_s = median(latencies)
    return agg


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run_benchmark(
    utterances: list[Utterance],
    whisper: Endpoint,
    parakeet: Endpoint,
) -> tuple[list[PerUtterance], AsrAggregate, AsrAggregate]:
    # Fail fast: both endpoints MUST answer AND report the ASR their label
    # claims, or the A/B comparison is meaningless.
    w_err, p_err = await asyncio.gather(_probe(whisper, "mlx"), _probe(parakeet, "parakeet"))
    problems = []
    if w_err is not None:
        problems.append(f"whisper ({whisper.describe()}): {w_err}")
    if p_err is not None:
        problems.append(f"parakeet ({parakeet.describe()}): {p_err}")
    if problems:
        raise SystemExit(
            "A/B benchmark needs BOTH servers reachable and running the ASR "
            "their labels claim. Problems:\n  " + "\n  ".join(problems)
        )

    per_utterance: list[PerUtterance] = []
    whisper_results: list[tuple[DecodeResult, str | None]] = []
    parakeet_results: list[tuple[DecodeResult, str | None]] = []

    for utt in utterances:
        try:
            pcm = _read_wav_pcm16(utt.audio_path)
        except ValueError as exc:
            print(f"warning: skipping {utt.stem}: {exc}", file=sys.stderr)
            continue
        # Decode against both servers (sequentially — keeps Metal decode
        # serialised per process and the latency numbers uncontended).
        w_res = await decode_utterance(whisper, pcm)
        p_res = await decode_utterance(parakeet, pcm)
        whisper_results.append((w_res, utt.reference))
        parakeet_results.append((p_res, utt.reference))

        w_wer = (
            word_error_rate(utt.reference, w_res.transcript)
            if utt.reference is not None and not w_res.failed
            else None
        )
        p_wer = (
            word_error_rate(utt.reference, p_res.transcript)
            if utt.reference is not None and not p_res.failed
            else None
        )
        per_utterance.append(
            PerUtterance(
                stem=utt.stem,
                reference=utt.reference,
                whisper={**asdict(w_res), "wer": w_wer},
                parakeet={**asdict(p_res), "wer": p_wer},
            )
        )
        print(
            f"  {utt.stem}: "
            f"whisper wer={_fmt(w_wer)} {w_res.latency_s:.2f}s | "
            f"parakeet wer={_fmt(p_wer)} {p_res.latency_s:.2f}s"
        )

    return (
        per_utterance,
        _aggregate("whisper", whisper_results),
        _aggregate("parakeet", parakeet_results),
    )


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _print_summary(whisper: AsrAggregate, parakeet: AsrAggregate) -> None:
    print("\n=== A/B summary ===")
    for agg in (whisper, parakeet):
        print(
            f"  {agg.name:9s}  utterances={agg.utterances} failures={agg.failures}  "
            f"mean WER={_fmt(agg.mean_wer)} median WER={_fmt(agg.median_wer)}  "
            f"mean latency={_fmt(agg.mean_latency_s)}s "
            f"median latency={_fmt(agg.median_latency_s)}s"
        )
    if whisper.mean_wer is not None and parakeet.mean_wer is not None:
        win, lose = (
            (parakeet, whisper) if parakeet.mean_wer < whisper.mean_wer else (whisper, parakeet)
        )
        print(f"  lower mean WER: {win.name}")
        # mean WER is computed only over *successful* decodes (failures are
        # excluded by _aggregate), so a backend that fails on hard utterances
        # and succeeds on easy ones can post an artificially low mean WER.
        # When the lower-WER backend also failed more often, its WER edge is
        # not trustworthy — say so rather than imply a clean win.
        if win.failures > lose.failures:
            print(
                f"  WARNING: {win.name} has the lower mean WER but MORE "
                f"failures ({win.failures} vs {lose.failures}); mean WER "
                f"excludes failed decodes, so this is not a clean win — "
                f"compare failure counts before deciding."
            )
    print(
        "\nThis is the operator A/B tool — record the numbers in the dev "
        "plan's `## Findings` and pick the winner there."
    )


def _build_endpoints(args: argparse.Namespace) -> tuple[Endpoint, Endpoint]:
    token = (os.environ.get("STT_WS_TOKEN") or "").strip() or None
    whisper = Endpoint(
        name="whisper",
        socket_path=None if args.whisper_uri else args.whisper_socket,
        uri=args.whisper_uri,
        auth_token=token,
    )
    parakeet = Endpoint(
        name="parakeet",
        socket_path=None if args.parakeet_uri else args.parakeet_socket,
        uri=args.parakeet_uri,
        auth_token=token,
    )
    return whisper, parakeet


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A/B-benchmark Whisper vs Parakeet over two running stt_server instances.",
    )
    parser.add_argument(
        "--corpus",
        required=True,
        type=Path,
        help="Directory of <stem>.wav (16 kHz mono PCM16) + <stem>.txt reference pairs.",
    )
    parser.add_argument(
        "--whisper-socket",
        default=os.path.expanduser("~/Library/Caches/koda-stt/stt.sock"),
        help="UDS path for the Whisper (mlx) server (default: the legacy stt.sock).",
    )
    parser.add_argument(
        "--parakeet-socket",
        default=os.path.expanduser("~/Library/Caches/koda-stt/parakeet.sock"),
        help="UDS path for the Parakeet server (default: parakeet.sock).",
    )
    parser.add_argument(
        "--whisper-uri",
        default=None,
        help="ws:// URI for the Whisper server (overrides --whisper-socket).",
    )
    parser.add_argument(
        "--parakeet-uri",
        default=None,
        help="ws:// URI for the Parakeet server (overrides --parakeet-socket).",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Optional path to write the full per-utterance + aggregate report as JSON.",
    )
    parser.add_argument(
        "--allow-pii-corpus",
        action="store_true",
        help="Override the guard that refuses a corpus under docs/benchmarks or ~/koda-data.",
    )
    args = parser.parse_args(argv)

    utterances = load_corpus(args.corpus, allow_pii=args.allow_pii_corpus)
    whisper_ep, parakeet_ep = _build_endpoints(args)
    print(
        f"corpus: {args.corpus} ({len(utterances)} utterances)\n"
        f"whisper:  {whisper_ep.describe()}\n"
        f"parakeet: {parakeet_ep.describe()}\n"
    )

    per_utterance, whisper_agg, parakeet_agg = asyncio.run(
        run_benchmark(utterances, whisper_ep, parakeet_ep)
    )
    _print_summary(whisper_agg, parakeet_agg)

    if args.json_out:
        report = {
            "corpus": str(args.corpus.resolve()),
            "utterance_count": len(utterances),
            "whisper_endpoint": whisper_ep.describe(),
            "parakeet_endpoint": parakeet_ep.describe(),
            "aggregate": {
                "whisper": asdict(whisper_agg),
                "parakeet": asdict(parakeet_agg),
            },
            "per_utterance": [asdict(u) for u in per_utterance],
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nwrote JSON report: {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
