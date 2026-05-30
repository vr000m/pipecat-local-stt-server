"""Tests for the Parakeet (NVIDIA TDT) STT backend.

``parakeet_mlx`` is fully stubbed via ``sys.modules`` injection so CI never
downloads a ~1.5 GB model. These tests pin the V1 wire contract the backend
must satisfy:

  * ``ParakeetBackend`` / ``_ParakeetStream`` satisfy the structural
    ``TranscriptionBackend`` / ``BackendStream`` protocols (``backend.py``);
  * a non-empty stubbed decode yields exactly one ``delta`` + one
    ``completed`` ``TranscriptEvent``;
  * an empty-text stubbed decode yields ``completed`` only, no ``delta``
    (matches ``_MLXStream.events``);
  * a stubbed decode that raises mid-decode propagates the exception out of
    ``events()`` / ``end()`` — no swallowed empty ``completed``, no
    ``failed`` event kind;
  * a first-decode model-load failure raises the same way from its distinct
    call site;
  * ``cancel()`` before ``end()`` yields no events; ``cancel()`` mid-decode
    is bounded and crash-free;
  * a 60 s+ stubbed utterance is not silently truncated;
  * if backend-scoped decode locks are kept, two overlapping decodes
    serialise;
  * ``DEFAULT_PARAKEET_MODEL`` is a non-empty string constant.

Audio is a programmatic synthetic PCM16LE buffer — no binary fixtures are
committed (PII / repo-bloat policy).
"""

from __future__ import annotations

import asyncio
import sys
import types
import wave
from typing import Any

import numpy as np
import pytest

# 16 kHz PCM16LE is the wire sample rate enforced by the protocol.
_SAMPLE_RATE_HZ = 16_000


# ---------------------------------------------------------------------------
# parakeet_mlx stub — installed before the backend module is imported so the
# backend's lazy ``import parakeet_mlx`` resolves to this fake.
# ---------------------------------------------------------------------------


