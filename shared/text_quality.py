"""Text-quality helpers — degenerate-output detection.

Used by:

- ``stt_server/backends/mlx_whisper.py`` — drops Whisper segments that came
  back as a single token repeated ``N`` times (e.g. the "subscription
  subscription subscription" hallucination cascade observed on
  20260430-5492e348faa29c46).
- ``shared/transcript_cleaner.py`` (Phase 3) — pre-cleanup short-circuit on
  degenerate input + symmetric output guard against same-length degenerate
  rewrites.

See ``docs/dev_plans/20260430-fix-whisper-hallucination.md``.

Defaults are calibrated against the existing ``~/koda-data`` raw-transcript
corpus. See ``scripts/calibrate_degenerate_threshold.py`` and the plan's
"Final Results" section for the empirical histogram.
"""

from __future__ import annotations

import logging
import os
from collections import Counter

logger = logging.getLogger("shared.text_quality")


# Defaults. Resolved at call time (not import time) so tests / operators can
# monkeypatch env vars without re-importing the module. Mirrors the env-helper
# pattern in ``stt_server/backends/mlx_whisper.py``.
_FLOAT_DEFAULT_RATIO = 0.40
_INT_DEFAULT_MIN_TOKENS = 10


def _env_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    try:
        return float(val)
    except ValueError:
        logger.warning("invalid float for %s=%r; using default %s", name, val, default)
        return default


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    try:
        return int(val)
    except ValueError:
        logger.warning("invalid int for %s=%r; using default %s", name, val, default)
        return default


def dominant_unigram_ratio(text: str) -> tuple[float, str | None, int]:
    """Return ``(ratio, dominant_token, total_tokens)``.

    Case-folds and whitespace-tokenises ``text``. ``ratio`` is the most-
    frequent unigram's share of total tokens (``0.0`` if the text has no
    tokens). ``dominant_token`` is ``None`` only for the empty-token case.
    """
    tokens = text.casefold().split()
    if not tokens:
        return 0.0, None, 0
    most_token, most_count = Counter(tokens).most_common(1)[0]
    return most_count / len(tokens), most_token, len(tokens)


def is_degenerate(text: str) -> bool:
    """True when ``text`` is dominated by a single repeated token.

    Degenerate := ``dominant_unigram_ratio(text) > KODA_STT_WHISPER_DEGENERATE_TOKEN_RATIO``
    AND ``len(tokens) >= KODA_STT_WHISPER_DEGENERATE_MIN_TOKENS``. Strictly
    greater-than for the ratio so a perfectly-distributed input at exactly the
    threshold is NOT flagged.
    """
    ratio_threshold = _env_float("KODA_STT_WHISPER_DEGENERATE_TOKEN_RATIO", _FLOAT_DEFAULT_RATIO)
    min_tokens = _env_int("KODA_STT_WHISPER_DEGENERATE_MIN_TOKENS", _INT_DEFAULT_MIN_TOKENS)
    ratio, _token, total = dominant_unigram_ratio(text)
    if total < min_tokens:
        return False
    return ratio > ratio_threshold
