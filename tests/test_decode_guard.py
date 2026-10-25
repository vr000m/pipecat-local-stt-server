"""``DecodeGuardMixin``: the decode-lock pair, in-flight counter and close() drain
shared by the four MLX/Metal-backed backends (lean: nothing is loaded)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from stt_server.backends import DEFAULT_MLX_MODEL
from stt_server.backends._thread_util import DecodeGuardMixin
from stt_server.backends.kyutai import KyutaiBackend
from stt_server.backends.mlx_whisper import MLXWhisperBackend
from stt_server.backends.nemotron import NemotronBackend
from stt_server.backends.parakeet import ParakeetBackend

BACKENDS = [MLXWhisperBackend, ParakeetBackend, NemotronBackend, KyutaiBackend]


@pytest.mark.parametrize("cls", BACKENDS)
def test_backends_share_the_mixin_and_have_no_private_copies(cls):
    assert issubclass(cls, DecodeGuardMixin)
    for name in ("_mark_inflight_start", "_mark_inflight_end", "_wait_inflight_drained"):
        assert name not in vars(cls), f"{cls.__name__} re-defines {name}"


@pytest.mark.parametrize("cls", BACKENDS)
def test_inflight_counter_and_drain(cls):
    async def run():
        backend = cls()
        try:
            assert backend._wait_inflight_drained(0.01) is True
            backend._mark_inflight_start()
            backend._mark_inflight_start()
            assert backend._wait_inflight_drained(0.01) is False
            backend._mark_inflight_end()
            assert backend._wait_inflight_drained(0.01) is False
            backend._mark_inflight_end()
            assert backend._wait_inflight_drained(0.01) is True
            assert isinstance(backend._decode_lock, asyncio.Lock)
        finally:
            await backend.close()

    asyncio.run(run())


@pytest.mark.parametrize("cls", BACKENDS)
def test_close_times_out_and_warns_when_a_decode_never_finishes(cls, caplog, monkeypatch):
    async def run():
        backend = cls()
        monkeypatch.setattr(
            cls,
            "_drain_inflight",
            lambda self, label, log, timeout_s=3.0: DecodeGuardMixin._drain_inflight(
                self, label, log, 0.05
            ),
        )
        backend._mark_inflight_start()  # a decode that never completes
        try:
            await backend.close()
        finally:
            backend._mark_inflight_end()

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    assert any("in-flight decode did not finish" in r.message for r in caplog.records)


def test_cli_and_backend_share_one_default_mlx_model():
    from stt_server import __main__ as cli

    assert cli._DEFAULT_MLX_MODEL is DEFAULT_MLX_MODEL
    assert MLXWhisperBackend().model == DEFAULT_MLX_MODEL


@pytest.mark.parametrize("cls", BACKENDS)
def test_thread_start_failure_does_not_leak_the_inflight_count(cls, monkeypatch):
    """The count is taken before the daemon thread starts; if ``Thread.start``
    raises, the decode function (which owns the decrement) never runs."""
    import threading

    backend = cls()

    def boom(self):
        raise RuntimeError("can't start new thread")

    async def run():
        monkeypatch.setattr(threading.Thread, "start", boom)
        with pytest.raises(RuntimeError):
            await backend._run_guarded_decode(lambda: "x", thread_name="t")
        monkeypatch.undo()
        assert backend._inflight_count == 0
        assert backend._wait_inflight_drained(0) is True

        def decode():
            try:
                return "ok"
            finally:
                backend._mark_inflight_end()

        assert await backend._run_guarded_decode(decode, thread_name="t") == "ok"
        assert backend._inflight_count == 0

    try:
        asyncio.run(run())
    finally:
        getattr(backend, "abort", lambda: None)()
