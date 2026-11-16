# pipecat-local-stt-server

Run speech recognition locally and connect over WebSocket from Python or a
Pipecat pipeline. The server supports five speech-recognition backends and
includes a small Python client.

The PyPI package is `pipecat-local-stt-server`; the import and CLI module remain
`stt_server`. BSD-2-Clause.

<a id="v1-scope"></a>

## What it supports

- one WebSocket connection per transcription session
- PCM16LE, mono, 16 kHz audio; binary frames by default, base64 JSON for
  compatibility
- commit-oriented decoding with one final transcript per utterance
- pipeline-provided VAD (`turn_detection: null`); no server-side VAD
- Unix sockets with same-uid peer checks, or loopback-only TCP with optional
  bearer-token authentication
- MLX Whisper, Parakeet, Nemotron, Kyutai, and Moonshine backends
- supervised lifecycle events for launchd, menu bar apps, and other parents

The protocol is intentionally smaller than OpenAI Realtime transcription. See
the [wire protocol](docs/protocol.md) for the event set and deviations.

<a id="running-the-server"></a>

## Quick start

Use Python 3.12+ and `uv`. These commands run from a repository checkout.
MLX backends need Apple silicon; see [backend requirements](docs/backends.md)
for platform constraints. The `echo` backend below tests the connection without
downloading a model; it does not recognise speech.

The legacy flat CLI form, `python -m stt_server <serve flags>`, remains
supported and routes to `serve`; new commands should use the explicit
`python -m stt_server serve <flags>` form shown below.

```bash
git clone https://github.com/vr000m/pipecat-local-stt-server.git
cd pipecat-local-stt-server
uv sync --inexact

# Recommended local transport: owner-only Unix socket with peer-uid checking.
uv run --no-sync python -m stt_server serve \
  --socket-path ~/Library/Caches/pipecat-stt/stt.sock \
  --backend echo
```

Keep the server running and open a second terminal for the status probe below.
Use `uv run --no-sync` to keep the installed dependencies.

Or use loopback TCP instead of the Unix socket. There is no default port;
non-loopback hosts are rejected.

```bash
uv run --no-sync python -m stt_server serve \
  --host 127.0.0.1 --port 9900 \
  --backend echo
```

Loopback TCP has no peer-uid check: any local process can connect. For a shared
host, add `--auth-token-file /absolute/path/to/token`. The file must be a
regular file you own, mode `0600`, under directories only you or root can
write. Browser `Origin` headers are rejected. See
[Endpoint security](docs/operations.md#loopback-tcp-caveats) and
[token-file requirements](docs/security.md#token-file-replaces-env-tokens).

In the second terminal, run the matching probe from the repository root:

```bash
uv run --no-sync python -m stt_server status \
  --socket-path ~/Library/Caches/pipecat-stt/stt.sock

uv run --no-sync python -m stt_server status \
  --host 127.0.0.1 --port 9900
```

<a id="choosing-a-backend-and-model"></a>
<a id="prefetching-models"></a>

## Backends

To transcribe speech, install a backend extra and replace `--backend echo`
with that backend in the quick-start command. Stop the echo server first.

| Backend | Install | Default model |
|---|---|---|
| MLX Whisper (`mlx`) | `uv sync --extra mlx --inexact` | `mlx-community/whisper-large-v3-turbo` |
| Parakeet | `uv sync --extra parakeet --inexact` | `mlx-community/parakeet-tdt-0.6b-v3` |
| Nemotron | `uv sync --extra nemotron --inexact` | `mlx-community/nemotron-3.5-asr-streaming-0.6b` |
| Kyutai | `just stt-install kyutai` | `kyutai/stt-1b-en_fr-mlx` |
| Moonshine | `uv sync --extra moonshine --inexact` | `medium-streaming@en` (CPU) |

Kyutai needs Python 3.12 and a separate `.venv-kyutai` because its MLX pins
conflict with the other extras. The Hugging Face defaults are pinned to
immutable revisions; Moonshine is pinned by its package version. Custom
repositories and local model paths are supported where
the backend allows them.

See [Backends and models](docs/backends.md) for model selection, pinning,
prefetch event semantics, local paths, and Whisper tuning.

Warm a model before starting an always-on server:

```bash
uv run --no-sync python -m stt_server prefetch --backend parakeet
uv run --no-sync python -m stt_server prefetch --backend parakeet --check
```

<a id="managing-agents-with-just"></a>
<a id="smoke-tests"></a>

## Persistent macOS agents

The repository includes a `just` operator layer over per-user LaunchAgents:

```bash
just stt-install whisper   # install/start the canonical agent
just stt-status whisper    # wire-level health probe
just stt-list              # state, pid, endpoint, and live backend for all agents
```

Agents use canonical loopback ports by default; Unix sockets are opt-in with
`PIPECAT_STT_SOCKET`. Use `PIPECAT_STT_AUTH_TOKEN_FILE` for authenticated TCP
agents—environment tokens are refused because a plist would persist them.

The [operations guide](docs/operations.md) covers installation, agent commands,
canonical ports, custom endpoints, and health checks.

## Python and Pipecat clients

Use `TranscriptionClient` to send 16 kHz mono PCM16 audio, commit an utterance,
and read its transcript. The [client guide](docs/integration.md) includes the
event loop, a WAV-file example, and a Pipecat `SegmentedSTTService` integration.
A client chooses the server's endpoint; the server chooses the backend.

<a id="supervised-mode"></a>
<a id="error-codes-per-repo"></a>

## Supervised processes

`serve --supervised` emits JSON Lines lifecycle events, reports stable error
codes, and treats stdin EOF like SIGTERM. See the
[supervised process contract](docs/architecture.md#supervised-process-contract)
for ready/error events and stop-during-startup guarantees.

<a id="adding-a-backend"></a>

## Documentation

| Task | Guide |
|---|---|
| Choose, prefetch, or tune a model | [Backends and models](docs/backends.md) |
| Install and operate persistent agents | [Operations and deployment](docs/operations.md) |
| Use the Python client or Pipecat | [Client and integration](docs/integration.md) |
| Understand lifecycle and supervision | [Server architecture](docs/architecture.md) |
| Implement the wire protocol | [Protocol subset](docs/protocol.md) |
| Upgrade an older installation | [Migration guide](docs/migration.md) |
| Change code, docs, or add a backend | [Contributing](docs/contributing.md) |
| Check a deployment or compare models | [Testing and benchmarks](docs/testing.md) |
| Configure authentication and socket access | [Transport security](docs/security.md) |

Development decisions and historical verification live in
[`docs/dev_plans/`](docs/dev_plans/).
