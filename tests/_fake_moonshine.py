"""A fake ``moonshine_voice`` package for the Moonshine backend tests.

Shaped after the real 0.1.6 wheel (``Transcriber``, ``ModelArch``,
``download.get_model_for_language``, the native STT catalog and the per-model
download manifest with size and CRC32C per file) so the backend, its model-spec
resolution and its prefetch callables run without the wheel or the network.

Use :func:`install` from a fixture; it injects every fake submodule through
``monkeypatch.setitem(sys.modules, ...)``, points the library's cache at a
``tmp_path`` directory, and drops ``stt_server.backends.moonshine`` so it
re-imports against the fake. No audio or transcript text beyond short synthetic
placeholders is ever written.
"""

from __future__ import annotations

import base64
import enum
import os
import struct
import sys
import threading
import types
from pathlib import Path
from typing import Any

import numpy as np

SAMPLE_RATE_HZ = 16_000


class ModelArch(enum.IntEnum):
    TINY = 0
    BASE = 1
    TINY_STREAMING = 2
    BASE_STREAMING = 3
    SMALL_STREAMING = 4
    MEDIUM_STREAMING = 5


_A = ModelArch

# Language -> models in catalog order (first entry is the library's default). Taken from the
# plan's "Model catalog" bullet.
CATALOG_ORDER: dict[str, list[ModelArch]] = {
    "en": [_A.MEDIUM_STREAMING, _A.SMALL_STREAMING, _A.BASE, _A.TINY_STREAMING, _A.TINY],
    "es": [_A.SMALL_STREAMING, _A.TINY_STREAMING, _A.BASE],
    "de": [_A.SMALL_STREAMING, _A.TINY_STREAMING],
    "ja": [_A.SMALL_STREAMING, _A.TINY_STREAMING, _A.BASE, _A.TINY],
    "ar": [_A.TINY_STREAMING, _A.BASE],
    "zh": [_A.TINY_STREAMING, _A.BASE],
    "vi": [_A.TINY_STREAMING, _A.BASE],
    "tl": [_A.TINY_STREAMING],
    "ko": [_A.TINY],
    "uk": [_A.BASE],
}


def arch_name(arch: Any) -> str:
    """``"tiny-streaming"`` for a ``ModelArch``, an int value or an already-named arch."""
    if isinstance(arch, str):
        return arch
    return ModelArch(int(arch)).name.lower().replace("_", "-")


def arch_by_name(name: str) -> ModelArch:
    return ModelArch[name.upper().replace("-", "_")]


def build_catalog() -> dict[str, dict]:
    """The shape of ``moonshine_voice.download._stt_catalog()``."""
    return {
        code: {
            "english_name": code,
            "models": [
                {
                    "model_arch": arch,
                    "download_url": f"https://download.moonshine.ai/{code}/{arch_name(arch)}",
                    "is_default": i == 0,
                }
                for i, arch in enumerate(archs)
            ],
        }
        for code, archs in CATALOG_ORDER.items()
    }


# --- CRC32C (Castagnoli), base64 of the big-endian digest, as the library stores it --------


def _crc_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0x82F63B78 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_TABLE = _crc_table()


def crc32c_b64(data: bytes) -> str:
    crc = 0xFFFFFFFF
    for byte in data:
        crc = _TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    crc ^= 0xFFFFFFFF
    return base64.b64encode(struct.pack(">I", crc)).decode("ascii")


# --- transcript objects --------------------------------------------------------------------


class FakeLine:
    def __init__(self, text: str) -> None:
        self.text = text
        self.start_time = 0.0
        self.duration = 1.0
        self.is_complete = True


class FakeTranscript:
    def __init__(self, lines: list[str]) -> None:
        self.lines = [FakeLine(t) for t in lines]


class TranscriptEvent:
    """Base of the library's stream events (listener callbacks receive these)."""


