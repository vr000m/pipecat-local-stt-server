"""Boundary tests for ``shared.text_quality.is_degenerate``.

Covers the post-decode degenerate-output filter from Phase 2 of
``docs/dev_plans/20260430-fix-whisper-hallucination.md``. Defaults shipped:

- ``KODA_STT_WHISPER_DEGENERATE_TOKEN_RATIO`` = 0.40 (strictly greater-than)
- ``KODA_STT_WHISPER_DEGENERATE_MIN_TOKENS`` = 10

Algorithm: case-fold, whitespace-tokenise, dominant-unigram share > ratio
AND tokens >= min_tokens.
"""

from __future__ import annotations

import pytest

from shared.text_quality import dominant_unigram_ratio, is_degenerate


# Env vars under test — keep in sync with shared/text_quality.py.
RATIO_ENV = "KODA_STT_WHISPER_DEGENERATE_TOKEN_RATIO"
MIN_TOKENS_ENV = "KODA_STT_WHISPER_DEGENERATE_MIN_TOKENS"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Ensure each test starts with the documented defaults.

    Other tests / shells may have set these vars; clear so this file always
    exercises the shipped defaults unless the test opts in via monkeypatch.
    """
    monkeypatch.delenv(RATIO_ENV, raising=False)
    monkeypatch.delenv(MIN_TOKENS_ENV, raising=False)


# ---------------------------------------------------------------------------
# Boundary cases from the plan.
# ---------------------------------------------------------------------------


def test_below_min_tokens_is_not_degenerate():
    # 9 tokens, all identical → 100% share but BELOW min_tokens (10).
    text = "subscription " * 9
    assert is_degenerate(text) is False


def test_above_ratio_threshold_with_enough_tokens_is_degenerate():
    # 11 tokens, 5 of them "x" → 5/11 ≈ 0.4545 > 0.40 default.
    # Layout: 5 dominant + 6 unique others.
    text = "x x x x x a b c d e f"
    ratio, token, total = dominant_unigram_ratio(text)
    assert total == 11
    assert token == "x"
    assert ratio == pytest.approx(5 / 11)
    assert ratio > 0.40
    assert is_degenerate(text) is True


def test_below_ratio_threshold_with_enough_tokens_is_not_degenerate():
    # 11 tokens, 4 of them "x" → 4/11 ≈ 0.3636 < 0.40 default.
    text = "x x x x a b c d e f g"
    ratio, token, total = dominant_unigram_ratio(text)
    assert total == 11
    assert token == "x"
    assert ratio == pytest.approx(4 / 11)
    assert ratio < 0.40
    assert is_degenerate(text) is False


def test_normal_english_sentence_is_not_degenerate():
    text = (
        "The quick brown fox jumps over the lazy dog while the curious cat "
        "watches quietly from the windowsill on a bright Tuesday morning."
    )
    assert is_degenerate(text) is False


def test_subscription_wall_is_degenerate():
    # The exact Whisper-hallucination shape this filter exists to catch.
    text = "subscription " * 100
    assert is_degenerate(text) is True


def test_60_percent_dominant_with_real_text_tail_is_degenerate():
    # 60 copies of "subscription" + 40 distinct real-text tokens → share 0.60.
    dominant = "subscription " * 60
    tail = " ".join(f"word{i}" for i in range(40))
    text = dominant + tail
    ratio, token, total = dominant_unigram_ratio(text)
    assert total == 100
    assert token == "subscription"
    assert ratio == pytest.approx(0.60)
    assert is_degenerate(text) is True


# ---------------------------------------------------------------------------
# Empty / whitespace boundary.
# ---------------------------------------------------------------------------


def test_empty_string_is_not_degenerate():
    assert is_degenerate("") is False


def test_whitespace_only_is_not_degenerate():
    assert is_degenerate("   \t\n  ") is False


# ---------------------------------------------------------------------------
# Env override — both knobs must be picked up at call time (not import time).
# ---------------------------------------------------------------------------


def test_env_override_ratio_threshold_relaxes_filter(monkeypatch):
    # 11 tokens at 5/11 ≈ 0.4545 — degenerate under default 0.40, but NOT
    # under a relaxed threshold of 0.50.
    text = "x x x x x a b c d e f"
    assert is_degenerate(text) is True  # default 0.40
    monkeypatch.setenv(RATIO_ENV, "0.50")
    assert is_degenerate(text) is False


def test_env_override_ratio_threshold_tightens_filter(monkeypatch):
    # 11 tokens at 4/11 ≈ 0.3636 — NOT degenerate under default 0.40, but IS
    # under a tightened threshold of 0.30.
    text = "x x x x a b c d e f g"
    assert is_degenerate(text) is False
    monkeypatch.setenv(RATIO_ENV, "0.30")
    assert is_degenerate(text) is True


def test_env_override_min_tokens_changes_floor(monkeypatch):
    # 9-token wall at 100% share. Under default min_tokens=10 it's NOT
    # degenerate; lower min_tokens to 5 and it flips.
    text = "subscription " * 9
    assert is_degenerate(text) is False
    monkeypatch.setenv(MIN_TOKENS_ENV, "5")
    assert is_degenerate(text) is True


def test_env_override_min_tokens_raised_suppresses_short_walls(monkeypatch):
    # 11 tokens, 5 of them dominant → degenerate under default min=10. Raise
    # min_tokens to 20 and the same input no longer qualifies.
    text = "x x x x x a b c d e f"
    assert is_degenerate(text) is True
    monkeypatch.setenv(MIN_TOKENS_ENV, "20")
    assert is_degenerate(text) is False


# ---------------------------------------------------------------------------
# Paragraph-aware variant — catches a single hallucinated paragraph buried
# in an otherwise long, normal transcript. The whole-doc gate misses these
# because the wall's share of the full document is below the 0.40 threshold.
# ---------------------------------------------------------------------------


from shared.text_quality import has_degenerate_paragraph  # noqa: E402


def test_has_degenerate_paragraph_catches_buried_wall():
    normal = " ".join(["hello world today we discussed the project"] * 50)
    wall = "subscription " * 100
    transcript = f"{normal}\n\n{wall}\n\n{normal}"
    # Whole-document gate misses it (dominant share < 0.40 across the doc)
    assert is_degenerate(transcript) is False
    # Paragraph-aware gate catches it
    assert has_degenerate_paragraph(transcript) is True


def test_has_degenerate_paragraph_passes_clean_transcript():
    normal = " ".join(["hello world today we discussed the project"] * 50)
    transcript = f"{normal}\n\n{normal}\n\n{normal}"
    assert has_degenerate_paragraph(transcript) is False


def test_has_degenerate_paragraph_handles_empty_input():
    assert has_degenerate_paragraph("") is False
    assert has_degenerate_paragraph("   \n\n   ") is False


# ---------------------------------------------------------------------------
# Canonical (KODA_TEXT_QUALITY_*) env names take precedence over the
# STT-prefixed aliases, but aliases are still honoured for backward compat.
# ---------------------------------------------------------------------------


CANONICAL_RATIO_ENV = "KODA_TEXT_QUALITY_DEGENERATE_TOKEN_RATIO"
CANONICAL_MIN_TOKENS_ENV = "KODA_TEXT_QUALITY_DEGENERATE_MIN_TOKENS"


def test_canonical_env_var_takes_effect(monkeypatch):
    monkeypatch.setenv(CANONICAL_RATIO_ENV, "0.99")
    # Same input that's degenerate at 0.40 should not be at 0.99.
    text = "subscription " * 100  # 100% subscription
    assert is_degenerate(text) is True  # still 1.0 > 0.99
    monkeypatch.setenv(CANONICAL_RATIO_ENV, "1.5")
    assert is_degenerate(text) is False  # 1.0 not > 1.5


def test_canonical_wins_over_alias(monkeypatch):
    # Alias says relax (1.5, never degenerate); canonical says default-strict (0.40).
    monkeypatch.setenv(RATIO_ENV, "1.5")
    monkeypatch.setenv(CANONICAL_RATIO_ENV, "0.40")
    text = "x x x x x a b c d e f"  # 5/11 = 0.45
    assert is_degenerate(text) is True


def test_alias_still_honoured_when_canonical_unset(monkeypatch):
    monkeypatch.setenv(RATIO_ENV, "1.5")  # alias only — relax
    text = "x x x x x a b c d e f"
    assert is_degenerate(text) is False
