# stt_server

Standalone local WebSocket transcription server plus minimal Python client.

Lives in-tree under `stt_server/` with a repo-neutral name so it can be
extracted into its own OSS repo once the V1 protocol and Pipecat integration
have stabilized. See `docs/dev_plans/20260420-design-whisper-websocket-server.md`
for the full design.

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
  the `stt-server-mlx` extra)
- `ParakeetBackend` shipped in `stt_server/backends/parakeet.py` (requires the
  `stt-server-parakeet` extra; default model `mlx-community/parakeet-tdt-0.6b-v3`).
  Parakeet decodes from a temp WAV; that WAV holds raw utterance audio (PII) and
  is written to a per-process private `0o700` directory (created at backend
  start, removed on `close()`), never the world-listable system temp dir.

## Running the server

```bash
# UDS (recommended for local use — no port, no bearer token)
uv run python -m stt_server --socket-path ~/Library/Caches/koda-stt/stt.sock --backend echo

# MLX Whisper over UDS
uv sync --extra stt-server-mlx
uv run python -m stt_server --socket-path ~/Library/Caches/koda-stt/stt.sock --backend mlx

# Loopback TCP (use --auth-token-file or PIPECAT_STT_AUTH_TOKEN env — legacy
# KODA_STT_AUTH_TOKEN still honoured; --auth-token on argv is visible via
# `ps` and marked DEPRECATED)
uv run python -m stt_server --host 127.0.0.1 --port 8765 --auth-token-file /path/to/token
```

The CLI accepts both `python -m stt_server <flags>` (the legacy flat form,
which implicitly routes to `serve`) and `python -m stt_server serve <flags>`
/ `python -m stt_server status <flags>` — see "Checking server health"
below for the `status` subcommand.

For persistent always-on operation on macOS, the Koda wrapper exposes
`./koda stt install|start|stop|restart|status|logs` as the primary
control surface. It delegates to `scripts/install_stt_agent.sh` (the
underlying LaunchAgent implementation) and layers a wire-level health
probe on top. See the Koda integration section below.

## Multi-backend operation

Each server process loads exactly **one** backend, pinned at launch via
`--backend {echo,mlx,parakeet}`. To run more than one ASR — for example to
A/B-benchmark Parakeet against Whisper — start a second server process on a
**separate socket**. The V1 wire protocol is unchanged; the only difference
between two ASRs from the bot's perspective is which socket it connects to.

### Per-ASR socket convention

| ASR | LaunchAgent label | Socket | Bot selection |
|---|---|---|---|
| whisper (`mlx`) | `koda.stt-server` | `~/Library/Caches/koda-stt/stt.sock` | leave `STT_WS_SOCKET` unset |
| parakeet | `koda.stt-server.parakeet` | `~/Library/Caches/koda-stt/parakeet.sock` | set `STT_WS_SOCKET` to the parakeet socket |

Whisper keeps the legacy label and socket, so the bot-side default in
`bot/runtime.py` (`~/Library/Caches/koda-stt/stt.sock`) still resolves to it
with no `.env` change. Selecting Parakeet is a one-env-var flip: point
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
uv sync --extra stt-server-parakeet
.venv/bin/python -c 'import parakeet_mlx; parakeet_mlx.from_pretrained("mlx-community/parakeet-tdt-0.6b-v3")'
PIPECAT_STT_LABEL=koda.stt-server.parakeet \
  PIPECAT_STT_SOCKET="$HOME/Library/Caches/koda-stt/parakeet.sock" \
  PIPECAT_STT_BACKEND=parakeet \
  scripts/install_stt_agent.sh install
```

The script manages exactly **one** agent per invocation, identified by
`PIPECAT_STT_LABEL` (+ its socket) — there is no registry or "all" mode. To run
any subcommand (`uninstall`/`start`/`stop`/`restart`/`status`/`logs`) against
the Parakeet agent you must re-export its `PIPECAT_STT_LABEL` and
`PIPECAT_STT_SOCKET` (legacy `KODA_STT_*` aliases still work); a default-env
invocation always targets the legacy `koda.stt-server` agent. See the recipe
in the `install_stt_agent.sh` header.

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
uv run python -m stt_server status --socket-path ~/Library/Caches/koda-stt/stt.sock

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
`stt_server.server`. This lets a client-only install (`stt-server-client`
extra) skip the `websockets.asyncio.server` dependency once the package is
extracted.

```python
from stt_server import TranscriptionClient

async def run():
    # UDS (recommended)
    async with TranscriptionClient(
        socket_path="~/Library/Caches/koda-stt/stt.sock"
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

## Koda integration

Shipped. `bot/stt/websocket_stt_service.py` is a Pipecat
`SegmentedSTTService` subclass that:

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
| `STT_WS_SOCKET` | *(unset)* | UDS path (Koda's `./koda stt` wrapper exports `STT_WS_DEFAULT_SOCKET=~/Library/Caches/koda-stt/stt.sock` as the fallback) |
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
`scripts/install_stt_agent.sh`) renders a LaunchAgent (`koda.stt-server`)
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
alias is still honoured (canonical wins if both are set).

| Variable (canonical) | Default | Description |
|---|---|---|
| `PIPECAT_STT_WHISPER_CONDITION_ON_PREVIOUS_TEXT` | `False` | Condition each chunk's decode on the previous chunk's text. Load-bearing — leave `False`. |
| `PIPECAT_STT_WHISPER_COMPRESSION_RATIO_THRESHOLD` | `2.4` | Flags zlib-compressible (repetitive) output as a failed segment, forces re-decode. |
| `PIPECAT_STT_WHISPER_LOGPROB_THRESHOLD` | `-1.0` | Flags low-confidence segments. |
| `PIPECAT_STT_WHISPER_NO_SPEECH_THRESHOLD` | `0.6` | Drops silence segments before they get a chance to hallucinate. |

After decode, `_decode_sync` runs a degenerate-output filter
(`shared/text_quality.is_degenerate`) on each segment. Segments where
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

See `docs/dev_plans/20260430-fix-whisper-hallucination.md` for context,
calibration histogram, and the cleanup-stage short-circuit + symmetric
output guard that pair with these decode-time defences.
