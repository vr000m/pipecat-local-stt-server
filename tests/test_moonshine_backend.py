"""Tests for the commit-oriented Moonshine STT backend.

``moonshine_voice`` is replaced by ``tests/_fake_moonshine.py`` (injected through
``sys.modules``), so no wheel, model or network is needed. Pinned behaviour:

* ``MoonshineBackend`` / its stream satisfy the structural backend protocols;
* the wire shape is the same as every other backend: one ``delta`` equal to
  ``completed`` (empty decode: ``completed`` only), failures raise;
* the constructor stores the spec verbatim and imports nothing; ``start()``
  imports, validates against the catalog and the licence guard (``ConfigError``);
* pure helpers ``parse_model_spec`` / ``resolve_arch`` / ``resolve_language`` are
  table-tested against the fake catalog;
* the served set is fixed from the spec: the downloader is never called for an
  unlisted language and the transcriber cache equals the served set;
* decodes are serialised across sessions and drain on ``idle``/``close``/``abort``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import pytest
from _fake_moonshine import (
    SAMPLE_RATE_HZ,
    ModelArch,
    arch_name,
    build_catalog,
    install,
    pcm,
    pcm_seconds,
)

from stt_server.errors import ConfigError


@pytest.fixture
def fake(monkeypatch, tmp_path):
    st = install(monkeypatch, tmp_path / "cache")
    yield st
    # Regression guard: the offline API adds a spurious "Mm" line from its 2nd call on
    # (moonshine-voice 0.1.6), so the backend must only ever decode through a stream.
    assert st.offline_calls == [], "backend called transcribe_without_streaming"


@pytest.fixture
def mod(fake):
    import stt_server.backends.moonshine as m

    return m


def _backend(mod, spec: str = "medium-streaming@en"):
    return mod.MoonshineBackend(model=spec)


async def _drive(backend, *, language="en", audio=None):
    stream = await backend.open_stream(language=language)
    await stream.feed(pcm(SAMPLE_RATE_HZ) if audio is None else audio)
    await stream.end()
    events = [ev async for ev in stream.events()]
    return stream, events


# ---------------------------------------------------------------------------
# Protocol / identity / registry
# ---------------------------------------------------------------------------


def test_backend_satisfies_transcription_backend_protocol(mod):
    from stt_server.backend import AbortableBackend, IdleAwareBackend, TranscriptionBackend

    backend = _backend(mod)
    assert isinstance(backend, TranscriptionBackend)
    assert isinstance(backend, AbortableBackend)
    assert isinstance(backend, IdleAwareBackend)


async def test_stream_satisfies_backend_stream_protocol(mod):
    from stt_server.backend import BackendStream

    backend = _backend(mod)
    await backend.start()
    stream = await backend.open_stream(language="en")
    assert isinstance(stream, BackendStream)
    await stream.cancel()


def test_shares_the_decode_guard_mixin(mod):
    from stt_server.backends._thread_util import DecodeGuardMixin

    assert issubclass(mod.MoonshineBackend, DecodeGuardMixin)
    for name in ("_mark_inflight_start", "_mark_inflight_end", "_wait_inflight_drained"):
        assert name not in vars(mod.MoonshineBackend)


def test_identity_and_verbatim_spec(mod):
    backend = _backend(mod, "small-streaming@en,es")
    assert backend.backend_name == "moonshine"
    assert backend.model == "small-streaming@en,es"  # exactly what the operator passed


def test_registry_entry_and_default(mod):
    from stt_server.backends import BACKENDS, DEFAULT_MOONSHINE_MODEL, PINNED_REVISIONS

    assert DEFAULT_MOONSHINE_MODEL == "medium-streaming@en"
    assert mod.DEFAULT_MOONSHINE_MODEL == DEFAULT_MOONSHINE_MODEL
    entry = BACKENDS["moonshine"]
    assert entry.module == "stt_server.backends.moonshine"
    assert entry.class_name == "MoonshineBackend"
    assert entry.default_model == DEFAULT_MOONSHINE_MODEL
    assert entry.local_model_paths is False
    # the package pin is the pin: no HF SHA entry for any moonshine spec
    assert DEFAULT_MOONSHINE_MODEL not in PINNED_REVISIONS
    assert not any("moonshine" in k for k in PINNED_REVISIONS)


def test_default_backend_uses_default_spec(mod):
    from stt_server.backends import DEFAULT_MOONSHINE_MODEL

    assert mod.MoonshineBackend().model == DEFAULT_MOONSHINE_MODEL


def test_extra_pins_the_exact_package_version():
    import tomllib

    root = Path(__file__).resolve().parent.parent
    extras = tomllib.loads((root / "pyproject.toml").read_text())["project"][
        "optional-dependencies"
    ]
    assert "moonshine" in extras
    assert "moonshine-voice==0.1.6" in [r.replace(" ", "") for r in extras["moonshine"]]


# ---------------------------------------------------------------------------
# Constructor imports nothing / start() imports and validates
# ---------------------------------------------------------------------------


def test_constructor_imports_nothing_and_does_not_validate():
    """Run clean: the package import is blocked, a garbage spec must still construct."""
    code = (
        "import sys, builtins\n"
        "for m in [m for m in sys.modules if m.split('.')[0] == 'moonshine_voice']:\n"
        "    sys.modules.pop(m)\n"
        "_real = builtins.__import__\n"
        "def _blocked(name, *a, **k):\n"
        "    if name == 'moonshine_voice' or name.startswith('moonshine_voice.'):\n"
        "        raise AssertionError('moonshine_voice imported at the constructor seam')\n"
        "    return _real(name, *a, **k)\n"
        "builtins.__import__ = _blocked\n"
        "from stt_server.__main__ import _make_backend, _resolve_model\n"
        "from stt_server.backends import DEFAULT_MOONSHINE_MODEL\n"
        "assert _resolve_model('moonshine', None) == DEFAULT_MOONSHINE_MODEL\n"
        "for spec in (DEFAULT_MOONSHINE_MODEL, 'x/y', '!!@@,,', 'no-such-arch@zz'):\n"
        "    b = _make_backend('moonshine', spec)\n"
        "    assert type(b).__name__ == 'MoonshineBackend' and b.model == spec\n"
        "    b.abort()\n"
        "assert not [m for m in sys.modules if m.split('.')[0] == 'moonshine_voice']\n"
        "print('OK')\n"
    )
    env = dict(os.environ)
    repo_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join([repo_root, env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "OK" in r.stdout


def test_constructor_does_not_touch_the_library(mod, fake):
    _backend(mod, "bogus-arch@zz")
    assert fake.downloads == [] and fake.loads == []


async def test_start_does_not_download_or_load(mod, fake):
    backend = _backend(mod)
    await backend.start()
    assert fake.downloads == [] and fake.loads == []


async def test_start_logs_the_resolved_map_once(mod, caplog):
    backend = _backend(mod, "@en,es")
    with caplog.at_level(logging.INFO):
        await backend.start()
    info = [r for r in caplog.records if r.levelno == logging.INFO and "moonshine" in r.name]
    assert len(info) == 1
    msg = info[0].getMessage()
    assert "en" in msg and "es" in msg
    assert "medium-streaming" in msg and "small-streaming" in msg


@pytest.mark.parametrize(
    "spec",
    [
        "no-such-arch",  # unknown arch
        "no-such-arch@en",
        "@xx",  # unknown language
        "@en,xx",
        "@ko",  # catalog has it, licence guard refuses (non-streaming, non-en)
        "@uk",
        "base@es",  # non-streaming non-English arch
        "base@ja",
        "tiny@zh",
        "tiny-streaming@en,ko",
        "medium-streaming@es",  # arch not in that language's catalog
    ],
)
async def test_start_rejects_invalid_or_non_mit_specs(mod, spec):
    with pytest.raises(ConfigError):
        await _backend(mod, spec).start()


@pytest.mark.parametrize(
    "spec",
    [
        "medium-streaming@en",
        "small-streaming",
        "@en,es",
        "tiny-streaming@en,es,de",
        "base@en",  # non-streaming is allowed for en only
        "tiny@en",
        "@ja,zh",
    ],
)
async def test_start_accepts_valid_specs(mod, spec):
    await _backend(mod, spec).start()


async def test_start_missing_package_is_actionable(monkeypatch):
    import builtins

    real = builtins.__import__

    def blocked(name, *a, **k):
        if name == "moonshine_voice" or name.startswith("moonshine_voice."):
            raise ModuleNotFoundError(f"No module named '{name}'", name=name)
        return real(name, *a, **k)

    monkeypatch.delitem(sys.modules, "stt_server.backends.moonshine", raising=False)
    for m in [m for m in sys.modules if m.split(".")[0] == "moonshine_voice"]:
        monkeypatch.delitem(sys.modules, m)
    monkeypatch.setattr(builtins, "__import__", blocked)
    from stt_server.backends.moonshine import MoonshineBackend

    with pytest.raises(ModuleNotFoundError) as exc:
        await MoonshineBackend(model="whatever").start()
    assert "uv sync --extra moonshine --inexact" in str(exc.value)


# ---------------------------------------------------------------------------
# Pure helpers: parse_model_spec / resolve_arch / resolve_language
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "arch", "langs"),
    [
        ("medium-streaming@en", "medium-streaming", ["en"]),
        ("small-streaming@en", "small-streaming", ["en"]),
        ("@en,es", None, ["en", "es"]),
        ("tiny-streaming@en,es,de", "tiny-streaming", ["en", "es", "de"]),
        ("base@en", "base", ["en"]),
    ],
)
def test_parse_model_spec_valid(mod, spec, arch, langs):
    got_arch, got_langs = mod.parse_model_spec(spec)
    assert got_arch == arch
    assert list(got_langs) == langs


@pytest.mark.parametrize("spec", ["small-streaming", "tiny-streaming", ""])
def test_parse_model_spec_without_languages_means_en(mod, spec):
    """An empty language list means ``en`` (represented either way by the parser)."""
    arch, langs = mod.parse_model_spec(spec)
    assert arch == (spec or None)
    assert list(langs) in ([], ["en"])


@pytest.mark.parametrize("spec", ["a@b@c", "x@en@es"])
def test_parse_model_spec_malformed_is_rejected(mod, spec):
    with pytest.raises(ValueError):  # ConfigError is a ValueError
        mod.parse_model_spec(spec)


def test_parse_model_spec_is_pure(mod, fake):
    mod.parse_model_spec("tiny-streaming@en,es")
    assert fake.downloads == [] and fake.loads == []


@pytest.fixture
def catalog():
    return build_catalog()


_VALID_ARCH_CELLS = [
    # omitted arch: first MIT-allowed catalog entry
    ("en", None, "medium-streaming"),
    ("es", None, "small-streaming"),
    ("de", None, "small-streaming"),
    ("ja", None, "small-streaming"),
    ("ar", None, "tiny-streaming"),
    ("zh", None, "tiny-streaming"),
    ("vi", None, "tiny-streaming"),
    ("tl", None, "tiny-streaming"),
    # explicit arch
    ("en", "small-streaming", "small-streaming"),
    ("en", "tiny-streaming", "tiny-streaming"),
    ("en", "base", "base"),  # non-streaming, en: MIT
    ("en", "tiny", "tiny"),
    ("es", "tiny-streaming", "tiny-streaming"),
    ("ja", "small-streaming", "small-streaming"),
    ("zh", "tiny-streaming", "tiny-streaming"),
]


@pytest.mark.parametrize(("code", "requested", "expected"), _VALID_ARCH_CELLS)
def test_resolve_arch_valid(mod, catalog, code, requested, expected):
    assert arch_name(mod.resolve_arch(code, requested, catalog)) == expected


@pytest.mark.parametrize(
    ("code", "requested"),
    [
        ("ko", None),  # only a non-streaming, non-en model: not MIT
        ("uk", None),
        ("ko", "tiny"),
        ("uk", "base"),
        ("es", "base"),  # Spanish Base is non-MIT
        ("ja", "base"),
        ("ja", "tiny"),
        ("ar", "base"),
        ("zh", "base"),
        ("vi", "base"),
        ("en", "no-such-arch"),  # unknown arch
        ("es", "medium-streaming"),  # known arch, not in that language's catalog
        ("de", "base"),
        ("xx", None),  # unknown language
        ("xx", "tiny-streaming"),
    ],
)
def test_resolve_arch_rejects(mod, catalog, code, requested):
    with pytest.raises(ConfigError):
        mod.resolve_arch(code, requested, catalog)


def test_resolve_arch_licence_guard_over_the_whole_catalog(mod, catalog):
    """For every (language, arch) in the catalog: streaming archs resolve for any
    language, non-streaming only for ``en``."""
    for code, info in catalog.items():
        for model in info["models"]:
            name = arch_name(model["model_arch"])
            if "streaming" in name or code == "en":
                assert arch_name(mod.resolve_arch(code, name, catalog)) == name
            else:
                with pytest.raises(ConfigError):
                    mod.resolve_arch(code, name, catalog)


# --- prefetch validates the whole spec exactly as start() does --------------------


def _record_build_served(mod, monkeypatch, *, fail_whole: bool = False):
    """Wrap ``build_served``: record each call's languages and each exception raised.
    ``fail_whole`` makes a multi-language call fail while every single language passes
    (a cross-language rule no per-language call reproduces)."""
    real = mod.build_served
    calls: list[tuple[str, ...]] = []
    raised: list[BaseException] = []

    def wrapper(langs, arch, catalog):
        calls.append(tuple(langs))
        try:
            if fail_whole and len(langs) > 1:
                raise ConfigError("moonshine: cross-language rule")
            return real(langs, arch, catalog)
        except Exception as exc:
            raised.append(exc)
            raise

    monkeypatch.setattr(mod, "build_served", wrapper)
    return calls, raised


@pytest.mark.parametrize(
    ("languages", "requested"),
    [((code,), requested) for code, requested, _ in _VALID_ARCH_CELLS]
    + [
        (("en", "es", "zh"), "tiny-streaming"),
        (("en", "es", "ar", "tl"), None),
        (("ja", "en", "de", "es"), "small-streaming"),
    ],
)
def test_validated_arch_is_whole_spec_build_served(mod, monkeypatch, languages, requested):
    """Every language's arch is ``build_served(<whole tuple>)[language]``, computed by one
    ``build_served`` call on the whole tuple, as ``start()`` calls it."""
    download = mod._import_package()[1]
    expected = mod.build_served(languages, requested, download._stt_catalog())
    calls, _ = _record_build_served(mod, monkeypatch)
    for language in languages:
        calls.clear()
        assert mod._validated_arch(download, language, requested, languages) == (expected[language])
        assert calls == [languages]


def test_validated_arch_tags_the_first_failing_language_on_the_original_error(mod, monkeypatch):
    from stt_server.snapshot import PREFETCH_REPO_ATTR

    download = mod._import_package()[1]
    _, raised = _record_build_served(mod, monkeypatch)
    with pytest.raises(ConfigError, match="'xx'") as info:
        mod._validated_arch(download, "en", None, ("en", "xx", "ko"))
    assert info.value is raised[0], "the whole-spec exception is re-raised unchanged"
    assert getattr(info.value, PREFETCH_REPO_ATTR) == "moonshine-voice/default-xx"


def test_validated_arch_leaves_a_cross_language_refusal_untagged(mod, monkeypatch):
    from stt_server.snapshot import PREFETCH_REPO_ATTR

    download = mod._import_package()[1]
    _, raised = _record_build_served(mod, monkeypatch, fail_whole=True)
    with pytest.raises(ConfigError, match="cross-language") as info:
        mod._validated_arch(download, "en", None, ("en", "es"))
    assert info.value is raised[0]
    assert getattr(info.value, PREFETCH_REPO_ATTR, None) is None


def test_live_wheel_manifest_directory_matches_the_cached_model():
    """The manifest-derived directory is where the real wheel cached the default model
    (pinned against 0.1.6; skipped without the wheel or the cached model, nothing is
    downloaded)."""
    pytest.importorskip("moonshine_voice")
    from moonshine_voice import download

    import stt_server.backends.moonshine as m

    arch = m.resolve_arch("en", "medium-streaming", download._stt_catalog())
    root, files = m._model_manifest(download, "en", arch)
    assert root.parts[-2:] == ("medium-streaming-en", "quantized_26_11_02")
    assert files, "the manifest lists the model files"
    if not all((root / f["name"]).is_file() for f in files):
        pytest.skip("default moonshine model is not cached; not downloading it for a test")
    # the cached files the library itself wrote sit exactly where the manifest says
    for f in files:
        assert (root / f["name"]).stat().st_size == f["size"]


def test_resolve_arch_real_catalog_matches_the_guard():
    """Against the live catalog (only when the real wheel is installed)."""
    pytest.importorskip("moonshine_voice")
    from moonshine_voice import download

    import stt_server.backends.moonshine as m

    live = download._stt_catalog()
    for code, info in live.items():
        for model in info["models"]:
            name = ModelArch(int(model["model_arch"])).name.lower().replace("_", "-")
            if "streaming" in name or code == "en":
                m.resolve_arch(code, name, live)
            else:
                with pytest.raises(ConfigError):
                    m.resolve_arch(code, name, live)
    for code in ("ko", "uk"):
        with pytest.raises(ConfigError):
            m.resolve_arch(code, None, live)


@pytest.mark.parametrize(
    "lang",
    [None, "", "   ", "auto", "AUTO", " auto ", "en", "EN", " en ", "en-US", "en_GB", "EN-us"],
)
def test_resolve_language_maps_to_en(mod, lang):
    code, note = mod.resolve_language(lang, ["en"])
    assert code == "en"
    assert not note


@pytest.mark.parametrize("lang", ["zh-CN", "zh_CN", "zh-Hans-CN", "ZH", " zh-cn "])
def test_resolve_language_strips_region(mod, lang):
    code, note = mod.resolve_language(lang, ["en", "zh"])
    assert code == "zh"
    assert not note


@pytest.mark.parametrize("lang", ["xx", "ko", "uk", "fr", "ja", "es", 123, 1.5, b"en", ["en"]])
def test_resolve_language_outside_served_set_falls_back_with_a_note(mod, lang):
    code, note = mod.resolve_language(lang, ["en"])
    assert code == "en"
    assert note, "an unlisted or invalid language must return an error note"


def test_resolve_language_fallback_is_first_listed_when_en_not_served(mod):
    code, note = mod.resolve_language("xx", ["es", "de"])
    assert code == "es" and note
    # an explicit ``en`` that is not served is an error note and falls back too
    code, note = mod.resolve_language("en", ["de", "es"])
    assert code == "de" and note


@pytest.mark.parametrize("lang", [None, "", "  ", "auto", "AUTO", " auto "])
def test_resolve_language_default_maps_to_the_fallback_without_a_note(mod, lang):
    """``None``/blank/``auto`` is the normal default, not an error: no note, whatever is served."""
    assert mod.resolve_language(lang, ["en"]) == ("en", None)
    assert mod.resolve_language(lang, ["es", "en"]) == ("en", None)
    assert mod.resolve_language(lang, ["de", "es"]) == ("de", None)
    assert mod.resolve_language(lang, ["ja"]) == ("ja", None)


def test_resolve_language_prefers_en_over_first_listed(mod):
    code, note = mod.resolve_language("xx", ["es", "en"])
    assert code == "en" and note


def test_resolve_language_served_language_has_no_note(mod):
    code, note = mod.resolve_language("es", ["en", "es"])
    assert code == "es" and not note


def test_resolve_language_is_pure(mod, fake):
    mod.resolve_language("xx", ["en"])
    assert fake.downloads == [] and fake.loads == []


# ---------------------------------------------------------------------------
# Events: one delta == completed
# ---------------------------------------------------------------------------


async def test_nonempty_decode_yields_one_delta_equal_to_completed(mod, fake):
    fake.lines = ["the quarterly numbers look solid"]
    backend = _backend(mod)
    await backend.start()
    _s, events = await _drive(backend)
    assert [e.kind for e in events] == ["delta", "completed"]
    assert events[0].text == events[1].text == "the quarterly numbers look solid"


async def test_multiple_lines_are_joined_into_one_delta(mod, fake):
    fake.lines = ["first line.", "second line.", "third line."]
    backend = _backend(mod)
    await backend.start()
    _s, events = await _drive(backend)
    assert [e.kind for e in events] == ["delta", "completed"]
    assert events[0].text == events[1].text == "first line. second line. third line."


@pytest.mark.parametrize("lines", [[], [""], ["   "], ["", "  ", "\t"]])
async def test_empty_or_whitespace_decode_yields_completed_only(mod, fake, lines):
    fake.lines = lines
    backend = _backend(mod)
    await backend.start()
    _s, events = await _drive(backend)
    assert [e.kind for e in events] == ["completed"]
    assert events[0].text == ""


async def test_no_audio_fed_yields_completed_only(mod, fake):
    fake.lines = []
    backend = _backend(mod)
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.end()
    events = [e async for e in stream.events()]
    assert [e.kind for e in events] == ["completed"]
    assert events[0].text == ""


def test_no_failed_event_kind_defined(mod):
    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert 'kind="failed"' not in src and "kind='failed'" not in src


# ---------------------------------------------------------------------------
# Audio path: PCM16LE -> float32 list at 16 kHz
# ---------------------------------------------------------------------------


async def test_audio_is_converted_to_a_float_list_at_16khz(mod, fake):
    samples = np.array([0, 1, -1, 16384, -16384, 32767, -32768], dtype="<i2")
    backend = _backend(mod)
    await backend.start()
    await _drive(backend, audio=samples.tobytes())
    (call,) = fake.transcribe_calls
    assert call["sample_rate"] == 16000
    assert call["add_audio_calls"] == 1, "the whole utterance goes in one add_audio call"
    audio = call["audio"]
    assert isinstance(audio, list), "the library builds a ctypes array from a list"
    assert all(isinstance(x, float) for x in audio)
    assert len(audio) == len(samples)
    assert audio == pytest.approx((samples / 32768).tolist(), abs=1e-9)
    assert min(audio) == -1.0 and max(audio) < 1.0


async def test_chunked_feed_is_concatenated(mod, fake):
    data = pcm_seconds(0.5)
    backend = _backend(mod)
    await backend.start()
    stream = await backend.open_stream(language="en")
    for i in range(0, len(data), 1000):
        await stream.feed(data[i : i + 1000])
    await stream.end()
    _ = [e async for e in stream.events()]
    (call,) = fake.transcribe_calls
    assert len(call["audio"]) == len(data) // 2


async def test_long_utterance_not_truncated(mod, fake):
    backend = _backend(mod)
    await backend.start()
    await _drive(backend, audio=pcm_seconds(60.0))
    (call,) = fake.transcribe_calls
    assert len(call["audio"]) == 60 * SAMPLE_RATE_HZ


async def test_no_audio_or_transcript_is_written_to_disk(mod, fake, tmp_path, monkeypatch):
    """No temp WAV: the decode must not create files (no private temp dir either)."""
    import tempfile

    monkeypatch.setattr(
        tempfile,
        "mkstemp",
        lambda *a, **k: pytest.fail("moonshine must not write a temp file"),
    )
    monkeypatch.setattr(
        tempfile,
        "mkdtemp",
        lambda *a, **k: pytest.fail("moonshine must not create a temp dir"),
    )
    backend = _backend(mod)
    assert not hasattr(backend, "_tmpdir")
    await backend.start()
    await _drive(backend)
    await backend.close()


# ---------------------------------------------------------------------------
# One-shot stream decode (not the offline API)
# ---------------------------------------------------------------------------


async def test_repeated_decodes_on_one_transcriber_never_get_a_spurious_mm(mod, fake):
    """The 0.1.6 defect: offline calls #2+ on one Transcriber prepend an "Mm" line."""
    fake.lines = ["the actual words"]
    backend = _backend(mod)
    await backend.start()
    texts = []
    for _ in range(3):
        _s, events = await _drive(backend)
        assert [e.kind for e in events] == ["delta", "completed"]
        texts.append(events[-1].text)
    assert texts == ["the actual words"] * 3
    assert all("Mm" not in t for t in texts)
    assert len(fake.loads) == 1, "one cached transcriber served all three decodes"
    assert fake.offline_calls == []


