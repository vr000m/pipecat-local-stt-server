"""Executable form of the README's "Adding a new backend" recipe.

Defines a trivial ``FakeBackend`` that satisfies the ``TranscriptionBackend``
structural protocol, plugs it into ``TranscriptionServer``, connects a real
``TranscriptionClient``, and round-trips a ``server.hello`` carrying
``backend.name == "fake"``.

If the backend protocol ever changes shape, this test either stays green (the
change is backward compatible for a minimal backend) or forces an explicit
edit here — which is the trip-wire that keeps the documented recipe honest.
"""

from __future__ import annotations

from typing import AsyncGenerator

from stt_server import TranscriptionClient
from stt_server import protocol as P
from stt_server.backend import BackendStream, TranscriptEvent, TranscriptionBackend
from stt_server.server import ServerConfig, TranscriptionServer


class _FakeStream:
    """Minimal commit-oriented stream: yields one delta + completed."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._ended = False
        self._cancelled = False

    async def feed(self, chunk: bytes) -> None:
        self._buf.extend(chunk)

    async def end(self) -> None:
        self._ended = True

    async def cancel(self) -> None:
        self._cancelled = True
        self._ended = True

    async def events(self) -> AsyncGenerator[TranscriptEvent, None]:
        if self._cancelled:
            return
        text = "fake-transcript"
        yield TranscriptEvent(kind="delta", text=text)
        yield TranscriptEvent(kind="completed", text=text)


class FakeBackend:
    """A trivial backend implementing the ``TranscriptionBackend`` protocol."""

    backend_name = "fake"
    model = "fake/model-id"

    async def start(self) -> None:
        return None

    async def open_stream(self, *, language: str | None = None) -> BackendStream:
        return _FakeStream()

    async def close(self) -> None:
        return None


def test_fake_backend_satisfies_protocol_structurally():
    # The @runtime_checkable Protocol must accept a minimal backend so the
    # README's "implement TranscriptionBackend" recipe is sound.
    assert isinstance(FakeBackend(), TranscriptionBackend)


async def test_fake_backend_round_trips_server_hello():
    srv = TranscriptionServer(
        FakeBackend(),
        ServerConfig(host="127.0.0.1", port=0, reject_browser_origins=False),
    )
    await srv.start()
    try:
        port = srv.listening_port()
        assert port is not None
        client = TranscriptionClient(host="127.0.0.1", port=port)
        hello = await client.connect()
        try:
            assert hello["type"] == P.EVT_SERVER_HELLO
            assert hello["protocol_version"] == P.PROTOCOL_VERSION
            # The backend identity carried by server.hello is the plug-in seam:
            # a new backend surfaces its name here with no protocol change.
            assert hello["backend"] == {"name": "fake", "model": "fake/model-id"}
        finally:
            await client.close()
    finally:
        await srv.shutdown()


async def test_fake_backend_drives_transcript_round_trip():
    srv = TranscriptionServer(
        FakeBackend(),
        ServerConfig(host="127.0.0.1", port=0, reject_browser_origins=False),
    )
    await srv.start()
    try:
        port = srv.listening_port()
        assert port is not None
        client = TranscriptionClient(host="127.0.0.1", port=port)
        await client.connect()
        try:
            await client.send_audio((0).to_bytes(2, "little") * 1600)  # 100 ms PCM16
            await client.commit()
            completed = None
            async for ev in client.events():
                if ev.get("type") == P.EVT_TRANSCRIPT_COMPLETED:
                    completed = ev
                    break
            assert completed is not None
            assert completed["transcript"] == "fake-transcript"
        finally:
            await client.close()
    finally:
        await srv.shutdown()
