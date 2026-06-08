# Feature: STT vocabulary/prompt biasing on the wire protocol

**Status:** Not started — analysis/handoff only. We are NOT doing the work yet.
**Date:** 2026-06-07
**Origin:** Cross-repo handoff from the onoats client. This must start server-side
because the wire protocol has no field for it yet.

---

## Why this starts on the server side

The onoats client has a `dictionary.txt` of domain vocabulary terms. It already
feeds these to Deepgram (via `keywords`). It wants the same for the local
WebSocket path, but our wire protocol (`update_session` / session config) only
carries `language` (and `turn_detection`). The server must add the protocol field
before the client can plumb anything.

## Current state (verified 2026-06-07)

None of the three backends expose `initial_prompt` / `prompt` / `hotwords` /
`vocabulary` / `biasing` / `suppress_tokens`. Only `language` is plumbed through.

| Backend | Params today | Prompt/hotword support | Can support a prompt? |
|---|---|---|---|
| **mlx_whisper** (`stt_server/backends/mlx_whisper.py:167-179`) | `language` (+ env-driven suppression knobs) | ❌ not exposed | ✅ **Yes** — underlying Whisper supports `initial_prompt` natively |
| **parakeet** (`stt_server/backends/parakeet.py:150-154`) | `language` (ignored) | ❌ | ❌ parakeet_mlx (NVIDIA TDT) has no prompt/lexicon mechanism |
| **nemotron** (`stt_server/backends/nemotron.py:167`) | `language` | ❌ | ❌ mlx-audio Nemotron ASR has no prompt mechanism |

**Confirmed work item:** `stt_server/backends/mlx_whisper.py:167-179` calls
`mlx_whisper.transcribe(...)` with an **explicit, hardcoded kwarg list** (not
`**kwargs`). So the wrapper genuinely needs a new param threaded through — this is
the same gap the pipecat client wrapper has. This is the real work, and it's
confirmed (onoats couldn't verify it from the client repo).

Config extraction reads only `language` at `stt_server/server.py:493,496`
(from `session.input_audio_transcription.language` / `session.audio.input.language`).
Wire schema lives in `stt_server/protocol.py` — only `language` documented today.

## Proposed change (protocol-first)

1. **New optional session-config field** in `protocol.py`, alongside `language`.
   Field name: **`initial_prompt`** (decided 2026-06-07 — chosen over the generic
   `prompt` because it names the Whisper concept exactly and avoids collision with
   LLM-"prompt" terminology elsewhere in the stack). A single free-text **string**,
   deliberately *not* `vocabulary: list[str]`, because:
   - The only engine that can use it is mlx_whisper, whose input is
     `initial_prompt` — a string, not a keyword list.
   - parakeet / nemotron have no prompt mechanism; they must ignore it gracefully
     (exactly like they already ignore `language`).
   - A string keeps the protocol engine-agnostic and lets the client decide
     serialization (bare terms vs. context-framed sentences).
2. **Plumb through config extraction** (the `server.py:493,496` spots): extract
   `prompt` the same way as `language` and thread it to the active backend's
   transcribe call.
3. **Backend wiring:**
   - mlx_whisper → `mlx_whisper.transcribe(..., initial_prompt=prompt)`.
   - parakeet / nemotron → accept-and-ignore (no-op), don't error.

## Constraints to bake in

- **Token budget:** `initial_prompt` is capped at ~224 tokens (half Whisper's
  448-token decoder context). Suggested: server **truncates defensively** and
  **logs** when it does — the client can't always know the tokenizer.
- **Soft-bias semantics:** `initial_prompt` conditions the first 30s window and
  biases spelling/style; it's **not a hard lexicon** and can make the model echo
  prompt terms. Document this in the field's docstring so callers set expectations.
- **Session-static is fine:** set at session-config time; no mid-stream updates in v1.
- **Empty/absent = current behavior:** field omitted → identical to today.
  Backward-compatible; old clients unaffected.

## Rollout order

1. Add optional `initial_prompt` field to the protocol schema (backward-compatible).
2. Extract + thread to backends; mlx_whisper uses it, others no-op.
3. Bump/version the wire protocol if tracked; note "older clients unaffected."
4. Ping onoats with the final field name + version. Then the onoats side wires
   `WebSocketSTTService` to forward `get_vocabulary_with_context()` serialized
   into a glossary string.

## Notes / caveats

- Library-level claims (Whisper supports `initial_prompt`; parakeet_mlx & mlx-audio
  don't) are from model knowledge, not verified against installed package
  signatures. Verify against installed versions before building.
- Related: [STT backend language contract] — `prompt` should follow the same
  per-backend accept-or-ignore pattern that `language` already uses (server
  recasts "auto"/blank→None for whisper; same spirit of graceful per-backend handling).
