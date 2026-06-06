"""Tests for the MLX Whisper backend hallucination-suppression knobs.

Four ``KODA_STT_WHISPER_*`` env-driven kwargs are wired into
``mlx_whisper.transcribe()``. These tests pin:

  * the four kwargs are forwarded with the documented defaults,
  * env-var overrides flow through to the call,
  * the boolean parser treats ``"False"``/``"false"``/``"0"``/``""``/unset
    as False and ``"true"``/``"True"``/``"1"`` as True.

Audio is a programmatic synthetic loop signal — no binary fixtures are
committed (PII / repo-bloat policy).
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import numpy as np
import pytest

from stt_server.env import env_bool as _env_bool  # noqa: E402
from stt_server.backends.mlx_whisper import MLXWhisperBackend  # noqa: E402


# ---------------------------------------------------------------------------
# Boolean env parser (covered by acceptance criterion: "False"/"false"/"0"/""/
# unset disable conditioning; "true"/"True"/"1" enable).
# ---------------------------------------------------------------------------


_VAR = "PIPECAT_STT_WHISPER_CONDITION_ON_PREVIOUS_TEXT"


@pytest.mark.parametrize("val", ["False", "false", "0", ""])
def test_env_bool_falsey_strings_disable(monkeypatch, val):
    monkeypatch.setenv(_VAR, val)
    assert _env_bool(_VAR, default=True) is False


def test_env_bool_unset_uses_default(monkeypatch):
    monkeypatch.delenv(_VAR, raising=False)
    # Default is False — and unset must mean "use default".
    assert _env_bool(_VAR, default=False) is False
    assert _env_bool(_VAR, default=True) is True


@pytest.mark.parametrize("val", ["true", "True", "1", "yes", "on", " TRUE "])
def test_env_bool_truthy_strings_enable(monkeypatch, val):
    monkeypatch.setenv(_VAR, val)
    assert _env_bool(_VAR, default=False) is True


@pytest.mark.parametrize("val", ["False", "FALSE", "0", "no", "off", "garbage"])
def test_env_bool_other_strings_are_false(monkeypatch, val):
    monkeypatch.setenv(_VAR, val)
    assert _env_bool(_VAR, default=True) is False


# ---------------------------------------------------------------------------
# Mocked-transcribe kwarg forwarding
# ---------------------------------------------------------------------------


class _FakeMLXWhisperModule(types.ModuleType):
    """Drop-in replacement for the ``mlx_whisper`` package.

    Records the kwargs of the most recent ``transcribe`` call and returns
    a synthetic loop signal so we exercise the same data path the real
    decoder produces when it hallucinates.
    """

    def __init__(self) -> None:
        super().__init__("mlx_whisper")
        self.last_kwargs: dict[str, Any] = {}
        self.last_args: tuple[Any, ...] = ()
        self.return_text = "subscription " * 100  # synthetic loop signal

    def transcribe(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.last_args = args
        self.last_kwargs = kwargs
        return {"text": self.return_text}


@pytest.fixture
def fake_mlx(monkeypatch):
    fake = _FakeMLXWhisperModule()
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)
    # Ensure no leftover env from another test perturbs defaults.
    for k in [
        "KODA_STT_WHISPER_CONDITION_ON_PREVIOUS_TEXT",
        "KODA_STT_WHISPER_COMPRESSION_RATIO_THRESHOLD",
        "KODA_STT_WHISPER_LOGPROB_THRESHOLD",
        "KODA_STT_WHISPER_NO_SPEECH_THRESHOLD",
    ]:
        monkeypatch.delenv(k, raising=False)
    return fake


def _pcm_nonempty() -> bytes:
    # A small non-zero PCM16LE buffer; content is irrelevant because we
    # mock transcribe(), but ``_decode_sync`` short-circuits on empty audio.
    samples = (np.ones(1600, dtype=np.int16) * 100).tobytes()
    return samples


async def _run_decode(backend: MLXWhisperBackend) -> str:
    stream = await backend.open_stream(language="en")
    await stream.feed(_pcm_nonempty())
    await stream.end()
    return stream._result or ""


def test_transcribe_forwards_default_suppression_kwargs(fake_mlx, monkeypatch):
    # Disable the post-decode degenerate filter for this kwargs-only check
    # so the synthetic loop signal round-trips and proves the mock was hit.
    monkeypatch.setenv("PIPECAT_STT_WHISPER_DEGENERATE_TOKEN_RATIO", "1.1")
    backend = MLXWhisperBackend(model="fake-model")
    result = asyncio.run(_run_decode(backend))

    # The synthetic loop signal must round-trip (proves the mock was hit).
    assert result.startswith("subscription")

    kw = fake_mlx.last_kwargs
    # condition_on_previous_text disabled by default — load-bearing.
    assert kw["condition_on_previous_text"] is False
    assert kw["compression_ratio_threshold"] == pytest.approx(2.4)
    assert kw["logprob_threshold"] == pytest.approx(-1.0)
    assert kw["no_speech_threshold"] == pytest.approx(0.6)
    # Existing kwargs preserved.
    assert kw["path_or_hf_repo"] == "fake-model"
    assert kw["language"] == "en"
    assert kw["fp16"] is True
    assert kw["verbose"] is False


@pytest.mark.parametrize(
    "client_language, expected_kwarg",
    [
        ("en", "en"),
        ("es-ES", "es-ES"),
        ("auto", None),
        ("AUTO", None),
        ("  auto  ", None),
        ("", None),
        (None, None),
    ],
)
def test_auto_and_blank_language_recast_to_none(
    fake_mlx, monkeypatch, client_language, expected_kwarg
):
    """The backend recasts the cross-backend ``"auto"``/blank sentinel to
    ``None`` (Whisper's auto-detect) so a client need not know which backend is
    behind the socket; Whisper would otherwise raise on an ``"auto"`` token.
    Real language codes pass through unchanged."""
    monkeypatch.setenv("PIPECAT_STT_WHISPER_DEGENERATE_TOKEN_RATIO", "1.1")
    backend = MLXWhisperBackend(model="fake-model")

    async def run() -> None:
        stream = await backend.open_stream(language=client_language)
        await stream.feed(_pcm_nonempty())
        await stream.end()

    asyncio.run(run())
    assert fake_mlx.last_kwargs["language"] == expected_kwarg


def test_transcribe_forwards_koda_alias_overrides(fake_mlx, monkeypatch):
    # Deprecated KODA_STT_WHISPER_* aliases still drive the decode knobs when
    # the canonical PIPECAT_STT_* names are unset (backward compat). The
    # canonical path is covered by
    # test_transcribe_forwards_pipecat_canonical_overrides.
    monkeypatch.setenv("KODA_STT_WHISPER_CONDITION_ON_PREVIOUS_TEXT", "true")
    monkeypatch.setenv("KODA_STT_WHISPER_COMPRESSION_RATIO_THRESHOLD", "1.8")
    monkeypatch.setenv("KODA_STT_WHISPER_LOGPROB_THRESHOLD", "-0.5")
    monkeypatch.setenv("KODA_STT_WHISPER_NO_SPEECH_THRESHOLD", "0.75")

    backend = MLXWhisperBackend(model="fake-model")
    asyncio.run(_run_decode(backend))

    kw = fake_mlx.last_kwargs
    assert kw["condition_on_previous_text"] is True
    assert kw["compression_ratio_threshold"] == pytest.approx(1.8)
    assert kw["logprob_threshold"] == pytest.approx(-0.5)
    assert kw["no_speech_threshold"] == pytest.approx(0.75)


def test_transcribe_forwards_pipecat_canonical_overrides(fake_mlx, monkeypatch):
    # Canonical PIPECAT_STT_* names drive the decode knobs.
    monkeypatch.setenv("PIPECAT_STT_WHISPER_CONDITION_ON_PREVIOUS_TEXT", "true")
    monkeypatch.setenv("PIPECAT_STT_WHISPER_COMPRESSION_RATIO_THRESHOLD", "1.8")
    monkeypatch.setenv("PIPECAT_STT_WHISPER_LOGPROB_THRESHOLD", "-0.5")
    monkeypatch.setenv("PIPECAT_STT_WHISPER_NO_SPEECH_THRESHOLD", "0.75")

    backend = MLXWhisperBackend(model="fake-model")
    asyncio.run(_run_decode(backend))

    kw = fake_mlx.last_kwargs
    assert kw["condition_on_previous_text"] is True
    assert kw["compression_ratio_threshold"] == pytest.approx(1.8)
    assert kw["logprob_threshold"] == pytest.approx(-0.5)
    assert kw["no_speech_threshold"] == pytest.approx(0.75)


def test_transcribe_pipecat_wins_over_koda(fake_mlx, monkeypatch):
    # When both names are set, the canonical PIPECAT_STT_* value wins; the
    # legacy KODA_STT_* alias is still honoured when PIPECAT is unset.
    monkeypatch.setenv("PIPECAT_STT_WHISPER_NO_SPEECH_THRESHOLD", "0.75")
    monkeypatch.setenv("KODA_STT_WHISPER_NO_SPEECH_THRESHOLD", "0.1")
    monkeypatch.setenv("PIPECAT_STT_WHISPER_CONDITION_ON_PREVIOUS_TEXT", "true")
    monkeypatch.setenv("KODA_STT_WHISPER_CONDITION_ON_PREVIOUS_TEXT", "false")

    backend = MLXWhisperBackend(model="fake-model")
    asyncio.run(_run_decode(backend))

    kw = fake_mlx.last_kwargs
    assert kw["no_speech_threshold"] == pytest.approx(0.75)
    assert kw["condition_on_previous_text"] is True


@pytest.mark.parametrize("val", ["False", "false", "0", ""])
def test_condition_env_falsey_disables_at_call_site(fake_mlx, monkeypatch, val):
    monkeypatch.setenv("PIPECAT_STT_WHISPER_CONDITION_ON_PREVIOUS_TEXT", val)
    backend = MLXWhisperBackend(model="fake-model")
    asyncio.run(_run_decode(backend))
    assert fake_mlx.last_kwargs["condition_on_previous_text"] is False


@pytest.mark.parametrize("val", ["true", "True", "1"])
def test_condition_env_truthy_enables_at_call_site(fake_mlx, monkeypatch, val):
    monkeypatch.setenv("PIPECAT_STT_WHISPER_CONDITION_ON_PREVIOUS_TEXT", val)
    backend = MLXWhisperBackend(model="fake-model")
    asyncio.run(_run_decode(backend))
    assert fake_mlx.last_kwargs["condition_on_previous_text"] is True


def test_condition_unset_defaults_to_false(fake_mlx, monkeypatch):
    # Unset means *both* the canonical and the deprecated alias are absent.
    monkeypatch.delenv("PIPECAT_STT_WHISPER_CONDITION_ON_PREVIOUS_TEXT", raising=False)
    monkeypatch.delenv("KODA_STT_WHISPER_CONDITION_ON_PREVIOUS_TEXT", raising=False)
    backend = MLXWhisperBackend(model="fake-model")
    asyncio.run(_run_decode(backend))
    assert fake_mlx.last_kwargs["condition_on_previous_text"] is False


def test_backend_exposes_identity():
    """``backend_name`` / ``model`` feed the server.hello backend field."""
    backend = MLXWhisperBackend(model="fake-model")
    assert backend.backend_name == "mlx"
    assert backend.model == "fake-model"
