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
# Echo backend (no ML deps, useful for smoke tests)
uv run python -m stt_server --host 127.0.0.1 --port 8765

# MLX Whisper
uv sync --extra stt-server-mlx
uv run python -m stt_server --backend mlx --host 127.0.0.1 --port 8765
```

## Client usage

```python
from stt_server import TranscriptionClient

async def run():
    async with TranscriptionClient(host="127.0.0.1", port=8765) as c:
        await c.update_session(turn_detection=None)
        await c.send_audio(pcm_bytes)     # binary PCM16LE frames
        await c.commit()
        async for ev in c.events():
            if ev["type"] == "conversation.item.input_audio_transcription.completed":
                print(ev["transcript"])
                await c.close_session()
                break
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

Not wired up yet. The planned seam is a Pipecat `STTService` wrapper that:

- owns the WebSocket session under `start(StartFrame)` / `stop(EndFrame)` /
  `cancel(CancelFrame)`
- drives `commit` from the branch-local VAD + SmartTurn stack on each branch
- translates `conversation.item.input_audio_transcription.completed` into
  `TranscriptionFrame` and preserves Koda's `me` / `them` source labels
  outside this package

`BranchVADUserStartedSpeakingFrame` / `BranchVADUserStoppedSpeakingFrame` stay
inside Koda; the server intentionally has no notion of branches or speakers.
