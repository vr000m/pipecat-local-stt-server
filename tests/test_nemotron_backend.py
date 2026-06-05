"""Tests for the Nemotron 3.5 ASR STT backend.

``mlx_audio`` is fully stubbed via ``sys.modules`` injection so CI never
downloads a model. These tests pin the V1 wire contract the backend must
satisfy:

  * ``NemotronBackend`` / ``_NemotronStream`` satisfy the structural
    ``TranscriptionBackend`` / ``BackendStream`` protocols (``backend.py``);
  * a non-empty stubbed decode yields exactly one ``delta`` + one
    ``completed`` ``TranscriptEvent``;
  * an empty-text stubbed decode yields ``completed`` only, no ``delta``;
  * a whitespace-only decode is treated as empty (exercises ``.strip()``);
  * a stubbed decode that raises mid-decode propagates the exception out of
    ``events()`` / ``end()`` — no swallowed empty ``completed``, no
    ``failed`` event kind;
  * a first-decode model-load failure raises the same way from its distinct
    call site;
  * ``cancel()`` before ``end()`` yields no events; ``cancel()`` mid-decode
    is bounded and crash-free;
  * a 60 s+ stubbed utterance is not silently truncated;
  * two overlapping decodes serialise on the backend-scoped lock;
  * the client-supplied ``language`` is FORWARDED to ``generate()`` (the one
    material difference from Parakeet) and a ``None`` language falls back to
    ``DEFAULT_NEMOTRON_LANGUAGE``;
  * ``DEFAULT_NEMOTRON_MODEL`` is a non-empty string constant;
  * temp-dir / PII + shutdown invariants hold (0o700, unlink, close-drain).

Audio is a programmatic synthetic PCM16LE buffer — no binary fixtures are
committed (PII / repo-bloat policy).
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import types
import wave
from typing import Any

import numpy as np
import pytest

# 16 kHz PCM16LE is the wire sample rate enforced by the protocol.
_SAMPLE_RATE_HZ = 16_000


# ---------------------------------------------------------------------------
# mlx_audio stub — installed before the backend module is imported so the
# backend's lazy ``from mlx_audio.stt import load`` resolves to this fake.
# ---------------------------------------------------------------------------


class _FakeAlignedResult:
    """Mimics the object ``mlx_audio`` model.generate() returns.

    The real object (``AlignedResult``) exposes a ``.text`` attribute. The
    backend only needs the final text, so a ``.text`` carrier is the minimal
    contract.
    """

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeNemotronModel:
    """Stub for the model object returned by ``mlx_audio.stt.load``."""

    def __init__(self) -> None:
        self.return_text = "hello from nemotron"
        self.raise_on_generate: BaseException | None = None
        self.generate_calls = 0
        self.last_path: Any = None
        self.last_language: Any = "<unset>"
        self.last_frame_count: int | None = None
        # When set, the model blocks inside generate() until released — used to
        # exercise cancel-mid-decode and decode serialisation.
        self._gate: "threading_Event | None" = None

    def generate(
        self, path: Any, *args: Any, language: Any = None, **kwargs: Any
    ) -> _FakeAlignedResult:
        # The real ``mlx_audio`` ``generate()`` takes a file *path*. The backend
        # writes the buffered PCM to a temp WAV and passes the path; capture the
        # decoded frame count by reading that WAV so tests can assert no silent
        # truncation. Read before the gate — the backend unlinks the temp file
        # only after ``generate()`` returns.
        self.generate_calls += 1
        self.last_path = path
        self.last_language = language
        try:
            with wave.open(str(path), "rb") as w:
                self.last_frame_count = w.getnframes()
        except (OSError, wave.Error):
            self.last_frame_count = None
        if self._gate is not None:
            self._gate.wait()
        if self.raise_on_generate is not None:
            raise self.raise_on_generate
        return _FakeAlignedResult(self.return_text)


# Imported lazily inside the stub to keep the module import cheap.
import threading as _threading  # noqa: E402

threading_Event = _threading.Event


class _FakeMLXAudioSTTModule(types.ModuleType):
    """Drop-in replacement for the ``mlx_audio.stt`` submodule."""

    def __init__(self) -> None:
        super().__init__("mlx_audio.stt")
        self.model = _FakeNemotronModel()
        self.load_calls: list[str] = []
        self.raise_on_load: BaseException | None = None

    def load(self, model_id: str, *args: Any, **kwargs: Any) -> _FakeNemotronModel:
        self.load_calls.append(model_id)
        if self.raise_on_load is not None:
            raise self.raise_on_load
        return self.model


@pytest.fixture
def fake_nemotron(monkeypatch):
    """Install the fake ``mlx_audio``/``mlx_audio.stt`` modules and (re)import
    the backend.

    The backend module is dropped from ``sys.modules`` so it re-imports against
    the fake — its lazy ``from mlx_audio.stt import load`` then resolves here.
    """
    stt_mod = _FakeMLXAudioSTTModule()
    pkg = types.ModuleType("mlx_audio")
    pkg.stt = stt_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio", pkg)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt", stt_mod)
    monkeypatch.delitem(sys.modules, "stt_server.backends.nemotron", raising=False)
    return stt_mod


@pytest.fixture
def nemotron_mod(fake_nemotron):
    """The freshly imported ``stt_server.backends.nemotron`` module."""
    import stt_server.backends.nemotron as mod

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


def test_backend_satisfies_transcription_backend_protocol(nemotron_mod):
    from stt_server.backend import TranscriptionBackend

    backend = nemotron_mod.NemotronBackend(model="fake-nemotron")
    assert isinstance(backend, TranscriptionBackend)


async def test_stream_satisfies_backend_stream_protocol(nemotron_mod):
    from stt_server.backend import BackendStream

    backend = nemotron_mod.NemotronBackend(model="fake-nemotron")
    await backend.start()
    stream = await backend.open_stream(language="en")
    assert isinstance(stream, BackendStream)
    await stream.cancel()


def test_default_nemotron_model_constant_is_nonempty_string(nemotron_mod):
    val = nemotron_mod.DEFAULT_NEMOTRON_MODEL
    assert isinstance(val, str)
    assert val.strip(), "DEFAULT_NEMOTRON_MODEL must be a non-empty string"


def test_backend_exposes_identity(nemotron_mod):
    """``backend_name`` / ``model`` feed the server.hello backend field."""
    backend = nemotron_mod.NemotronBackend(model="fake-nemotron")
    assert backend.backend_name == "nemotron"
    assert backend.model == "fake-nemotron"


def test_no_failed_event_kind_defined(nemotron_mod):
    """The backend must not invent a ``failed`` TranscriptEvent kind."""
    with open(nemotron_mod.__file__, encoding="utf-8") as f:
        src = f.read()
    # A literal "failed" kind would be a protocol violation; decode failure
    # is signalled by raising.
    assert 'kind="failed"' not in src
    assert "kind='failed'" not in src


# ---------------------------------------------------------------------------
# Lazy import discipline — mlx_audio must not load at module/__init__ time
# ---------------------------------------------------------------------------


def test_init_does_not_load_model(nemotron_mod, fake_nemotron):
    """Constructing the backend must not call ``load``."""
    nemotron_mod.NemotronBackend(model="fake-nemotron")
    assert fake_nemotron.load_calls == []


async def test_start_does_not_load_model(nemotron_mod, fake_nemotron):
    """``start()`` does an eager import to fail fast but defers model load
    to the first decode (the model must not load in start())."""
    backend = nemotron_mod.NemotronBackend(model="fake-nemotron")
    await backend.start()
    assert fake_nemotron.load_calls == [], "model must load lazily on first decode, not in start()"


async def test_model_loads_on_first_decode(nemotron_mod, fake_nemotron):
    backend = nemotron_mod.NemotronBackend(model="fake-nemotron")
    await backend.start()
    await _drive(backend)
    assert fake_nemotron.load_calls == ["fake-nemotron"]


# ---------------------------------------------------------------------------
# TranscriptEvent sequence — non-empty and empty-text decode
# ---------------------------------------------------------------------------


async def test_nonempty_decode_yields_delta_then_completed(nemotron_mod, fake_nemotron):
    fake_nemotron.model.return_text = "the quarterly numbers look solid"
    backend = nemotron_mod.NemotronBackend(model="fake-nemotron")
    await backend.start()
    _stream, events = await _drive(backend)

    assert [ev.kind for ev in events] == ["delta", "completed"]
    assert events[0].text == "the quarterly numbers look solid"
    assert events[1].text == "the quarterly numbers look solid"


async def test_empty_text_decode_yields_completed_only(nemotron_mod, fake_nemotron):
    """Near-silence: Nemotron returns empty text -> completed only, no delta."""
    fake_nemotron.model.return_text = ""
    backend = nemotron_mod.NemotronBackend(model="fake-nemotron")
    await backend.start()
    _stream, events = await _drive(backend)

    assert [ev.kind for ev in events] == ["completed"]
    assert events[0].text == ""


async def test_whitespace_only_decode_yields_completed_only(nemotron_mod, fake_nemotron):
    """Whitespace-only decode is treated as empty (no meaningful delta)."""
    fake_nemotron.model.return_text = "   "
    backend = nemotron_mod.NemotronBackend(model="fake-nemotron")
    await backend.start()
    _stream, events = await _drive(backend)
    assert "delta" not in [ev.kind for ev in events]
    assert events[-1].kind == "completed"


# ---------------------------------------------------------------------------
# Decode-failure path — raise, do not emit
# ---------------------------------------------------------------------------


async def test_decode_failure_propagates_out_of_end_or_events(nemotron_mod, fake_nemotron):
    """A stubbed decode raising mid-decode must propagate the exception."""
    fake_nemotron.model.raise_on_generate = RuntimeError("metal OOM mid-decode")
    backend = nemotron_mod.NemotronBackend(model="fake-nemotron")
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(_pcm(_SAMPLE_RATE_HZ))

    with pytest.raises(RuntimeError, match="metal OOM mid-decode"):
        await stream.end()
        # If end() did not raise, the decode is deferred to events().
        _ = [ev async for ev in stream.events()]


async def test_decode_failure_does_not_emit_silent_completed(nemotron_mod, fake_nemotron):
    """No swallowed empty ``completed`` may slip through on decode failure."""
    fake_nemotron.model.raise_on_generate = RuntimeError("decode boom")
    backend = nemotron_mod.NemotronBackend(model="fake-nemotron")
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(_pcm(_SAMPLE_RATE_HZ))

    events: list[Any] = []
    with pytest.raises(RuntimeError):
        try:
            await stream.end()
        finally:
            async for ev in stream.events():
                events.append(ev)
    assert events == [], "decode failure must raise, not emit a fake completed"


async def test_first_decode_model_load_failure_propagates(nemotron_mod, fake_nemotron):
    """First-decode model-load failure raises from its distinct call site."""
    fake_nemotron.raise_on_load = RuntimeError("model download failed")
    backend = nemotron_mod.NemotronBackend(model="fake-nemotron")
    await backend.start()  # eager import succeeds; model load is deferred
    stream = await backend.open_stream(language="en")
    await stream.feed(_pcm(_SAMPLE_RATE_HZ))

    with pytest.raises(RuntimeError, match="model download failed"):
        await stream.end()
        _ = [ev async for ev in stream.events()]


# ---------------------------------------------------------------------------
# cancel() semantics
# ---------------------------------------------------------------------------


async def test_cancel_before_end_yields_no_events(nemotron_mod, fake_nemotron):
    backend = nemotron_mod.NemotronBackend(model="fake-nemotron")
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(_pcm(_SAMPLE_RATE_HZ))
    await stream.cancel()

    events = [ev async for ev in stream.events()]
    assert events == []
    # A cancelled stream must not have run a decode.
    assert fake_nemotron.model.generate_calls == 0


async def test_cancel_mid_decode_is_bounded_and_crash_free(nemotron_mod, fake_nemotron):
    """cancel() while a decode is in flight must not crash and must return
    promptly — the awaiting end() coroutine may unwind, but cancel() itself
    is bounded."""
    gate = threading_Event()
    fake_nemotron.model._gate = gate
    backend = nemotron_mod.NemotronBackend(model="fake-nemotron")
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


async def test_long_utterance_not_silently_truncated(nemotron_mod, fake_nemotron):
    """A 60 s utterance must reach the decoder without dropped bytes."""
    seconds = 60.0
    audio = _pcm_seconds(seconds)
    backend = nemotron_mod.NemotronBackend(model="fake-nemotron")
    await backend.start()
    _stream, events = await _drive(backend, audio=audio)

    # The decode must have happened and produced the normal event shape.
    assert events[-1].kind == "completed"
    assert fake_nemotron.model.generate_calls >= 1

    # No silent truncation: the WAV the model decoded spans the full 60 s.
    frames = fake_nemotron.model.last_frame_count
    assert frames is not None, "model did not receive a readable WAV path"
    duration_s = frames / _SAMPLE_RATE_HZ
    assert duration_s == pytest.approx(seconds, rel=0.02), (
        f"60s utterance silently truncated to {duration_s:.1f}s"
    )


# ---------------------------------------------------------------------------
# Decode serialisation — two overlapping decodes must not run concurrently
# ---------------------------------------------------------------------------


async def test_overlapping_decodes_serialise(nemotron_mod, fake_nemotron):
    """Two streams from one backend must not decode concurrently.

    mlx-audio / Metal is not verified concurrency-safe, so the backend keeps a
    backend-scoped decode lock. This test gates the first decode and proves the
    second cannot enter ``generate()`` until the first releases.
    """
    gate = threading_Event()
    model = fake_nemotron.model
    model._gate = gate

    backend = nemotron_mod.NemotronBackend(model="fake-nemotron")
    await backend.start()

    s1 = await backend.open_stream(language="en")
    s2 = await backend.open_stream(language="en")
    await s1.feed(_pcm(_SAMPLE_RATE_HZ))
    await s2.feed(_pcm(_SAMPLE_RATE_HZ))

    t1 = asyncio.create_task(s1.end())
    await asyncio.sleep(0.05)  # let decode 1 reach the gate
    t2 = asyncio.create_task(s2.end())
    await asyncio.sleep(0.05)  # give decode 2 a chance to (wrongly) start

    # With serialisation, only decode 1 has entered generate().
    assert model.generate_calls == 1, "second decode must wait for the lock"

    gate.set()  # release decode 1; decode 2 may now proceed
    await asyncio.wait_for(asyncio.gather(t1, t2), timeout=5.0)
    assert model.generate_calls == 2


# ---------------------------------------------------------------------------
# Language handling — the one material difference from Parakeet
# ---------------------------------------------------------------------------


async def test_client_language_is_forwarded_to_generate(nemotron_mod, fake_nemotron):
    """(a) forwarding (deterministic): a client-supplied language reaches the
    stub generate()'s ``language`` kwarg. Asserts forwarded-not-ignored
    regardless of the default value."""
    backend = nemotron_mod.NemotronBackend(model="fake-nemotron")
    await backend.start()
    await _drive(backend, language="es-ES")
    assert fake_nemotron.model.last_language == "es-ES"


async def test_none_language_falls_back_to_default(nemotron_mod, fake_nemotron):
    """(b) None-default (gated on the constant): with the client sending None,
    generate() receives exactly ``DEFAULT_NEMOTRON_LANGUAGE``."""
    backend = nemotron_mod.NemotronBackend(model="fake-nemotron")
    await backend.start()
    await _drive(backend, language=None)
    assert fake_nemotron.model.last_language == nemotron_mod.DEFAULT_NEMOTRON_LANGUAGE
    assert nemotron_mod.DEFAULT_NEMOTRON_LANGUAGE == "auto"


# ---------------------------------------------------------------------------
# PII / temp-dir + shutdown invariants
# ---------------------------------------------------------------------------


def test_temp_dir_is_owner_only(nemotron_mod):
    """The decode-WAV temp dir must be 0o700 (owner-only) so PII audio is not
    world-readable."""
    backend = nemotron_mod.NemotronBackend(model="fake-nemotron")
    mode = stat.S_IMODE(os.stat(backend._tmpdir).st_mode)
    assert mode == 0o700, f"temp dir mode is {oct(mode)}, expected 0o700"
    # cleanup
    import shutil

    shutil.rmtree(backend._tmpdir, ignore_errors=True)


async def test_temp_wav_is_unlinked_after_decode(nemotron_mod, fake_nemotron):
    """The per-utterance temp WAV is written under the private dir and unlinked
    after the decode returns — no PII left on disk."""
    backend = nemotron_mod.NemotronBackend(model="fake-nemotron")
    await backend.start()
    await _drive(backend)

    # The path the model saw was inside the backend's private temp dir...
    seen = fake_nemotron.model.last_path
    assert seen is not None
    assert str(seen).startswith(backend._tmpdir)
    # ...and it no longer exists.
    assert not os.path.exists(seen), "temp WAV must be unlinked after decode"
    await backend.close()


async def test_close_removes_temp_dir(nemotron_mod, fake_nemotron):
    """``close()`` removes the private temp dir."""
    backend = nemotron_mod.NemotronBackend(model="fake-nemotron")
    await backend.start()
    tmpdir = backend._tmpdir
    assert os.path.isdir(tmpdir)
    await backend.close()
    assert not os.path.exists(tmpdir)


async def test_close_waits_while_decode_in_flight(nemotron_mod, fake_nemotron):
    """``close()`` bounds its wait while a decode is in flight (drain)."""
    gate = threading_Event()
    fake_nemotron.model._gate = gate
    backend = nemotron_mod.NemotronBackend(model="fake-nemotron")
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(_pcm(_SAMPLE_RATE_HZ))

    end_task = asyncio.create_task(stream.end())
    await asyncio.sleep(0.05)  # let decode reach the gate

    # close() must observe the in-flight decode and wait (bounded). Release the
    # gate from a timer so the drain completes within the 3.0s bound.
    loop = asyncio.get_running_loop()
    loop.call_later(0.1, gate.set)
    await asyncio.wait_for(backend.close(), timeout=3.5)

    await asyncio.wait_for(end_task, timeout=3.0)
