# Backends and models

> Backend selection, model management, prefetching, and decode tuning for
> [pipecat-local-stt-server](../README.md). For persistent multi-backend
> deployment, see [Operations](operations.md#multi-backend-operation).

## Choosing a backend

The server loads exactly one ASR backend at launch. Clients select an endpoint,
not a backend: to compare Whisper and Parakeet, run two servers on different
endpoints and point the client at the one you want.

| `--backend` | Install | Default model | Notes |
|---|---|---|---|
| `echo` | base package | none | Test and smoke-check backend; returns deterministic output. |
| `mlx` | `uv sync --extra mlx` | `mlx-community/whisper-large-v3-turbo` | MLX Whisper on Apple silicon. |
| `parakeet` | `uv sync --extra parakeet` | `mlx-community/parakeet-tdt-0.6b-v3` | Writes utterance audio to a private per-process `0700` temporary directory and removes it on close. |
| `nemotron` | `uv sync --extra nemotron` | `mlx-community/nemotron-3.5-asr-streaming-0.6b` | Requires `mlx-audio>=0.4.4`; uses the same private temporary-WAV boundary as Parakeet. |
| `kyutai` | `uv sync --extra kyutai` | `kyutai/stt-1b-en_fr-mlx` | English and French; CC-BY-4.0; Python 3.12; runs from its own `.venv-kyutai` because its MLX pins conflict with the other extras. |

Missing extras fail with an actionable `uv sync --extra <name> --inexact`
message. `just stt-install kyutai` creates the separate Python 3.12 environment
automatically. The canonical LaunchAgent labels and ports are in the
[operations table](operations.md#per-backend-port-convention).

Start backends directly over Unix sockets:

```bash
# Whisper
uv sync --extra mlx
uv run --no-sync python -m stt_server serve --backend mlx \
  --socket-path ~/Library/Caches/pipecat-stt/stt.sock

# Parakeet
uv sync --extra parakeet
uv run --no-sync python -m stt_server serve --backend parakeet \
  --socket-path ~/Library/Caches/pipecat-stt/parakeet.sock

# Nemotron
uv sync --extra nemotron
uv run --no-sync python -m stt_server serve --backend nemotron \
  --socket-path ~/Library/Caches/pipecat-stt/nemotron.sock
```

Kyutai decodes in memory through `moshi_mlx`. Its extra pins an older MLX stack
and must not be installed alongside the other MLX extras. Run it through its
dedicated environment or use `just stt-install kyutai`.

## Models and revisions

`--model` is passed to the selected backend. When it is unset, the backend uses
the default in the table above. A mismatched repository fails at model load or
decode.

```bash
uv run --no-sync python -m stt_server serve --backend mlx \
  --model mlx-community/whisper-small \
  --socket-path ~/Library/Caches/pipecat-stt/stt.sock
```

The four production defaults are pinned to Hugging Face commits in
`stt_server.backends.PINNED_REVISIONS`. Both `serve` and `prefetch` use those
pins, so a model repository changing its `main` branch cannot silently change a
default deployment. A non-default repository ID follows that repository's
`main`; a local path follows the files on disk.

To move a default pin, update `PINNED_REVISIONS`, run live decode tests against
the new revision, and prefetch it before restarting agents. On cancellation,
Kyutai stops at the next 80 ms frame and skips a queued decode; the other
backends finish a decode after it starts.

Common Whisper choices:

| Model | Trade-off |
|---|---|
| `mlx-community/whisper-large-v3-turbo` | Default; strong accuracy/speed balance. |
| `mlx-community/whisper-large-v3` | Highest accuracy, slowest, most RAM. |
| `mlx-community/whisper-medium` | Middle accuracy and resource use. |
| `mlx-community/whisper-small` | Faster and lighter. |
| `mlx-community/whisper-base` | Fast with lower accuracy. |
| `mlx-community/whisper-tiny` | Fastest and least accurate. |

Compatible `mlx-community` forks and quantized variants can also be passed as
`--model`; this list is not an allowlist. Use `python -m stt_server status` to
confirm the backend and model loaded by a running server.

## Prefetching and cache checks

Production backends download their checkpoints on first use. `prefetch` warms
the cache before a server starts; `--check` inspects the cache without network
access.

```bash
uv run --no-sync python -m stt_server prefetch --backend parakeet
uv run --no-sync python -m stt_server prefetch --backend parakeet --check
uv run --no-sync python -m stt_server prefetch --backend mlx \
  --model mlx-community/whisper-small --json
```

Models are stored in the standard Hugging Face cache (`HF_HOME` /
`HF_HUB_CACHE`). A value beginning with `/`, `./`, `../`, or `~`—or an existing
relative path—is treated as a local model directory. Local models are never
downloaded. `--check` reports one as cached only when every backend-required
file exists; fetch mode raises `ModelInvalidError` when it is incomplete.
Kyutai accepts Hub repository IDs only and rejects local paths in `prefetch`.

```bash
uv run --no-sync python -m stt_server prefetch --backend parakeet \
  --model ~/models/parakeet --check
```

`--check` exits zero when the probe itself succeeds; read `cached` for the
answer. With `--json`, stdout contains one event per line:

| Event | Fields |
|---|---|
| `start` | `repo`, `revision`, `kind` |
| `progress` | `repo`, `downloaded_bytes`, `total_bytes`, `files_done`, `files_total` |
| `done` | `repo`, `path`, `size_bytes` |
| `complete` | `backend`, `model`, `cached`, `size_bytes` |
| `check` | `repo`, `revision`, `kind`, `required`, `cached`, `path`, `size_bytes` |
| `summary` | `backend`, `model`, `cached`, `size_bytes` |
| `error` | `code`, `message`, `repo` |

Progress counts only files this run must download. Cached files are excluded
from `total_bytes` and `files_total`; resumed partial bytes count toward
`downloaded_bytes`. The final progress event equals its totals, including an
all-zero event when everything was cached. The schema matches the sibling
`pipecat-local-tts-server` prefetch channel. Stable error codes are documented
under [Supervised process errors](architecture.md#stable-process-error-codes).

## Whisper hallucination suppression

The MLX Whisper backend forwards four decode options to
`mlx_whisper.transcribe()`. Defaults match reference Whisper except
`condition_on_previous_text`, which is disabled to prevent hallucinated text
from reinforcing itself across chunks.

Canonical `PIPECAT_STT_*` variables win over deprecated `KODA_STT_*` aliases.
For numeric and boolean knobs, a present but empty canonical value resolves to
the default instead of falling through to the alias.

| Variable | Default | Purpose |
|---|---:|---|
| `PIPECAT_STT_WHISPER_CONDITION_ON_PREVIOUS_TEXT` | `False` | Keep previous output out of the next decoder prompt. Leave this `False`. |
| `PIPECAT_STT_WHISPER_COMPRESSION_RATIO_THRESHOLD` | `2.4` | Re-decode highly compressible, repetitive output. |
| `PIPECAT_STT_WHISPER_LOGPROB_THRESHOLD` | `-1.0` | Reject low-confidence segments. |
| `PIPECAT_STT_WHISPER_NO_SPEECH_THRESHOLD` | `0.6` | Drop likely-silence segments. |

After decoding, `stt_server.text_quality.is_degenerate` drops a segment when
its dominant case-folded unigram exceeds the configured ratio and it contains
enough tokens. Defaults are calibrated above the original corpus p99.5 so short
backchannels are not rejected.

| Variable | Default | Purpose |
|---|---:|---|
| `PIPECAT_STT_WHISPER_DEGENERATE_TOKEN_RATIO` | `0.40` | Dominant-token share above which a segment is dropped. Raise toward `0.45` first if monitoring shows excessive drops. |
| `PIPECAT_STT_WHISPER_DEGENERATE_MIN_TOKENS` | `10` | Minimum segment length before the ratio check applies. |

The older `KODA_TEXT_QUALITY_DEGENERATE_*` and
`KODA_STT_WHISPER_DEGENERATE_*` names remain deprecated compatibility aliases.
New deployments should use `PIPECAT_STT_WHISPER_DEGENERATE_*`.
