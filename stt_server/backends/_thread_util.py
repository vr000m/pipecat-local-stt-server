"""Shared daemon-thread → asyncio.Future bridge for decode backends.

Extracted from the per-backend copies (``MLXWhisperBackend`` and
``ParakeetBackend``) so the daemon-thread invariant lives in exactly one
place: a future fix to the bridge cannot silently drift between backends.
"""

from __future__ import annotations

import asyncio
import logging
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


class DecodeGuardMixin:
    """Decode-lock pair, in-flight counter and ``close()`` drain, shared by the
    MLX/Metal-backed backends (mlx_whisper, parakeet, nemotron, kyutai).

    ``_decode_lock`` (asyncio) serialises decodes across sessions on the event
    loop; ``_thread_lock`` (threading) serialises the actual MLX/Metal work on
    the daemon thread. The in-flight counter is incremented by
    ``_run_guarded_decode`` before the daemon thread starts and decremented in a
    ``finally`` by the stream's decode function (or by ``_run_guarded_decode``
    itself when the thread fails to start), so ``close()`` cannot observe
    "nothing in flight" between ``Thread.start()`` and the thread's first
    statement.

    Call ``_init_decode_guards()`` from ``__init__``.
    """

    def _init_decode_guards(self) -> None:
        self._decode_lock = asyncio.Lock()
        self._thread_lock = threading.Lock()
        self._inflight_count = 0
        self._inflight_cond = threading.Condition()

    def _mark_inflight_start(self) -> None:
        with self._inflight_cond:
            self._inflight_count += 1

    def _mark_inflight_end(self) -> None:
        with self._inflight_cond:
            self._inflight_count -= 1
            if self._inflight_count == 0:
                self._inflight_cond.notify_all()

    def _run_guarded_decode(
        self, func: Callable[[], _T], *, thread_name: str
    ) -> "asyncio.Future[_T]":
        """Mark a decode in flight, then run ``func`` on a daemon thread.

        ``func`` must call ``_mark_inflight_end()`` in a ``finally``. If the
        thread cannot be started (``Thread.start`` raising, e.g. the OS thread
        limit), ``func`` never runs, so the mark is undone here -- otherwise the
        count would never return to zero and every later ``close()`` would wait
        out its full drain window and report a stuck decode.
        """
        self._mark_inflight_start()
        try:
            return run_in_daemon_thread(func, thread_name=thread_name)
        except BaseException:
            self._mark_inflight_end()
            raise

    def _wait_inflight_drained(self, timeout_s: float | None) -> bool:
        with self._inflight_cond:
            return self._inflight_cond.wait_for(
                lambda: self._inflight_count == 0,
                timeout=timeout_s,
            )

    def idle(self) -> "asyncio.Future[None]":
        """A future that completes once no decode daemon thread is in flight.

        The optional ``idle()`` hook (``stt_server.backend.IdleAwareBackend``):
        a decode thread outlives a cancelled decode task, so the server cannot
        tell from its own tasks whether the backend is still running work. It
        keeps the single-server lock until this future completes. Never blocks
        the loop: the wait runs on a daemon thread, which exits when the count
        reaches zero (and is reaped with the process otherwise).
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        with self._inflight_cond:
            if self._inflight_count == 0:
                fut.set_result(None)
                return fut

        def _wait() -> None:
            self._wait_inflight_drained(None)
            try:
                loop.call_soon_threadsafe(_set_result_safe, fut, None)
            except RuntimeError:
                pass  # loop already closed: nobody is waiting any more

        threading.Thread(target=_wait, daemon=True, name="decode-idle-wait").start()
        return fut

    async def _drain_inflight(
        self, label: str, log: logging.Logger, timeout_s: float = 3.0
    ) -> None:
        """Give an in-flight decode a bounded window to finish flushing Metal
        work before the process exits (SIGTERM mid-decode can otherwise trip
        ``-[IOGPUMetalCommandBuffer validate]`` at exit). Logs and returns if
        the window expires."""
        drained = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._wait_inflight_drained(timeout_s)
        )
        if not drained:
            log.warning(
                "%s: in-flight decode did not finish within %.1fs; "
                "Metal assertion possible at process exit",
                label,
                timeout_s,
            )
