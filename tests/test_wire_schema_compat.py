"""Old-vs-new wire-frame schema comparison.

Proves the extracted ``stt_server`` package emits ``server.hello`` and
``server.status`` frames whose shape is identical to the ``stt-extraction-base``
version of the package (the pre-extraction Koda commit). This is the executable
form of the wire-protocol-invariance requirement: the protocol is frozen at
``PROTOCOL_VERSION == "0.1"`` and neither emitter may drift.

Mechanism (true side-by-side dual-import):

- The pre-extraction package is materialised at test-collection time by
  ``git archive``-ing ``stt-extraction-base:stt_server`` out of the Koda repo
  into a private temp directory, then imported under the synthetic top-level
  name ``stt_server_base`` via an isolated importer. The base ``shared/``
  helpers (``shared/env.py``, ``shared/text_quality.py``) are archived
  alongside so the base package's ``from shared...`` imports resolve.
- Both the extracted (in-repo) package and the base package boot an
  ``EchoBackend`` server, a client connects, and we capture ``server.hello``
  and ``server.status``.
- Dynamic fields (``event_id``, ``session_id``, ``pid``, ``uptime_seconds``,
  ``rss_bytes``) are normalised to a ``("__type__", typename)`` marker; all
  other (stable) fields are compared by value. The two normalised frames must
  be deep-equal.

If the base package cannot be materialised (e.g. the ``stt-extraction-base``
tag is unreachable from the test environment, or ``git`` is unavailable), the
dual-import body is skipped, but a self-consistency check on the extracted
package still runs so the file is never silently a no-op.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

# Extracted (in-repo) package under test.
import stt_server as stt_server_new
from stt_server import protocol as P_new
from stt_server.server import ServerConfig as ServerConfig_new
from stt_server.server import TranscriptionServer as TranscriptionServer_new

# The Koda repo that holds the ``stt-extraction-base`` tag. The extraction
# clone is a sibling of the Koda checkout; allow an override for CI.
_KODA_REPO = os.environ.get(
    "KODA_REPO_PATH",
    str(Path.home() / "Code" / "pipecat-ai" / "koda-pipecat"),
)
_BASE_TAG = "stt-extraction-base"

# Fields whose values are non-deterministic across runs/processes. Compared by
# type only, not by value.
_DYNAMIC_FIELDS = {"event_id", "session_id", "pid", "uptime_seconds", "rss_bytes"}


def _normalize(frame: dict) -> dict:
    """Replace dynamic scalar fields with a type marker, recursively."""
    out: dict = {}
    for key, value in frame.items():
        if key in _DYNAMIC_FIELDS:
            out[key] = ("__type__", type(value).__name__)
        elif isinstance(value, dict):
            out[key] = _normalize(value)
        else:
            out[key] = value
    return out


async def _capture_frames(transcription_server, server_config, client_factory) -> dict:
    """Boot a server with EchoBackend, capture normalized hello + status."""

    raise AssertionError  # replaced below per-call; see _capture_with


def _materialize_base_package(dest: Path) -> bool:
    """git-archive ``stt-extraction-base`` package + shared helpers into ``dest``.

    Returns True on success, False if the tag/repo is unavailable.
    """
    repo = Path(_KODA_REPO)
    if not (repo / ".git").exists():
        return False
    # Confirm the tag resolves before attempting the archives.
    rc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{_BASE_TAG}^{{commit}}"],
        capture_output=True,
    )
    if rc.returncode != 0:
        return False
    try:
        for spec in (f"{_BASE_TAG}:stt_server", f"{_BASE_TAG}:shared"):
            archive = subprocess.run(
                ["git", "-C", str(repo), "archive", "--format=tar", spec],
                capture_output=True,
                check=True,
            )
            subdir = dest / spec.split(":", 1)[1]
            subdir.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["tar", "-x", "-C", str(subdir)],
                input=archive.stdout,
                check=True,
            )
    except subprocess.CalledProcessError:
        return False
    return True


def _import_base_stt_server(base_root: Path) -> ModuleType | None:
    """Import the archived base package as top-level ``stt_server_base``.

    The base package imports ``from shared.env import ...`` and
    ``from shared.text_quality import ...`` at module load (mlx backend only,
    not on the hello/status path) and the base ``stt_server`` uses relative
    imports internally, so we add ``base_root`` to ``sys.path`` and import the
    real ``stt_server`` package name from it under an isolated copy.

    To avoid clobbering the already-imported extracted ``stt_server``, we load
    the base package's ``__init__``, ``protocol``, ``backend`` and ``server``
    modules under an aliased package name ``stt_server_base``.
    """
    pkg_init = base_root / "stt_server" / "__init__.py"
    if not pkg_init.exists():
        return None

    # Make ``shared`` importable for the base package's mlx backend import path
    # (defensive — hello/status don't touch it, but a top-level import might).
    if str(base_root) not in sys.path:
        sys.path.insert(0, str(base_root))

    alias = "stt_server_base"
    spec = importlib.util.spec_from_file_location(
        alias,
        pkg_init,
        submodule_search_locations=[str(base_root / "stt_server")],
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(alias, None)
        return None
    return module


async def _capture_with(transcription_server_cls, server_config_cls, client_cls, hello_type):
    """Boot an EchoBackend server with the given package's classes; capture frames."""
    from importlib import import_module

    # Resolve EchoBackend from the same package family the server class came from.
    backend_mod = import_module(transcription_server_cls.__module__.rsplit(".", 1)[0])
    EchoBackend = backend_mod.EchoBackend

    srv = transcription_server_cls(
        EchoBackend(),
        server_config_cls(host="127.0.0.1", port=0, reject_browser_origins=False),
    )
    await srv.start()
    try:
        port = srv.listening_port()
        client = client_cls(host="127.0.0.1", port=port)
        hello = await client.connect()
        try:
            await client.status()
            status = None
            async for ev in client.events():
                if ev.get("type") == "server.status":
                    status = ev
                    break
            assert status is not None, "server closed before server.status reply"
            return {"hello": _normalize(hello), "status": _normalize(status)}
        finally:
            await client.close()
    finally:
        await srv.shutdown()


