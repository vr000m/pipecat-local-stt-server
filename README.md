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
  `nemotron` dev group — `uv sync --group nemotron`, not an extra; default model
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

The CLI accepts both `python -m stt_server <flags>` (the legacy flat form,
which implicitly routes to `serve`) and `python -m stt_server serve <flags>`
/ `python -m stt_server status <flags>` — see "Checking server health"
below for the `status` subcommand.

For persistent always-on operation on macOS, install the server as a
LaunchAgent via `scripts/install_stt_agent.sh`
(`install|start|stop|restart|status|logs`); pair it with
`python -m stt_server status` for a wire-level health probe. A consumer
may wrap these behind its own CLI — see "Reference consumer (Koda)"
below for one such integration.

## Multi-backend operation

Each server process loads exactly **one** backend, pinned at launch via
`--backend {echo,mlx,parakeet,nemotron}`. To run more than one ASR — for example to
A/B-benchmark Parakeet against Whisper — start a second server process on a
**separate socket**. The V1 wire protocol is unchanged; the only difference
between two ASRs from the bot's perspective is which socket it connects to.

### Per-ASR socket convention

| ASR | LaunchAgent label | Socket | Bot selection |
|---|---|---|---|
| whisper (`mlx`) | `pipecat.stt-server` | `~/Library/Caches/pipecat-stt/stt.sock` | leave `STT_WS_SOCKET` unset |
| parakeet | `pipecat.stt-server.parakeet` | `~/Library/Caches/pipecat-stt/parakeet.sock` | set `STT_WS_SOCKET` to the parakeet socket |
| nemotron | `pipecat.stt-server.nemotron` | `~/Library/Caches/pipecat-stt/nemotron.sock` | set `STT_WS_SOCKET` to the nemotron socket |

Whisper uses the default label and socket, so a bot-side default of
`~/Library/Caches/pipecat-stt/stt.sock` resolves to it with no `.env`
change. Selecting Parakeet is a one-env-var flip: point
`STT_WS_SOCKET` at `.../parakeet.sock`. The flip is **bot-wide** — in the
dual-input bot both the Me and Them branches connect to the same resolved
endpoint, so both arms always use the same ASR. See `.env.example` for the
client-side configuration.

### Two-agent install

`scripts/install_stt_agent.sh` is parameterised by `PIPECAT_STT_LABEL` /
`PIPECAT_STT_SOCKET` / `PIPECAT_STT_BACKEND` (the legacy `KODA_STT_*` names
are still honoured as deprecated aliases) so two LaunchAgents can coexist
without plist or log collisions:

```bash
# 1. Whisper agent — default env keeps the legacy label + socket.
scripts/install_stt_agent.sh install

# 2. Parakeet agent — distinct label, socket and backend.
#    Warm the ~1.5 GB Hugging Face model cache FIRST: a cold first launch
#    downloads it under KeepAlive + ThrottleInterval=10 and launchd may
#    throttle-loop the agent before the download finishes.
uv sync --extra parakeet
.venv/bin/python -c 'import parakeet_mlx; parakeet_mlx.from_pretrained("mlx-community/parakeet-tdt-0.6b-v3")'
PIPECAT_STT_LABEL=pipecat.stt-server.parakeet \
  PIPECAT_STT_SOCKET="$HOME/Library/Caches/pipecat-stt/parakeet.sock" \
  PIPECAT_STT_BACKEND=parakeet \
  scripts/install_stt_agent.sh install
```

The script manages exactly **one** agent per invocation, identified by
`PIPECAT_STT_LABEL` (+ its socket) — there is no registry or "all" mode. To run
any subcommand (`uninstall`/`start`/`stop`/`restart`/`status`/`logs`) against
the Parakeet agent you must re-export its `PIPECAT_STT_LABEL` and
`PIPECAT_STT_SOCKET` (legacy `KODA_STT_*` aliases still work); a default-env
invocation always targets the default `pipecat.stt-server` agent. See the recipe
in the `install_stt_agent.sh` header.

