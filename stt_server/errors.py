"""Error taxonomy shared by the server core, ``prefetch`` and the CLI.

Stdlib-only and dependency-free, so ``stt_server.server``, ``stt_server.prefetch``
and ``stt_server.backends`` can raise and recognise these without importing the
supervisor JSON channel (``stt_server.supervise``, which re-exports
:class:`ConfigError` for existing callers).

Two mechanisms, on purpose:

- :class:`ConfigError` is a type, because this package raises it itself.
- The ``ModelInvalidError`` / download-phase origins are attribute tags
  (``stt_server.snapshot.mark_model_invalid`` / ``mark_download_phase``),
  because they are attached to exceptions raised by third-party code
  (``huggingface_hub``, ``json``, the model libraries) whose types we do not own.

``supervise.classify_startup_error`` reads both.
"""

from __future__ import annotations

__all__ = ["ConfigError", "is_config_error"]

# Raised by ``huggingface_hub`` for a malformed repo id / revision before any
# request: bad input, not a download failure. Recognised by name (lean import).
_HUB_INPUT_ERROR_NAMES = frozenset({"HFValidationError"})


class ConfigError(ValueError):
    """A rejected endpoint or CLI configuration (``ConfigError`` code).

    Raised by ``ServerConfig``, the socket-directory enforcement, the backend
    registry (unknown ``--backend`` name), ``prefetch``'s model checks and
    ``serve(on_fatal=...)`` without ``hard_exit_on_stop=True``. A ``ValueError``
    subclass, so existing ``except ValueError`` callers are unaffected; a plain
    ``ValueError`` (for example from a model library during warm-up) is NOT a
    config error. A Hub input-validation error (``HFValidationError``: a
    malformed repo id or revision) is not a ``ConfigError`` instance but is
    classified as one: use :func:`is_config_error`, not ``isinstance``.
    """


def is_config_error(exc: BaseException) -> bool:
    """True for :class:`ConfigError` and for a Hub input-validation error
    (``HFValidationError``: a malformed repo id or revision)."""
    return isinstance(exc, ConfigError) or (
        isinstance(exc, ValueError) and type(exc).__name__ in _HUB_INPUT_ERROR_NAMES
    )