class LineStarted(TranscriptEvent):
    def __init__(self, line: FakeLine) -> None:
        self.line = line


class LineTextChanged(TranscriptEvent):
    """An in-progress update; the backend must ignore it."""

    def __init__(self, line: FakeLine) -> None:
        self.line = line


class LineCompleted(TranscriptEvent):
    def __init__(self, line: FakeLine) -> None:
        self.line = line


class Error(TranscriptEvent):
    def __init__(self, error: BaseException | str) -> None:
        self.error = error


class FakeMoonshine:
    """Mutable behaviour and call records shared by every fake object."""

    def __init__(self, cache_root: Path) -> None:
        self.cache_root = cache_root
        self.catalog = build_catalog()
        self.version = "0.1.6"
        # behaviour
        self.lines: list[str] = ["hello from moonshine"]
        self.raise_on_transcribe: BaseException | None = None
        self.raise_on_download: BaseException | None = None
        self.raise_on_load: BaseException | None = None
        self.corrupt_downloads = False  # write same-size, wrong-content files
        self.gate: threading.Event | None = None  # block inside stream.stop() until set
        # Error events fired from stop(): replace (False) or follow (True) the lines.
        self.error_on_stop: str | None = None
        self.error_after_lines = False
        # Extra in-progress events fired before the completed lines.
        self.emit_in_progress = False
        # Reproduce moonshine-voice 0.1.6: the offline call adds a leading "Mm" line from
        # a Transcriber's 2nd call on. The stream path stays clean.
        self.offline_mm_defect = True
        # records
        self.downloads: list[tuple[str, str]] = []  # (language, arch name)
        self.loads: list[tuple[str, str]] = []  # (model_path, arch name)
        # One entry per stream decode (recorded when stop() starts).
        self.transcribe_calls: list[dict[str, Any]] = []
        # Every stream operation as (stream id, op), in call order.
        self.stream_ops: list[tuple[int, str]] = []
        self.created_streams: list[FakeStream] = []
        self.create_stream_kwargs: list[dict[str, Any]] = []
        # Calls to the offline API; the backend must never make any.
        self.offline_calls: list[dict[str, Any]] = []
        self._active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    # -- manifest ---------------------------------------------------------------------------

    @staticmethod
    def file_bytes(language: str, arch: Any, name: str) -> bytes:
        seed = f"{language}/{arch_name(arch)}/{name}".encode()
        return (seed * (3 + len(name)))[: 40 + 7 * len(name)]

    def group(self, language: str, arch: Any) -> dict:
        base = (
            f"https://download.moonshine.ai/model/{arch_name(arch)}-{language}/quantized_26_11_02"
        )
        files = []
        for name in ("encoder.onnx", "decoder.onnx", "tokenizer.bin"):
            data = self.file_bytes(language, arch, name)
            files.append(
                {
                    "name": name,
                    "url": f"{base}/{name}",
                    "size": len(data),
                    "checksum": crc32c_b64(data),
                    "checksum_type": "crc32c",
                }
            )
        return {"base_url": base, "files": files}

    def group_dir(self, language: str, arch: Any) -> Path:
        base = self.group(language, arch)["base_url"]
        return self.cache_root / base.replace("https://", "")

    def total_size(self, language: str, arch: Any) -> int:
        return sum(f["size"] for f in self.group(language, arch)["files"])

    def write_model(self, language: str, arch: Any, *, corrupt: bool = False) -> str:
        root = self.group_dir(language, arch)
        root.mkdir(parents=True, exist_ok=True)
        for f in self.group(language, arch)["files"]:
            data = self.file_bytes(language, arch, f["name"])
            if corrupt:
                data = bytes(b ^ 0x01 for b in data)  # same size, different CRC
            (root / f["name"]).write_bytes(data)
        return str(root)

    # -- transcribe bookkeeping ---------------------------------------------------------------

    def enter(self) -> None:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)

    def leave(self) -> None:
        with self._lock:
            self._active -= 1