### Managing agents with `just`

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
just stt-install parakeet  # delegates to install_stt_agent.sh
just stt-uninstall parakeet
```

`<backend>` is one of `whisper` / `parakeet` / `nemotron`, mapped to the labels
and sockets in the [per-ASR table](#per-asr-socket-convention) above (the
justfile map is a checked mirror of that table — a test fails CI on drift).

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

### A/B benchmark — Whisper vs Parakeet

With both agents installed and socket-live, `scripts/benchmark_asr_ab.py`
replays a corpus of utterances through **both** servers and reports
per-utterance Word Error Rate (WER) + decode latency, plus aggregates. It is
a pure V1 client — no protocol surface added — and a one-off operator tool
(no REST counterpart, not a CI gate).

```bash
# Default endpoints: whisper on stt.sock, parakeet on parakeet.sock.
uv run python scripts/benchmark_asr_ab.py --corpus path/to/corpus

# Write a full JSON report alongside the console summary.
uv run python scripts/benchmark_asr_ab.py --corpus path/to/corpus \
    --json-out benchmarks/results/asr_ab.json
```

The corpus is a directory of `<stem>.wav` (16 kHz mono PCM16) + `<stem>.txt`
reference-transcript pairs, named explicitly on the command line. The
benchmark **fails fast** if only one of the two endpoints answers — it never
silently benchmarks a single ASR. The corpus is never baked into the script;
`docs/benchmarks` / `~/koda-data` JSON corpora carry real names and
financials, so the script refuses a `--corpus` under those roots unless
`--allow-pii-corpus` is passed. Use a synthetic or consented-recording corpus
and keep it outside the repo.

## Upgrading from 0.1.x to 0.2.0

0.2.0 renames the default runtime surface from the legacy `koda`-prefixed
namespace to a `pipecat`-namespaced default. Nothing changes for the wire
protocol or the Python import name (`stt_server`); only the LaunchAgent
label, default socket path, and default log dir/basenames move:

| | v0.1.x default | 0.2.0 default |
|---|---|---|
| LaunchAgent label | (legacy `koda`-prefixed) | `pipecat.stt-server` |
| Socket | `~/Library/Caches/`…`/stt.sock` (legacy dir) | `~/Library/Caches/pipecat-stt/stt.sock` |
| Log dir | `~/Library/Logs/`… (legacy dir) | `~/Library/Logs/pipecat-stt/` |
| Log basenames | (legacy `*-stt.{log,err}`) | `pipecat-stt.{log,err}` |

The deprecated `KODA_STT_*` environment-variable **names** are unaffected —
they remain honoured aliases (`KODA_STT_LABEL` / `KODA_STT_SOCKET` /
`KODA_STT_LOG_DIR` still override the new defaults). Only the default
*values* changed.

To upgrade an existing v0.1.x install:

1. **Re-run the installer.** `scripts/install_stt_agent.sh install` (with the
   default env) bootstraps the renamed `pipecat.stt-server` agent and
   automatically retires the legacy `koda`-prefixed agents — both the v0.1.x
   whisper and parakeet LaunchAgents — by booting them out of launchd and
   removing their `*.plist` files. This migration is idempotent: it is a no-op
   on a fresh machine and never retires the new agent. It only fires for the
   default `pipecat.stt-server` install; custom-label installs manage only
   their own selected label.

2. **Re-point pinned socket consumers.** Anything hard-coded to the old socket
   path must move to the new one. Set `STT_WS_SOCKET` to
   `~/Library/Caches/pipecat-stt/stt.sock` (or re-point a wrapper's
   `STT_WS_DEFAULT_SOCKET` fallback at the same path). The rename does **not**
   reach across to the external koda-pipecat `./koda stt` wrapper — its
   `STT_WS_DEFAULT_SOCKET` default still points at the old (v0.1.x) Caches
   socket, so re-point it or set `STT_WS_SOCKET` directly.

3. **Old dirs are left in place.** The previous socket and log directories are
   not deleted — they are simply orphaned and harmless once the new agent is
   running. Remove them by hand if you want to reclaim the space.

## Checking server health

The server answers a `server.status` wire event with its current session
state (queue depth, uncommitted bytes, uptime) and process health (pid,
peak RSS), and, on connect, replies with a `server.hello` carrying
protocol version, audio format, and capabilities. Both `server.hello` and
`server.status` also carry a `backend` object — `{"name": ..., "model": ...}` —
naming the ASR actually behind the socket, so a client can verify it rather
than trust the socket path. The `status` subcommand wraps that round-trip:

```bash
# Text output (exit 0 on success, 1 on not-reachable/timeout/error)
uv run python -m stt_server status --socket-path ~/Library/Caches/pipecat-stt/stt.sock

