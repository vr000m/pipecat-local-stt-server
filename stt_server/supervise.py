"""Machine-readable lifecycle channel for a supervising parent process.

Used by ``serve --supervised`` and ``prefetch --json`` so an app (for example a
macOS menu bar app managing local speech services) can drive the CLI without
scraping log text. The channel mirrors ``tts_server.supervise`` in
pipecat-local-tts-server, so one supervisor handles both servers; the ``code``
vocabulary is per repo (STT adds ``ModelInvalidError``, TTS has
``SocketPathConflict``), and a supervisor should read an unknown code as
``BackendStartError``.

- **stdout carries only JSON lines**, one compact object per line, flushed.
  While :func:`json_channel` is active ``sys.stdout`` points at stderr, so a
  stray ``print()`` from a model library cannot corrupt the event stream. A
  C-level write straight to fd 1 bypasses that redirect, so consumers should
  skip any stdout line that does not parse as a JSON object.
- **stdin EOF means the supervisor is gone.** :func:`watch_stdin_eof` fires a
  callback when the parent closes its end of the pipe or dies, so a server
  never outlives the process that started it.
- :func:`classify_startup_error` maps a fatal exception to a stable ``code``.

Stdlib-only and lean at import: download errors are recognised by their
exception's module name, never by importing ``huggingface_hub``.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import select
import sys
import threading
from collections.abc import Callable, Iterator
from typing import Any, TextIO

# The origin tags live in the stdlib-only ``stt_server.snapshot`` so the backends
# and ``prefetch`` can set them without importing this module; re-exported here.
# ``ConfigError`` lives in the stdlib-only ``stt_server.errors`` (the server core
# and ``prefetch`` raise it without importing this module); re-exported here.
from .errors import ConfigError, is_config_error
from .snapshot import DOWNLOAD_PHASE_ATTR as _DOWNLOAD_PHASE_ATTR
from .snapshot import MODEL_INVALID_ATTR as _MODEL_INVALID_ATTR
from .snapshot import mark_download_phase, mark_model_invalid

__all__ = [
    "ConfigError",
    "classify_startup_error",
    "emit",
    "json_channel",
    "mark_download_phase",
    "mark_model_invalid",
    "watch_stdin_eof",
]

logger = logging.getLogger("stt_server")

# Stable ``code`` values for ``{"event": "error"}`` lines (part of the contract;
# listed in README "Error codes (per repo)").
CODE_MODULE_NOT_FOUND = "ModuleNotFoundError"
CODE_ADDR_IN_USE = "EADDRINUSE"
CODE_ACCESS_DENIED = "EACCES"
CODE_MODEL_DOWNLOAD = "ModelDownloadError"
CODE_CONFIG = "ConfigError"
CODE_BACKEND_START = "BackendStartError"
#: The model snapshot is present but unusable (incomplete, corrupt or
#: incomplete ``config.json``): re-fetching or fixing the local model is the
#: remedy, not the network.
CODE_MODEL_INVALID = "ModelInvalidError"

_DOWNLOAD_MODULE_PREFIXES = ("huggingface_hub", "requests", "urllib3", "hf_xet", "httpx")


_channel: TextIO | None = None


@contextlib.contextmanager
def json_channel() -> Iterator[None]:
    """Reserve the current ``sys.stdout`` for :func:`emit`; route prints to stderr.

    Restores both on exit, so in-process callers (tests) are left untouched.
    """
    global _channel
    prev_channel, prev_stdout = _channel, sys.stdout
    _channel = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = prev_stdout
        _channel = prev_channel


def emit(event: dict[str, Any]) -> None:
    """Write one compact JSON object as a single stdout line and flush it."""
    out = _channel if _channel is not None else sys.stdout
    out.write(json.dumps(event, separators=(",", ":")) + "\n")
    out.flush()


def watch_stdin_eof(loop: Any, on_eof: Callable[[], None]) -> None:
    """Call ``on_eof`` on ``loop`` once stdin reaches EOF.

    A daemon thread blocks in ``os.read`` rather than using
    ``loop.connect_read_pipe``, which rejects a TTY stdin on macOS. Whatever the
    parent writes is discarded; only EOF matters.
    """
    stream = sys.stdin
    if stream is None:
        logger.warning("stt_server: --supervised without a stdin; parent-exit detection disabled")
        return
    try:
        fd = stream.fileno()
    except (OSError, ValueError):
        logger.warning("stt_server: stdin has no file descriptor; parent-exit detection disabled")
        return

    def _run() -> None:
        while True:
            try:
                chunk = os.read(fd, 4096)
            except InterruptedError:
                continue
            except BlockingIOError:
                # A non-blocking inherited pipe with nothing to read is not EOF:
                # wait for data or hang-up rather than stopping the server.
                try:
                    select.select([fd], [], [])
                except (OSError, ValueError):
                    chunk = b""
                else:
                    continue
            except OSError:
                chunk = b""
            if not chunk:
                break
        logger.info("stt_server: stdin closed (supervisor gone); stopping")
        try:
            loop.call_soon_threadsafe(on_eof)
        except RuntimeError:
            # Loop already closed: the server is shutting down anyway.
            pass

    threading.Thread(target=_run, name="stt-stdin-watch", daemon=True).start()


def _walk(exc: BaseException) -> Iterator[BaseException]:
    """``exc`` then its ``__cause__``/``__context__`` chain (cycle-safe)."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


def classify_startup_error(exc: BaseException) -> tuple[str, str]:
    """Map a fatal startup exception to ``(code, human message)``.

    Checked in priority order across the cause chain, so a wrapped
    ``OSError(EADDRINUSE)`` still classifies as a busy address.
    """
    chain = list(_walk(exc))
    message = str(exc) or type(exc).__name__
    for e in chain:
        if isinstance(e, ImportError):
            return CODE_MODULE_NOT_FOUND, message
    for e in chain:
        if isinstance(e, OSError) and e.errno == errno.EADDRINUSE:
            return CODE_ADDR_IN_USE, f"address already in use: {e}"
    for e in chain:
        if isinstance(e, OSError) and e.errno == errno.EACCES:
            return CODE_ACCESS_DENIED, f"permission denied: {e}"
    # A download failure is recognised by origin, never by errno alone: an
    # exception from a network library, or a builtin ConnectionError/TimeoutError
    # raised while a download phase was running (``mark_download_phase``). The
    # same builtins from the listener preflight (e.g. a UDS probe's ECONNRESET)
    # are NOT downloads and fall through to ``BackendStartError``.
    for e in chain:
        if is_config_error(e):
            return CODE_CONFIG, message
    in_download_phase = any(getattr(e, _DOWNLOAD_PHASE_ATTR, False) for e in chain)
    for e in chain:
        module = type(e).__module__ or ""
        if module.startswith(_DOWNLOAD_MODULE_PREFIXES) or (
            in_download_phase and isinstance(e, (ConnectionError, TimeoutError))
        ):
            return CODE_MODEL_DOWNLOAD, f"model download failed: {e}"
    if any(getattr(e, _MODEL_INVALID_ATTR, False) for e in chain):
        return CODE_MODEL_INVALID, f"model snapshot is unusable: {message}"
    # Anything else -- including a bare ValueError from a model library -- is a
    # backend start failure. A supervisor should treat a code it does not know
    # the same way.
    return CODE_BACKEND_START, message
