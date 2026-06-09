# Client and integration

> Python client usage, Pipecat integration, and the Koda reference consumer for [pipecat-local-stt-server](../README.md).

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
| `STT_WS_SOCKET` | *(unset)* | UDS path. Koda's `./koda stt` wrapper exports a `STT_WS_DEFAULT_SOCKET` fallback that still points at the old (v0.1.x) Caches socket path; after the 0.2.0 rename, re-point that wrapper default at `~/Library/Caches/pipecat-stt/stt.sock` (or set `STT_WS_SOCKET` directly). See the [migration guide](migration.md). |
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

