"""Text-quality helpers — degenerate-output detection.

Used by:

- ``stt_server/backends/mlx_whisper.py`` — drops Whisper segments that came
  back as a single token repeated ``N`` times (e.g. the "subscription
  subscription subscription" hallucination cascade observed on
  20260430-5492e348faa29c46).
- ``shared/transcript_cleaner.py`` — pre-cleanup short-circuit on degenerate
  input + symmetric output guard against same-length degenerate rewrites.

See ``docs/dev_plans/20260430-fix-whisper-hallucination.md``.

Defaults are calibrated against the existing ``~/koda-data`` raw-transcript
corpus. See ``scripts/calibrate_degenerate_threshold.py`` and the plan's
"Final Results" section for the empirical histogram.

Env vars (canonical PIPECAT_STT_* names first; the legacy KODA_* names are
deprecated but still honoured as aliases for backward compat):

- ``PIPECAT_STT_WHISPER_DEGENERATE_TOKEN_RATIO`` (canonical)
  / ``KODA_TEXT_QUALITY_DEGENERATE_TOKEN_RATIO`` (deprecated alias)
  / ``KODA_STT_WHISPER_DEGENERATE_TOKEN_RATIO`` (deprecated alias)
- ``PIPECAT_STT_WHISPER_DEGENERATE_MIN_TOKENS`` (canonical)
  / ``KODA_TEXT_QUALITY_DEGENERATE_MIN_TOKENS`` (deprecated alias)
  / ``KODA_STT_WHISPER_DEGENERATE_MIN_TOKENS`` (deprecated alias)

Precedence is canonical-first: the ``PIPECAT_STT_*`` name wins when set,
then the legacy ``KODA_*`` names are honoured. (The older
``KODA_TEXT_QUALITY_*`` names dropped the STT-specific prefix because this
module is imported from cleanup as well as STT — they remain supported.)
"""

from __future__ import annotations

import re
from collections import Counter

from stt_server.env import env_first

# A "word-shaped" token has at least one alphanumeric character (Unicode
# word class). Pure-punctuation tokens like ``-``, ``--``, ``***``, ``===``,
# ``•`` are markdown bullets / separators / page rules — semantically not
# hallucinations even when they cross the dominance threshold (e.g. a tight
# References-bullet list at the end of a cleaned transcript). We require the
# *dominant* token to be word-shaped before flagging the input as degenerate.
_WORD_RE = re.compile(r"\w", re.UNICODE)

_DEFAULT_RATIO = 0.40
_DEFAULT_MIN_TOKENS = 10

_RATIO_ENVS = (
    "PIPECAT_STT_WHISPER_DEGENERATE_TOKEN_RATIO",
    "KODA_TEXT_QUALITY_DEGENERATE_TOKEN_RATIO",
    "KODA_STT_WHISPER_DEGENERATE_TOKEN_RATIO",
)
_MIN_TOKENS_ENVS = (
    "PIPECAT_STT_WHISPER_DEGENERATE_MIN_TOKENS",
    "KODA_TEXT_QUALITY_DEGENERATE_MIN_TOKENS",
    "KODA_STT_WHISPER_DEGENERATE_MIN_TOKENS",
)


def _ratio_threshold() -> float:
    val = env_first(*_RATIO_ENVS)
    if val is None:
        return _DEFAULT_RATIO
    try:
        return float(val)
    except ValueError:
        return _DEFAULT_RATIO


def _min_tokens() -> int:
    val = env_first(*_MIN_TOKENS_ENVS)
    if val is None:
        return _DEFAULT_MIN_TOKENS
    try:
        return int(val)
    except ValueError:
        return _DEFAULT_MIN_TOKENS


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
    """True when ``text`` is dominated by a single repeated word-shaped token.

    Degenerate := dominant-unigram ratio strictly greater than the
    ratio threshold AND total tokens >= the min-tokens threshold AND the
    dominant token contains at least one alphanumeric character. Strictly
    greater-than for the ratio so a perfectly-distributed input at exactly
    the threshold is NOT flagged. The word-shape requirement filters out
    markdown bullet lists and separator rules whose dominant token is pure
    punctuation (``-``, ``*``, ``===``, ``•``) — these are not Whisper
    hallucinations even when they technically cross the dominance gate.
    """
    ratio, token, total = dominant_unigram_ratio(text)
    if total < _min_tokens():
        return False
    if ratio <= _ratio_threshold():
        return False
    if token is None or not _WORD_RE.search(token):
        return False
    return True


def has_degenerate_paragraph(text: str) -> bool:
    """True when ANY paragraph or utterance line in ``text`` is degenerate.

    The whole-document gate misses paragraph-local hallucination walls in
    long transcripts (e.g. one ``"subscription " * 11189`` paragraph buried
    in 120K chars of normal speech — whole-doc dominant share stays well
    below the 0.40 threshold). We scan two shapes:

    - Blank-line-separated paragraphs — matches the cleaned-markdown shape
      produced by the cleanup pipeline and the repair script.
    - Single-newline-separated lines — matches the utterance-line shape
      produced by ``shared.classifier._build_transcript``, where each
      utterance is one line joined by ``"\\n"``. A wall-of-tokens utterance
      embedded between normal lines is diluted below the per-paragraph
      threshold when the whole transcript is one blank-line block, so the
      per-line pass is the gate that actually catches it.
    """
    if not text or not text.strip():
        return False
    for paragraph in text.split("\n\n"):
        if is_degenerate(paragraph):
            return True
    for line in text.splitlines():
        if is_degenerate(line):
            return True
    return False