class _FakeAlignedResult:
    """Mimics the object ``parakeet_mlx`` model.transcribe() returns.

    The real object exposes a ``.text`` attribute (and chunked sentence
    objects). The backend only needs the final text, so a ``.text`` carrier
    is the minimal contract. ``text`` is exposed both as an attribute and via
    mapping access so the backend can read it either way.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.sentences: list[Any] = []

    def __getitem__(self, key: str) -> Any:  # tolerate dict-style access
        return {"text": self.text, "sentences": self.sentences}[key]

    def get(self, key: str, default: Any = None) -> Any:
        return {"text": self.text, "sentences": self.sentences}.get(key, default)


class _FakeParakeetModel:
    """Stub for the model object returned by ``from_pretrained``."""

    def __init__(self) -> None:
        self.return_text = "hello from parakeet"
        self.raise_on_transcribe: BaseException | None = None
        self.transcribe_calls = 0
        self.last_path: Any = None
        self.last_frame_count: int | None = None
        # When set, the model blocks inside transcribe() until released —
        # used to exercise cancel-mid-decode and decode serialisation.
        self._gate: "threading_Event | None" = None

    def transcribe(self, path: Any, *args: Any, **kwargs: Any) -> _FakeAlignedResult:
        # The real ``parakeet_mlx`` ``transcribe()`` takes a file *path*. The
        # backend writes the buffered PCM to a temp WAV and passes the path;
        # capture the decoded frame count by reading that WAV so tests can
        # assert no silent truncation. Read before the gate — the backend
        # unlinks the temp file only after ``transcribe()`` returns.
        self.transcribe_calls += 1
        self.last_path = path
        try:
            with wave.open(str(path), "rb") as w:
                self.last_frame_count = w.getnframes()
        except (OSError, wave.Error):
            self.last_frame_count = None
        if self._gate is not None:
            self._gate.wait()
        if self.raise_on_transcribe is not None:
            raise self.raise_on_transcribe
        return _FakeAlignedResult(self.return_text)

    # parakeet_mlx model objects are also directly callable in some versions.
    __call__ = transcribe


# Imported lazily inside the stub to keep the module import cheap.
import threading as _threading  # noqa: E402

threading_Event = _threading.Event


class _FakeParakeetMLXModule(types.ModuleType):
    """Drop-in replacement for the ``parakeet_mlx`` package."""

    def __init__(self) -> None:
        super().__init__("parakeet_mlx")
        self.model = _FakeParakeetModel()
        self.from_pretrained_calls: list[str] = []
        self.raise_on_from_pretrained: BaseException | None = None

    def from_pretrained(self, model_id: str, *args: Any, **kwargs: Any) -> _FakeParakeetModel:
        self.from_pretrained_calls.append(model_id)
        if self.raise_on_from_pretrained is not None:
            raise self.raise_on_from_pretrained
        return self.model


@pytest.fixture
def fake_parakeet(monkeypatch):
    """Install the fake ``parakeet_mlx`` module and (re)import the backend.

    The backend module is dropped from ``sys.modules`` so it re-imports
    against the fake — its lazy ``import parakeet_mlx`` then resolves here.
    """
    fake = _FakeParakeetMLXModule()
    monkeypatch.setitem(sys.modules, "parakeet_mlx", fake)
    monkeypatch.delitem(sys.modules, "stt_server.backends.parakeet", raising=False)
    return fake


@pytest.fixture
def parakeet_mod(fake_parakeet):
    """The freshly imported ``stt_server.backends.parakeet`` module."""
    import stt_server.backends.parakeet as mod

    return mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pcm(num_samples: int, *, amplitude: int = 800) -> bytes:
    """Return ``num_samples`` of non-silent PCM16LE audio."""
    sig = (np.ones(num_samples, dtype=np.int16) * amplitude).tobytes()
    return sig


def _pcm_seconds(seconds: float) -> bytes:
    return _pcm(int(seconds * _SAMPLE_RATE_HZ))


async def _drive(backend, *, language: str | None = "en", audio: bytes | None = None):
    """Open a stream, feed audio, end it, and collect emitted events."""
    if audio is None:
        audio = _pcm(_SAMPLE_RATE_HZ)  # 1 s
    stream = await backend.open_stream(language=language)
    await stream.feed(audio)
    await stream.end()
    events = [ev async for ev in stream.events()]
    return stream, events


# ---------------------------------------------------------------------------
# Structural protocol conformance
# ---------------------------------------------------------------------------


def test_backend_satisfies_transcription_backend_protocol(parakeet_mod):
    from stt_server.backend import TranscriptionBackend

    backend = parakeet_mod.ParakeetBackend(model="fake-parakeet")
    assert isinstance(backend, TranscriptionBackend)


async def test_stream_satisfies_backend_stream_protocol(parakeet_mod):
    from stt_server.backend import BackendStream

    backend = parakeet_mod.ParakeetBackend(model="fake-parakeet")
    await backend.start()
    stream = await backend.open_stream(language="en")
    assert isinstance(stream, BackendStream)
    await stream.cancel()


def test_default_parakeet_model_constant_is_nonempty_string(parakeet_mod):
    val = parakeet_mod.DEFAULT_PARAKEET_MODEL
    assert isinstance(val, str)
    assert val.strip(), "DEFAULT_PARAKEET_MODEL must be a non-empty string"


def test_backend_exposes_identity(parakeet_mod):
    """``backend_name`` / ``model`` feed the server.hello backend field."""
    backend = parakeet_mod.ParakeetBackend(model="fake-parakeet")
    assert backend.backend_name == "parakeet"
    assert backend.model == "fake-parakeet"


def test_no_failed_event_kind_defined(parakeet_mod):
    """The backend must not invent a ``failed`` TranscriptEvent kind."""
    src = "".join(open(parakeet_mod.__file__, encoding="utf-8").readlines())
    # A literal "failed" kind would be a protocol violation; decode failure
    # is signalled by raising.
    assert 'kind="failed"' not in src
    assert "kind='failed'" not in src


# ---------------------------------------------------------------------------
# Lazy import discipline — parakeet_mlx must not load at module/__init__ time
# ---------------------------------------------------------------------------


def test_init_does_not_load_model(parakeet_mod, fake_parakeet):
    """Constructing the backend must not call ``from_pretrained``."""
    parakeet_mod.ParakeetBackend(model="fake-parakeet")
    assert fake_parakeet.from_pretrained_calls == []


async def test_start_does_not_load_model(parakeet_mod, fake_parakeet):
    """``start()`` does an eager import to fail fast but defers model load
    to the first decode (the ~1.5 GB model must not load in start())."""
    backend = parakeet_mod.ParakeetBackend(model="fake-parakeet")
    await backend.start()
    assert fake_parakeet.from_pretrained_calls == [], (
        "model must load lazily on first decode, not in start()"
    )


async def test_model_loads_on_first_decode(parakeet_mod, fake_parakeet):
    backend = parakeet_mod.ParakeetBackend(model="fake-parakeet")
    await backend.start()
    await _drive(backend)
    assert fake_parakeet.from_pretrained_calls == ["fake-parakeet"]


# ---------------------------------------------------------------------------
# TranscriptEvent sequence — non-empty and empty-text decode
# ---------------------------------------------------------------------------


async def test_nonempty_decode_yields_delta_then_completed(parakeet_mod, fake_parakeet):
    fake_parakeet.model.return_text = "the quarterly numbers look solid"
    backend = parakeet_mod.ParakeetBackend(model="fake-parakeet")
    await backend.start()
    _stream, events = await _drive(backend)

    assert [ev.kind for ev in events] == ["delta", "completed"]
    assert events[0].text == "the quarterly numbers look solid"
    assert events[1].text == "the quarterly numbers look solid"


async def test_empty_text_decode_yields_completed_only(parakeet_mod, fake_parakeet):
    """Near-silence: Parakeet returns empty text -> completed only, no delta.

    This mirrors ``_MLXStream.events`` and is load-bearing for wire parity.
    """
    fake_parakeet.model.return_text = ""
    backend = parakeet_mod.ParakeetBackend(model="fake-parakeet")
    await backend.start()
    _stream, events = await _drive(backend)

    assert [ev.kind for ev in events] == ["completed"]
    assert events[0].text == ""


async def test_whitespace_only_decode_yields_completed_only(parakeet_mod, fake_parakeet):
    """Whitespace-only decode is treated as empty (no meaningful delta)."""
    fake_parakeet.model.return_text = "   "
    backend = parakeet_mod.ParakeetBackend(model="fake-parakeet")
    await backend.start()
    _stream, events = await _drive(backend)
    assert "delta" not in [ev.kind for ev in events]
    assert events[-1].kind == "completed"


# ---------------------------------------------------------------------------
# Decode-failure path — raise, do not emit
# ---------------------------------------------------------------------------


async def test_decode_failure_propagates_out_of_end_or_events(parakeet_mod, fake_parakeet):
    """A stubbed decode raising mid-decode must propagate the exception.

    The failure may surface from ``end()`` (decode runs there) or from
    ``events()`` (decode runs lazily) depending on the implementation; either
    is acceptable, but the exception must NOT be swallowed into an empty
    ``completed``.
    """
    fake_parakeet.model.raise_on_transcribe = RuntimeError("metal OOM mid-decode")
    backend = parakeet_mod.ParakeetBackend(model="fake-parakeet")
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(_pcm(_SAMPLE_RATE_HZ))

    with pytest.raises(RuntimeError, match="metal OOM mid-decode"):
        await stream.end()
        # If end() did not raise, the decode is deferred to events().
        _ = [ev async for ev in stream.events()]


async def test_decode_failure_does_not_emit_silent_completed(parakeet_mod, fake_parakeet):
    """No swallowed empty ``completed`` may slip through on decode failure."""
    fake_parakeet.model.raise_on_transcribe = RuntimeError("decode boom")
    backend = parakeet_mod.ParakeetBackend(model="fake-parakeet")
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(_pcm(_SAMPLE_RATE_HZ))

    events: list[Any] = []
    with pytest.raises(RuntimeError):
        try:
            await stream.end()
        finally:
            # Whatever events() yields before/instead of raising, it must
            # never be a lone empty ``completed`` standing in for the error.
            async for ev in stream.events():
                events.append(ev)
    assert events == [], "decode failure must raise, not emit a fake completed"


async def test_first_decode_model_load_failure_propagates(parakeet_mod, fake_parakeet):
    """First-decode model-load failure raises from its distinct call site."""
    fake_parakeet.raise_on_from_pretrained = RuntimeError("model download failed")
    backend = parakeet_mod.ParakeetBackend(model="fake-parakeet")
    await backend.start()  # eager import succeeds; model load is deferred
    stream = await backend.open_stream(language="en")
    await stream.feed(_pcm(_SAMPLE_RATE_HZ))

    with pytest.raises(RuntimeError, match="model download failed"):
        await stream.end()
        _ = [ev async for ev in stream.events()]


# ---------------------------------------------------------------------------
# cancel() semantics
# ---------------------------------------------------------------------------


async def test_cancel_before_end_yields_no_events(parakeet_mod, fake_parakeet):
    backend = parakeet_mod.ParakeetBackend(model="fake-parakeet")
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(_pcm(_SAMPLE_RATE_HZ))
    await stream.cancel()

    events = [ev async for ev in stream.events()]
    assert events == []
    # A cancelled stream must not have run a decode.
    assert fake_parakeet.model.transcribe_calls == 0


async def test_cancel_mid_decode_is_bounded_and_crash_free(parakeet_mod, fake_parakeet):
    """cancel() while a decode is in flight must not crash and must return
    promptly — the awaiting end() coroutine may unwind, but cancel() itself
    is bounded (mirror ``_MLXStream.cancel`` / test_stt_server.py:313)."""
    gate = threading_Event()
    fake_parakeet.model._gate = gate
    backend = parakeet_mod.ParakeetBackend(model="fake-parakeet")
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(_pcm(_SAMPLE_RATE_HZ))

    end_task = asyncio.create_task(stream.end())
    # Let the decode reach the gate.
    await asyncio.sleep(0.05)

    # cancel() must return promptly even though a decode thread is blocked.
    await asyncio.wait_for(stream.cancel(), timeout=2.0)

    # Release the decode thread so it can exit cleanly.
    gate.set()
    # end() must settle (return or raise CancelledError) — never hang.
    try:
        await asyncio.wait_for(end_task, timeout=3.0)
    except (asyncio.CancelledError, Exception):
        pass
    # A cancelled stream yields no events regardless of decode outcome.
    events = [ev async for ev in stream.events()]
    assert events == []


# ---------------------------------------------------------------------------
# Long-utterance guard — a 60 s+ utterance must not be silently truncated
# ---------------------------------------------------------------------------


async def test_long_utterance_not_silently_truncated(parakeet_mod, fake_parakeet):
    """A 60 s utterance (well past Parakeet's ~24 s native chunk window) must
    reach the decoder without dropped bytes.

    The plan defers the chunk-vs-cap choice. This test asserts the weaker,
    choice-independent invariant: every fed byte is accounted for at decode
    time (chunk-and-concatenate -> all bytes decoded; cap-with-warning ->
    still no SILENT loss because the cap is the V1 protocol ceiling, which a
    60 s utterance is under). If the implementer chose chunking, the audio
    handed to the model covers the full duration.
    """
    seconds = 60.0
    audio = _pcm_seconds(seconds)
    backend = parakeet_mod.ParakeetBackend(model="fake-parakeet")
    await backend.start()
    _stream, events = await _drive(backend, audio=audio)

    # The decode must have happened and produced the normal event shape.
    assert events[-1].kind == "completed"
    assert fake_parakeet.model.transcribe_calls >= 1

    # No silent truncation: the WAV the model decoded spans the full 60 s.
    frames = fake_parakeet.model.last_frame_count
    assert frames is not None, "model did not receive a readable WAV path"
    duration_s = frames / _SAMPLE_RATE_HZ
    assert duration_s == pytest.approx(seconds, rel=0.02), (
        f"60s utterance silently truncated to {duration_s:.1f}s"
    )


# ---------------------------------------------------------------------------
# Decode serialisation — two overlapping decodes must not run concurrently
# (only meaningful if the implementer kept the backend-scoped locks).
# ---------------------------------------------------------------------------


async def test_overlapping_decodes_serialise(parakeet_mod, fake_parakeet):
    """Two streams from one backend must not decode concurrently.

    parakeet-mlx / Metal is not verified concurrency-safe, so the plan keeps
    a backend-scoped decode lock. This test gates the first decode and proves
    the second cannot enter ``transcribe()`` until the first releases.
    """
    gate = threading_Event()
    model = fake_parakeet.model
    model._gate = gate

    backend = parakeet_mod.ParakeetBackend(model="fake-parakeet")
    await backend.start()

    s1 = await backend.open_stream(language="en")
    s2 = await backend.open_stream(language="en")
    await s1.feed(_pcm(_SAMPLE_RATE_HZ))
    await s2.feed(_pcm(_SAMPLE_RATE_HZ))

    t1 = asyncio.create_task(s1.end())
    await asyncio.sleep(0.05)  # let decode 1 reach the gate
    t2 = asyncio.create_task(s2.end())
    await asyncio.sleep(0.05)  # give decode 2 a chance to (wrongly) start

    # With serialisation, only decode 1 has entered transcribe().
    assert model.transcribe_calls == 1, "second decode must wait for the lock"

    gate.set()  # release decode 1; decode 2 may now proceed
    await asyncio.wait_for(asyncio.gather(t1, t2), timeout=5.0)
    assert model.transcribe_calls == 2
