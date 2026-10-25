# pipecat-local-stt-server

Standalone local WebSocket transcription (STT) server, minimal Python client,
and pluggable ASR backends for the Pipecat ecosystem.

Distributed as `pipecat-local-stt-server` (PyPI); the import name is
`stt_server` (every `import stt_server` / `python -m stt_server` invocation
keeps working). Extracted, history-preserving, from the private `koda-pipecat`
monorepo. BSD-2-Clause.

## V1 scope

- one WebSocket connection = one transcription session
- pinned wire format: `PCM16LE`, mono, `16000 Hz`
- binary WebSocket audio frames are the default transport; JSON
  `input_audio_buffer.append` with base64 remains as a compatibility mode
- `turn_detection: null` required; server VAD is not implemented in V1
- commit-oriented decode (single large `delta` + `completed`)
- local-only trust model:
  - Unix domain socket by default
  - optional loopback TCP (`127.0.0.1`) with optional bearer token
  - browser `Origin` headers rejected
- backend interface (`TranscriptionBackend`) so MLX can be swapped later
- `EchoBackend` reference implementation for tests and smoke-checks
- `MLXWhisperBackend` shipped in `stt_server/backends/mlx_whisper.py` (requires
  the `mlx` extra)
- `ParakeetBackend` shipped in `stt_server/backends/parakeet.py` (requires the
  `parakeet` extra; default model `mlx-community/parakeet-tdt-0.6b-v3`).
  Parakeet decodes from a temp WAV; that WAV holds raw utterance audio (PII) and
  is written to a per-process private `0o700` directory (created at backend
  start, removed on `close()`), never the world-listable system temp dir.
- `NemotronBackend` shipped in `stt_server/backends/nemotron.py` (requires the
  `nemotron` extra — `uv sync --extra nemotron`; default model
  `mlx-community/nemotron-3.5-asr-streaming-0.6b`). Nemotron decodes
  from a temp WAV; that WAV holds raw utterance audio (PII) and is written to a
  per-process private `0o700` directory (created at backend start, removed on
  `close()`), never the world-listable system temp dir.
- `KyutaiBackend` shipped in `stt_server/backends/kyutai.py` (requires the
  `kyutai` extra — `uv sync --extra kyutai`; default model
  `kyutai/stt-1b-en_fr-mlx`, English and French, CC-BY-4.0). Decodes in memory
  through `moshi_mlx`. The extra pins an older MLX stack and is declared
  conflicting with the other MLX extras, so install it on its own. It needs
  Python 3.12 (`moshi_mlx` pins `sentencepiece==0.2.0`, which has no 3.13 wheel;
  elsewhere it builds from source and needs `cmake`), and it is not wired into
  the LaunchAgent tooling (`install_stt_agent.sh`, `just stt-*`): run it with
  `serve --backend kyutai`, e.g. under `--supervised`.

## Running the server

> **Prerequisite:** install deps with `uv sync` first, and run every command
> through `uv run` (or `source .venv/bin/activate` once per shell). Bare
> `python -m stt_server …` uses the system interpreter and fails with
> `ModuleNotFoundError: No module named 'websockets'`.

```bash
# UDS (recommended for local use — no port, no bearer token)
uv run python -m stt_server --socket-path ~/Library/Caches/pipecat-stt/stt.sock --backend echo

# MLX Whisper over UDS
uv sync --extra mlx
uv run python -m stt_server --socket-path ~/Library/Caches/pipecat-stt/stt.sock --backend mlx

# Loopback TCP — minimal form. Pick any free port; there is no default port,
# so --host and --port are both required for TCP. On loopback an auth token is
# optional (the server logs a warning and serves anyway — fine for local
# experiments). The listener comes up on 127.0.0.1:<port>.
uv run python -m stt_server --host 127.0.0.1 --port 9900 --backend echo

# Loopback TCP with auth (recommended for anything non-experimental — use
# --auth-token-file or PIPECAT_STT_AUTH_TOKEN env; legacy KODA_STT_AUTH_TOKEN
# still honoured; --auth-token on argv is visible via `ps` and marked DEPRECATED)
uv run python -m stt_server --host 127.0.0.1 --port 8765 --auth-token-file /path/to/token
```

