"""Model-snapshot contract shared by the backends, ``prefetch`` and ``supervise``.

Stdlib-only, so a backend module can import it at load time without pulling the
CLI-level ``prefetch`` module (which itself imports the backends) and without
importing ``huggingface_hub``.

- :class:`SnapshotSpec` is what a backend's module-level
  ``prefetch_specs(model) -> list[SnapshotSpec]`` returns.
- :func:`download_phase` / :func:`mark_download_phase` tag an exception as
  raised by a model fetch, so ``supervise.classify_startup_error`` can report a
  bare builtin ``ConnectionError``/``TimeoutError`` as ``ModelDownloadError``.
  Wrap only the call that fetches (or fetches-and-loads) weights, never a
  decode.
- :func:`mark_model_invalid` tags a failure where the fetched snapshot itself is
  unusable (incomplete, a corrupt or incomplete ``config.json``): reported as
  ``ModelInvalidError``, neither a download nor a backend-start failure.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class SnapshotSpec:
    """One model snapshot a backend needs before it can decode offline (a Hugging
    Face snapshot unless ``check``/``fetch`` are set)."""

    repo_id: str
    revision: str | None = None  # None = the repo's main branch, as the loaders use
    #: ``None`` downloads the whole repo (what ``mlx_whisper`` does).
    allow_patterns: tuple[str, ...] | None = None
    #: What must be on disk for the snapshot to count as cached. Each entry is a
    #: glob that must match a file (path relative to the snapshot root), or a
    #: tuple of globs of which at least one must match: ``mlx_whisper`` reads
    #: ``weights.safetensors``, else ``weights.npz``, so it requires
    #: ``("weights.safetensors", "weights.npz")``. Alternatives are structural
    #: (a tuple), never an in-band delimiter in a string. Checked explicitly
    #: because older ``huggingface_hub`` releases return a partial snapshot
    #: folder from ``local_files_only=True``.
    required: tuple[str | tuple[str, ...], ...] = ()
    kind: str = "model"
    #: Set only for an explicit local model reference. It bypasses Hub calls but
    #: still uses this backend-owned spec's ``required`` completeness patterns.
    local_path: str | None = None
    #: For repos whose weight filenames live in their own ``config.json`` (Kyutai:
    #: ``moshi_name`` may be a ``.q4``/``.q8`` variant). Called with the parsed
    #: config, it returns the exact filenames to fetch and require, replacing
    #: ``allow_patterns``/``required``. ``config.json`` is fetched first. The
    #: names must be literal plain filenames (no path separators, glob
    #: characters or ``|``): they are used as ``allow_patterns`` and matched
    #: with ``fnmatch``.
    resolve: Callable[[dict], tuple[str, ...]] | None = None
    #: For backends whose weights do not come from the Hugging Face Hub
    #: (``moonshine``: the package's own CDN for weights, its wheel's manifest for sizes and CRC32C). When set, ``prefetch``
    #: calls it instead of the Hub probe: it is given this spec and returns
    #: ``(path, size_bytes)`` of a complete snapshot, or ``None`` when files are
    #: missing or incomplete. A snapshot that is present but corrupt raises an
    #: exception tagged with :func:`mark_model_invalid`. ``allow_patterns``,
    #: ``required``, ``resolve`` are ignored when it is set.
    check: Callable[["SnapshotSpec"], tuple[str, int] | None] | None = None
    #: Counterpart of ``check`` for a fetch. Given this spec and an optional
    #: ``progress(downloaded_bytes, total_bytes, files_done, files_total)``
    #: callback (``None`` when no JSON progress was requested), it downloads
    #: what is missing and returns the snapshot's path. It reports progress with
    #: the same meaning as the Hub path (only not-yet-cached files count) and a
    #: final 100% event; a corrupt result raises an exception tagged with
    #: :func:`mark_model_invalid`. ``prefetch`` emits ``start`` and ``done`` and
    #: runs the call inside :func:`download_phase`.
    fetch: (
        Callable[
            ["SnapshotSpec", Callable[[int, int, int, int], None] | None],
            str,
        ]
        | None
    ) = None

    def __post_init__(self) -> None:
        # ``prefetch`` picks the check and the fetch path independently, so a spec
        # with only one would send the other down the Hugging Face path.
        if (self.check is None) != (self.fetch is None):
            raise ValueError(f"SnapshotSpec {self.repo_id!r}: set both check and fetch, or neither")


DOWNLOAD_PHASE_ATTR = "_stt_download_phase"
MODEL_INVALID_ATTR = "_stt_model_invalid"
PREFETCH_REPO_ATTR = "_stt_prefetch_repo"


def _tag(exc: BaseException, attr: str, value: object = True) -> None:
    try:
        setattr(exc, attr, value)
    except AttributeError:  # exotic exception without a __dict__: stay unmarked
        pass


def mark_download_phase(exc: BaseException) -> None:
    """Tag ``exc`` as raised while a model download was running, so a bare
    ``ConnectionError``/``TimeoutError`` is classified as ``ModelDownloadError``."""
    _tag(exc, DOWNLOAD_PHASE_ATTR)


def mark_model_invalid(exc: BaseException) -> None:
    """Tag ``exc`` as "the model snapshot is unusable" (``ModelInvalidError``)."""
    _tag(exc, MODEL_INVALID_ATTR)


def tag_prefetch_repo(exc: BaseException, repo: str, *, overwrite: bool = True) -> None:
    """Tag ``exc`` with the repo ``prefetch``'s ``error`` event reports (read back
    with ``getattr(exc, PREFETCH_REPO_ATTR, None)``).

    By default it overwrites any earlier tag (the raiser knows the repo). The
    prefetch drivers pass ``overwrite=False``: a tag a backend already set (the
    language that failed) wins over the spec being checked or fetched.
    """
    if not overwrite and getattr(exc, PREFETCH_REPO_ATTR, None) is not None:
        return
    _tag(exc, PREFETCH_REPO_ATTR, repo)


@contextlib.contextmanager
def download_phase() -> Iterator[None]:
    """Tag any ``Exception`` escaping the block with :func:`mark_download_phase`."""
    try:
        yield
    except Exception as exc:
        mark_download_phase(exc)
        raise


def check_plain_filenames(names: tuple[str, ...], *, source: str) -> tuple[str, ...]:
    """Return ``names`` if each is a literal, plain filename; else ``ValueError``.

    A name taken from a remote ``config.json`` is used both as a Hub filename and
    as an ``fnmatch`` pattern, so a path separator, ``..`` or a glob character
    could make a completeness check pass for a file the loader never finds.
    ``|`` is refused too: no legitimate weight file carries it, and it reads as
    an alternatives separator to anyone used to the old ``a|b`` syntax.
    """
    for name in names:
        if (
            not isinstance(name, str)
            or not name
            or name in (".", "..")
            or any(c in name for c in "/\\*?[]|")
        ):
            raise ValueError(f"{source} names an invalid file: {name!r}")
    return names