# Raw JSON for scripting / monitoring
uv run python -m stt_server status --socket-path ... --json

# Loopback TCP with bearer token
uv run python -m stt_server status --host 127.0.0.1 --port 8765 \
    --auth-token-file /path/to/token
```

Representative text output:

```
stt_server status: ok
  protocol: 0.1
  audio: pcm16 @ 16000 Hz / 1ch
  capabilities: binary_audio=True base64=True server_vad=False
  session_id: session_abc123
  queue_depth: 0
  uncommitted_bytes: 0
  session_uptime: 0.1s
  pid: 12345
  rss: 1800.3MB (peak)
```

`rss` is **peak** resident set size from `resource.getrusage` — it
climbs monotonically within a process lifetime and resets on
LaunchAgent restart. Useful for leak detection (peak only grows when
a leak is actually growing), not for real-time memory monitoring.

The `server.status` reply fields, for scripting against `--json`:

| Field | Type | Meaning |
|---|---|---|
| `type` | string | `"server.status"` |
| `session_id` | string | current session id |
| `queue_depth` | int | 0 or 1 — in-flight decode tasks for this session |
| `uncommitted_bytes` | int | PCM bytes buffered but not yet committed |
| `uptime_seconds` | float | seconds since this session was created |
| `pid` | int | server process id |
| `rss_bytes` | int | peak RSS in bytes, normalized across macOS/Linux |

Use this as a preflight before starting a client, in CI smoke tests, or
from a LaunchAgent keepalive script. The existing `--socket-path`/`--host`/
`--port`/`--auth-token-file` endpoint flags work for both `serve` and
`status` subcommands.

## Client usage

Only `TranscriptionClient` (plus `protocol`, `backend` interfaces, and
`EchoBackend`) is re-exported from the package root — server runtime
(`TranscriptionServer`, `ServerConfig`, `serve`) lives under
`stt_server.server`. This lets a client-only install (`client` extra)
skip the `websockets.asyncio.server` dependency.

```python
from stt_server import TranscriptionClient

async def run():
    # UDS (recommended)
    async with TranscriptionClient(
        socket_path="~/Library/Caches/pipecat-stt/stt.sock"
    ) as c:
        await c.update_session(turn_detection=None)
        await c.send_audio(pcm_bytes)     # binary PCM16LE frames
        await c.commit()
        async for ev in c.events():
            if ev["type"] == "conversation.item.input_audio_transcription.completed":
                print(ev["transcript"])
                await c.close_session()
                break

    # Loopback TCP with bearer token
    async with TranscriptionClient(
        host="127.0.0.1", port=8765, auth_token="..."
    ) as c:
        ...