def _capture_new() -> dict:
    return asyncio.run(
        _capture_with(
            TranscriptionServer_new,
            ServerConfig_new,
            stt_server_new.TranscriptionClient,
            P_new.EVT_SERVER_HELLO,
        )
    )


def test_extracted_hello_status_self_consistent():
    """Extracted package emits the expected stable hello/status shape.

    Runs unconditionally so this file is never a silent no-op even when the
    base package cannot be materialised.
    """
    frames = _capture_new()
    hello = frames["hello"]
    status = frames["status"]

    assert hello["type"] == "server.hello"
    assert hello["protocol_version"] == "0.1"
    assert hello["capabilities"] == {
        "binary_audio": True,
        "base64_audio_append": True,
        "server_vad": False,
    }
    assert hello["audio"] == {"format": "pcm16", "rate": 16000, "channels": 1}
    assert hello["backend"] == {"name": "echo", "model": None}
    assert hello["event_id"] == ("__type__", "str")

    assert status["type"] == "server.status"
    assert status["session_id"] == ("__type__", "str")
    assert status["backend"] == {"name": "echo", "model": None}
    assert status["queue_depth"] == 0
    assert status["uncommitted_bytes"] == 0
    assert status["uptime_seconds"] == ("__type__", "float")
    assert status["pid"] == ("__type__", "int")
    assert status["rss_bytes"] == ("__type__", "int")


def test_old_vs_new_wire_frame_schema_match():
    """Normalized server.hello/server.status are deep-equal old vs new."""
    with tempfile.TemporaryDirectory(prefix="stt-base-") as tmp:
        base_root = Path(tmp)
        if not _materialize_base_package(base_root):
            pytest.skip(
                f"{_BASE_TAG} not reachable from {_KODA_REPO}; "
                "dual-import comparison skipped (self-consistency test still ran)"
            )
        base_pkg = _import_base_stt_server(base_root)
        if base_pkg is None:
            pytest.skip("could not import archived base stt_server package")

        try:
            base_server_mod = importlib.import_module("stt_server_base.server")
            base_frames = asyncio.run(
                _capture_with(
                    base_server_mod.TranscriptionServer,
                    base_server_mod.ServerConfig,
                    base_pkg.TranscriptionClient,
                    "server.hello",
                )
            )
        finally:
            # Clean the aliased modules so they don't leak into other tests.
            for name in list(sys.modules):
                if name == "stt_server_base" or name.startswith("stt_server_base."):
                    sys.modules.pop(name, None)
            if str(base_root) in sys.path:
                sys.path.remove(str(base_root))

        new_frames = _capture_new()

        assert base_frames["hello"] == new_frames["hello"], (
            "server.hello schema drifted between stt-extraction-base and extracted package"
        )
        assert base_frames["status"] == new_frames["status"], (
            "server.status schema drifted between stt-extraction-base and extracted package"
        )
