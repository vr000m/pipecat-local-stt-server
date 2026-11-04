# Contributing a backend

> Maintainer checklist for adding an ASR backend to
> [pipecat-local-stt-server](../README.md). Read the
> [backend and model guide](backends.md) for the current user-facing surface and
> [server architecture](architecture.md#backends) for lifecycle constraints.

A backend is registered once and is then available to `serve`, model-default
selection, and `prefetch`.

## Implementation checklist

1. **Backend module.** Add `stt_server/backends/<name>.py` with a class that
   implements `TranscriptionBackend`: `backend_name`, `model`, `start()`,
   `open_stream()`, and `close()`.
2. **Prefetch contract.** Add module-level
   `prefetch_specs(model) -> list[SnapshotSpec]`. Each
   `SnapshotSpec.required` value is a glob relative to the snapshot root. A
   tuple is a set of alternatives where any one match is sufficient. The exact
   field contract lives in `stt_server/snapshot.py`.
3. **Optional dependency boundary.** Import the backend's optional extra only
   inside `start()` or lazy model loading. Re-raise a missing extra as
   `ModuleNotFoundError` with the `uv sync --extra <name> --inexact` remedy.
4. **Decode lifecycle.** Reuse `DecodeGuardMixin` from
   `stt_server/backends/_thread_util.py` for MLX/Metal decode threads.
5. **Error origin.** Wrap only fetch-and-load work in
   `snapshot.download_phase()`. Never wrap a decode. Mark an unusable fetched
   snapshot with `snapshot.mark_model_invalid()`.
6. **Cleanup.** Add synchronous, idempotent `abort()` when cleanup must survive
   a failed start or hard exit. It must remain safe while a decode thread runs
   and the next `start()` must recreate what it removed. Add `idle()` through
   `IdleAwareBackend` when the server must retain its lock until worker threads
   finish. Full contracts live in `stt_server/backend.py`.
7. **Registry.** Add one `BackendEntry` to `BACKENDS` in
   `stt_server/backends/__init__.py`, including module, class name, default
   model, and `local_model_paths` support.
8. **Packaging.** Add an extra in `pyproject.toml`. Add a `[tool.uv] conflicts`
   entry when its pins conflict with another MLX extra, and give it a separate
   CI environment when required.
9. **Tests.** Keep `tests/test_backend_registry.py` green and add
   `tests/test_<name>_backend.py` using fake modules to cover the stream,
   lifecycle, prefetch, and failure contracts.
10. **Documentation.** Update [Backends and models](backends.md), the
    [operations port table](operations.md#per-backend-port-convention) if the
    backend gets a LaunchAgent, and `CHANGELOG.md`.

## Verification

Run formatting and linting before the complete test suite:

```bash
uv run ruff format
uv run ruff check
uv run python -m pytest -q
```

For a production model, also run a live decode and a `prefetch --check` against
the exact revision before changing `PINNED_REVISIONS`.
