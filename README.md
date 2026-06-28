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
`parakeet`, `nemotron-3.5-asr-streaming-0.6b` for `nemotron`). Pointing a
backend at a mismatched repo fails fast at decode.

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

Any `mlx-community` Whisper repo (e.g. `…-large-v3-turbo-q4` quantised
variants, or `…-large-v3-turbo` language-specialised forks) works as a
`--model` value — the table lists common starting points, not an allowlist.
Verify which model a running server actually loaded with
`python -m stt_server status` (it prints `backend: <name> (model: <repo>)`).

## Documentation

Deeper references live under [`docs/`](docs/):

- [Operations and deployment](docs/operations.md) — multi-backend operation, two-agent LaunchAgent install, A/B benchmarking, health checks, and Whisper hallucination-suppression knobs.
- [Client and integration](docs/integration.md) — Python `TranscriptionClient` usage, the Pipecat `SegmentedSTTService` example, and the Koda reference consumer.
- [Protocol subset](docs/protocol.md) — the V1 wire events and how they deviate from the OpenAI Realtime transcription snapshot.
- [Upgrading 0.1.x → 0.2.0](docs/migration.md) — runtime-surface rename migration.

Development plans live in [`docs/dev_plans/`](docs/dev_plans/).
