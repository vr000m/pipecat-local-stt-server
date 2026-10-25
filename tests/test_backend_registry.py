"""``backends.BACKENDS`` is the single registry behind ``--backend``: the CLI
choices, the ``--model`` default, the constructor and ``prefetch``'s module map
are all derived from it, so a backend is added in one place."""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest

from stt_server import __main__ as M
from stt_server import prefetch as PF
from stt_server.backends import BACKENDS


def test_cli_choices_are_the_registry():
    assert M.BACKEND_CHOICES == tuple(BACKENDS)


@pytest.mark.parametrize("name", [n for n, e in BACKENDS.items() if e.module])
def test_entry_matches_its_module(name):
    entry = BACKENDS[name]
    module = importlib.import_module(entry.module)
    cls = getattr(module, entry.class_name)
    assert cls.backend_name == name
    assert M._resolve_model(name, None) == entry.default_model
    backend = M._make_backend(name, "x/y")
    try:
        assert isinstance(backend, cls) and backend.model == "x/y"
    finally:
        getattr(backend, "abort", lambda: None)()
    assert callable(module.prefetch_specs)
    (spec,) = PF.specs_for(name, None)
    assert spec.repo_id == entry.default_model


def test_echo_entry():
    assert M._make_backend("echo", "ignored").backend_name == "echo"
    assert PF.specs_for("echo", None) == []


def test_unknown_backend():
    with pytest.raises(SystemExit):
        M._make_backend("nope", "x")
    with pytest.raises(ValueError):
        PF.specs_for("nope", None)


def test_local_paths_flag_drives_prefetch():
    for name, entry in BACKENDS.items():
        if entry.module is None:
            continue
        if entry.local_model_paths:
            assert PF.local_specs_for(name, "/tmp/m")[0].local_path == "/tmp/m"
        else:
            with pytest.raises(ValueError, match="local model paths"):
                PF.local_specs_for(name, "/tmp/m")


def test_snapshot_spec_is_stdlib_only_and_reexported():
    from stt_server.snapshot import SnapshotSpec

    assert PF.SnapshotSpec is SnapshotSpec
    code = (
        "import sys, stt_server.snapshot, stt_server.backends; "
        "print(sorted(m for m in ('stt_server.prefetch', 'numpy', 'huggingface_hub') "
        "if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_model_default_comes_only_from_the_registry():
    """``default_model=None`` means "no model": no hidden Whisper fallback."""
    assert BACKENDS["echo"].default_model is None
    assert M._resolve_model("echo", None) is None
    assert M._resolve_model("echo", "x/y") == "x/y"


def test_import_backend_is_the_single_lookup(monkeypatch):
    """One lookup returns everything callers need (entry, module, class); an
    unknown name is one ``ConfigError``, translated only at the CLI boundary."""
    from stt_server import backends as B
    from stt_server.errors import ConfigError

    echo = B.import_backend("echo")
    assert echo.module is None and echo.cls is None and echo.entry is BACKENDS["echo"]
    with pytest.raises(ConfigError, match="unknown backend"):
        B.import_backend("nope")
    with pytest.raises(ConfigError, match="unknown backend"):
        PF.specs_for("nope", None)
    with pytest.raises(SystemExit, match="unknown backend"):
        M._make_backend("nope", None)
    seen = []
    real = B.import_backend
    monkeypatch.setattr(B, "import_backend", lambda n: seen.append(n) or real(n))
    monkeypatch.setattr(M, "import_backend", B.import_backend)
    # Callers must not re-index the registry after the lookup.
    monkeypatch.setattr(M, "BACKENDS", {})
    M._make_backend("echo", None)
    PF.specs_for("echo", None)
    with pytest.raises(ConfigError, match="takes no model"):
        PF.local_specs_for("echo", "/tmp/x")  # echo has no model: refused after the lookup
    assert seen == ["echo", "echo", "echo"]


def test_abort_backend_uses_the_abortable_protocol():
    from stt_server import AbortableBackend
    from stt_server.backend import EchoBackend, abort_backend

    class WithAbort(EchoBackend):
        aborted = 0

        def abort(self):
            self.aborted += 1

    b = WithAbort()
    assert isinstance(b, AbortableBackend) and not isinstance(EchoBackend(), AbortableBackend)
    abort_backend(b, None)
    abort_backend(EchoBackend(), None)  # no hook: nothing to call
    assert b.aborted == 1


def test_optional_backend_hooks_are_exported_together():
    """Both optional-hook protocols are public, from the package root."""
    import stt_server
    from stt_server import AbortableBackend, IdleAwareBackend, backend

    assert {"AbortableBackend", "IdleAwareBackend"} <= set(stt_server.__all__)
    assert AbortableBackend is backend.AbortableBackend
    assert IdleAwareBackend is backend.IdleAwareBackend


def test_config_error_lives_outside_the_cli_layer():
    """The server core and prefetch raise ``ConfigError`` without importing the
    supervisor channel; ``supervise.ConfigError`` and the package export are the
    same class."""
    code = (
        "import sys, stt_server.server, stt_server.prefetch, stt_server.backends\n"
        "assert 'stt_server.supervise' not in sys.modules, 'core imports the CLI layer'\n"
        "import stt_server, stt_server.errors as E, stt_server.supervise as S\n"
        "assert stt_server.ConfigError is E.ConfigError is S.ConfigError\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
