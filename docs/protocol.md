# Protocol subset

> Wire protocol for [pipecat-local-stt-server](../README.md). See also the [client and integration guide](integration.md).

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

## Connection rejection (pre-handshake)

This document pins the **presence** of wire events, not their field schema; the
field shape of `server.hello`/`server.status` is defined by the server source
(`stt_server/server.py`) and the table under
[Checking server health](operations.md#checking-server-health).

Connection-level rejections happen **before** the WebSocket handshake and are
returned as plain HTTP responses, not protocol JSON envelopes (there is no
`error` event for these):

| Condition | Status | Body |
|---|---|---|
| Disallowed browser `Origin` | `403` | `origin not permitted` |
| UDS peer uid `!=` server uid, or peer-cred fails closed | `403` | `peer not permitted` |
| TCP bearer token missing/incorrect | `401` | `unauthorized` |

The UDS peer-credential check (`403 peer not permitted`) is server-side only and
requires no client change — see
[Trust model and socket security](operations.md#trust-model-and-socket-security-same-host-uds).

