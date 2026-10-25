"""``python -m stt_server prefetch`` — fetch a backend's weights without serving.

The ``mlx``, ``parakeet`` and ``nemotron`` backends download their checkpoint
on the first decode, so a fresh server answers its first request only after a
silent multi-GB download. ``prefetch`` does that download up front, with
optional JSON-lines progress for a supervising app, and ``--check`` answers
"are the weights already cached?" without touching the network.

Each backend module declares what it needs with a module-level
``prefetch_specs(model) -> list[SnapshotSpec]`` next to its default model, so a
model change stays a one-file change.

JSON events (one object per stdout line; the same schema as
``tts_server prefetch`` in pipecat-local-tts-server):

- fetch: ``start{repo,revision,kind}`` -> ``progress{repo,downloaded_bytes,
  total_bytes,files_done,files_total}``* -> ``done{repo,path,size_bytes}`` per
  snapshot, then ``complete{backend,model,cached,size_bytes}``;
- ``--check``: ``check{repo,revision,kind,required,cached,path,size_bytes}`` per
  snapshot, then ``summary{backend,model,cached,size_bytes}``;
- failure: ``error{code,message,repo}`` and exit status 1.

Progress is measured from the Hugging Face cache on disk (bytes landing in the
repo's ``blobs/``), so it works with every ``huggingface_hub`` version the
backends pin; Kyutai's ``moshi_mlx`` requires ``huggingface-hub<0.29``.

Lean at import: ``huggingface_hub`` is imported only inside the seams.
"""

from __future__ import annotations

import contextlib
import fnmatch
import importlib
import logging
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("stt_server.prefetch")

_PROGRESS_INTERVAL_S = 0.3


@dataclass(frozen=True)
class SnapshotSpec:
    """One Hugging Face snapshot a backend needs before it can decode offline."""

    repo_id: str
    revision: str | None = None  # None = the repo's main branch, as the loaders use
    #: ``None`` downloads the whole repo (what ``mlx_whisper`` does).
    allow_patterns: tuple[str, ...] | None = None
    #: Glob patterns that must each match a file for the snapshot to count as
    #: cached. Checked explicitly because older ``huggingface_hub`` releases
    #: return a partial snapshot folder from ``local_files_only=True``.
    required: tuple[str, ...] = ()
    kind: str = "model"


def is_local_model(model: str) -> bool:
    """True when ``model`` names a local path rather than a hub repo id."""
    return model.startswith(("/", "./", "../", "~")) or Path(model).expanduser().exists()


_BACKEND_MODULES = {
    "mlx": "stt_server.backends.mlx_whisper",
    "parakeet": "stt_server.backends.parakeet",
    "nemotron": "stt_server.backends.nemotron",
    "kyutai": "stt_server.backends.kyutai",
}


def specs_for(backend: str, model: str | None) -> list[SnapshotSpec]:
    """Every snapshot ``backend`` needs to start ``model`` without the network."""
    if backend == "echo":
        return []
    try:
        module = _BACKEND_MODULES[backend]
    except KeyError:
        raise ValueError(f"unknown backend: {backend!r}") from None
    return list(importlib.import_module(module).prefetch_specs(model))


# --- patchable seams -------------------------------------------------------------


def _snapshot_download(spec: SnapshotSpec, *, local_files_only: bool) -> str:
    from huggingface_hub import snapshot_download  # type: ignore

    return str(
        snapshot_download(
            spec.repo_id,
            revision=spec.revision,
            allow_patterns=list(spec.allow_patterns) if spec.allow_patterns else None,
            local_files_only=local_files_only,
        )
    )


def _remote_files(spec: SnapshotSpec) -> list[tuple[str, int]]:
    """``(path, size)`` of the files the snapshot will hold (for progress totals)."""
    from huggingface_hub import HfApi  # type: ignore

    files = []
    for entry in HfApi().list_repo_tree(spec.repo_id, revision=spec.revision, recursive=True):
        size = getattr(entry, "size", None)
        if size is None:
            continue  # a folder
        lfs = getattr(entry, "lfs", None)
        size = getattr(lfs, "size", None) or size
        if spec.allow_patterns and not any(
            fnmatch.fnmatch(entry.path, p) for p in spec.allow_patterns
        ):
            continue
        files.append((entry.path, int(size)))
    return files


def _repo_cache_dir(repo_id: str) -> Path:
    from huggingface_hub import constants  # type: ignore

    return Path(constants.HF_HUB_CACHE) / ("models--" + repo_id.replace("/", "--"))


def _dir_bytes(path: Path) -> int:
    """Bytes of the regular files under ``path`` (blobs, including ``.incomplete``)."""
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _snapshot_size(path: str | Path) -> int:
    """Bytes in a snapshot folder (follows its symlinks into ``blobs/``)."""
    total = 0
    for p in Path(path).rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _complete(path: str | Path, required: tuple[str, ...]) -> bool:
    names = [str(p.relative_to(path)) for p in Path(path).rglob("*") if p.is_file()]
    return all(any(fnmatch.fnmatch(n, pattern) for n in names) for pattern in required)


# --- check / fetch ---------------------------------------------------------------


