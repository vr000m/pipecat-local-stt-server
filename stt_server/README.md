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

For persistent always-on operation on macOS, `scripts/install_stt_agent.sh`
installs a LaunchAgent that keeps the server alive across login and auto-
restarts on crash. See the Koda integration section below.

## Checking server health

The server answers a `server.status` wire event with its current session
state (queue depth, uncommitted bytes, uptime) and, on connect, replies
with a `server.hello` carrying protocol version, audio format, and
capabilities. The `status` subcommand wraps that round-trip:

```bash
# Text output (exit 0 on success, 1 on not-reachable/timeout/error)
uv run python -m stt_server status --socket-path ~/Library/Caches/koda-stt/stt.sock

# Raw JSON for scripting / monitoring
uv run python -m stt_server status --socket-path ... --json

# Loopback TCP with bearer token
uv run python -m stt_server status --host 127.0.0.1 --port 8765 \
    --auth-token-file /path/to/token
```

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
  with one 250 ms-back-off retry on the next `run_stt`

Enable via `STT_SERVICE=websocket` in `.env`. Client env vars:

| Variable | Default | Description |
|---|---|---|
| `STT_WS_SOCKET` | `~/Library/Caches/koda-stt/stt.sock` | UDS path |
| `STT_WS_HOST` / `STT_WS_PORT` | *(unset)* | Loopback TCP target |
| `STT_WS_URI` | *(unset)* | Full `ws://` or `wss://` URI |
| `STT_WS_TOKEN` | *(unset)* | Bearer token; only enforced when the server was started with a matching token. Configure one for any TCP deployment. |

Each `WebSocketSTTService` instance owns exactly one websocket session,
so Koda's dual bot gets two independent sessions (`me` / `them`) against
a single shared server. `BranchVADUserStartedSpeakingFrame` /
`BranchVADUserStoppedSpeakingFrame` stay inside Koda; the server
intentionally has no notion of branches or speakers.

For persistent operation, `scripts/install_stt_agent.sh install` renders
a LaunchAgent (`koda.stt-server`) via `scripts/render_stt_plist.py`
(stdlib `plistlib` + allowlist validation — do not reintroduce `sed`
templating). Overrides: `KODA_STT_SOCKET`, `KODA_STT_BACKEND`,
`KODA_STT_MODEL`, `KODA_STT_LOG_DIR`, `KODA_STT_AUTH_TOKEN`.