def test_fake_reproduces_the_offline_defect(fake):
    """Guards the guard: the fake's offline path really is dirty from the 2nd call."""
    from moonshine_voice import Transcriber

    t = Transcriber("p", ModelArch.TINY)
    first = [ln.text for ln in t.transcribe_without_streaming([0.0]).lines]
    second = [ln.text for ln in t.transcribe_without_streaming([0.0]).lines]
    assert first == ["hello from moonshine"] and second == ["Mm", "hello from moonshine"]
    fake.offline_calls.clear()  # deliberate calls; not the backend


async def test_stream_call_sequence_and_close_on_success(mod, fake):
    backend = _backend(mod)
    await backend.start()
    await _drive(backend)
    (sid,) = {i for i, _ in fake.stream_ops}
    assert [op for _, op in fake.stream_ops] == [
        "create_stream",
        "add_listener",
        "start",
        "add_audio",
        "stop",
        "close",
    ]
    (stream,) = fake.created_streams
    assert stream.closed and stream.stopped and sid == stream.id
    assert fake.create_stream_kwargs == [{"update_interval": 0.3}]


async def test_error_event_fails_the_decode_and_never_completes(mod, fake):
    fake.error_on_stop = "flush failed"
    backend = _backend(mod)
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(pcm(SAMPLE_RATE_HZ))
    events = []
    with pytest.raises(RuntimeError) as exc:
        try:
            await stream.end()
        finally:
            events = [e async for e in stream.events()]
    assert events == [], "a failed flush must not produce completed"
    assert "flush failed" not in str(exc.value), "no library/transcript text in the message"
    # the library's own error is kept as the cause, so the server log shows why
    assert isinstance(exc.value.__cause__, RuntimeError)
    assert str(exc.value.__cause__) == "flush failed"
    assert backend._wait_inflight_drained(1.0) is True


