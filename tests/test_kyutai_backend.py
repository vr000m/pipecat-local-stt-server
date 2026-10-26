"""Kyutai backend: lean contract tests, plus a live decode when the ``kyutai``
extra and the model weights are both present (skipped otherwise; the extra
conflicts with the other MLX extras, so CI's full-extras env skips it).
"""

from __future__ import annotations

import asyncio
import threading
import importlib.util
import subprocess
import sys
import wave

import numpy as np
import pytest

from stt_server.backends import kyutai as K


def test_module_import_is_lean():
    code = (
        "import sys, stt_server.backends.kyutai, stt_server.__main__; "
        "print(sorted(m for m in ('moshi_mlx', 'mlx', 'sentencepiece') if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_cli_wiring():
    from stt_server import __main__ as M

    assert "kyutai" in M.BACKEND_CHOICES
    assert M._resolve_model("kyutai", None) == K.DEFAULT_KYUTAI_MODEL
    backend = M._make_backend("kyutai", "x/y")
    assert backend.backend_name == "kyutai" and backend.model == "x/y"


def test_resample_16k_to_24k_preserves_a_tone():
    t = np.arange(16000) / 16000
    tone = np.sin(2 * np.pi * 440 * t).astype(np.float32)
    out = K.resample_16k_to_24k(tone)
    assert out.size == 24000
    # Dominant frequency survives the rate change; amplitude ~unity away from the edges.
    spectrum = np.abs(np.fft.rfft(out))
    assert abs(np.argmax(spectrum) * 24000 / out.size - 440) < 2
    assert 0.9 < np.max(np.abs(out[1000:-1000])) < 1.1
    assert K.resample_16k_to_24k(np.zeros(0, dtype=np.float32)).size == 0


@pytest.mark.parametrize(
    ("text", "kinds"), [("hello there", ["delta", "completed"]), ("", ["completed"])]
)
def test_stream_contract(monkeypatch, text, kinds):
    backend = K.KyutaiBackend(model="fake")
    seen: list[bytes] = []

    def fake_transcribe(pcm: bytes, **_kw) -> str:
        seen.append(pcm)
        return text

    monkeypatch.setattr(backend, "_transcribe", fake_transcribe)

    async def run():
        stream = await backend.open_stream(language="en")
        await stream.feed(b"\x01\x00" * 100)
        await stream.feed(b"\x02\x00" * 100)
        await stream.end()
        events = [(e.kind, e.text) for e in [ev async for ev in stream.events()]]
        await backend.close()
        return events

    events = asyncio.run(run())
    assert [k for k, _ in events] == kinds
    assert events[-1] == ("completed", text)
    assert seen == [b"\x01\x00" * 100 + b"\x02\x00" * 100]


def test_cancelled_stream_yields_nothing(monkeypatch):
    backend = K.KyutaiBackend(model="fake")
    monkeypatch.setattr(
        backend, "_transcribe", lambda pcm, **_kw: pytest.fail("decoded after cancel")
    )

    async def run():
        stream = await backend.open_stream()
        await stream.feed(b"\x00\x00" * 10)
        await stream.cancel()
        await stream.end()
        return [e async for e in stream.events()]

    assert asyncio.run(run()) == []


def _weights_cached() -> bool:
    if importlib.util.find_spec("moshi_mlx") is None:
        return False
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(K.DEFAULT_KYUTAI_MODEL, local_files_only=True)
        return True
    except Exception:
        return False


@pytest.mark.skipif(sys.platform != "darwin", reason="say/afconvert are macOS tools")
@pytest.mark.skipif(not _weights_cached(), reason="needs the kyutai extra and cached weights")
def test_live_decode_of_spoken_clip(tmp_path):
    aiff, wav_path = tmp_path / "clip.aiff", tmp_path / "clip.wav"
    subprocess.run(["say", "-o", str(aiff), "The weather today is sunny and warm."], check=True)
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav_path)],
        check=True,
    )
    with wave.open(str(wav_path)) as w:
        pcm = w.readframes(w.getnframes())
    backend = K.KyutaiBackend()

    async def run():
        await backend.start()
        texts = []
        for _ in range(2):  # twice: per-utterance state must reset cleanly
            stream = await backend.open_stream()
            await stream.feed(pcm)
            await stream.end()
            texts.append([e.text async for e in stream.events()][-1])
        await backend.close()
        return texts

    first, second = asyncio.run(run())
    assert "weather" in first.lower()
    assert first == second