```

The example `stt_server/examples/file_stream.py` streams a WAV file end to end.

## Choosing a backend and model

The **server** picks the ASR, pinned at launch — the client never selects it.
A client (including the Pipecat service below) only points at an *endpoint*
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

# Nemotron 3.5 — default model mlx-community/nemotron-3.5-asr-streaming-0.6b.
# NOTE: installed via a dev GROUP, not an extra: `uv sync --group nemotron`
# (there is intentionally no `--extra nemotron`).
uv sync --group nemotron
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

Nemotron ships behind a `[dependency-groups]` **dev group** rather than a
PyPI extra (`uv sync --group nemotron`, not `--extra nemotron`). The backend
needs Nemotron STT support from `mlx-audio`, which only landed in PR #774 —
not yet in any published `mlx-audio` release. A dev group therefore git-pins
the dependency directly:

```bash
# Equivalent direct install of the git-pinned mlx-audio one-liner:
uv pip install "mlx-audio @ git+https://github.com/Blaizzy/mlx-audio"
```

It is a dev group on purpose: a direct-URL (`@ git+…`) dependency cannot be
emitted into a published wheel's `Requires-Dist` (PyPI rejects direct-URL
deps in published extra metadata), whereas PEP 735 dependency groups are
never written into wheel/sdist metadata at all. Keeping Nemotron in a dev
group lets `uv sync --group nemotron` install it locally while 0.3.0 stays
PyPI-clean. Once `mlx-audio` cuts a PyPI release containing #774, this can
be promoted to a versioned `nemotron` extra.

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
| `nemotron` | `mlx-community/nemotron-3.5-asr-streaming-0.6b` | **default** Nemotron 3.5 ASR (dev group — `uv sync --group nemotron`) |

Any `mlx-community` Whisper repo (e.g. `…-large-v3-turbo-q4` quantised
variants, or `…-large-v3-turbo` language-specialised forks) works as a
`--model` value — the table lists common starting points, not an allowlist.
Verify which model a running server actually loaded with
`python -m stt_server status` (it prints `backend: <name> (model: <repo>)`).

## Pipecat integration

`stt_server/examples/pipecat_stt_service.py` is a runnable
`SegmentedSTTService` subclass (`LocalWebSocketSTTService`) that wires
`TranscriptionClient` into a Pipecat pipeline. It is an example, not part of
the installed package — Pipecat is not a dependency of this project. Install
both to use it:

```bash
uv pip install "pipecat-ai" "pipecat-local-stt-server[client]"
```

Then point the service at a running server's endpoint and add it to a
pipeline:

```python
from stt_server.examples.pipecat_stt_service import LocalWebSocketSTTService