async def test_error_event_exception_is_chained_as_the_cause(mod, fake):
    cause = ValueError("model said no")
    fake.error_on_stop = cause  # type: ignore[assignment]  # the real Error.error is an Exception
    backend = _backend(mod)
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(pcm(SAMPLE_RATE_HZ))
    with pytest.raises(RuntimeError) as exc:
        await stream.end()
    assert exc.value.__cause__ is cause
    assert "model said no" not in str(exc.value)


async def test_error_event_after_lines_still_fails_not_truncated_completed(mod, fake):
    fake.lines = ["partial text"]
    fake.error_on_stop = "late error"
    fake.error_after_lines = True
    backend = _backend(mod)
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(pcm(SAMPLE_RATE_HZ))
    with pytest.raises(RuntimeError):
        await stream.end()
    assert [e async for e in stream.events()] == []


async def test_stream_is_closed_on_error_and_on_raised_failure(mod, fake):
    fake.error_on_stop = "boom"
    backend = _backend(mod)
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(pcm(SAMPLE_RATE_HZ))
    with pytest.raises(RuntimeError):
        await stream.end()
    assert fake.created_streams[-1].closed

    fake.error_on_stop = None
    fake.raise_on_transcribe = ValueError("library raised")
    stream = await backend.open_stream(language="en")
    await stream.feed(pcm(SAMPLE_RATE_HZ))
    with pytest.raises(ValueError, match="library raised"):
        await stream.end()
    assert fake.created_streams[-1].closed


