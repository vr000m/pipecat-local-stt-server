"""Moonshine Voice ASR backend (``moonshine-voice``: ONNX on CPU, 8 languages).

Commit-oriented, like ``nemotron``: PCM16LE is buffered until ``end()``, then one
one-shot stream decode runs (create stream, one ``add_audio``, ``stop()``) and the
stream emits one ``delta`` equal to ``completed`` (empty decode: ``completed`` only).
The wire protocol and ``server.py`` session handling are unchanged.

``--model`` is a spec, not a repo id: ``[<arch>][@<lang>[,<lang>...]]`` (default
``DEFAULT_MOONSHINE_MODEL``, ``medium-streaming@en``). The constructor stores it
verbatim and imports nothing; ``start()`` imports the package, validates the
spec against the live catalog and the licence guard (``ConfigError``) and logs
the resolved language-to-arch map. The served set is fixed at startup: the
transcriber cache holds exactly those languages (memory is the sum of the listed
models) and the downloader is never called for any other language.

Licence guard: only MIT models are served. Streaming archs are MIT for every
language; non-streaming archs only for ``en`` (the upstream LICENSE enumerates
the non-MIT models: legacy non-streaming non-English ones).

Language contract: the server does no validation; each backend decides.
Moonshine has no language identification, so ``None``/blank/``"auto"`` means
the fallback language, silently: ``en`` if served, else the first listed (a
documented departure from the uniform "auto means detect" contract). Otherwise
the value is lowercased and its region stripped (``en-US`` -> ``en``,
``zh_CN`` -> ``zh``). An explicit language outside the served set (or an
invalid value) is logged with ``logger.error`` and falls back the same way; it
never raises and nothing is downloaded for it.

Pin policy: HF SHAs do not apply. The pin is the exact ``moonshine-voice``
version, which fixes the model URLs; per-model directory names come from the
package manifest, never a constant.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import threading
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from ..backend import TranscriptEvent
from ..errors import ConfigError
from ..protocol import AUDIO_SAMPLE_RATE_HZ
from ..snapshot import SnapshotSpec, download_phase, mark_model_invalid
from . import DEFAULT_MOONSHINE_MODEL
from ._thread_util import DecodeGuardMixin

logger = logging.getLogger("stt_server.backends.moonshine")

#: Preferred fallback language (used for ``None``/blank/``"auto"`` when served).
DEFAULT_MOONSHINE_LANGUAGE = "en"

#: The ``moonshine-voice`` version pinned in ``pyproject.toml`` (the model pin).
MOONSHINE_VOICE_VERSION = "0.1.6"

_PROGRESS_INTERVAL_S = 0.3


# --- pure helpers (table-tested against a fake catalog) -------------------------


def _arch_name(arch: Any) -> str:
    """``ModelArch.MEDIUM_STREAMING`` -> ``"medium-streaming"`` (strings pass through)."""
    name = getattr(arch, "name", None)
    if not isinstance(name, str):
        name = str(arch)
    return name.strip().lower().replace("_", "-")


def _is_mit(code: str, arch_name: str) -> bool:
    """Licence guard: streaming archs for any language, non-streaming only for ``en``."""
    return arch_name.endswith("-streaming") or code == "en"


def parse_model_spec(spec: str | None) -> tuple[str | None, tuple[str, ...]]:
    """Parse ``[<arch>][@<lang>[,<lang>...]]`` into ``(arch | None, languages)``.

    Pure: no catalog access. Whitespace is stripped and case lowercased; an
    omitted language list means ``("en",)``; duplicates are dropped (order kept).
    A malformed spec (not a string, a second ``@``, an empty language entry)
    raises ``ConfigError``.
    """
    if spec is None:
        spec = ""
    if not isinstance(spec, str):
        raise ConfigError(f"moonshine model spec must be a string, got {type(spec).__name__}")
    arch_part, sep, lang_part = spec.partition("@")
    if "@" in lang_part:
        raise ConfigError(f"invalid moonshine model spec {spec!r}: more than one '@'")
    arch = arch_part.strip().lower() or None
    langs: list[str] = []
    if sep and lang_part.strip():
        for raw in lang_part.split(","):
            lang = raw.strip().lower()
            if not lang:
                raise ConfigError(f"invalid moonshine model spec {spec!r}: empty language entry")
            if lang not in langs:
                langs.append(lang)
    if not langs:
        langs.append(DEFAULT_MOONSHINE_LANGUAGE)
    return arch, tuple(langs)


def resolve_arch(code: str, requested: str | None, catalog: dict) -> Any:
    """The catalog ``model_arch`` to serve for language ``code``.

    ``catalog`` maps language code -> ``{"models": [{"model_arch": ...}, ...]}``
    in catalog order (the shape of ``moonshine_voice.download._stt_catalog()``).
    An omitted ``requested`` arch takes the first MIT-allowed entry. Raises
    ``ConfigError`` for an unknown language, an arch the catalog does not list
    for it, or a non-MIT combination (licence guard).
    """
    entry = catalog.get(code) if isinstance(catalog, dict) else None
    if entry is None:
        known = ", ".join(sorted(catalog))
        raise ConfigError(f"moonshine: unknown language {code!r} (catalog: {known})")
    archs = [m["model_arch"] for m in entry["models"]]
    if requested is None:
        for arch in archs:
            if _is_mit(code, _arch_name(arch)):
                return arch
        raise ConfigError(
            f"moonshine: no MIT-licensed model for language {code!r}; it cannot be served"
        )
    wanted = requested.strip().lower()
    for arch in archs:
        if _arch_name(arch) == wanted:
            if not _is_mit(code, wanted):
                raise ConfigError(
                    f"moonshine: {wanted!r} for {code!r} is not MIT-licensed "
                    "(only streaming models, or any model for 'en', are served)"
                )
            return arch
    listed = ", ".join(_arch_name(a) for a in archs)
    raise ConfigError(f"moonshine: no {wanted!r} model for language {code!r} (available: {listed})")


def resolve_language(lang: Any, served: Any) -> tuple[str, str | None]:
    """Map a client ``language`` onto a served code: ``(code, note)``.

    ``None``/blank/``"auto"`` -> the fallback (``en`` if served, else the first
    listed) with no note: it is the normal default, not an error. Otherwise
    lowercase and strip the region (``-`` or ``_`` suffix). A code outside
    ``served`` (or a non-string value) returns the same fallback plus an error
    ``note`` for the caller to ``logger.error``; ``note`` is ``None`` when the
    language is served or defaulted. Never raises.
    """
    served_list = list(served)
    if DEFAULT_MOONSHINE_LANGUAGE in served_list or not served_list:
        fallback = DEFAULT_MOONSHINE_LANGUAGE
    else:
        fallback = served_list[0]
    if lang is None:
        return fallback, None
    if isinstance(lang, str):
        text = lang.strip().lower()
        if not text or text == "auto":
            return fallback, None
        code = text.replace("_", "-").split("-", 1)[0]
    else:
        code = ""
    if code in served_list:
        return code, None
    shown = repr(lang if isinstance(lang, str) else type(lang).__name__)[:48]
    note = (
        f"language {shown} is not served (served: {', '.join(served_list) or 'none'}); "
        f"falling back to {fallback!r}"
    )
    return fallback, note


def build_served(langs: tuple[str, ...], arch: str | None, catalog: dict) -> dict[str, Any]:
    """Language -> catalog ``model_arch`` for every served language (``ConfigError`` if invalid)."""
    return {code: resolve_arch(code, arch, catalog) for code in langs}


# --- package access ------------------------------------------------------------


def _import_package():
    """``(moonshine_voice, moonshine_voice.download)``; an actionable error if missing."""
    try:
        import moonshine_voice  # type: ignore
        from moonshine_voice import download  # type: ignore
    except ImportError as exc:
        # ImportError (not just ModuleNotFoundError): a present-but-skewed
        # package raises a plain ImportError, which would otherwise escape
        # _cmd_serve and crash-loop as a bare traceback.
        missing = getattr(exc, "name", None) or "moonshine_voice"
        raise ModuleNotFoundError(
            f"the 'moonshine' extra is not installed or failed to import "
            f"({missing}) — run: uv sync --extra moonshine --inexact"
        ) from exc
    # The private API used here (``_stt_catalog``, the manifest) and the model
    # URLs are fixed by the exact pin; an older wheel imports but lacks them. The
    # justfile ``_ensure-extra`` probe applies the same check.
    installed = getattr(moonshine_voice, "__version__", None)
    if installed != MOONSHINE_VOICE_VERSION:
        raise ImportError(
            f"moonshine-voice {installed} is installed, this server needs exactly "
            f"{MOONSHINE_VOICE_VERSION} — run: uv sync --extra moonshine --inexact",
            name="moonshine_voice",
        )
    return moonshine_voice, download


# --- backend -------------------------------------------------------------------


class _MoonshineStream:
    def __init__(
        self,
        language: str | None,
        decode_lock: asyncio.Lock,
        thread_lock: threading.Lock,
        backend: MoonshineBackend,
    ) -> None:
        self._language = language
        self._buf = bytearray()
        self._ended = False
        self._cancelled = False
        self._result: str | None = None
        self._decode_lock = decode_lock
        self._thread_lock = thread_lock
        self._backend = backend

    async def feed(self, chunk: bytes) -> None:
        if self._cancelled:
            return
        self._buf.extend(chunk)

    async def end(self) -> None:
        if self._ended or self._cancelled:
            return
        self._ended = True
        # Same pattern as ``_NemotronStream``: the asyncio lock orders decodes on
        # the loop; the threading lock inside ``_decode_sync`` keeps a second
        # decode thread blocked even when this awaiter is cancelled.
        async with self._decode_lock:
            if self._cancelled:
                return
            self._result = await self._backend._run_guarded_decode(
                self._decode_sync, thread_name="moonshine-decode"
            )

    async def cancel(self) -> None:
        self._cancelled = True
        self._ended = True

    def _decode_sync(self) -> str:
        try:
            # Whole-sample frames only: a stray odd byte would make
            # ``frombuffer`` raise.
            pcm = bytes(self._buf)
            pcm = pcm[: len(pcm) // 2 * 2]
            if not pcm:
                return ""
            import numpy as np
            from moonshine_voice.transcriber import Error, LineCompleted  # type: ignore

            texts: list[str] = []
            errors: list[Any] = []

            def on_event(ev: Any) -> None:
                if isinstance(ev, Error):
                    errors.append(ev)
                elif isinstance(ev, LineCompleted):
                    texts.append((getattr(ev.line, "text", "") or "").strip())

            # The whole decode runs under the backend-scope threading lock, so
            # decodes across sessions serialise (concurrent runs gave no speedup
            # and, with two transcribers, differing output). Language
            # resolution and the lazy model load happen inside the lock too.
            with self._thread_lock:
                # PCM16LE -> float32 in [-1, 1]; the library takes a Python sequence
                # of floats, hence ``.tolist()`` (~16x the PCM size), built under
                # the lock so queued decodes do not each hold one.
                samples = (np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0).tolist()
                transcriber = self._backend._transcriber_for(self._language)
                # One-shot stream, not ``transcribe_without_streaming``: repeated
                # offline calls on one Transcriber add a spurious leading "Mm"
                # line from the 2nd call on (moonshine-voice 0.1.6). ``stop()``
                # flushes and fires the listener callbacks on this thread.
                stream = transcriber.create_stream(update_interval=0.3)
                try:
                    stream.add_listener(on_event)
                    stream.start()
                    stream.add_audio(samples, AUDIO_SAMPLE_RATE_HZ)
                    stream.stop()
                finally:
                    close = getattr(stream, "close", None)
                    if callable(close):
                        close()
            if errors:
                # Never emit a truncated ``completed`` after a failed flush; the
                # message carries no transcript text or audio.
                # The library's own ``Error.error`` is kept as the cause so the
                # server log shows why; the message itself stays generic.
                cause = getattr(errors[0], "error", None)
                if cause is not None and not isinstance(cause, BaseException):
                    cause = RuntimeError(str(cause))
                raise RuntimeError("moonshine stream decode reported an error") from cause
            return " ".join(t for t in texts if t)
        finally:
            self._backend._mark_inflight_end()

    async def events(self) -> AsyncGenerator[TranscriptEvent, None]:
        if self._cancelled or self._result is None:
            return
        text = self._result
        # Empty-decode contract: ``delta`` only on non-empty text, ``completed``
        # always; the single ``delta`` equals ``completed``.
        if text:
            yield TranscriptEvent(kind="delta", text=text)
        yield TranscriptEvent(kind="completed", text=text)


class MoonshineBackend(DecodeGuardMixin):
    """ASR backend backed by the ``moonshine-voice`` package (one-shot stream decode)."""

    backend_name = "moonshine"

    def __init__(self, *, model: str = DEFAULT_MOONSHINE_MODEL) -> None:
        # Stored verbatim: no import and no validation here (the registry and
        # missing-extra tests construct backends without the package); ``start()``
        # validates.
        self._model_id = model
        self.model = model
        # language -> catalog ``model_arch``; empty until configured.
        self._served: dict[str, Any] = {}
        self._configured = False
        # (language, arch name) -> Transcriber; exactly the served set, filled lazily.
        self._transcribers: dict[tuple[str, str], Any] = {}
        self._model_lock = threading.Lock()
        self._init_decode_guards()

    def _configure(self) -> None:
        """Import the package, validate the spec, build the served map (idempotent)."""
        with self._model_lock:
            if self._configured:
                return
            _, download = _import_package()
            _cache_root(download)
            arch, langs = parse_model_spec(self._model_id)
            served = build_served(langs, arch, download._stt_catalog())
            self._served = served
            self._configured = True
            logger.info(
                "moonshine: serving %s",
                ", ".join(f"{code}={_arch_name(a)}" for code, a in served.items()),
            )

    def _transcriber_for(self, language: Any) -> Any:
        """The cached transcriber for ``language`` (loaded on first use).

        Called from the decode thread while holding ``_thread_lock``. A language
        outside the served set is logged and falls back; the downloader is only
        ever called for a served (language, arch).
        """
        self._configure()
        code, note = resolve_language(language, self._served)
        if note:
            logger.error("moonshine: %s", note)
        arch = self._served[code]
        key = (code, _arch_name(arch))
        with self._model_lock:
            transcriber = self._transcribers.get(key)
            if transcriber is None:
                from moonshine_voice import Transcriber  # type: ignore
                from moonshine_voice.download import get_model_for_language  # type: ignore

                # Fetch-and-load only: a bare network error is a download failure.
                with download_phase():
                    path, model_arch = get_model_for_language(code, arch)
                    transcriber = Transcriber(path, model_arch)
                self._transcribers[key] = transcriber
            return transcriber

    async def start(self) -> None:
        # Eager import and validation; fail fast before the socket binds. The
        # models are NOT loaded here (lazy on first decode, like nemotron).
        self._configure()

    async def open_stream(self, *, language: str | None = None) -> _MoonshineStream:
        return _MoonshineStream(language, self._decode_lock, self._thread_lock, self)

    def abort(self) -> None:
        """Synchronous, non-blocking cleanup for a hard exit. Moonshine keeps no
        temp files (audio is passed in memory), so there is nothing to remove."""

    async def close(self) -> None:
        # Bounded drain of an in-flight decode (SIGTERM mid-decode), then free
        # the transcribers -- but only if no decode still holds the lock, so a
        # stuck decode thread is never closed from under itself.
        await self._drain_inflight("moonshine", logger)
        if self._thread_lock.acquire(blocking=False):
            try:
                with self._model_lock:
                    transcribers = list(self._transcribers.values())
                    self._transcribers.clear()
                for transcriber in transcribers:
                    with contextlib.suppress(Exception):
                        transcriber.close()
            finally:
                self._thread_lock.release()
        self.abort()


# --- prefetch ------------------------------------------------------------------


def _cache_root(download: Any) -> Path:
    """The library's model cache root, which must be absolute.

    The library joins a relative ``MOONSHINE_VOICE_CACHE`` onto itself twice
    (``download_model`` prepends the cache dir to a path that already has it), so
    a download lands under ``<cache>/<cache>/...`` while the manifest path and the
    path serve loads from name it once. ``--check``, fetch and ``start()`` all
    derive the root here, so a relative value is refused up front.
    """
    root = Path(download._cache_root_dir(None))
    if not root.is_absolute():
        raise ConfigError(
            f"MOONSHINE_VOICE_CACHE={os.environ.get('MOONSHINE_VOICE_CACHE')!r} is a "
            "relative path; it must be an absolute path"
        )
    return root


def _model_manifest(download: Any, code: str, arch: Any) -> tuple[Path, list[dict]]:
    """``(model root dir, files)`` from the package's own manifest.

    The one derivation ``--check`` (``_check_model``) and prefetch
    (``_fetch_model``) share, built from the same library calls serve's
    ``get_model_for_language(code, arch)`` makes, without downloading:
    ``find_model_info`` -> ``_primary_stt_group`` for the group, and
    ``_cache_root_dir(None)`` for the cache root. Only the final join is
    restated, because the library does it inline in ``_download_manifest_group``
    (which downloads): cache root plus ``base_url`` without its scheme. Each file
    carries ``name``, ``size`` and a base64 CRC32C. Directory names (dated or
    not) are never hard-coded. ``_fetch_model`` still compares the result with
    the path the library returns.
    """
    # An empty manifest raises the library's own error here, as it would in serve.
    group = download._primary_stt_group(download.find_model_info(code, arch))
    root = _cache_root(download) / group["base_url"].replace("https://", "")
    return root, list(group.get("files", []))


def _crc_mismatch(path: Path, info: dict) -> bool:
    """True when the manifest has a CRC32C for the file and the file's differs
    (``False`` when the manifest has none).

    Fails closed: ``crc32c_file`` returns ``None`` without ``google-crc32c`` (a
    hard dependency of ``moonshine-voice``) and the library then skips the check,
    so an uncomputable CRC32C raises ``ModuleNotFoundError`` instead of passing."""
    if info.get("checksum_type") == "crc32c" and info.get("checksum"):
        from moonshine_voice.download_file import crc32c_file  # type: ignore

        actual = crc32c_file(path)
        if actual is None:
            raise ModuleNotFoundError(
                f"cannot verify the CRC32C of {path}: google-crc32c is not importable "
                "— run: uv sync --extra moonshine --inexact",
                name="google_crc32c",
            )
        return actual != info["checksum"]
    return False


def _file_state(path: Path, info: dict) -> str:
    """``"missing"``, ``"incomplete"`` (absent/size mismatch) or ``"ok"``; raises on a
    CRC32C mismatch (size alone is not trusted: the library accepts a size match)."""
    try:
        size = path.stat().st_size
    except OSError:
        return "missing"
    expected = info.get("size")
    if isinstance(expected, int) and expected >= 0 and size != expected:
        return "incomplete"
    try:
        corrupt = _crc_mismatch(path, info)
    except FileNotFoundError:
        # Removed after the stat by a concurrent prefetch's locked repair or download.
        return "missing"
    if corrupt:
        exc = ValueError(f"CRC32C mismatch for {path}: cached file is corrupt")
        mark_model_invalid(exc)
        raise exc
    return "ok"


def _remove_if_still_corrupt(path: Path, info: dict) -> None:
    """Delete a same-size, bad-CRC32C cached file, rechecked under ``<file>.lock``.

    That is the lock ``moonshine_voice.download_file.download_file`` holds while
    it writes and renames the file, so a concurrent prefetch's repair or download
    is serialised with this one: a file it already removed is left alone, and a
    replacement it already downloaded is never deleted.
    """
    from filelock import FileLock  # a hard dependency of moonshine-voice

    with FileLock(path.with_suffix(path.suffix + ".lock")):  # the library's lock path
        try:
            same_size = path.stat().st_size == info.get("size")
        except FileNotFoundError:
            return
        if same_size and _crc_mismatch(path, info):
            logger.warning("moonshine: %s failed its CRC32C check; re-downloading it once", path)
            path.unlink(missing_ok=True)


def _check_model(
    spec: SnapshotSpec, language: str, requested_arch: str | None
) -> tuple[str, int] | None:
    _, download = _import_package()
    arch = resolve_arch(language, requested_arch, download._stt_catalog())
    root, files = _model_manifest(download, language, arch)
    total = 0
    for info in files:
        if _file_state(root / info["name"], info) != "ok":
            return None
        total += int(info.get("size") or 0)
    return str(root), total


def _fetch_model(
    spec: SnapshotSpec, progress: Any, language: str, requested_arch: str | None
) -> str:
    _, download = _import_package()
    arch = resolve_arch(language, requested_arch, download._stt_catalog())
    root, files = _model_manifest(download, language, arch)

    def have(info: dict) -> bool:
        try:
            return (root / info["name"]).stat().st_size == info.get("size")
        except OSError:
            return False

    # Self-repair, once: the library's cache fast path accepts a size match, so a
    # same-size file with a bad CRC32C would never be re-downloaded. Delete it so
    # the download below fetches it again. If the fresh copy is bad too, the
    # library's post-download check raises (naming only the file, and leaving
    # nothing on disk); that is re-raised below as ``ModelInvalidError`` naming
    # the full path.
    for info in files:
        path = root / info["name"]
        if not have(info):
            continue
        # This unlocked read is only a hint: a concurrent prefetch may remove or
        # replace the file at any point, so a file that vanished is a cache miss
        # (the download below fetches it) and the delete is rechecked under the
        # library's lock in ``_remove_if_still_corrupt``.
        try:
            corrupt = _crc_mismatch(path, info)
        except FileNotFoundError:
            continue
        if corrupt:
            _remove_if_still_corrupt(path, info)
    pending = [info for info in files if not have(info)]
    total = sum(int(i.get("size") or 0) for i in pending)
    done = threading.Event()
    reporter: threading.Thread | None = None

    def measure() -> tuple[int, int]:
        got = n_done = 0
        for info in pending:
            size = int(info.get("size") or 0)
            dest = root / info["name"]
            if have(info):
                got += size
                n_done += 1
                continue
            with contextlib.suppress(OSError):
                partial = dest.with_suffix(dest.suffix + ".partial")
                got += min(size, partial.stat().st_size)
        return got, n_done

    if progress is not None:

        def report() -> None:
            while not done.wait(_PROGRESS_INTERVAL_S):
                got, n_done = measure()
                progress(got, total, n_done, len(pending))

        reporter = threading.Thread(target=report, name="prefetch-progress", daemon=True)
        reporter.start()
    try:
        # ``get_model_for_language`` also best-effort fetches the optional
        # alphanumeric spelling model; it is not part of the snapshot contract
        # and not required for a decode.
        try:
            served_path, _ = download.get_model_for_language(language, arch)
        except ValueError as exc:
            # The library verifies size/CRC32C after a fresh download and raises
            # ``ValueError("<kind> mismatch for <file name>: ...")``, deleting the
            # bad copy. Name the full destination so the operator can find it.
            if "mismatch" not in str(exc):
                raise
            named = [i["name"] for i in files if f" for {i['name']}:" in str(exc)]
            where = root / named[0] if named else root
            invalid = ValueError(f"moonshine {language}: {exc} (downloading to {where})")
            mark_model_invalid(invalid)
            raise invalid from exc
    finally:
        done.set()
        if reporter is not None:
            reporter.join(timeout=1)
    # The server loads from the path ``get_model_for_language`` returns; the
    # checks here verify the manifest-derived root. They must be one directory.
    if Path(served_path) != root:
        raise RuntimeError(
            f"moonshine {language}: the library's model directory {served_path} is not "
            f"the manifest directory {root} that prefetch verifies"
        )
    for info in files:
        # Re-verify on disk ourselves (the library's cache fast path is size-only).
        if _file_state(root / info["name"], info) != "ok":
            exc = FileNotFoundError(f"moonshine {language}: {info['name']} missing after download")
            mark_model_invalid(exc)
            raise exc
    if progress is not None:
        progress(total, total, len(pending), len(pending))  # 100%
    return str(root)


def prefetch_specs(model: str | None) -> list[SnapshotSpec]:
    """Snapshots ``python -m stt_server prefetch --backend moonshine`` fetches.

    One spec per served language. Needs no package import (the registry and CLI
    build specs without the extra): ``repo_id`` is synthetic
    (``moonshine-voice/<arch>-<lang>``, ``default`` for an omitted arch),
    ``revision`` is the ``moonshine-voice`` version, and ``check``/``fetch``
    import the package lazily and derive completeness from its own manifest.
    """
    arch, langs = parse_model_spec(model or DEFAULT_MOONSHINE_MODEL)
    return [
        SnapshotSpec(
            f"moonshine-voice/{arch or 'default'}-{code}",
            # The pin itself, as hub backends report their pinned revision; any
            # other installed version is refused by ``_import_package``.
            revision=MOONSHINE_VOICE_VERSION,
            check=functools.partial(_check_model, language=code, requested_arch=arch),
            fetch=functools.partial(_fetch_model, language=code, requested_arch=arch),
        )
        for code in langs
    ]
