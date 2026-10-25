"""Optional backend implementations. Import lazily to avoid hard deps.

``BACKENDS`` is the single registry of ``--backend`` names: the CLI's choices,
the backend-aware ``--model`` default, the constructor, and the ``prefetch``
module map are all derived from it. Stdlib-only (no backend module is imported
here), so the CLI can read it at every launch without pulling ``mlx_whisper``
(numpy) or an optional extra.

A backend module named here must define the class named in its entry and a
module-level ``prefetch_specs(model) -> list[SnapshotSpec]`` (see
``stt_server.backend`` for the optional hooks a backend may implement).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType

from ..errors import ConfigError

# Default model per backend: single definitions, re-exported by each backend
# module (``backends.parakeet.DEFAULT_PARAKEET_MODEL`` etc.).
DEFAULT_MLX_MODEL = "mlx-community/whisper-large-v3-turbo"
# ``parakeet-tdt-0.6b-v3`` is the package's own CLI default.
DEFAULT_PARAKEET_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"
DEFAULT_NEMOTRON_MODEL = "mlx-community/nemotron-3.5-asr-streaming-0.6b"
DEFAULT_KYUTAI_MODEL = "kyutai/stt-1b-en_fr-mlx"


@dataclass(frozen=True)
class BackendEntry:
    """How the CLI reaches one backend without importing it up front."""

    #: Dotted module path, or ``None`` for the built-in echo backend.
    module: str | None
    #: Backend class name inside ``module``.
    class_name: str | None
    #: The ``--model`` default when none is given (``None``: the backend has none).
    default_model: str | None
    #: Whether ``prefetch --model <local dir>`` is supported.
    local_model_paths: bool = True


BACKENDS: dict[str, BackendEntry] = {
    "echo": BackendEntry(None, None, None),
    "mlx": BackendEntry("stt_server.backends.mlx_whisper", "MLXWhisperBackend", DEFAULT_MLX_MODEL),
    "parakeet": BackendEntry(
        "stt_server.backends.parakeet", "ParakeetBackend", DEFAULT_PARAKEET_MODEL
    ),
    "nemotron": BackendEntry(
        "stt_server.backends.nemotron", "NemotronBackend", DEFAULT_NEMOTRON_MODEL
    ),
    # The kyutai loader fetches by Hub repo id (``hf_hub_download``) only.
    "kyutai": BackendEntry(
        "stt_server.backends.kyutai",
        "KyutaiBackend",
        DEFAULT_KYUTAI_MODEL,
        local_model_paths=False,
    ),
}


@dataclass(frozen=True)
class LoadedBackend:
    """A registry entry with its module imported (``import_backend``'s result)."""

    name: str
    entry: BackendEntry
    #: The imported backend module, or ``None`` for the built-in echo backend.
    module: ModuleType | None

    @property
    def cls(self) -> type | None:
        """The backend class (``None`` for echo)."""
        if self.module is None:
            return None
        return getattr(self.module, self.entry.class_name)


def import_backend(name: str) -> LoadedBackend:
    """Look ``--backend name`` up and import its module lazily.

    The one registry lookup for the CLI constructor and ``prefetch``: callers
    read the entry, module and class from the result instead of indexing
    ``BACKENDS`` again. An unknown name raises :class:`~stt_server.errors.ConfigError`
    (the CLI boundary turns it into its exit message / error event).
    """
    try:
        entry = BACKENDS[name]
    except KeyError:
        raise ConfigError(f"unknown backend: {name!r}") from None
    if entry.module is None:
        return LoadedBackend(name, entry, None)
    import importlib

    return LoadedBackend(name, entry, importlib.import_module(entry.module))