async def test_error_event_surfaces_as_transcript_failed_on_the_wire(mod, fake):
    """The failure path yields a failed-style outcome, never ``completed``."""
    fake.error_on_stop = "x"
    backend = _backend(mod)
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(pcm(SAMPLE_RATE_HZ))
    kinds: list[str] = []
    try:
        await stream.end()
        kinds = [e.kind async for e in stream.events()]
    except RuntimeError:
        kinds.append("raised")  # the session turns a raised decode into transcript.failed
    assert kinds == ["raised"] and "completed" not in kinds


async def test_empty_audio_makes_no_library_call(mod, fake):
    backend = _backend(mod)
    await backend.start()
    for audio in (b"", b"\x01"):  # nothing fed / a lone odd byte
        _s, events = await _drive(backend, audio=audio)
        assert [e.kind for e in events] == ["completed"]
        assert events[0].text == ""
    assert fake.stream_ops == [] and fake.created_streams == []
    assert fake.transcribe_calls == []
    assert fake.loads == [], "no model load either"


async def test_only_completed_lines_count_and_are_stripped_dropped_and_joined(mod, fake):
    fake.lines = ["  first ", "", "   ", "second\t"]
    fake.emit_in_progress = True  # LineStarted / LineTextChanged precede each completion
    backend = _backend(mod)
    await backend.start()
    _s, events = await _drive(backend)
    assert [e.kind for e in events] == ["delta", "completed"]
    assert events[0].text == events[1].text == "first second"


async def test_stream_calls_do_not_interleave_across_concurrent_decodes(mod, fake):
    gate = threading.Event()
    fake.gate = gate
    backend = _backend(mod)
    await backend.start()
    s1 = await backend.open_stream(language="en")
    s2 = await backend.open_stream(language="en")
    await s1.feed(pcm(SAMPLE_RATE_HZ))
    await s2.feed(pcm(SAMPLE_RATE_HZ))
    t1 = asyncio.create_task(s1.end())
    await asyncio.sleep(0.1)
    t2 = asyncio.create_task(s2.end())
    await asyncio.sleep(0.1)
    # Decode 1 is parked inside stop(); decode 2 must not have created its stream yet.
    assert {i for i, _ in fake.stream_ops} == {0}
    assert fake.stream_ops[-1] == (0, "stop")

    gate.set()
    await asyncio.wait_for(asyncio.gather(t1, t2), timeout=5.0)
    ids = [i for i, _ in fake.stream_ops]
    assert ids == sorted(ids), "stream 0's calls all precede stream 1's"
    assert fake.max_active == 1
    assert all(s.closed for s in fake.created_streams)


# ---------------------------------------------------------------------------
# Failure propagation
# ---------------------------------------------------------------------------


