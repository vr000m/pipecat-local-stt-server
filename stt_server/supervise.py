"""Machine-readable lifecycle channel for a supervising parent process.

Used by ``serve --supervised`` and ``prefetch --json`` so an app (for example a
macOS menu bar app managing local speech services) can drive the CLI without
scraping log text. Mirrors ``tts_server.supervise`` in
pipecat-local-tts-server, so one supervisor handles both servers.

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
import sys
import threading
from collections.abc import Callable, Iterator
from typing import Any, TextIO

logger = logging.getLogger("stt_server")

# Stable ``code`` values for ``{"event": "error"}`` lines (part of the contract).
CODE_MODULE_NOT_FOUND = "ModuleNotFoundError"
CODE_ADDR_IN_USE = "EADDRINUSE"
CODE_ACCESS_DENIED = "EACCES"
CODE_MODEL_DOWNLOAD = "ModelDownloadError"
CODE_CONFIG = "ConfigError"
CODE_BACKEND_START = "BackendStartError"

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
    for e in chain:
        module = type(e).__module__ or ""
        if module.startswith(_DOWNLOAD_MODULE_PREFIXES) or isinstance(
            e, (ConnectionError, TimeoutError)
        ):
            return CODE_MODEL_DOWNLOAD, f"model download failed: {e}"
    if isinstance(exc, ValueError):
        # ServerConfig and socket-directory enforcement reject bad endpoints
        # with ValueError before anything binds.
        return CODE_CONFIG, message
    return CODE_BACKEND_START, message
