"""Unit tests for the pure helpers in ``scripts/benchmark_asr_ab.py``.

The A/B benchmark is an operator tool that replays a corpus against two live
``stt_server`` instances; that end-to-end path is *not* CI-testable. These
tests cover only the pure, deterministic helpers:

* ``word_error_rate`` — Levenshtein-based WER over normalised tokens, plus the
  empty-reference edge cases;
* ``load_corpus`` — pair discovery, orphan handling, the missing-directory and
  empty-corpus errors, and the PII-corpus-root guard;
* ``_read_wav_pcm16`` — format validation against the pinned 16 kHz/mono/16-bit
  wire shape;
* ``_aggregate`` — mean/median WER + latency aggregation and failure counting;
* ``run_benchmark``'s fail-fast guard — both endpoints must be reachable, which
  is exercisable with closed/nonexistent socket paths;
* ``_fmt`` and ``Endpoint.describe`` — trivial formatting helpers.
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import pytest

from scripts.benchmark_asr_ab import (
    Endpoint,
    Utterance,
    _aggregate,
    _build_endpoints,
    _fmt,
    _normalize,
    _read_wav_pcm16,
    DecodeResult,
    load_corpus,
    run_benchmark,
    word_error_rate,
)


# ---------------------------------------------------------------------------
# WER
# ---------------------------------------------------------------------------


def test_wer_identical_is_zero():
    assert word_error_rate("the quick brown fox", "the quick brown fox") == 0.0


def test_wer_is_case_and_punctuation_insensitive():
    assert word_error_rate("The quick, brown FOX!", "the quick brown fox") == 0.0


def test_wer_single_substitution():
    # one of four tokens wrong => 1/4
    assert word_error_rate("the quick brown fox", "the quick green fox") == pytest.approx(0.25)


def test_wer_single_deletion():
    # hypothesis dropped one of four tokens => 1 deletion / 4 ref tokens
    assert word_error_rate("the quick brown fox", "the quick fox") == pytest.approx(0.25)


def test_wer_single_insertion():
    # hypothesis added one token => 1 insertion / 4 ref tokens
    assert word_error_rate("the quick brown fox", "the quick brown red fox") == pytest.approx(0.25)


def test_wer_full_mismatch():
    assert word_error_rate("alpha beta", "gamma delta") == pytest.approx(1.0)


def test_wer_empty_reference_and_empty_hypothesis_is_zero():
    assert word_error_rate("", "") == 0.0
    assert word_error_rate("   ", "") == 0.0


def test_wer_empty_reference_nonempty_hypothesis_is_one():
    # undefined denominator => treated as a full miss
    assert word_error_rate("", "stray words") == 1.0


def test_wer_can_exceed_nothing_but_caps_sensibly():
    # empty hypothesis against a 3-token reference => 3 deletions / 3 = 1.0
    assert word_error_rate("one two three", "") == pytest.approx(1.0)


def test_normalize_collapses_and_strips():
    assert _normalize("  Hello,   WORLD!! it's  fine ") == ["hello", "world", "it's", "fine"]


# ---------------------------------------------------------------------------
# _read_wav_pcm16
# ---------------------------------------------------------------------------


def _write_wav(
    path: Path,
    *,
    channels: int = 1,
    sampwidth: int = 2,
    framerate: int = 16000,
    nframes: int = 16000,
) -> Path:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(framerate)
        wf.writeframes(b"\x00" * nframes * channels * sampwidth)
    return path


def test_read_wav_pcm16_accepts_canonical_format(tmp_path):
    wav = _write_wav(tmp_path / "ok.wav", nframes=8000)
    pcm = _read_wav_pcm16(wav)
    assert pcm == b"\x00" * 8000 * 2


def test_read_wav_pcm16_rejects_stereo(tmp_path):
    wav = _write_wav(tmp_path / "stereo.wav", channels=2)
    with pytest.raises(ValueError, match="expected mono"):
        _read_wav_pcm16(wav)


def test_read_wav_pcm16_rejects_non_16bit(tmp_path):
    wav = _write_wav(tmp_path / "wide.wav", sampwidth=1)
    with pytest.raises(ValueError, match="expected 16-bit"):
        _read_wav_pcm16(wav)


def test_read_wav_pcm16_rejects_wrong_sample_rate(tmp_path):
    wav = _write_wav(tmp_path / "fast.wav", framerate=44100)
    with pytest.raises(ValueError, match="expected 16000 Hz"):
        _read_wav_pcm16(wav)


# ---------------------------------------------------------------------------
# load_corpus
# ---------------------------------------------------------------------------


def test_load_corpus_discovers_pairs(tmp_path):
    _write_wav(tmp_path / "a.wav")
    (tmp_path / "a.txt").write_text("hello world\n", encoding="utf-8")
    _write_wav(tmp_path / "b.wav")
    (tmp_path / "b.txt").write_text("  second  ", encoding="utf-8")

    utts = load_corpus(tmp_path, allow_pii=False)

    assert [u.stem for u in utts] == ["a", "b"]
    assert utts[0].reference == "hello world"
    # leading/trailing whitespace is stripped from references
    assert utts[1].reference == "second"


def test_load_corpus_wav_without_txt_is_latency_only(tmp_path):
    _write_wav(tmp_path / "noref.wav")
    utts = load_corpus(tmp_path, allow_pii=False)
    assert len(utts) == 1
    assert utts[0].reference is None


def test_load_corpus_orphan_txt_is_skipped(tmp_path):
    _write_wav(tmp_path / "a.wav")
    (tmp_path / "a.txt").write_text("ref", encoding="utf-8")
    (tmp_path / "orphan.txt").write_text("no audio here", encoding="utf-8")

    utts = load_corpus(tmp_path, allow_pii=False)
    # orphan .txt contributes nothing
    assert [u.stem for u in utts] == ["a"]


def test_load_corpus_missing_directory_raises(tmp_path):
    with pytest.raises(SystemExit, match="corpus directory not found"):
        load_corpus(tmp_path / "does-not-exist", allow_pii=False)


def test_load_corpus_empty_directory_raises(tmp_path):
    with pytest.raises(SystemExit, match="no .wav utterances"):
        load_corpus(tmp_path, allow_pii=False)


def test_load_corpus_refuses_pii_root(tmp_path, monkeypatch):
    import scripts.benchmark_asr_ab as mod

    pii_root = tmp_path / "koda-data"
    sub = pii_root / "audio"
    sub.mkdir(parents=True)
    _write_wav(sub / "x.wav")
    monkeypatch.setattr(mod, "_PII_CORPUS_ROOTS", (pii_root.resolve(),))

    # directory under a PII root is refused without the override...
    with pytest.raises(SystemExit, match="refusing PII-bearing corpus"):
        load_corpus(sub, allow_pii=False)
    # the PII root itself is also refused
    with pytest.raises(SystemExit, match="refusing PII-bearing corpus"):
        load_corpus(pii_root, allow_pii=False)


def test_load_corpus_pii_root_allowed_with_override(tmp_path, monkeypatch):
    import scripts.benchmark_asr_ab as mod

    pii_root = tmp_path / "koda-data"
    sub = pii_root / "audio"
    sub.mkdir(parents=True)
    _write_wav(sub / "x.wav")
    monkeypatch.setattr(mod, "_PII_CORPUS_ROOTS", (pii_root.resolve(),))

    utts = load_corpus(sub, allow_pii=True)
    assert [u.stem for u in utts] == ["x"]


# ---------------------------------------------------------------------------
# _aggregate
# ---------------------------------------------------------------------------


def _result(transcript="", latency=1.0, failed=False):
    return DecodeResult(transcript=transcript, latency_s=latency, failed=failed)


def test_aggregate_computes_mean_and_median():
    results = [
        (_result("the quick brown fox", latency=1.0), "the quick brown fox"),  # wer 0.0
        (_result("the quick green fox", latency=3.0), "the quick brown fox"),  # wer 0.25
    ]
    agg = _aggregate("whisper", results)

    assert agg.name == "whisper"
    assert agg.utterances == 2
    assert agg.failures == 0
    assert agg.mean_wer == pytest.approx(0.125)
    assert agg.median_wer == pytest.approx(0.125)
    assert agg.mean_latency_s == pytest.approx(2.0)
    assert agg.median_latency_s == pytest.approx(2.0)


def test_aggregate_excludes_failures_from_stats():
    results = [
        (_result("hello world", latency=1.0), "hello world"),  # wer 0.0
        (_result(failed=True, latency=9.0), "ignored ref"),  # excluded
    ]
    agg = _aggregate("parakeet", results)

    assert agg.utterances == 2
    assert agg.failures == 1
    # only the non-failed utterance feeds WER + latency
    assert agg.mean_wer == pytest.approx(0.0)
    assert agg.mean_latency_s == pytest.approx(1.0)


def test_aggregate_latency_only_utterance_has_no_wer():
    # a successful decode with no reference contributes latency but not WER
    results = [(_result("anything", latency=2.5), None)]
    agg = _aggregate("whisper", results)

    assert agg.failures == 0
    assert agg.mean_wer is None
    assert agg.median_wer is None
    assert agg.mean_latency_s == pytest.approx(2.5)


def test_aggregate_all_failures_yields_no_stats():
    results = [
        (_result(failed=True), "ref a"),
        (_result(failed=True), "ref b"),
    ]
    agg = _aggregate("whisper", results)

    assert agg.utterances == 2
    assert agg.failures == 2
    assert agg.mean_wer is None
    assert agg.mean_latency_s is None


def test_aggregate_empty_results():
    agg = _aggregate("whisper", [])
    assert agg.utterances == 0
    assert agg.failures == 0
    assert agg.mean_wer is None
    assert agg.mean_latency_s is None


# ---------------------------------------------------------------------------
# Fail-fast guard in run_benchmark
# ---------------------------------------------------------------------------


def test_run_benchmark_fails_fast_when_both_unreachable(tmp_path):
    # nonexistent socket paths => neither endpoint answers a connect handshake
    whisper = Endpoint(name="whisper", socket_path=str(tmp_path / "whisper.sock"))
    parakeet = Endpoint(name="parakeet", socket_path=str(tmp_path / "parakeet.sock"))
    utt = Utterance(stem="x", audio_path=tmp_path / "x.wav", reference="ref")

    with pytest.raises(SystemExit) as exc:
        asyncio.run(run_benchmark([utt], whisper, parakeet))

    msg = str(exc.value)
    assert "BOTH servers reachable" in msg
    # both endpoints are named in the unreachable list
    assert "whisper" in msg and "parakeet" in msg


def test_run_benchmark_fails_fast_when_one_unreachable(tmp_path, monkeypatch):
    # Make the whisper probe "succeed" while parakeet's socket is dead — the
    # A/B benchmark must still refuse rather than silently bench one ASR.
    import scripts.benchmark_asr_ab as mod

    async def _fake_probe(endpoint, expected_backend):
        return None if endpoint.name == "whisper" else "ConnectionRefusedError: dead"

    monkeypatch.setattr(mod, "_probe", _fake_probe)

    whisper = Endpoint(name="whisper", socket_path=str(tmp_path / "whisper.sock"))
    parakeet = Endpoint(name="parakeet", socket_path=str(tmp_path / "parakeet.sock"))
    utt = Utterance(stem="x", audio_path=tmp_path / "x.wav", reference="ref")

    with pytest.raises(SystemExit) as exc:
        asyncio.run(run_benchmark([utt], whisper, parakeet))

    msg = str(exc.value)
    assert "BOTH servers reachable" in msg
    # only parakeet is listed as unreachable
    assert "parakeet" in msg
    assert "whisper (" not in msg


def test_run_benchmark_fails_closed_on_backend_identity_mismatch(tmp_path, monkeypatch):
    # A socket that answers the handshake but reports a different ASR than its
    # label claims (stale LaunchAgent, swapped socket, mis-exported KODA_STT_*)
    # must abort the run — never emit a mislabeled comparison.
    import scripts.benchmark_asr_ab as mod

    async def _fake_probe(endpoint, expected_backend):
        # The "parakeet" endpoint is actually running mlx — identity mismatch.
        if endpoint.name == "parakeet":
            return (
                "endpoint reports backend 'mlx', expected 'parakeet' "
                "— this socket is not running the ASR its label claims"
            )
        return None

    monkeypatch.setattr(mod, "_probe", _fake_probe)

    whisper = Endpoint(name="whisper", socket_path=str(tmp_path / "whisper.sock"))
    parakeet = Endpoint(name="parakeet", socket_path=str(tmp_path / "parakeet.sock"))
    utt = Utterance(stem="x", audio_path=tmp_path / "x.wav", reference="ref")

    with pytest.raises(SystemExit) as exc:
        asyncio.run(run_benchmark([utt], whisper, parakeet))

    msg = str(exc.value)
    assert "running the ASR their labels claim" in msg
    assert "not running the ASR its label claims" in msg
    assert "parakeet" in msg


# ---------------------------------------------------------------------------
# Endpoint / formatting helpers
# ---------------------------------------------------------------------------


def test_fmt_handles_none_and_floats():
    assert _fmt(None) == "n/a"
    assert _fmt(0.12345) == "0.123"
    assert _fmt(1.0) == "1.000"


def test_endpoint_describe_prefers_uri():
    ep = Endpoint(name="whisper", socket_path="/tmp/s.sock", uri="ws://127.0.0.1:8765/")
    assert ep.describe() == "ws://127.0.0.1:8765/"


def test_endpoint_describe_falls_back_to_socket():
    ep = Endpoint(name="whisper", socket_path="/tmp/s.sock")
    assert ep.describe() == "/tmp/s.sock"


def test_endpoint_describe_unset():
    ep = Endpoint(name="whisper")
    assert ep.describe() == "<unset>"


# ---------------------------------------------------------------------------
# _build_endpoints
# ---------------------------------------------------------------------------


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_build_endpoints_uses_sockets_by_default(monkeypatch):
    monkeypatch.delenv("STT_WS_TOKEN", raising=False)
    args = _Args(
        whisper_socket="/tmp/w.sock",
        parakeet_socket="/tmp/p.sock",
        whisper_uri=None,
        parakeet_uri=None,
    )
    whisper, parakeet = _build_endpoints(args)

    assert whisper.socket_path == "/tmp/w.sock"
    assert whisper.uri is None
    assert parakeet.socket_path == "/tmp/p.sock"
    assert whisper.auth_token is None


def test_build_endpoints_uri_overrides_socket(monkeypatch):
    monkeypatch.setenv("STT_WS_TOKEN", "  secret  ")
    args = _Args(
        whisper_socket="/tmp/w.sock",
        parakeet_socket="/tmp/p.sock",
        whisper_uri="ws://127.0.0.1:8765/",
        parakeet_uri=None,
    )
    whisper, parakeet = _build_endpoints(args)

    # a URI nulls out the socket path for that endpoint
    assert whisper.socket_path is None
    assert whisper.uri == "ws://127.0.0.1:8765/"
    # the other endpoint keeps its socket
    assert parakeet.socket_path == "/tmp/p.sock"
    assert parakeet.uri is None
    # the shared auth token is trimmed
    assert whisper.auth_token == "secret"
    assert parakeet.auth_token == "secret"