def _fake_loaded_model(encoded: list[int], on_encode=None):
    """A ``_get_model`` result whose Mimi counts the frames it is given."""
    import types

    import numpy as np

    def encode_step(_x):
        encoded.append(1)
        if on_encode is not None:
            on_encode(len(encoded))
        return np.zeros((1, 1, 1))

    mimi = types.SimpleNamespace(reset_all=lambda: None, encode_step=encode_step)
    gen = types.SimpleNamespace(step=lambda _t: np.array([0]))
    models = types.SimpleNamespace(LmGen=lambda **_k: gen)
    utils = types.SimpleNamespace(Sampler=lambda **_k: None)
    return {
        "mx": types.SimpleNamespace(array=np.asarray),
        "models": models,
        "utils": utils,
        "lm": types.SimpleNamespace(transformer_cache=[]),
        "mimi": mimi,
        "tokenizer": types.SimpleNamespace(id_to_piece=lambda _t: ""),
        "prefix_samples": 0,
        "flush_samples": 0,
    }


def test_transcribe_stops_at_the_next_frame_once_cancelled(monkeypatch):
    import threading

    backend = K.KyutaiBackend(model="fake")
    cancel = threading.Event()
    encoded: list[int] = []
    model = _fake_loaded_model(encoded, on_encode=lambda n: cancel.set() if n == 2 else None)
    monkeypatch.setattr(backend, "_get_model", lambda: model)
    pcm = b"\x00\x00" * 16_000  # 1 s -> 12 frames of 80 ms

    assert backend._transcribe(pcm, cancel=cancel) == ""
    assert len(encoded) == 2

    # Without a cancel event the same audio decodes every frame.
    encoded.clear()
    backend._transcribe(pcm)
    assert len(encoded) == 12


def test_cancel_during_decode_frees_the_decode_lock_for_the_next_stream(monkeypatch):
    backend = K.KyutaiBackend(model="fake")
    started, release = threading.Event(), threading.Event()

    def blocking_transcribe(pcm, cancel=None):
        started.set()
        # Stands in for the frame loop: runs until it sees the cancel event.
        while not cancel.is_set():
            release.wait(0.01)
        return ""

    monkeypatch.setattr(backend, "_transcribe", blocking_transcribe)

    async def run():
        first = await backend.open_stream()
        await first.feed(b"\x01\x00" * 10)
        task = asyncio.ensure_future(first.end())
        while not started.is_set():
            await asyncio.sleep(0.005)
        await first.cancel()
        task.cancel()
        # The abandoned decode ends promptly, so the backend goes idle.
        await asyncio.wait_for(backend.idle(), timeout=2)
        assert backend._thread_lock.acquire(blocking=False)
        backend._thread_lock.release()

    asyncio.run(run())


def test_pinned_defaults_and_unpinned_overrides():
    from stt_server import backends

    assert (
        backends.pinned_revision(backends.DEFAULT_KYUTAI_MODEL)
        == (backends.PINNED_REVISIONS[backends.DEFAULT_KYUTAI_MODEL])
    )
    assert backends.pinned_revision("some/other-model") is None
    assert backends.pinned_revision(None) is None
    assert all(len(sha) == 40 for sha in backends.PINNED_REVISIONS.values())


def test_kyutai_downloads_at_the_pinned_revision(monkeypatch, tmp_path):
    from stt_server import backends

    seen: list[tuple[str, str | None]] = []
    cfg = tmp_path / "config.json"
    cfg.write_text("{not json")  # stops _get_model right after the config fetch

    def fake_download(repo, name, revision=None):
        seen.append((name, revision))
        return str(cfg)

    import sys
    import types

    core, nn = types.SimpleNamespace(), types.SimpleNamespace()
    for name, mod in {
        "mlx": types.SimpleNamespace(core=core, nn=nn),
        "mlx.core": core,
        "mlx.nn": nn,
        "sentencepiece": types.SimpleNamespace(),
        "moshi_mlx": types.SimpleNamespace(models=None, utils=None),
        "huggingface_hub": types.SimpleNamespace(hf_hub_download=fake_download),
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    with pytest.raises(Exception):
        K.KyutaiBackend(model=backends.DEFAULT_KYUTAI_MODEL)._get_model()
    assert seen == [("config.json", backends.PINNED_REVISIONS[backends.DEFAULT_KYUTAI_MODEL])]

    seen.clear()
    with pytest.raises(Exception):
        K.KyutaiBackend(model="x/y")._get_model()
    assert seen == [("config.json", None)]


def test_prefetch_spec_carries_the_pin():
    from stt_server import backends

    (spec,) = K.prefetch_specs(None)
    assert spec.revision == backends.PINNED_REVISIONS[backends.DEFAULT_KYUTAI_MODEL]
    assert K.prefetch_specs("x/y")[0].revision is None
