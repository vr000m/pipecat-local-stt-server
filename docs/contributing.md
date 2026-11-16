# Contributing

<a id="contributing-a-backend"></a>

Start here to change code, fix documentation, or add a backend. For server
usage, see the [README](../README.md); for model configuration, see
[Backends and models](backends.md).

- [Set up a checkout](#development-setup)
- [Find the relevant code](#source-map)
- [Add a backend](#implementation-checklist)
- [Check your changes](#verification)

## Development setup

Use Python 3.12 or newer and `uv`. Run commands from the repository root:

```bash
git clone https://github.com/vr000m/pipecat-local-stt-server.git
cd pipecat-local-stt-server
git switch -c docs/my-change
uv sync --inexact
uv run --no-sync python -m pytest -q
```

The development dependencies include pytest, Ruff, and NumPy. The base suite
uses fake backend modules and needs no downloaded model. Install an optional
extra only for live testing, for example `uv sync --extra parakeet --inexact`.
Kyutai needs its own Python 3.12 environment; see
[backend setup](backends.md#choosing-a-backend). Use `uv run --no-sync` to avoid
removing installed extras when running commands.

## Source map

| Area | Start here |
|---|---|
| CLI, startup, status, and prefetch dispatch | [`stt_server/__main__.py`](../stt_server/__main__.py) |
| Sessions and shutdown | [`stt_server/server.py`](../stt_server/server.py), [architecture guide](architecture.md) |
| Python client and wire events | [`stt_server/client.py`](../stt_server/client.py), [protocol guide](protocol.md) |
| Backend interfaces and registry | [`stt_server/backend.py`](../stt_server/backend.py), [`stt_server/backends/`](../stt_server/backends/) |
| Model downloads and cache checks | [`stt_server/prefetch.py`](../stt_server/prefetch.py), [`stt_server/snapshot.py`](../stt_server/snapshot.py) |
| LaunchAgents and operator commands | [`scripts/`](../scripts/), [`justfile`](../justfile), [operations guide](operations.md) |
| Regression tests and live checks | [`tests/`](../tests/), [testing guide](testing.md) |

## Implementation checklist

A backend is registered once and becomes available to `serve`, model-default
selection, and `prefetch`. Read the [lifecycle constraints](architecture.md#backends)
before implementing one.

### Implement the backend and stream

Add `stt_server/backends/<name>.py` with a class implementing
`TranscriptionBackend`: `backend_name`, `model`, `start()`, `open_stream()`,
and `close()`. Its stream implements `feed()`, `end()`, `events()`, and
`cancel()` as specified in `stt_server/backend.py`.

Import optional dependencies inside `start()` or lazy model loading. Re-raise
a missing extra as `ModuleNotFoundError` with the remedy
`uv sync --extra <name> --inexact`.

For MLX/Metal decode threads, reuse `DecodeGuardMixin` from
`stt_server/backends/_thread_util.py`. Add synchronous, idempotent `abort()`
when cleanup must survive a failed start or hard exit. It must remain safe
while a decode thread runs; the next `start()` must recreate what it removed.
Add `idle()` through `IdleAwareBackend` when the server must retain its lock
until worker threads finish. The full contracts are in `stt_server/backend.py`.

### Define prefetch and errors

Add module-level `prefetch_specs(model) -> list[SnapshotSpec]`.
Each `SnapshotSpec.required` value is a glob relative to the snapshot root;
a tuple means any one of its alternatives is sufficient.

For weights outside the Hugging Face Hub, as in Moonshine, set both `check`
and `fetch` callables. Setting only one raises `ValueError`. Prefetch invokes
these callables without touching `huggingface_hub`; see `stt_server/snapshot.py`
for the field contract. If a callable rejects a particular language, tag its
exception with `tag_prefetch_repo` so the error event names the correct repo.
Prefetch preserves a tag that is already set.

Wrap only fetch-and-load work in `snapshot.download_phase()`, never decoding.
Mark an unusable fetched snapshot with `snapshot.mark_model_invalid()`.

### Register, package, and test

1. Add a `BackendEntry` to `BACKENDS` in `stt_server/backends/__init__.py`,
   including the module, class name, default model, and `local_model_paths`.
2. Add an extra in `pyproject.toml`. Declare `[tool.uv] conflicts` when its
   pins conflict with another extra, and use a separate CI environment when
   required.
3. Add `tests/test_<name>_backend.py` with fake modules covering stream,
   lifecycle, prefetch, and failure contracts. Keep
   `tests/test_backend_registry.py` passing.
4. Update [Backends and models](backends.md), the
   [operations port table](operations.md#per-backend-port-convention) if the
   backend gets a LaunchAgent, and `CHANGELOG.md`.

## Documentation changes

Keep the README focused on setup and navigation. Put detailed commands and
contracts in the guide that owns the topic, and link to it from other pages.
Preserve old anchors when moving a section so existing links still work.

Keep `CHANGELOG.md` bullets to about 40 words: say what changed and what a user
must do. Put mechanisms, rationale, and error cases in the relevant guide or
dev plan, and link to them. GitHub release notes repeat the changelog section.

## Verification

Run formatting, linting, and the full suite before opening a pull request:

```bash
uv run --no-sync ruff format
uv run --no-sync ruff check
uv run --no-sync python -m pytest -q
```

For documentation changes, the suite checks relative links, heading anchors,
README size, and the port table against the installer. Check the rendered prose
and examples too. Describe the change and validation in the pull request.

For a production backend, run a live decode and `prefetch --check` against the
exact revision before changing `PINNED_REVISIONS`. See
[Testing and benchmarks](testing.md) for smoke-test commands and coverage limits.