stt = LocalWebSocketSTTService(
    socket_path="~/Library/Caches/pipecat-stt/stt.sock",
    # or: host="127.0.0.1", port=8765, auth_token="..."
    sample_rate=16000,  # the server's wire format is pinned to 16 kHz mono
)
# pipeline = Pipeline([transport.input(), stt, llm, tts, transport.output()])
```

Two requirements follow from how the server works:

- **VAD is supplied by your pipeline, not this service.** `SegmentedSTTService`
  transcribes one utterance per VAD segment, so the transport/pipeline must
  emit `VADUserStartedSpeakingFrame` / `VADUserStoppedSpeakingFrame` (e.g. a
  Silero VAD analyzer on the transport). The service buffers between those and
  calls the server once per segment — which matches this server's
  commit-oriented protocol (append → commit → one final transcript).
- **Run at 16 kHz mono.** The wire format is pinned to 16 kHz mono PCM16, so
  configure the transport/pipeline `sample_rate=16000`; the example emits an
  `ErrorFrame` rather than silently mis-transcribing a mismatched rate.

To switch Whisper ↔ Parakeet, change *which server* the service connects to
(its `socket_path`/`host`+`port`), not the service code. `server.hello` carries
the backend identity, so the example logs which ASR it connected to and can
optionally hard-assert it (see `_log_backend`).

## Protocol subset

Client -> server JSON events:

- `session.update`
- `input_audio_buffer.append` (base64 compat mode; binary frames are the V1 default)
- `input_audio_buffer.commit`
- `server.status`
- `session.close`
- `session.cancel`

Server -> client JSON events:

- `server.hello`
- `session.created`
- `session.updated`
- `input_audio_buffer.committed`
- `conversation.item.input_audio_transcription.delta`
- `conversation.item.input_audio_transcription.completed`
- `session.closed`
- `server.status`
- `error`

Deviations from the OpenAI Realtime transcription snapshot (2026-04-20):

- no conversation graph, no output audio, no tools/assistant responses
- `item_id` and server `event_id` are server-minted; `previous_item_id`
  omitted
- deltas collapse to a single final-sized `delta` + `completed` on the MLX
  backend
- `speech_started` / `speech_stopped` are never emitted in V1 (server VAD
  disabled)
- custom events: `server.hello`, `server.status`, `session.close`,
  `session.cancel`, `session.closed`

## Reference consumer (Koda)

This section documents how one consumer — the Koda bot, for which this
server was originally built — integrates the client. It is included as a
worked example of the client contract, not a dependency: nothing in this
package imports or requires Koda. `bot/stt/websocket_stt_service.py`
(in the consumer repo) is a Pipecat `SegmentedSTTService` subclass that:

- owns the WebSocket session across `start(StartFrame)` / `stop(EndFrame)` /
  `cancel(CancelFrame)` / `cleanup()`
- drives `commit` from Koda's branch-local VAD (server-side VAD stays
  disabled via `turn_detection: null`)
- translates `conversation.item.input_audio_transcription.completed` into
  a single finalized `TranscriptionFrame` per segment
- awaits `session.updated` via a one-shot future before returning from
  `_ensure_connected`, so the first commit cannot race the language
  config
- on decode timeout, tears down the socket (V1 has no `item_id`
  correlation on the client side, so a late `completed` from an
  abandoned decode would otherwise resolve the next segment's future
  with stale text)
- on server crash, fails the in-flight segment fast via a reader that
  sets `ConnectionError` on unexpected socket close, then reconnects
  on the next `run_stt` with up to 6 attempts on an exponential
  schedule (0.5 → 8s, ~15.5s total budget) before surfacing an
  `ErrorFrame`

Enable via `STT_SERVICE=websocket` in `.env`. Client env vars:

| Variable | Default | Description |
|---|---|---|
| `STT_WS_SOCKET` | *(unset)* | UDS path. Koda's `./koda stt` wrapper exports a `STT_WS_DEFAULT_SOCKET` fallback that still points at the old (v0.1.x) Caches socket path; after the 0.2.0 rename, re-point that wrapper default at `~/Library/Caches/pipecat-stt/stt.sock` (or set `STT_WS_SOCKET` directly). See "Upgrading from 0.1.x to 0.2.0". |
| `STT_WS_HOST` / `STT_WS_PORT` | *(unset)* | Loopback TCP target |
| `STT_WS_URI` | *(unset)* | Full `ws://` or `wss://` URI. Pairing `STT_WS_TOKEN` with `ws://` to a non-loopback host emits a cleartext-token WARNING. |
| `STT_WS_TOKEN` | *(unset)* | Bearer token; only enforced when the server was started with a matching token. Configure one for any TCP deployment. |
| `STT_WS_DEFAULT_SOCKET` | *(unset)* | Consumer-supplied fallback UDS path when no other target is configured — the library ships no built-in default. |

Precedence (`STT_WS_URI > STT_WS_SOCKET > STT_WS_HOST+PORT`) is enforced by
`stt_server.client.resolve_endpoint_from_env`. Consumers (Koda's `bot/runtime`
and `python -m stt_server status`) both call it so the resolution rules
cannot drift. `stt_server.client.is_cleartext_remote(uri)` is the helper
the Koda bot uses to detect cleartext-token misconfigurations.

Each `WebSocketSTTService` instance owns exactly one websocket session,
so Koda's dual bot gets two independent sessions (`me` / `them`) against
a single shared server. `BranchVADUserStartedSpeakingFrame` /
`BranchVADUserStoppedSpeakingFrame` stay inside Koda; the server
intentionally has no notion of branches or speakers.

For persistent operation, `./koda stt install` (which shells into
`scripts/install_stt_agent.sh`) renders a LaunchAgent (`pipecat.stt-server`)
via `scripts/render_stt_plist.py` (stdlib `plistlib` + allowlist
validation — do not reintroduce `sed` templating). Overrides (canonical
`PIPECAT_STT_*` names; legacy `KODA_STT_*` names still honoured as
deprecated aliases): `PIPECAT_STT_SOCKET`, `PIPECAT_STT_BACKEND`,
`PIPECAT_STT_MODEL`, `PIPECAT_STT_LOG_DIR`, `PIPECAT_STT_AUTH_TOKEN`.
Use `./koda stt status` for a wire-level health check.