async def test_decode_failure_propagates_and_emits_nothing(mod, fake):
    fake.raise_on_transcribe = RuntimeError("onnx boom")
    backend = _backend(mod)
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(pcm(SAMPLE_RATE_HZ))
    events = []
    with pytest.raises(RuntimeError, match="onnx boom"):
        try:
            await stream.end()
        finally:
            events = [e async for e in stream.events()]
    assert events == [], "a failed decode must raise, not emit a fake completed"


async def test_model_load_failure_propagates(mod, fake):
    fake.raise_on_load = RuntimeError("cannot load model")
    backend = _backend(mod)
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(pcm(SAMPLE_RATE_HZ))
    with pytest.raises(RuntimeError, match="cannot load model"):
        await stream.end()
        _ = [e async for e in stream.events()]


async def test_download_failure_is_classified_as_a_download_error(mod, fake):
    from stt_server import supervise

    fake.raise_on_download = ConnectionResetError("model host hung up")
    backend = _backend(mod)
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(pcm(SAMPLE_RATE_HZ))
    with pytest.raises(ConnectionResetError) as exc:
        await stream.end()
    assert supervise.classify_startup_error(exc.value)[0] == "ModelDownloadError"


async def test_decode_failure_is_not_tagged_as_a_download_error(mod, fake):
    """Only the fetch-and-load is the download phase, never the decode itself."""
    from stt_server import supervise

    fake.raise_on_transcribe = ConnectionResetError("socket reset inside decode")
    backend = _backend(mod)
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(pcm(SAMPLE_RATE_HZ))
    with pytest.raises(ConnectionResetError) as exc:
        await stream.end()
    assert supervise.classify_startup_error(exc.value)[0] != "ModelDownloadError"


async def test_failure_does_not_leak_the_inflight_count(mod, fake):
    fake.raise_on_transcribe = RuntimeError("boom")
    backend = _backend(mod)
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(pcm(SAMPLE_RATE_HZ))
    with pytest.raises(RuntimeError):
        await stream.end()
    assert backend._wait_inflight_drained(1.0) is True
    # and the backend still decodes afterwards
    fake.raise_on_transcribe = None
    _s, events = await _drive(backend)
    assert events[-1].kind == "completed"


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


async def test_cancel_before_end_yields_no_events_and_no_decode(mod, fake):
    backend = _backend(mod)
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(pcm(SAMPLE_RATE_HZ))
    await stream.cancel()
    assert [e async for e in stream.events()] == []
    assert fake.transcribe_calls == []
    assert fake.loads == [] and fake.downloads == []


async def test_cancel_mid_decode_is_bounded_and_drops_the_result(mod, fake):
    gate = threading.Event()
    fake.gate = gate
    backend = _backend(mod)
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(pcm(SAMPLE_RATE_HZ))
    end_task = asyncio.create_task(stream.end())
    await asyncio.sleep(0.05)
    await asyncio.wait_for(stream.cancel(), timeout=2.0)
    gate.set()
    try:
        await asyncio.wait_for(end_task, timeout=3.0)
    except (asyncio.CancelledError, Exception):
        pass
    assert [e async for e in stream.events()] == []
    assert backend._wait_inflight_drained(3.0) is True


# ---------------------------------------------------------------------------
# Serialisation, idle, abort, close
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("second_language", ["en", "es"])
async def test_overlapping_decodes_serialise(mod, fake, second_language):
    gate = threading.Event()
    fake.gate = gate
    backend = _backend(mod, "small-streaming@en,es")
    await backend.start()
    s1 = await backend.open_stream(language="en")
    s2 = await backend.open_stream(language=second_language)
    await s1.feed(pcm(SAMPLE_RATE_HZ))
    await s2.feed(pcm(SAMPLE_RATE_HZ))

    t1 = asyncio.create_task(s1.end())
    await asyncio.sleep(0.1)
    t2 = asyncio.create_task(s2.end())
    await asyncio.sleep(0.1)
    assert len(fake.transcribe_calls) == 1, "the second decode must wait for the first"

    gate.set()
    await asyncio.wait_for(asyncio.gather(t1, t2), timeout=5.0)
    assert len(fake.transcribe_calls) == 2
    assert fake.max_active == 1, "no two transcribe calls may overlap, even across languages"


async def test_idle_completes_after_the_decode_thread_finishes(mod, fake):
    gate = threading.Event()
    fake.gate = gate
    backend = _backend(mod)
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(pcm(SAMPLE_RATE_HZ))
    end_task = asyncio.create_task(stream.end())
    await asyncio.sleep(0.05)

    idle = backend.idle()
    await asyncio.sleep(0.05)
    assert not idle.done(), "idle() must wait while a decode thread is in flight"
    gate.set()
    await asyncio.wait_for(idle, timeout=3.0)
    await asyncio.wait_for(end_task, timeout=3.0)


async def test_idle_is_immediate_when_nothing_is_in_flight(mod):
    backend = _backend(mod)
    await asyncio.wait_for(backend.idle(), timeout=1.0)


async def test_close_waits_while_decode_in_flight(mod, fake):
    gate = threading.Event()
    fake.gate = gate
    backend = _backend(mod)
    await backend.start()
    stream = await backend.open_stream(language="en")
    await stream.feed(pcm(SAMPLE_RATE_HZ))
    end_task = asyncio.create_task(stream.end())
    await asyncio.sleep(0.05)

    asyncio.get_running_loop().call_later(0.1, gate.set)
    await asyncio.wait_for(backend.close(), timeout=3.5)
    await asyncio.wait_for(end_task, timeout=3.0)


def test_abort_is_synchronous_idempotent_and_safe_mid_decode(mod, fake):
    gate = threading.Event()
    fake.gate = gate

    async def body():
        backend = _backend(mod)
        await backend.start()
        stream = await backend.open_stream(language="en")
        await stream.feed(pcm(SAMPLE_RATE_HZ))
        task = asyncio.ensure_future(stream.end())
        await asyncio.sleep(0.05)
        backend.abort()  # decode thread is mid-call
        backend.abort()  # idempotent
        gate.set()
        await asyncio.wait_for(asyncio.wait({task}), timeout=5)
        assert task.done()
        await backend.close()

    asyncio.run(body())


def test_abort_before_start_is_safe(mod):
    backend = _backend(mod)
    backend.abort()
    backend.abort()


# ---------------------------------------------------------------------------
# Lazy load, served set, language fallback
# ---------------------------------------------------------------------------


async def test_model_loads_lazily_on_first_decode_and_is_cached(mod, fake):
    backend = _backend(mod)
    await backend.start()
    assert fake.loads == []
    await _drive(backend)
    assert fake.downloads == [("en", "medium-streaming")]
    assert len(fake.loads) == 1
    await _drive(backend)
    assert fake.downloads == [("en", "medium-streaming")], "a cached transcriber is reused"
    assert len(fake.loads) == 1


async def test_loading_runs_inside_the_decode_not_on_the_event_loop_thread(mod, fake):
    backend = _backend(mod)
    await backend.start()
    await _drive(backend)
    assert fake.transcribe_calls[0]["thread"] != threading.main_thread().name


async def test_each_served_language_gets_its_own_transcriber(mod, fake):
    backend = _backend(mod, "small-streaming@en,es,de")
    await backend.start()
    for lang in ("en", "es", "de", "es", "en"):
        await _drive(backend, language=lang)
    assert sorted(fake.downloads) == [
        ("de", "small-streaming"),
        ("en", "small-streaming"),
        ("es", "small-streaming"),
    ]
    assert len(fake.loads) == 3, "the cache is exactly the served set: no eviction, no extras"


