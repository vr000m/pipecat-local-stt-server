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
| `parakeet` | `uv sync --extra parakeet` | `mlx-community/parakeet-tdt-0.6b-v3` | Writes raw utterance audio (PII) to a private per-process `0700` temporary directory and removes it on close. |
| `nemotron` | `uv sync --extra nemotron` | `mlx-community/nemotron-3.5-asr-streaming-0.6b` | Requires `mlx-audio>=0.4.4`; writes raw utterance audio (PII) with the same private temporary-WAV boundary as Parakeet. |
| `kyutai` | `uv sync --extra kyutai` | `kyutai/stt-1b-en_fr-mlx` | English and French; CC-BY-4.0; Python 3.12; runs from its own `.venv-kyutai` because its MLX pins conflict with the other extras. |
| `moonshine` | `uv sync --extra moonshine` | `medium-streaming@en` | Moonshine Voice on CPU (ONNX Runtime, no MLX); commit-oriented. `--model` is a spec, not a repo id; see [Moonshine](#moonshine). |

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

### Moonshine

Moonshine Voice (`moonshine-voice`) runs on CPU and shares the default
environment. It buffers audio until `end()`, then decodes once and emits one
`delta` equal to `completed`; the wire protocol is unchanged. The first install
is `just stt-install moonshine` (port 9965). The `moonshine-voice==0.1.6` macOS
wheel is `macosx_15_0_arm64` only: macOS 14 or earlier, and Intel Macs, have no
matching wheel, so `uv sync --extra moonshine` fails there. The server
requires exactly `moonshine-voice==0.1.6` at start; any other installed
version fails with an `ImportError` that names the fix.

`--model` is a spec, `[<arch>][@<lang>[,<lang>...]]`, default
`medium-streaming@en`. Examples: `medium-streaming@en`, `tiny-streaming@en,es`,
`@en,es` (first MIT model for each language). The served set is fixed at
startup; memory is the sum of the listed models, and nothing is downloaded for
a language outside it. Spec errors fail at start with a `ConfigError`.

Licence guard: only MIT models are served, which means streaming archs for any
language and non-streaming archs for `en` only. A non-streaming non-`en` arch
is refused. Models are never shipped or bundled by this project; they come from
the package's own CDN (`download.moonshine.ai`), not the Hugging Face Hub. The
`moonshine-voice` package (not this project) prints a notice on stderr whenever
it downloads a model for a non-`en` language, naming its non-commercial
Moonshine Community License for some models. Whether a model's licence suits
your use is your call.

Language: Moonshine has no language identification, so a client `auto`, blank
or missing `language` silently means `en` if served, else the first listed
language. This departs from the uniform "auto means detect" contract of the
other backends. A region suffix is stripped (`en-US` is `en`). An unserved
language is logged as an error and falls back the same way; it never fails the
request. `ja` and `zh` models are downloadable and servable with
`--model <arch>@ja`, but carry no verified-support claim, and line texts are
joined with a space, so their output reads with spaces (known limitation).

Cache and pin: `prefetch --backend moonshine` (and `--check`) uses the
package's own manifest and verifies each file's CRC32C; it does not use
`HF_HOME`. CRC32C detects corruption, not deliberate tampering: the manifest
and the files both come from `download.moonshine.ai`, so trusting that CDN is
your call. If a CRC32C cannot be computed (`google-crc32c` not importable),
`--check` and the repair fail instead of passing. The files live under the
package's cache directory. There is no
Hugging Face revision: the exact `moonshine-voice==0.1.6` pin in
`pyproject.toml` fixes the model URLs, so moving it moves the models. Models
download from a CDN, so a CDN outage blocks a cold start; warm the cache with
`prefetch` before installing an agent. Local model paths are not supported.

Removing models: the server never deletes a model. A file that fails its
CRC32C check is the one exception: `prefetch` removes it and downloads it once
more. A model you no longer serve stays in the cache, including after a
`--model` change to another architecture. `prefetch --backend moonshine --model
<spec> --check --json` reports each model's directory (`path`) and size
(`size_bytes`) without using the network, so a deployment tool can remove that
directory. The optional `spelling-<lang>` model sits beside it, outside the
check. The cache is shared with any other program that uses `moonshine-voice`
(on macOS, `~/Library/Caches/moonshine_voice`), so removing models is the
operator's or the supervising tool's job.

Latency: `scripts/benchmark_moonshine_latency.py` measures, against a running
moonshine server, the p50 from `commit` to `transcription.completed` for 3, 5
and 10 s utterances fed in real time (JSON key `one_shot_p50_s` with
`--json-out`), plus the library `stop()` flush after a streamed feed. Run it with
`uv run --inexact python scripts/benchmark_moonshine_latency.py --uri
ws://127.0.0.1:9965/` (or `--socket <path>`; token from `--auth-token-file` or
`STT_WS_TOKEN`).

## Models and revisions

`--model` is passed to the selected backend. When it is unset, the backend uses
the default in the table above. A mismatched repository fails at model load or
decode.

```bash
uv run --no-sync python -m stt_server serve --backend mlx \
  --model mlx-community/whisper-small \
  --socket-path ~/Library/Caches/pipecat-stt/stt.sock
```

The four Hugging Face defaults (not Moonshine) are pinned to commits in
`stt_server.backends.PINNED_REVISIONS`. Both `serve` and `prefetch` use those
pins, so a model repository changing its `main` branch cannot silently change a
default deployment. A non-default repository ID follows that repository's
`main`. MLX Whisper, Parakeet, and Nemotron local paths follow the files on
disk. Kyutai accepts Hub repository IDs only: `prefetch` rejects a local path,
while `serve` passes an explicit value to the Hub as a repository ID and fails
during model load if it is a path.

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
The boolean parser accepts `1`/`true`/`yes`/`on` case-insensitively. Every other
value, including `False`, `0`, empty, or unset, resolves to `False`.

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