### Whisper hallucination suppression (MLX backend)

The MLX Whisper backend forwards four decode-time knobs to
`mlx_whisper.transcribe()` to suppress the cascading-repetition failure
mode (hundreds of `subscription subscription…` lines emitted as a single
segment). Defaults match OpenAI's reference Whisper EXCEPT
`condition_on_previous_text`, which we disable: feeding the previous
chunk's emitted text back as a decoder prompt creates a self-amplifying
loop on hallucinated tokens. Bool parser accepts `1`/`true`/`yes`/`on`
(case-insensitive); anything else — including `False`, `0`, empty, or
unset — is `False`.

Each variable below is canonical (`PIPECAT_STT_*`); its legacy `KODA_STT_*`
alias is still honoured (canonical wins if both are set). For these numeric
(and the boolean) knobs precedence is *presence-based*: a present-but-empty
canonical value wins and resolves to the default rather than falling through
to a set legacy alias — so blanking the canonical reliably overrides the
alias. (String knobs like the LaunchAgent label instead skip an empty
canonical and fall through to the alias.)

| Variable (canonical) | Default | Description |
|---|---|---|
| `PIPECAT_STT_WHISPER_CONDITION_ON_PREVIOUS_TEXT` | `False` | Condition each chunk's decode on the previous chunk's text. Load-bearing — leave `False`. |
| `PIPECAT_STT_WHISPER_COMPRESSION_RATIO_THRESHOLD` | `2.4` | Flags zlib-compressible (repetitive) output as a failed segment, forces re-decode. |
| `PIPECAT_STT_WHISPER_LOGPROB_THRESHOLD` | `-1.0` | Flags low-confidence segments. |
| `PIPECAT_STT_WHISPER_NO_SPEECH_THRESHOLD` | `0.6` | Drops silence segments before they get a chance to hallucinate. |

After decode, `_decode_sync` runs a degenerate-output filter
(`stt_server.text_quality.is_degenerate`) on each segment. Segments where
the dominant case-folded unigram exceeds the ratio threshold AND the
segment has at least the minimum token count are replaced with an empty
string (and a `mlx_whisper.degenerate_dropped` warning is logged).
Defaults are calibrated against the existing transcript corpus —
p99 = 0.36, p99.5 = 0.40 — so backchannels ("yeah yeah yeah") and other
legitimate high-repetition paragraphs are not flagged.

| Variable (canonical) | Default | Description |
|---|---|---|
| `PIPECAT_STT_WHISPER_DEGENERATE_TOKEN_RATIO` | `0.40` | Drop a segment whose dominant unigram exceeds this share of all tokens. Pinned above the corpus p99.5; raise toward `0.45` first if the monitoring audit shows >1% of segments dropped. |
| `PIPECAT_STT_WHISPER_DEGENERATE_MIN_TOKENS` | `10` | Minimum token count before the ratio check fires — short utterances with one repeated word are not flagged. |

`PIPECAT_STT_WHISPER_DEGENERATE_*` are the canonical names. The earlier
`KODA_TEXT_QUALITY_DEGENERATE_TOKEN_RATIO` /
`KODA_TEXT_QUALITY_DEGENERATE_MIN_TOKENS` names, and the original
`KODA_STT_WHISPER_DEGENERATE_TOKEN_RATIO` /
`KODA_STT_WHISPER_DEGENERATE_MIN_TOKENS` names from the initial ship, are
all still honoured as deprecated backward-compat aliases (canonical wins if
several are set). New deployments should prefer the `PIPECAT_STT_*` names.

These decode-time defences were calibrated against the original transcription
corpus; a consumer's cleanup stage can pair them with a short-circuit on
degenerate input and a symmetric output guard against same-length degenerate
rewrites.