async def test_omitted_arch_uses_each_languages_catalog_default(mod, fake):
    backend = _backend(mod, "@en,es,zh")
    await backend.start()
    for lang in ("en", "es", "zh"):
        await _drive(backend, language=lang)
    assert sorted(fake.downloads) == [
        ("en", "medium-streaming"),
        ("es", "small-streaming"),
        ("zh", "tiny-streaming"),
    ]


@pytest.mark.parametrize("language", ["xx", "ko", "uk", "fr", "ja", 123])
async def test_unlisted_language_logs_error_falls_back_and_never_downloads(
    mod, fake, caplog, language
):
    backend = _backend(mod, "small-streaming@en,es")
    await backend.start()
    with caplog.at_level(logging.ERROR):
        _s, events = await _drive(backend, language=language)
    assert any(r.levelno == logging.ERROR for r in caplog.records), "logger.error at the call site"
    assert events[-1].kind == "completed", "the decode proceeds; nothing is raised"
    assert fake.downloads == [("en", "small-streaming")]
    assert [lang for lang, _a in fake.downloads] == ["en"]


async def test_unlisted_language_falls_back_to_first_listed_when_en_not_served(mod, fake, caplog):
    backend = _backend(mod, "small-streaming@es,de")
    await backend.start()
    with caplog.at_level(logging.ERROR):
        await _drive(backend, language="xx")
    assert fake.downloads == [("es", "small-streaming")]
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.parametrize("language", [None, "", "auto", "AUTO", "EN", "en-US"])
async def test_en_aliases_decode_as_en_without_an_error(mod, fake, caplog, language):
    backend = _backend(mod, "small-streaming@en,es")
    await backend.start()
    with caplog.at_level(logging.ERROR):
        await _drive(backend, language=language)
    assert fake.downloads == [("en", "small-streaming")]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.parametrize("language", [None, "", "auto", "AUTO"])
async def test_default_language_falls_back_silently_when_en_is_not_served(
    mod, fake, caplog, language
):
    backend = _backend(mod, "small-streaming@es,de")
    await backend.start()
    with caplog.at_level(logging.ERROR):
        _s, events = await _drive(backend, language=language)
    assert events[-1].kind == "completed"
    assert fake.downloads == [("es", "small-streaming")]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.parametrize("language", ["zh-CN", "zh_CN", "zh-Hans-CN", "ZH"])
async def test_region_suffixed_language_is_stripped_to_the_served_code(mod, fake, language):
    backend = _backend(mod, "@en,zh")
    await backend.start()
    await _drive(backend, language=language)
    assert fake.downloads == [("zh", "tiny-streaming")]


async def test_served_language_does_not_download_siblings(mod, fake):
    backend = _backend(mod, "@en,es,de")
    await backend.start()
    await _drive(backend, language="es")
    assert [lang for lang, _a in fake.downloads] == ["es"]


# ---------------------------------------------------------------------------
# Wire level: the same event shape as every other backend
# ---------------------------------------------------------------------------


async def _serve(backend):
    from stt_server import TranscriptionClient
    from stt_server.server import ServerConfig, TranscriptionServer

    srv = TranscriptionServer(
        backend, ServerConfig(host="127.0.0.1", port=0, reject_browser_origins=False)
    )
    await srv.start()
    client = TranscriptionClient(host="127.0.0.1", port=srv.listening_port())
    await client.connect()
    return srv, client


async def _next_of_types(client, types: set[str], *, timeout=5.0) -> dict:
    async def _read():
        async for ev in client.events():
            if ev.get("type") in types:
                return ev
        raise RuntimeError(f"socket closed before receiving {types}")

    return await asyncio.wait_for(_read(), timeout=timeout)


async def _wire_commit(backend) -> list[dict]:
    """One real session: append + commit; every event up to ``completed``/``failed``."""
    from stt_server import protocol as P

    srv, c = await _serve(backend)
    try:
        await c.update_session(turn_detection=None)
        await _next_of_types(c, {P.EVT_SESSION_UPDATED})
        await c.send_audio(pcm(SAMPLE_RATE_HZ))
        await c.commit()
        seen: list[dict] = []
        async for ev in c.events():
            seen.append(ev)
            if ev["type"] in (P.EVT_TRANSCRIPT_COMPLETED, P.EVT_TRANSCRIPT_FAILED):
                break
        return seen
    finally:
        await c.close()
        await srv.shutdown()


async def test_wire_level_one_delta_equal_to_completed(mod, fake):
    from stt_server import protocol as P

    fake.lines = ["wire level text"]
    backend = _backend(mod)
    await backend.start()
    seen = await _wire_commit(backend)
    kinds = [e["type"] for e in seen]
    assert kinds.count(P.EVT_TRANSCRIPT_DELTA) == 1
    assert kinds[-1] == P.EVT_TRANSCRIPT_COMPLETED
    assert kinds.index(P.EVT_TRANSCRIPT_DELTA) < kinds.index(P.EVT_TRANSCRIPT_COMPLETED)
    delta = next(e for e in seen if e["type"] == P.EVT_TRANSCRIPT_DELTA)
    completed = seen[-1]
    assert delta["delta"] == completed["transcript"] == "wire level text"
    assert set(delta) == {"type", "event_id", "item_id", "content_index", "delta"}
    assert set(completed) == {"type", "event_id", "item_id", "content_index", "transcript"}


async def test_wire_level_empty_decode_is_completed_only(mod, fake):
    from stt_server import protocol as P

    fake.lines = []
    backend = _backend(mod)
    await backend.start()
    seen = await _wire_commit(backend)
    kinds = [e["type"] for e in seen]
    assert P.EVT_TRANSCRIPT_DELTA not in kinds
    assert seen[-1]["type"] == P.EVT_TRANSCRIPT_COMPLETED and seen[-1]["transcript"] == ""


async def test_wire_level_decode_failure_is_transcript_failed(mod, fake):
    from stt_server import protocol as P

    fake.raise_on_transcribe = RuntimeError("onnx boom detail")
    backend = _backend(mod)
    await backend.start()
    seen = await _wire_commit(backend)
    assert seen[-1]["type"] == P.EVT_TRANSCRIPT_FAILED
    assert seen[-1]["error"]["code"] == P.ErrorCode.BACKEND_ERROR.value
    assert "onnx boom detail" not in str(seen[-1]), "details stay in the server log"
    assert P.EVT_TRANSCRIPT_COMPLETED not in [e["type"] for e in seen]


# ---------------------------------------------------------------------------
# prefetch_specs (module-level contract used by the shared prefetch path)
# ---------------------------------------------------------------------------


def test_prefetch_specs_one_per_served_language_and_arch(mod, fake):
    specs = mod.prefetch_specs("small-streaming@en,es")
    assert [s.repo_id for s in specs] == [
        "moonshine-voice/small-streaming-en",
        "moonshine-voice/small-streaming-es",
    ]
    assert all(s.revision == fake.version for s in specs), "the package version is the revision"
    assert all(callable(s.check) and callable(s.fetch) for s in specs)


def test_prefetch_specs_default_is_a_single_spec(mod, fake):
    for model in (None, "medium-streaming@en"):
        (spec,) = mod.prefetch_specs(model)
        assert spec.repo_id == "moonshine-voice/medium-streaming-en"
        assert spec.repo_id


def test_prefetch_specs_omitted_arch_gets_one_distinct_spec_per_language(mod, fake):
    specs = mod.prefetch_specs("@en,es")
    ids = [s.repo_id for s in specs]
    assert len(ids) == 2 and len(set(ids)) == 2
    assert ids[0].startswith("moonshine-voice/") and ids[0].endswith("-en")
    assert ids[1].startswith("moonshine-voice/") and ids[1].endswith("-es")