class FakeStream:
    """The library's ``Stream``: ``stop()`` flushes and fires the listeners synchronously."""

    def __init__(self, transcriber: FakeTranscriber, update_interval: float) -> None:
        st = type(transcriber).state
        self._st = st
        self._transcriber = transcriber
        self.update_interval = update_interval
        self.id = len(st.created_streams)
        self.listeners: list[Any] = []
        self.audio_calls: list[tuple[list[float], int]] = []
        self.started = False
        self.stopped = False
        self.closed = False
        st.created_streams.append(self)
        st.stream_ops.append((self.id, "create_stream"))

    def add_listener(self, cb: Any) -> None:
        self._st.stream_ops.append((self.id, "add_listener"))
        self.listeners.append(cb)

    def start(self) -> None:
        self._st.stream_ops.append((self.id, "start"))
        self.started = True

    def add_audio(self, audio_data: Any, sample_rate: int = 16000) -> None:
        self._st.stream_ops.append((self.id, "add_audio"))
        self.audio_calls.append((audio_data, sample_rate))

    def _fire(self, ev: TranscriptEvent) -> None:
        for cb in list(self.listeners):
            cb(ev)

    def stop(self) -> None:
        st = self._st
        st.stream_ops.append((self.id, "stop"))
        st.enter()
        try:
            audio: list[float] = []
            for chunk, _rate in self.audio_calls:
                audio.extend(chunk)
            st.transcribe_calls.append(
                {
                    "audio": audio,
                    "sample_rate": self.audio_calls[0][1] if self.audio_calls else None,
                    "add_audio_calls": len(self.audio_calls),
                    "arch": arch_name(self._transcriber.model_arch),
                    "thread": threading.current_thread().name,
                }
            )
            if st.gate is not None:
                st.gate.wait()
            if st.raise_on_transcribe is not None:
                raise st.raise_on_transcribe
            self.stopped = True
            if st.error_on_stop is not None and not st.error_after_lines:
                self._fire(Error(st.error_on_stop))
                return
            for text in list(st.lines):
                line = FakeLine(text)
                if st.emit_in_progress:
                    line.is_complete = False
                    self._fire(LineStarted(line))
                    self._fire(LineTextChanged(line))
                    line.is_complete = True
                self._fire(LineCompleted(line))
            if st.error_on_stop is not None:
                self._fire(Error(st.error_on_stop))
        finally:
            st.leave()

    def close(self) -> None:
        self._st.stream_ops.append((self.id, "close"))
        self.closed = True


class FakeTranscriber:
    state: FakeMoonshine  # set by install()

    def __init__(self, model_path: str, model_arch: Any = None, *args: Any, **kwargs: Any):
        st = type(self).state
        if st.raise_on_load is not None:
            raise st.raise_on_load
        st.loads.append((str(model_path), arch_name(model_arch)))
        self.model_arch = model_arch
        self.closed = False

    def create_stream(self, update_interval: float = 0.5, flags: int = 0) -> FakeStream:
        st = type(self).state
        st.create_stream_kwargs.append({"update_interval": update_interval})
        return FakeStream(self, update_interval)

    def transcribe_without_streaming(
        self, audio_data: Any, sample_rate: int = 16000, flags: int = 0
    ) -> FakeTranscript:
        """The offline API. The backend must not use it: calls are recorded and the
        ``fake`` fixtures fail the test at teardown if any happened. Faithful to
        0.1.6, a Transcriber's 2nd and later calls get a spurious leading "Mm" line."""
        st = type(self).state
        self._offline_n = getattr(self, "_offline_n", 0) + 1
        st.offline_calls.append({"audio": audio_data, "sample_rate": sample_rate})
        lines = list(st.lines)
        if st.offline_mm_defect and self._offline_n >= 2:
            lines = ["Mm", *lines]
        return FakeTranscript(lines)

    def close(self) -> None:
        self.closed = True


