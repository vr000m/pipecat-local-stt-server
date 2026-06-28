"""Backstop: a backend whose optional extra is not installed must fail with an
actionable message (``run: uv sync --extra <X> --inexact``) rather than a bare
``ModuleNotFoundError`` traceback that crash-loops in the LaunchAgent log.

The import is forced to fail via a ``builtins.__import__`` shim so the test is
deterministic regardless of which extras happen to be present in the dev/CI venv
(``uv run`` prunes optional extras — see the justfile ``_ensure-extra`` helper).
"""

import asyncio
import builtins

import pytest

from stt_server.backends.mlx_whisper import MLXWhisperBackend
from stt_server.backends.nemotron import NemotronBackend
from stt_server.backends.parakeet import ParakeetBackend


@pytest.mark.parametrize(
    "factory, missing, extra",
    [
        (lambda: MLXWhisperBackend(model="fake"), "mlx_whisper", "mlx"),
        (lambda: NemotronBackend(model="fake"), "mlx_audio", "nemotron"),
        (lambda: ParakeetBackend(model="fake"), "parakeet_mlx", "parakeet"),
    ],
)
def test_backend_missing_extra_raises_actionable_error(monkeypatch, factory, missing, extra):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == missing or name.startswith(missing + "."):
            raise ModuleNotFoundError(f"No module named '{name}'", name=name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    backend = factory()
    with pytest.raises(ModuleNotFoundError) as excinfo:
        asyncio.run(backend.start())
    msg = str(excinfo.value)
    assert f"'{extra}' extra is not installed" in msg
    assert f"uv sync --extra {extra} --inexact" in msg
