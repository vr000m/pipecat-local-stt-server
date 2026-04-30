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

## Running the server

```bash
# UDS (recommended for local use — no port, no bearer token)
uv run python -m stt_server --socket-path ~/Library/Caches/koda-stt/stt.sock --backend echo

# MLX Whisper over UDS
uv sync --extra stt-server-mlx
uv run python -m stt_server --socket-path ~/Library/Caches/koda-stt/stt.sock --backend mlx

# Loopback TCP (use --auth-token-file or KODA_STT_AUTH_TOKEN env; --auth-token
# on argv is visible via `ps` and marked DEPRECATED)
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

## Checking server health

The server answers a `server.status` wire event with its current session
state (queue depth, uncommitted bytes, uptime) and process health (pid,
peak RSS), and, on connect, replies with a `server.hello` carrying
protocol version, audio format, and capabilities. The `status` subcommand
wraps that round-trip:

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
validation — do not reintroduce `sed` templating). Overrides:
`KODA_STT_SOCKET`, `KODA_STT_BACKEND`, `KODA_STT_MODEL`,
`KODA_STT_LOG_DIR`, `KODA_STT_AUTH_TOKEN`. Use `./koda stt status` for
a wire-level health check.

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

| Variable | Default | Description |
|---|---|---|
| `KODA_STT_WHISPER_CONDITION_ON_PREVIOUS_TEXT` | `False` | Condition each chunk's decode on the previous chunk's text. Load-bearing — leave `False`. |
| `KODA_STT_WHISPER_COMPRESSION_RATIO_THRESHOLD` | `2.4` | Flags zlib-compressible (repetitive) output as a failed segment, forces re-decode. |
| `KODA_STT_WHISPER_LOGPROB_THRESHOLD` | `-1.0` | Flags low-confidence segments. |
| `KODA_STT_WHISPER_NO_SPEECH_THRESHOLD` | `0.6` | Drops silence segments before they get a chance to hallucinate. |

See `docs/dev_plans/20260430-fix-whisper-hallucination.md` for context.
