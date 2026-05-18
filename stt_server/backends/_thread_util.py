"""Shared daemon-thread → asyncio.Future bridge for decode backends.

Extracted from the per-backend copies (``MLXWhisperBackend`` and
``ParakeetBackend``) so the daemon-thread invariant lives in exactly one
place: a future fix to the bridge cannot silently drift between backends.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Callable, TypeVar

_T = TypeVar("_T")


def run_in_daemon_thread(func: Callable[[], _T], *, thread_name: str) -> "asyncio.Future[_T]":
    """Run ``func`` on a fresh daemon thread and return an asyncio Future.

    Unlike ``loop.run_in_executor`` with ``ThreadPoolExecutor``, the thread is
    a daemon and is NOT registered with the ``concurrent.futures`` atexit
    handler — so a stuck blocking decode cannot block process exit when
    ``session.cancel`` / ``shutdown()`` fires while a decode is running.
    MLX/Metal has no cooperative cancellation hook; the only honest way to
    bound shutdown is to let the OS reap the thread.

    ``thread_name`` names the OS thread (e.g. ``mlx-decode`` /
    ``parakeet-decode``) so a hung decode is identifiable in a stack dump.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[_T] = loop.create_future()

    def _runner() -> None:
        try:
            result = func()
        except BaseException as exc:  # noqa: BLE001 — marshal across threads
            loop.call_soon_threadsafe(_set_exception_safe, fut, exc)
        else:
            loop.call_soon_threadsafe(_set_result_safe, fut, result)

    threading.Thread(target=_runner, daemon=True, name=thread_name).start()
    return fut


def _set_result_safe(fut: "asyncio.Future", value) -> None:
    if not fut.done():
        fut.set_result(value)


def _set_exception_safe(fut: "asyncio.Future", exc: BaseException) -> None:
    if not fut.done():
        fut.set_exception(exc)