def install(monkeypatch, cache_root: Path) -> FakeMoonshine:
    """Inject the fake ``moonshine_voice`` package and return its controller."""
    st = FakeMoonshine(cache_root)
    cache_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MOONSHINE_VOICE_CACHE", str(cache_root))

    class Transcriber(FakeTranscriber):
        pass

    Transcriber.state = st

    pkg = types.ModuleType("moonshine_voice")
    pkg.__path__ = []  # type: ignore[attr-defined]
    pkg.__version__ = st.version  # type: ignore[attr-defined]

    # moonshine_voice.download -------------------------------------------------------------
    dl = types.ModuleType("moonshine_voice.download")

    def _stt_catalog() -> dict:
        return st.catalog

    def find_model_info(language: str = "en", model_arch: Any = None) -> dict:
        if language not in st.catalog:
            raise ValueError(f"Language not found: {language}")
        models = st.catalog[language]["models"]
        if model_arch is None:
            info = dict(models[0])
        else:
            matches = [m for m in models if int(m["model_arch"]) == int(model_arch)]
            if not matches:
                raise ValueError(f"Model not found for language: {language}")
            info = dict(matches[0])
        info["language"] = language
        return info

    def _stt_dependency_manifest(language: str, model_arch: Any, **_kw: Any) -> dict:
        return {"groups": [st.group(language, model_arch)]}

    def _primary_stt_group(model_info: dict, **_kw: Any) -> dict:
        return st.group(model_info["language"], model_info["model_arch"])

    def _cache_root_dir(cache_root_arg: Any = None) -> Path:
        # As the real one: an explicit root, else ``download_file.get_cache_dir()``.
        if cache_root_arg is not None:
            return Path(cache_root_arg).resolve()
        return Path(os.environ.get("MOONSHINE_VOICE_CACHE", st.cache_root))

    def download_model_from_info(model_info: dict, *, cache_root=None, on_progress=None, **_kw):
        language, arch = model_info["language"], model_info["model_arch"]
        if st.raise_on_download is not None:
            raise st.raise_on_download
        st.downloads.append((language, arch_name(arch)))
        total = max(1, st.total_size(language, arch))
        done = 0
        root = Path(dl._cache_root_dir(cache_root))  # a module-global lookup, as the real one
        target = root / st.group(language, arch)["base_url"].replace("https://", "")
        target.mkdir(parents=True, exist_ok=True)
        for f in st.group(language, arch)["files"]:
            data = st.file_bytes(language, arch, f["name"])
            if st.corrupt_downloads:
                data = bytes(b ^ 0x01 for b in data)
            dest = target / f["name"]
            # The real library's cache fast path is size-only (CRC32C is checked only
            # after a fresh download): a same-size corrupt file is kept as it is.
            if not (dest.is_file() and dest.stat().st_size == len(data)):
                # ``download_file`` after a fresh download: a bad CRC32C deletes the
                # temp copy (``dest`` is never written) and raises naming only the file.
                actual = crc32c_b64(data)
                if actual != f["checksum"]:
                    raise ValueError(
                        f"CRC32C mismatch for {f['name']}: expected {f['checksum']}, got {actual}"
                    )
                dest.write_bytes(data)
            done += f["size"]
            if on_progress is not None:
                on_progress(done / total, f["name"])
        return str(target), arch

    def get_model_for_language(
        wanted_language: str = "en",
        wanted_model_arch: Any = None,
        *,
        cache_root: Any = None,
        on_progress: Any = None,
        include_word_timestamps: bool = False,
    ):
        info = find_model_info(wanted_language, wanted_model_arch)
        return download_model_from_info(info, cache_root=cache_root, on_progress=on_progress)

    def supported_languages() -> list[str]:
        return list(st.catalog)

    for fn in (
        _stt_catalog,
        find_model_info,
        _stt_dependency_manifest,
        _primary_stt_group,
        _cache_root_dir,
        download_model_from_info,
        get_model_for_language,
        supported_languages,
    ):
        setattr(dl, fn.__name__, fn)
    dl.ModelArch = ModelArch  # type: ignore[attr-defined]

    # moonshine_voice.download_file --------------------------------------------------------
    dfile = types.ModuleType("moonshine_voice.download_file")

    def crc32c_file(path: Any) -> str | None:
        return crc32c_b64(Path(path).read_bytes())

    def get_cache_dir(app_name: str = "moonshine_voice") -> Path:
        return Path(os.environ.get("MOONSHINE_VOICE_CACHE", st.cache_root))

    dfile.crc32c_file = crc32c_file  # type: ignore[attr-defined]
    dfile.get_cache_dir = get_cache_dir  # type: ignore[attr-defined]

    # moonshine_voice.moonshine_api / transcriber -----------------------------------------
    api = types.ModuleType("moonshine_voice.moonshine_api")
    api.ModelArch = ModelArch  # type: ignore[attr-defined]
    trmod = types.ModuleType("moonshine_voice.transcriber")
    trmod.Transcriber = Transcriber  # type: ignore[attr-defined]
    trmod.TranscriptEvent = TranscriptEvent  # type: ignore[attr-defined]
    trmod.LineStarted = LineStarted  # type: ignore[attr-defined]
    trmod.LineTextChanged = LineTextChanged  # type: ignore[attr-defined]
    trmod.LineCompleted = LineCompleted  # type: ignore[attr-defined]
    trmod.Error = Error  # type: ignore[attr-defined]

    pkg.ModelArch = ModelArch  # type: ignore[attr-defined]
    pkg.Transcriber = Transcriber  # type: ignore[attr-defined]
    pkg.download = dl  # type: ignore[attr-defined]
    pkg.download_file = dfile  # type: ignore[attr-defined]
    pkg.moonshine_api = api  # type: ignore[attr-defined]
    pkg.transcriber = trmod  # type: ignore[attr-defined]
    # the library re-exports these lazily at top level too
    pkg.get_model_for_language = get_model_for_language  # type: ignore[attr-defined]
    pkg.find_model_info = find_model_info  # type: ignore[attr-defined]

    for name, mod in {
        "moonshine_voice": pkg,
        "moonshine_voice.download": dl,
        "moonshine_voice.download_file": dfile,
        "moonshine_voice.moonshine_api": api,
        "moonshine_voice.transcriber": trmod,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    # ``filelock`` (a hard dependency of moonshine-voice: its per-file download lock, which
    # prefetch's repair shares) is real wherever it is installed; a client-only test env
    # has none, so a single-process stand-in takes its place there.
    try:
        import filelock  # noqa: F401
    except ImportError:
        shim = types.ModuleType("filelock")
        shim.FileLock = _ThreadFileLock  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "filelock", shim)
    monkeypatch.delitem(sys.modules, "stt_server.backends.moonshine", raising=False)
    return st


class _ThreadFileLock:
    """In-process stand-in for ``filelock.FileLock`` (one lock per path)."""

    _locks: dict[str, threading.Lock] = {}

    def __init__(self, path: Any, **_kw: Any) -> None:
        self._lock = self._locks.setdefault(str(path), threading.Lock())

    def __enter__(self) -> _ThreadFileLock:
        self._lock.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self._lock.release()


# --- audio helpers ---------------------------------------------------------------------------


def pcm(num_samples: int, *, amplitude: int = 800) -> bytes:
    """Non-silent synthetic PCM16LE (a quiet sine, never speech)."""
    t = np.arange(num_samples)
    return (np.sin(2 * np.pi * 220 * t / SAMPLE_RATE_HZ) * amplitude).astype("<i2").tobytes()


def pcm_seconds(seconds: float) -> bytes:
    return pcm(int(seconds * SAMPLE_RATE_HZ))