@pytest.mark.parametrize("spec", ["a@b@c", "x@en@es", "@en,,es"])
def test_prefetch_specs_reject_a_malformed_spec(mod, spec):
    with pytest.raises(ConfigError):
        mod.prefetch_specs(spec)


def test_prefetch_specs_never_download(mod, fake):
    mod.prefetch_specs("@en,es,de")
    assert fake.downloads == [] and fake.loads == []


# ---------------------------------------------------------------------------
# Package version pin: one value in pyproject, the constant and the installer probe
# ---------------------------------------------------------------------------


def _justfile_moonshine_probe() -> str:
    import re

    text = (Path(__file__).resolve().parent.parent / "justfile").read_text()
    m = re.search(r'^\s*moonshine\)\s*extra="moonshine";\s*probe="([^"]*)"', text, re.MULTILINE)
    assert m, "justfile _ensure-extra has no moonshine probe"
    return m.group(1)


def test_version_pin_is_identical_in_pyproject_constant_and_installer_probe():
    """A pin bump must move all three together: the extra, the backend's runtime check
    and the installer's ``_ensure-extra`` probe (else a stale venv passes the probe)."""
    import re
    import tomllib

    from stt_server.backends.moonshine import MOONSHINE_VOICE_VERSION

    root = Path(__file__).resolve().parent.parent
    extra = tomllib.loads((root / "pyproject.toml").read_text())["project"][
        "optional-dependencies"
    ]["moonshine"]
    pins = [r.replace(" ", "").split("==", 1)[1] for r in extra if r.startswith("moonshine-voice")]
    assert pins == [MOONSHINE_VOICE_VERSION]
    assert re.findall(r"'(\d+\.\d+\.\d+)'", _justfile_moonshine_probe()) == [
        MOONSHINE_VOICE_VERSION
    ]


@pytest.mark.parametrize(("version", "ok"), [("0.1.6", True), ("0.0.62", False), (None, False)])
def test_installer_probe_requires_the_pinned_version(tmp_path, version, ok):
    """The probe string, run as ``_ensure-extra`` runs it, against a stub package: an
    importable but older wheel must fail the probe so the installer re-syncs."""
    from stt_server.backends.moonshine import MOONSHINE_VOICE_VERSION

    assert MOONSHINE_VOICE_VERSION == "0.1.6", "update the parametrisation with the pin"
    pkg = tmp_path / "moonshine_voice"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("" if version is None else f"__version__ = {version!r}\n")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(tmp_path)
    r = subprocess.run(
        [sys.executable, "-c", _justfile_moonshine_probe()], env=env, capture_output=True
    )
    assert (r.returncode == 0) is ok, r.stderr


@pytest.mark.parametrize("version", ["0.0.62", "0.2.0", None])
async def test_start_refuses_a_package_version_other_than_the_pin(mod, fake, version):
    """A stale wheel imports fine but lacks the private API (``_stt_catalog``): fail
    with the remedy, not an ``AttributeError``."""
    pkg = sys.modules["moonshine_voice"]
    if version is None:
        del pkg.__version__
    else:
        pkg.__version__ = version
    with pytest.raises(ImportError) as excinfo:
        await _backend(mod).start()
    msg = str(excinfo.value)
    assert mod.MOONSHINE_VOICE_VERSION in msg and str(version) in msg
    assert "uv sync --extra moonshine --inexact" in msg
    with pytest.raises(ImportError):
        mod.prefetch_specs(None)[0].check(None)
    assert fake.downloads == []


# ---------------------------------------------------------------------------
# Decode: the PCM -> list conversion happens under the decode lock
# ---------------------------------------------------------------------------


async def test_pcm_conversion_runs_under_the_decode_thread_lock(mod, fake, monkeypatch):
    backend = _backend(mod)
    await backend.start()
    seen: list[bool] = []
    real = np.frombuffer

    def spy(*args, **kwargs):
        seen.append(backend._thread_lock.locked())
        return real(*args, **kwargs)

    monkeypatch.setattr(np, "frombuffer", spy)
    _, events = await _drive(backend)
    assert events[-1].kind == "completed" and events[-1].text == "hello from moonshine"
    assert seen == [True], "each queued decode would otherwise build its float list unlocked"
    await backend.close()


# ---------------------------------------------------------------------------
# Cache integrity contract (prefetch): reader ``_file_state``, writer repair
# ---------------------------------------------------------------------------

_NAME = "decoder.onnx"
_ARCH = ModelArch.MEDIUM_STREAMING


def _info(fake, *, checksum: bool = True) -> dict:
    (info,) = [f for f in fake.group("en", _ARCH)["files"] if f["name"] == _NAME]
    return info if checksum else {k: v for k, v in info.items() if k != "checksum"}