def check_spec(spec: SnapshotSpec) -> dict[str, Any]:
    """Offline cache probe for one snapshot. ``ImportError`` propagates."""
    row: dict[str, Any] = {
        "event": "check",
        "repo": spec.repo_id,
        "revision": spec.revision,
        "kind": spec.kind,
        "required": True,
        "cached": False,
        "path": None,
        "size_bytes": 0,
    }
    try:
        path = _snapshot_download(spec, local_files_only=True)
    except ImportError:
        raise
    except Exception as exc:  # noqa: BLE001 - any miss (incl. incomplete snapshot) = not cached
        logger.debug("prefetch: %s not cached (%s)", spec.repo_id, exc)
        return row
    if not _complete(path, spec.required):
        return row
    row.update(cached=True, path=path, size_bytes=_snapshot_size(path))
    return row


def fetch_spec(spec: SnapshotSpec, emit: Callable[[dict], None] | None) -> dict[str, Any]:
    """Download one snapshot (resumable). ``emit`` set = JSON progress events."""
    if emit is not None:
        emit({"event": "start", "repo": spec.repo_id, "revision": spec.revision, "kind": spec.kind})
    reporter: threading.Thread | None = None
    done = threading.Event()
    if emit is not None:
        files = _remote_files(spec)
        total = sum(size for _, size in files)
        blobs = _repo_cache_dir(spec.repo_id) / "blobs"
        baseline = _dir_bytes(blobs)

        def report() -> None:
            while not done.wait(_PROGRESS_INTERVAL_S):
                grown = _dir_bytes(blobs) - baseline
                emit(
                    {
                        "event": "progress",
                        "repo": spec.repo_id,
                        "downloaded_bytes": max(0, min(grown, total)),
                        "total_bytes": total,
                        "files_done": 0,
                        "files_total": len(files),
                    }
                )

        reporter = threading.Thread(target=report, name="prefetch-progress", daemon=True)
        reporter.start()
    try:
        path = _snapshot_download(spec, local_files_only=False)
    finally:
        done.set()
        if reporter is not None:
            reporter.join(timeout=1)
    if not _complete(path, spec.required):
        raise FileNotFoundError(f"{spec.repo_id}: snapshot is missing {', '.join(spec.required)}")
    result = {
        "event": "done",
        "repo": spec.repo_id,
        "path": path,
        "size_bytes": _snapshot_size(path),
    }
    if emit is not None:
        emit(result)
    return result


def run_prefetch(backend: str, model: str | None, *, check: bool, json_mode: bool) -> int:
    """CLI body; returns the process exit status."""
    from . import supervise

    if json_mode:
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    ctx = supervise.json_channel() if json_mode else contextlib.nullcontext()
    with ctx:
        emit = supervise.emit if json_mode else None
        try:
            specs = [] if model and is_local_model(model) else specs_for(backend, model)
            return (
                _run_check(backend, model, specs, emit)
                if check
                else _run_fetch(backend, model, specs, emit)
            )
        except Exception as exc:  # noqa: BLE001 - CLI boundary: report, exit 1
            code, message = supervise.classify_startup_error(exc)
            if code == supervise.CODE_MODULE_NOT_FOUND:
                message = (
                    f"{message} (prefetch needs huggingface_hub, which every model extra installs)"
                )
            repo = getattr(exc, "_prefetch_repo", None)
            if emit is not None:
                emit({"event": "error", "code": code, "message": message, "repo": repo})
            else:
                print(f"stt_server: prefetch failed: {message}", file=sys.stderr)
            return 1


def _run_check(backend: str, model: str | None, specs: list[SnapshotSpec], emit) -> int:
    rows = [check_spec(s) for s in specs]
    summary = {
        "event": "summary",
        "backend": backend,
        "model": model,
        "cached": all(r["cached"] for r in rows),
        "size_bytes": sum(r["size_bytes"] for r in rows),
    }
    if emit is not None:
        for r in rows:
            emit(r)
        emit(summary)
        return 0
    for r in rows:
        print(
            f"{r['kind']:<6} {r['repo']}@{r['revision'] or 'main'}: {'cached' if r['cached'] else 'missing'}"
        )
    print(
        f"{backend}: {'cached' if summary['cached'] else 'not cached'}"
        f" ({summary['size_bytes'] / 1e6:.1f} MB on disk)"
    )
    return 0


def _run_fetch(backend: str, model: str | None, specs: list[SnapshotSpec], emit) -> int:
    total = 0
    for spec in specs:
        if emit is None:
            print(f"prefetch: {spec.kind} {spec.repo_id}@{spec.revision or 'main'}")
        try:
            total += fetch_spec(spec, emit)["size_bytes"]
        except ImportError:
            raise
        except Exception as exc:
            exc._prefetch_repo = spec.repo_id  # type: ignore[attr-defined]
            raise
    if emit is not None:
        emit(
            {
                "event": "complete",
                "backend": backend,
                "model": model,
                "cached": True,
                "size_bytes": total,
            }
        )
    else:
        print(f"prefetch: {backend} ready ({total / 1e6:.1f} MB)")
    return 0