> **Ports:** there is no baked-in default port — you choose it (`--port 9900`).
> Use `--port 0` to let the OS pick a free one. V1 only permits loopback binds
> (`127.0.0.1`/`::1`/`localhost`); a non-loopback `--host` is rejected.

> **Backends need their own install.** Every `--backend` above except `echo`
> requires a `uv sync` extra first — `mlx`/`parakeet`/`nemotron` (e.g.
> `uv sync --extra mlx`). Without it the server exits with an actionable
> `stt_server: the '<extra>' extra is not installed … run: uv sync --extra <extra>
> --inexact` message. See [Choosing a backend and model](#choosing-a-backend-and-model)
> for each backend's install command and `--model` defaults.

The CLI accepts both `python -m stt_server <flags>` (the legacy flat form,
which implicitly routes to `serve`) and `python -m stt_server serve <flags>`
/ `python -m stt_server status <flags>` — see [Checking server
health](docs/operations.md#checking-server-health) for the `status` subcommand.

For persistent always-on operation on macOS, install the server as a
LaunchAgent via `scripts/install_stt_agent.sh`
(`install|start|stop|restart|status|logs`); pair it with
`python -m stt_server status` for a wire-level health probe. A consumer
may wrap these behind its own CLI — see [Reference consumer
(Koda)](docs/integration.md#reference-consumer-koda) for one such integration.

## Managing agents with `just`

`install_stt_agent.sh` manages exactly one agent per invocation, so once you run
two or three ASRs side by side there is no single command to see them or stop the
idle ones. The repo-root `justfile` is a thin operator layer over `launchctl`
(macOS only) that fills that gap. Run recipes from the repo root:

```bash
just                       # list available recipes
just stt-list              # every pipecat.stt-server* agent: state, pid, live backend
just stt-status nemotron   # wire health probe for one backend
just stt-disable whisper   # stop until next login (keeps the plist)
just stt-enable whisper    # re-load it from the existing plist
just stt-install parakeet  # delegates to install_stt_agent.sh (+ ensures the extra)
just stt-uninstall parakeet
just smoke-peercred        # local UDS peer-cred smoke (cross-uid leg needs a 2nd uid)
```

`<backend>` is one of `whisper` / `parakeet` / `nemotron`, mapped to the labels
and sockets in the [per-ASR table](docs/operations.md#per-asr-socket-convention)
(the justfile map is a checked mirror of that table — a test fails CI on drift).

`stt-install` / `stt-enable` also ensure the backend's optional Python extra is
installed (`uv sync --extra <backend> --inexact`) so a freshly installed agent
doesn't crash-loop on a missing import; set `PIPECAT_STT_SKIP_DEP_SYNC=1` to
manage the extras yourself.

`stt-list` prints each agent's `socket:` line in the same `~`-form a consumer's
config uses for its endpoint (e.g. onoats' `config.toml` `[stt] ws_socket`), so
you can match a config line to a running agent directly. Note whisper's socket is
`stt.sock`, not `whisper.sock` — the socket line removes that guesswork.

**`stt-disable` vs `stt-uninstall`.** The LaunchAgent plist sets `RunAtLoad` +
`KeepAlive`, so `install_stt_agent.sh stop` (a plain `SIGTERM`) is respawned
immediately. `stt-disable` instead does `launchctl bootout`, which takes the
agent down **until the next login** — the plist stays on disk and launchd
reloads it when you log back in. `stt-uninstall` removes the plist, so the agent
stays gone. For cross-login suppression without removing the plist, use
`launchctl disable gui/$(id -u)/<label>`.

`stt-list` sweeps the `pipecat.stt-server*` label prefix, so a custom-labelled
agent still shows up (without a live-backend line, since its socket is not
derivable from its label). Legacy `koda.stt-server*` agents are **not** covered —
check those manually during migration with `launchctl list | grep koda`.


## Choosing a backend and model

The **server** picks the ASR, pinned at launch — the client never selects it.
A client (including the Pipecat service in the [integration
guide](docs/integration.md#pipecat-integration)) only points at an *endpoint*
and transcribes against whatever backend that server was started with. To run
Whisper vs Parakeet you start a *different* server (or a second one on its own
socket — see "Multi-backend operation"); no client code changes.

```bash
# Whisper (MLX) — default model mlx-community/whisper-large-v3-turbo
uv sync --extra mlx
uv run python -m stt_server serve --backend mlx \
    --socket-path ~/Library/Caches/pipecat-stt/stt.sock

# Parakeet — default model mlx-community/parakeet-tdt-0.6b-v3
uv sync --extra parakeet
uv run python -m stt_server serve --backend parakeet \
    --socket-path ~/Library/Caches/pipecat-stt/parakeet.sock

# Nemotron 3.5 — default model mlx-community/nemotron-3.5-asr-streaming-0.6b
uv sync --extra nemotron
uv run python -m stt_server serve --backend nemotron \
    --socket-path ~/Library/Caches/pipecat-stt/nemotron.sock

# Pick a specific model with --model (any compatible mlx-community HF repo id)
uv run python -m stt_server serve --backend mlx \
    --model mlx-community/whisper-small --socket-path .../stt.sock
```

`--model` is passed through verbatim; an unset value uses the backend-aware
default (the Whisper repo for `mlx`/`echo`, `parakeet-tdt-0.6b-v3` for
`parakeet`, `nemotron-3.5-asr-streaming-0.6b` for `nemotron`,
`kyutai/stt-1b-en_fr-mlx` for `kyutai`). Pointing a
backend at a mismatched repo fails fast at decode.

Each Unix socket gets a sibling `<socket>.lock` file (e.g. `parakeet.sock.lock`,
mode `0600`). The server holds an `flock` on it for its lifetime, so a second
server for the same socket fails at once with `EADDRINUSE`. The kernel releases
it when the process exits; the file is never deleted and is safe to leave. See
[Operations: single-server lock file](docs/operations.md#single-server-lock-file).

Nemotron's runtime dependency is `mlx-audio>=0.4.4` — the first PyPI release
carrying Nemotron STT support ([Blaizzy/mlx-audio#774](https://github.com/Blaizzy/mlx-audio/pull/774),
merged 2026-06-05). Before 0.4.4 this backend had to git-pin `mlx-audio` in a
`[dependency-groups]` dev group (a direct-URL dependency PyPI rejects in a
published extra); since 0.3.2 it is a clean, PyPI-installable `nemotron` extra
like `mlx` and `parakeet`.

Common MLX Whisper models (smaller = faster + lower RAM, larger = more
accurate). These are `mlx-community` Hugging Face repos; the first launch
downloads and caches the weights.

| `--backend` | `--model` | Notes |
|---|---|---|
| `mlx` | `mlx-community/whisper-large-v3-turbo` | **default** — best accuracy/speed balance |
| `mlx` | `mlx-community/whisper-large-v3` | highest accuracy, slowest, most RAM |
| `mlx` | `mlx-community/whisper-medium` | mid accuracy/speed |
| `mlx` | `mlx-community/whisper-small` | faster, lighter |
| `mlx` | `mlx-community/whisper-base` | fast, lower accuracy |
| `mlx` | `mlx-community/whisper-tiny` | fastest, lowest accuracy |
| `parakeet` | `mlx-community/parakeet-tdt-0.6b-v3` | **default** Parakeet TDT |
| `nemotron` | `mlx-community/nemotron-3.5-asr-streaming-0.6b` | **default** Nemotron 3.5 ASR (`uv sync --extra nemotron`) |
| `kyutai` | `kyutai/stt-1b-en_fr-mlx` | **default** Kyutai streaming STT, English + French (`uv sync --extra kyutai`, on its own) |

Any `mlx-community` Whisper repo (e.g. `…-large-v3-turbo-q4` quantised
variants, or `…-large-v3-turbo` language-specialised forks) works as a
`--model` value — the table lists common starting points, not an allowlist.
Verify which model a running server actually loaded with
`python -m stt_server status` (it prints `backend: <name> (model: <repo>)`).

## Prefetching models

`mlx`, `parakeet`, `nemotron` and `kyutai` download their checkpoint on the first decode,
so a cold server answers its first request only after a multi-GB download.
`prefetch` does the download up front, and `--check` answers "is it cached?"
without touching the network:

```bash
uv run python -m stt_server prefetch --backend parakeet          # download
uv run python -m stt_server prefetch --backend parakeet --check  # offline probe
uv run python -m stt_server prefetch --backend mlx --model mlx-community/whisper-small --json
```

It fetches exactly what the backend loads, into the standard Hugging Face cache
(`HF_HOME` / `HF_HUB_CACHE`). `--model` may also name a local model directory: a
value starting with `/`, `./`, `../` or `~` (or an existing relative path) is a
local path, anything else a Hub repo id. A local directory is never downloaded;
`--check` reports it cached only if it holds every file the backend requires,
and a fetch fails with `ModelInvalidError` if it does not. `kyutai` loads by Hub
repo id only: `prefetch` rejects a local path for it (`ConfigError`), while
`serve --backend kyutai --model <path>` passes the value to the Hub as a repo id
and fails at model load.

```bash
uv run python -m stt_server prefetch --backend parakeet --model ~/models/parakeet --check
```

`--check` exits 0 whenever the probe ran; the answer is the `cached` field. With
`--json` each stdout line is one event:

| Event | Fields |
|---|---|
| `start` | `repo`, `revision`, `kind` |
| `progress` | `repo`, `downloaded_bytes`, `total_bytes`, `files_done`, `files_total` (counts only files not yet cached; see below) |
| `done` | `repo`, `path`, `size_bytes` |
| `complete` | `backend`, `model`, `cached`, `size_bytes` |
| `check` | `repo`, `revision`, `kind`, `required`, `cached`, `path`, `size_bytes` (with `--check`) |
| `summary` | `backend`, `model`, `cached`, `size_bytes` (with `--check`) |
| `error` | `code`, `message`, `repo`; exit status 1 |

`progress` counts only what this run must download, as in `tts_server prefetch`:
`total_bytes` / `files_total` exclude files already in the cache,
`downloaded_bytes` includes partial blobs from an interrupted run, and the last
`progress` before `done` has `downloaded_bytes == total_bytes` and
`files_done == files_total` (all zero when everything was cached).

The event schema matches `tts_server prefetch` in
[pipecat-local-tts-server](https://github.com/vr000m/pipecat-local-tts-server),
so one supervisor can drive both. The `error` codes are per repo; see
[Error codes](#error-codes-per-repo) below.

## Supervised mode

`serve --supervised` is for a parent process that manages the server, such as a
menu bar app. Logging on stderr is unchanged. In addition:

- **stdout carries only JSON lines.** Once the listener is bound and the model
  is warm (supervised mode decodes 1 s of silence first), the server prints one
  `{"event":"ready","backend","model","host","port","socket_path","rate","pid"}`
  line. With `--port 0`, `port` is the real port.
- **A fatal error prints `{"event":"error","code","message","exc_type"}`** and
  exits 1. See [Error codes](#error-codes-per-repo) for the `code` values.
- **stdin EOF stops the server** exactly like SIGTERM. The parent keeps a pipe
  open, so the server exits when the parent closes it or dies. During startup
  the process exits at once; interrupted downloads resume next time. (Embedding
  `stt_server.server.serve()` in your own process? It cancels startup and shuts
  down normally instead, unless you pass `hard_exit_on_stop=True`. A
  `TranscriptionServer` is single-use once `shutdown()` was called or a failed
  start left work running (`stopping`); a failed start that returned to `new`
  may be retried. Await `wait_stopped()` before reusing the socket: the lock
  can outlive `shutdown()`. Which of the server's other signals to read, and
  `adopt()` for your own tasks, is documented once in the
  `TranscriptionServer` class docstring.)

```bash
( sleep 5 ) | uv run python -m stt_server serve --backend echo \
    --host 127.0.0.1 --port 0 --supervised
```

Skip any stdout line that does not parse as JSON: native code writing straight
to file descriptor 1 can bypass the redirect.

### Error codes (per repo)

The `error` event's `code` (from `serve --supervised` and `prefetch --json`) is
a stable string. The lifecycle channel has the same shape as `tts_server`'s,
but each repo has its own code list:

| Code | This repo (STT) | When |
|---|---|---|
| `EADDRINUSE` | yes | a busy port, or a Unix socket a live server already owns (checked before the model loads) |
| `EACCES` | yes | permission denied (including an unopenable `<socket>.lock`) |
| `ModuleNotFoundError` | yes | the backend's extra is not installed |
| `ModelDownloadError` | yes | a model fetch failed (network library error, or a network error during a fetch) |
| `ModelInvalidError` | yes (STT only) | the cached or local model is incomplete or unreadable (missing files, a `config.json` that does not parse or name its files) |
| `ConfigError` | yes | a rejected endpoint or CLI input: non-loopback host, missing endpoint, an untrusted socket directory, a malformed Hub repo id, an unsupported local model path, an unknown backend name (`--backend` / `import_backend()`) |
| `BackendStartError` | yes | anything else |

`pipecat-local-tts-server` has no `ModelInvalidError` and adds
`SocketPathConflict`; check its README for its list. **A supervisor driving
both should treat any code it does not recognise as `BackendStartError`.**

## Adding a backend

A backend is registered in one place and picked up by `--backend`, the
`--model` default and `prefetch`. Checklist:

1. **Module** `stt_server/backends/<name>.py`: a class implementing
   `TranscriptionBackend` (`backend_name`, `model`, `start()`, `open_stream()`,
   `close()`) and a module-level `prefetch_specs(model) -> list[SnapshotSpec]`
   (`stt_server.snapshot`). Each `SnapshotSpec.required` entry is a glob
   (relative to the snapshot root) that must match a file for `prefetch
   --check` to report it cached; a tuple entry is a set of alternatives, any one
   of which suffices (see the field docstring in `stt_server/snapshot.py`).
   Import the optional extra only inside `start()` or
   the lazy model load, never at module load, and re-raise a missing extra as
   `ModuleNotFoundError` with the `uv sync --extra <name> --inexact` hint. Reuse
   `DecodeGuardMixin` (`backends/_thread_util.py`) for MLX/Metal decodes.
2. **Error origin**: wrap only the fetch-and-load call in
   `snapshot.download_phase()` (never a decode) and tag an unusable fetched
   snapshot with `snapshot.mark_model_invalid()`.
3. **Optional `abort()`** if the backend owns cleanup that must survive a failed
   start or a hard exit (e.g. a private temp dir): synchronous, idempotent, safe
   while a decode thread still runs, and undone by the next `start()`. A failed
   start calls it after `close()` when the backend's `start()` had returned,
   and alone when it had not; the server may then be retried (a failed start that left work running is
   single-use instead, but the backend can go to a new server).
   Optional `idle()` (`IdleAwareBackend`) reports when no decode thread is left;
   the server keeps its lock until then. The full contracts are in
   `stt_server/backend.py`.
4. **Registry**: a `BackendEntry` in `stt_server/backends/__init__.py`
   `BACKENDS` (module, class name, default model, `local_model_paths`).
5. **Packaging**: an extra in `pyproject.toml` (plus a `[tool.uv]` `conflicts`
   entry if its pins clash with the other MLX extras) and a CI job in
   `.github/workflows/test.yml` if it needs its own environment.
6. **Tests**: `tests/test_backend_registry.py` covers every registry entry; add a
   `tests/test_<name>_backend.py` with fake modules for the stream contract.
7. **Docs**: the backend table above, [`docs/operations.md`](docs/operations.md)
   (`--backend` list; socket table if it gets a LaunchAgent), and `CHANGELOG.md`.

## Documentation

Deeper references live under [`docs/`](docs/):

- [Operations and deployment](docs/operations.md) — multi-backend operation, two-agent LaunchAgent install, A/B benchmarking, health checks, and Whisper hallucination-suppression knobs.
- [Client and integration](docs/integration.md) — Python `TranscriptionClient` usage, the Pipecat `SegmentedSTTService` example, and the Koda reference consumer.
- [Server architecture](docs/architecture.md) — the lifecycle state machine, the single-server lock, stop-during-startup, and what a supervisor can rely on.
- [Protocol subset](docs/protocol.md) — the V1 wire events and how they deviate from the OpenAI Realtime transcription snapshot.
- [Upgrading 0.1.x → 0.2.0](docs/migration.md) — runtime-surface rename migration.

Development plans live in [`docs/dev_plans/`](docs/dev_plans/).