def _place(fake, path: Path, state: str) -> None:
    good = fake.file_bytes("en", _ARCH, _NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    if state == "missing":
        path.unlink(missing_ok=True)
    elif state == "size_bad":
        path.write_bytes(good[:-1])
    elif state == "crc_bad":
        path.write_bytes(bytes(b ^ 0x01 for b in good))
    else:  # "crc_ok" / "uncomputable"
        path.write_bytes(good)


def _uncomputable(monkeypatch) -> None:
    # What ``crc32c_file`` returns when ``google-crc32c`` cannot be imported.
    monkeypatch.setattr(sys.modules["moonshine_voice.download_file"], "crc32c_file", lambda p: None)


# Reader: (file state, manifest has a CRC32C) -> result ("raise:<code>" or the state).
_READER_CELLS = [
    ("missing", True, "missing"),
    ("size_bad", True, "incomplete"),
    ("crc_ok", True, "ok"),
    ("crc_bad", True, "raise:ModelInvalidError"),
    ("uncomputable", True, "raise:ModuleNotFoundError"),
    ("crc_bad", False, "ok"),  # no CRC32C in the manifest: size is all there is
    ("uncomputable", False, "ok"),
]


@pytest.mark.parametrize(("state", "checksum", "expected"), _READER_CELLS)
def test_file_state_over_the_whole_state_space(mod, fake, monkeypatch, state, checksum, expected):
    from stt_server.supervise import classify_startup_error

    path = fake.group_dir("en", _ARCH) / _NAME
    _place(fake, path, state)
    if state == "uncomputable":
        _uncomputable(monkeypatch)
    info = _info(fake, checksum=checksum)
    if expected.startswith("raise:"):
        with pytest.raises(Exception) as excinfo:
            mod._file_state(path, info)
        assert classify_startup_error(excinfo.value)[0] == expected.split(":", 1)[1]
        assert str(path) in str(excinfo.value)
    else:
        assert mod._file_state(path, info) == expected


# Writer: the file's state when the repair takes the lock (after another prefetch may
# have removed or replaced it) -> what the repair leaves behind.
_REPAIR_CELLS = [
    ("missing", "missing"),  # another prefetch already removed it: no FileNotFoundError
    ("size_bad", "kept"),  # not ours to judge: the library replaces a size mismatch itself
    ("crc_ok", "kept"),  # another prefetch already re-downloaded it: never delete it
    ("crc_bad", "removed"),
    ("uncomputable", "raise"),  # fail closed, delete nothing
]


@pytest.mark.parametrize(("state", "expected"), _REPAIR_CELLS)
def test_repair_rechecks_under_the_lock_over_the_whole_state_space(
    mod, fake, monkeypatch, state, expected
):
    path = fake.group_dir("en", _ARCH) / _NAME
    _place(fake, path, state)
    before = path.read_bytes() if path.exists() else None
    if state == "uncomputable":
        _uncomputable(monkeypatch)
    if expected == "raise":
        with pytest.raises(ModuleNotFoundError):
            mod._remove_if_still_corrupt(path, _info(fake))
    else:
        mod._remove_if_still_corrupt(path, _info(fake))
    if expected in ("missing", "removed"):
        assert not path.exists()
    else:
        assert path.read_bytes() == before


def test_repair_holds_the_librarys_per_file_download_lock(mod, fake, monkeypatch):
    """The recheck and the delete run under ``<file>.lock``, the lock
    ``moonshine_voice.download_file.download_file`` holds while it writes and renames."""
    filelock = pytest.importorskip("filelock")
    if not hasattr(filelock, "Timeout"):
        pytest.skip("real filelock not installed (single-process stand-in)")
    path = fake.group_dir("en", _ARCH) / _NAME
    _place(fake, path, "crc_bad")
    held: list[bool] = []
    real = mod._crc_mismatch

    def probe(p, info):
        try:
            filelock.FileLock(str(p) + ".lock", timeout=0).acquire()
        except filelock.Timeout:
            held.append(True)
        else:
            held.append(False)
        return real(p, info)

    monkeypatch.setattr(mod, "_crc_mismatch", probe)
    mod._remove_if_still_corrupt(path, _info(fake))
    assert held == [True] and not path.exists()


@pytest.mark.parametrize("other", ["removed", "replaced"])
def test_fetch_survives_a_concurrent_repair_of_the_same_file(mod, fake, monkeypatch, other):
    """Two prefetches see the same corrupt file; the other one wins the lock first and
    either just removed it or already re-downloaded it. This one must neither crash
    (``FileNotFoundError``) nor delete the good replacement."""
    root = Path(fake.write_model("en", _ARCH, corrupt=True))
    good = fake.file_bytes("en", _ARCH, _NAME)
    real = mod._remove_if_still_corrupt

    def racing(path, info):
        if path.name == _NAME:
            if other == "removed":
                path.unlink()
            else:
                path.write_bytes(good)
        return real(path, info)

    monkeypatch.setattr(mod, "_remove_if_still_corrupt", racing)
    (spec,) = mod.prefetch_specs("medium-streaming@en")
    assert spec.fetch(spec, None) == str(root)
    assert (root / _NAME).read_bytes() == good
    assert spec.check(spec) == (str(root), fake.total_size("en", _ARCH))


def _race_on_first_crc_read(fake, monkeypatch, other: str) -> None:
    """Between the size check and the CRC32C read of ``_NAME``, another prefetch
    leaves the file ``other``: "present" (untouched), "vanishes" (its locked
    repair removed it) or "replaced" (its download renamed a good copy in)."""
    dfile = sys.modules["moonshine_voice.download_file"]
    real = dfile.crc32c_file
    fired: list[bool] = []

    def racing(path):
        if Path(path).name == _NAME and not fired:
            fired.append(True)
            if other == "vanishes":
                Path(path).unlink()
            elif other == "replaced":
                Path(path).write_bytes(fake.file_bytes("en", _ARCH, _NAME))
        return real(path)

    monkeypatch.setattr(dfile, "crc32c_file", racing)


@pytest.mark.parametrize("other", ["present", "vanishes", "replaced"])
def test_fetch_survives_the_file_changing_before_its_unlocked_crc_read(
    mod, fake, monkeypatch, other
):
    """The repair's first CRC32C read runs outside the lock: a file that vanishes
    after its size check is a cache miss (re-downloaded), never a ``FileNotFoundError``."""
    root = Path(fake.write_model("en", _ARCH, corrupt=True))
    _race_on_first_crc_read(fake, monkeypatch, other)
    (spec,) = mod.prefetch_specs("medium-streaming@en")
    assert spec.fetch(spec, None) == str(root)
    assert (root / _NAME).read_bytes() == fake.file_bytes("en", _ARCH, _NAME)
    assert spec.check(spec) == (str(root), fake.total_size("en", _ARCH))


@pytest.mark.parametrize(("other", "expected"), [("vanishes", "missing"), ("replaced", "ok")])
def test_file_state_treats_a_file_changing_before_its_crc_read_as_its_new_state(
    mod, fake, monkeypatch, other, expected
):
    """``--check`` reads without the lock too: a file removed between the stat and the
    CRC32C read is ``missing`` (not cached), never a ``FileNotFoundError``."""
    path = fake.group_dir("en", _ARCH) / _NAME
    _place(fake, path, "crc_bad")
    _race_on_first_crc_read(fake, monkeypatch, other)
    assert mod._file_state(path, _info(fake)) == expected


def test_check_derives_the_model_directory_the_way_serve_does(mod, fake, monkeypatch):
    """``--check`` (the readiness signal) and prefetch take the cache root and the
    manifest group from the same library calls ``get_model_for_language`` makes, so
    a library-side change to either moves the checked directory with the served one."""
    dl = sys.modules["moonshine_voice.download"]
    real_root = dl._cache_root_dir
    moved = fake.cache_root / "moved-root"
    monkeypatch.setattr(
        dl, "_cache_root_dir", lambda arg=None: moved if arg is None else real_root(arg)
    )
    served, _ = dl.get_model_for_language("en", _ARCH)
    (spec,) = mod.prefetch_specs("medium-streaming@en")
    assert spec.check(spec) == (served, fake.total_size("en", _ARCH))
    assert Path(served).is_relative_to(moved)


def test_fetch_fails_clearly_when_the_library_serves_another_directory(mod, fake, monkeypatch):
    """Prefetch verifies the manifest-derived root; the server loads from the path
    ``get_model_for_language`` returns. They must be the same directory."""
    dl = sys.modules["moonshine_voice.download"]
    real = dl.get_model_for_language
    elsewhere = fake.cache_root / "elsewhere"

    def moved(*args, **kwargs):
        _path, arch = real(*args, **kwargs)
        return str(elsewhere), arch

    monkeypatch.setattr(dl, "get_model_for_language", moved)
    (spec,) = mod.prefetch_specs("medium-streaming@en")
    with pytest.raises(RuntimeError) as excinfo:
        spec.fetch(spec, None)
    msg = str(excinfo.value)
    assert str(elsewhere) in msg and str(fake.group_dir("en", _ARCH)) in msg


# --- a relative MOONSHINE_VOICE_CACHE is refused (the library doubles the prefix) ----


@pytest.fixture
def relative_cache(monkeypatch):
    monkeypatch.setenv("MOONSHINE_VOICE_CACHE", "review-model-cache")


def test_relative_cache_root_is_refused_by_check(mod, fake, relative_cache):
    (spec,) = mod.prefetch_specs("medium-streaming@en")
    with pytest.raises(ConfigError, match="MOONSHINE_VOICE_CACHE.*absolute"):
        spec.check(spec)
    assert fake.downloads == []


def test_relative_cache_root_is_refused_by_fetch(mod, fake, relative_cache):
    (spec,) = mod.prefetch_specs("medium-streaming@en")
    with pytest.raises(ConfigError, match="MOONSHINE_VOICE_CACHE.*absolute"):
        spec.fetch(spec, None)
    assert fake.downloads == []


async def test_relative_cache_root_is_refused_by_start(mod, fake, relative_cache):
    with pytest.raises(ConfigError, match="MOONSHINE_VOICE_CACHE.*absolute"):
        await _backend(mod).start()


async def test_absolute_cache_root_still_works(mod, fake, monkeypatch, tmp_path):
    monkeypatch.setenv("MOONSHINE_VOICE_CACHE", str(tmp_path / "abs-cache"))
    await _backend(mod).start()
    (spec,) = mod.prefetch_specs("medium-streaming@en")
    root = spec.fetch(spec, None)
    assert Path(root).is_absolute() and spec.check(spec)[0] == root
