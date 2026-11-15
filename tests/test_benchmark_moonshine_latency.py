"""Unit tests for the pure helpers in ``scripts/benchmark_moonshine_latency.py``.

The benchmark is an operator tool (paced, real wire, real model); only its
deterministic helpers are CI-testable: resampling, utterance cutting, the
PCM16 -> float conversion shared with the backend, p50 / cold-run discard and
the report table. No audio file is read and no real speech is synthesised.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import numpy as np
import pytest

from scripts.benchmark_moonshine_latency import (
    DEFAULT_LENGTHS_S,
    DEFAULT_RUNS,
    build_utterances,
    cut_utterance,
    discard_cold,
    format_report,
    frame_bytes,
    main,
    measure_stop_flush,
    p50,
    paced_send,
    pcm16_to_float_list,
    resample_pcm16,
    resolve_auth_token,
    summarise,
)

_RATE = 16_000


def _tone(seconds: float, rate: int = _RATE, amplitude: int = 3000) -> bytes:
    t = np.arange(int(seconds * rate))
    return (np.sin(2 * np.pi * 220 * t / rate) * amplitude).astype("<i2").tobytes()


# --- p50 / cold run --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "expected"),
    [([5.0], 5.0), ([3.0, 1.0, 2.0], 2.0), ([4.0, 1.0, 3.0, 2.0], 2.5), ([1, 1, 1, 9], 1.0)],
)
def test_p50(values, expected):
    assert p50(values) == pytest.approx(expected)


def test_p50_of_nothing_is_an_error():
    with pytest.raises(ValueError):
        p50([])


def test_discard_cold_drops_only_the_first_run():
    assert discard_cold([9.0, 1.0, 2.0, 3.0]) == [1.0, 2.0, 3.0]


@pytest.mark.parametrize("runs", [[], [1.0]])
def test_discard_cold_needs_at_least_two_runs(runs):
    with pytest.raises(ValueError):
        discard_cold(runs)


def test_summarise_is_the_p50_of_the_warm_runs_per_length():
    # the cold run (first) is a large outlier and must not influence the median
    out = summarise({5: [99.0, 0.2, 0.4, 0.3], 3: [50.0, 0.1, 0.1, 0.3]})
    assert out == {3: pytest.approx(0.1), 5: pytest.approx(0.3)}
    assert list(out) == [3, 5], "sorted by utterance length"


def test_defaults_match_the_plan():
    assert tuple(DEFAULT_LENGTHS_S) == (3, 5, 10)
    assert DEFAULT_RUNS == 10


# --- cutting / resampling / conversion -----------------------------------------------------------


@pytest.mark.parametrize("seconds", [3, 5, 10])
def test_cut_utterance_is_exactly_n_seconds_of_16khz_pcm16(seconds):
    source = _tone(12)
    cut = cut_utterance(source, seconds)
    assert len(cut) == seconds * _RATE * 2
    assert cut == source[: len(cut)]


def test_cut_utterance_refuses_a_too_short_source():
    with pytest.raises(ValueError):
        cut_utterance(_tone(2), 3)


def test_cut_utterance_accepts_an_exact_length_source():
    assert len(cut_utterance(_tone(3), 3)) == 3 * _RATE * 2


def test_resample_48k_to_16k_has_a_third_of_the_samples():
    out = resample_pcm16(_tone(1, 48_000), 48_000)
    assert len(out) // 2 == _RATE


def test_resample_same_rate_is_identity():
    data = _tone(0.5)
    assert resample_pcm16(data, _RATE) == data


def test_resample_non_integer_ratio():
    out = resample_pcm16(_tone(1, 44_100), 44_100)
    assert abs(len(out) // 2 - _RATE) <= 1


def test_resample_preserves_a_low_frequency_tone():
    src = np.frombuffer(_tone(1, 48_000), dtype="<i2").astype(np.float64)
    out = np.frombuffer(resample_pcm16(src.astype("<i2").tobytes(), 48_000), dtype="<i2")
    assert np.abs(out).max() == pytest.approx(np.abs(src).max(), rel=0.05)


@pytest.mark.parametrize("bad", [(0, 16000), (48000, 0), (-1, 16000)])
def test_resample_rejects_non_positive_rates(bad):
    with pytest.raises(ValueError):
        resample_pcm16(_tone(0.1), *bad)


def test_resample_empty_input():
    assert resample_pcm16(b"", 48_000) == b""


def test_build_utterances_cuts_each_length_at_16khz():
    utts = build_utterances(_tone(12, 48_000), 48_000)
    assert sorted(utts) == [3, 5, 10]
    for n, pcm in utts.items():
        assert len(pcm) == n * _RATE * 2


def test_build_utterances_source_too_short_is_an_error():
    with pytest.raises(ValueError):
        build_utterances(_tone(4, 48_000), 48_000)


def test_pcm16_to_float_list_matches_the_backend_conversion():
    samples = np.array([0, 1, -1, 16384, -16384, 32767, -32768], dtype="<i2")
    out = pcm16_to_float_list(samples.tobytes())
    assert isinstance(out, list) and all(isinstance(x, float) for x in out)
    assert out == pytest.approx((samples / 32768).tolist(), abs=1e-7)
    assert min(out) == -1.0 and max(out) < 1.0


def test_pcm16_to_float_list_ignores_a_trailing_odd_byte():
    assert len(pcm16_to_float_list(b"\x01\x00\x02")) == 1


def test_frame_bytes_is_whole_samples():
    assert frame_bytes(100) == 3200
    assert frame_bytes(100) % 2 == 0


# --- paced sender --------------------------------------------------------------------------------


async def test_paced_send_delivers_every_byte_in_frame_sized_chunks():
    data = _tone(1.0)
    chunks: list[bytes] = []
    await paced_send(chunks.append, data, pace=False)
    assert b"".join(chunks) == data
    assert all(len(c) == frame_bytes(100) for c in chunks[:-1])


async def test_paced_send_awaits_coroutine_senders():
    got = []

    async def send(chunk: bytes) -> None:
        got.append(len(chunk))

    await paced_send(send, _tone(0.5), pace=False)
    assert sum(got) == len(_tone(0.5))


async def test_paced_send_paces_at_real_time():
    data = _tone(0.5)  # five 100 ms frames
    t0 = time.perf_counter()
    await asyncio.wait_for(paced_send(lambda c: None, data, pace=True), timeout=5)
    assert time.perf_counter() - t0 >= 0.4


# --- report ----------------------------------------------------------------------------------------


def test_format_report_has_a_row_per_length_with_milliseconds():
    text = format_report({3: 0.250, 5: 0.400, 10: 0.900}, {3: 0.100, 5: 0.150, 10: 0.300})
    for length in ("3", "5", "10"):
        assert any(line.split()[:2] == [length, "s"] for line in text.splitlines())
    assert "250 ms" in text and "400 ms" in text and "900 ms" in text
    assert "100 ms" in text and "150 ms" in text and "300 ms" in text
    assert "0.40" in text, "flush/one-shot ratio: 0.100/0.250 = 0.40"


def test_format_report_without_flush_numbers_says_na():
    text = format_report({5: 0.4}, None)
    assert "n/a" in text and "400 ms" in text


def test_format_report_rows_are_sorted_by_length():
    text = format_report({10: 0.9, 3: 0.2, 5: 0.4}, None)
    rows = [ln for ln in text.splitlines() if ln.split()[1:2] == ["s"]]
    assert [int(r.split()[0]) for r in rows] == [3, 5, 10]


def test_format_report_names_the_backend_and_runs():
    text = format_report({3: 0.2}, None, backend="moonshine", runs=10)
    assert "moonshine" in text and "9" in text and "cold" in text


# --- library stop() flush ------------------------------------------------------------------------


@pytest.fixture
def transcriber(monkeypatch, tmp_path):
    from _fake_moonshine import ModelArch, install

    fake = install(monkeypatch, tmp_path / "cache")
    return fake, sys.modules["moonshine_voice"].Transcriber("m", ModelArch.MEDIUM_STREAMING)


def test_measure_stop_flush_times_a_clean_flush(transcriber):
    fake, t = transcriber
    elapsed = measure_stop_flush(t, _tone(0.3), pace=False)
    assert isinstance(elapsed, float) and elapsed >= 0
    assert fake.created_streams[-1].closed


@pytest.mark.parametrize("after_lines", [False, True])
def test_measure_stop_flush_fails_the_run_on_an_error_event(transcriber, after_lines):
    """``Stream.stop()`` (moonshine-voice 0.1.6) swallows a failed final transcription and
    emits an ``Error`` event instead: that run must fail, never feed the p50 as a fast flush."""
    fake, t = transcriber
    fake.error_on_stop = "flush failed"
    fake.error_after_lines = after_lines
    with pytest.raises(RuntimeError, match="stop\\(\\) flush reported an error"):
        measure_stop_flush(t, _tone(0.3), pace=False)
    assert fake.created_streams[-1].closed


# --- auth token: never from argv ------------------------------------------------------------------


def test_plaintext_auth_token_flag_is_refused(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--uri", "ws://127.0.0.1:9965/", "--auth-token", "secret"])
    assert excinfo.value.code == 2
    assert "secret" not in capsys.readouterr().out


def _token_file(tmp_path, text: str) -> str:
    path = tmp_path / "token"
    path.write_text(text)
    os.chmod(path, 0o600)
    return str(path)


@pytest.mark.parametrize(
    ("file_text", "env", "expected"),
    [
        (None, None, None),
        (None, "env-token", "env-token"),
        ("file-token\n", None, "file-token"),
        ("file-token\n", "env-token", "file-token"),  # the file wins
    ],
)
def test_resolve_auth_token_is_the_status_probe_policy(
    tmp_path, monkeypatch, file_text, env, expected
):
    """``--auth-token-file`` else ``STT_WS_TOKEN`` (what a client such as the bot sees),
    never the server-side ``PIPECAT_STT_AUTH_TOKEN``."""
    monkeypatch.setenv("PIPECAT_STT_AUTH_TOKEN", "server-side-secret")
    if env is None:
        monkeypatch.delenv("STT_WS_TOKEN", raising=False)
    else:
        monkeypatch.setenv("STT_WS_TOKEN", env)
    token_file = None if file_text is None else _token_file(tmp_path, file_text)
    assert resolve_auth_token(token_file) == expected
