# Task: Moonshine Voice STT backend (multilingual, commit-oriented)

**Status**: In Progress
**Component**: ASR Backends
**Assigned to**: Varun Singh
**Priority**: Medium
**Branch**: feature/moonshine-streaming-backend
**Created**: 2026-11-14
**Review Gates**: full

## Objective

Add a `moonshine` backend built on the `moonshine-voice` package (MIT streaming models, ONNX on CPU, 8 languages) as a commit-oriented backend, with no change to the server's session handling and no change to the wire protocol. A streaming mode that overlaps decode with incoming audio is a separate plan (`20261114-feature-moonshine-streaming-mode.md`), started only if this plan's benchmark justifies it.

## Context

- Every backend today is commit-oriented. `_run_decode` (`stt_server/server.py`) opens a stream, feeds the whole buffered utterance once, calls `end()`, then reads `events()`. Every current backend yields at most one `delta` equal to `completed` (`stt_server/backends/nemotron.py:182-184`, `docs/protocol.md:31`). The wire follows the OpenAI Realtime transcription shape (`committed`, `delta`, `completed`); nothing in the repo defines interim or replaceable text.
- `moonshine-voice` 0.1.6 (MIT, PyPI) is not mlx-audio's v1 Moonshine port. It ships a native ONNX Runtime library (CPU only; CoreML is not shipped) behind a `Transcriber`/`Stream` API. The macOS wheel is `macosx_15_0_arm64`; Linux and Windows wheels also exist (not checked here). It declares `numpy, sounddevice, requests, tqdm, filelock, platformdirs, google-crc32c`: no MLX and no `huggingface-hub`. Models download from `download.moonshine.ai` into a platformdirs cache, not from the HF Hub.
- Model catalog (read from the installed package): `en` {medium-streaming (default), small-streaming, base, tiny-streaming, tiny}, `es` {small-streaming (default), tiny-streaming, base}, `de` {small-streaming, tiny-streaming}, `ja` {small-streaming, tiny-streaming, base, tiny}, `ar`/`zh`/`vi` {tiny-streaming, base}, `tl` {tiny-streaming}, `ko` {tiny}, `uk` {base}.
- Licence, verified against the upstream project LICENSE (2026-11-14): the non-MIT list is exhaustive and consists only of the legacy non-streaming non-English models (Arabic, Japanese, Korean, Mandarin, Ukrainian, Vietnamese Base and Tiny; Spanish Base); "any speech-to-text model not named in that list is MIT, including every streaming model". The Community Licence also carries registration terms for commercial use. The package prints a non-commercial notice to stderr for every non-`en` model, MIT ones included, which is misleading but harmless.
- Phase 0 spike (scratchpad venv, Apple Silicon, two_cities.wav, 44 s, 48 kHz input to every call, so the 16 kHz path is unmeasured). Raw numbers are in `## Findings`:
  - Listener callbacks fire synchronously on the thread that called `add_audio`/`stop`.
  - `add_audio` blocks while the model runs (medium: p95 170 ms, max 475 ms), which matters for the streaming plan, not this one.
  - Offline `transcribe_without_streaming` runs at RTF 0.075 on medium (3.35 s for 44 s) and 0.02 on tiny; it also works with the streaming models. It is not used by the backend: on one long-lived `Transcriber`, repeated calls insert a spurious leading "Mm" line from the second call on (found in Phase 1, reproduced on tiny and medium; see Decision 11), so the backend decodes through a one-shot stream instead.
  - Medium en: 267 MB on disk, about 1.0 GB RSS after load.
  - Concurrent decodes gave no speedup. With two transcribers the outputs differed. The cause is unproven (the compared transcriber had already served six streams, which confounds reuse with concurrency), so decodes are serialised and Phase 1 adds a check.
  - Language ids are bare lowercase codes (`en`, `zh`); `en-US`, `EN`, `zh-CN` and `auto` raise `ValueError`.
- Review run `20261114T215650Z` raised 50 findings; the six decisions that came out of it are recorded under `### Architecture Decisions` and in `## Findings`.

## Requirements

- New `--backend moonshine` with extra `moonshine`; `uv sync --extra moonshine --inexact` installs it.
- **Model spec.** `--model` / `PIPECAT_STT_MODEL` is `[<arch>][@<lang>[,<lang>...]]`. Default `DEFAULT_MOONSHINE_MODEL = "medium-streaming@en"`. An omitted arch resolves each language to the first MIT-allowed catalog entry. Examples: `small-streaming` (en, small), `@en,es` (en and es at catalog defaults), `tiny-streaming@en,es,de`. The constructor stores the spec verbatim, with no import and no validation, so `backend.model` is exactly what the operator passed. `start()` imports the package, parses the spec, validates it against the live catalog and the licence guard, raises `ConfigError` on an unknown arch, unknown language or non-MIT combination, and logs the resolved language-to-arch map once at INFO.
- **Licence guard.** Serve only MIT models: streaming archs for any language, non-streaming archs only for `en` (matches the upstream LICENSE enumeration above). `ko` and `uk` therefore cannot be served.
- **Served-language set.** Fixed at startup from the spec. The transcriber cache is exactly that set (no eviction; memory is the sum of the listed models, documented). The downloader is never called for a language outside the set.
- **Language contract**, following the existing pattern (no server-side validation; each backend decides): `None`, blank or `"auto"` becomes the fallback language (`en` if served, else the first listed) without logging an error. Otherwise lowercase and strip the region (`en-US` becomes `en`, `zh-CN` and `zh_CN` become `zh`). An explicit language outside the served set, or unsupported by the catalog, calls `logger.error` and falls back to `en` if `en` is served, else the first listed language; it does not raise. Moonshine has no language identification, which departs from the uniform "auto means detect" contract; this is documented, and the client gets no signal on the wire.
- **Wire shape unchanged.** One `delta` whose text equals `completed` (empty decode: `completed` only), the same as `nemotron`. No new events, no `PROTOCOL_VERSION` change, no change to `server.py` session handling.
- Decodes are serialised across sessions, with the same SIGTERM-mid-decode drain, `abort()` and `idle()` behaviour as the other backends.
- **Prefetch.** `prefetch_specs` stays the only prefetch contract. `SnapshotSpec` gains optional `check` and `fetch` callables; moonshine returns one spec per served (language, arch) with a synthetic `repo_id` (`moonshine-voice/<arch>-<lang>`), the `moonshine-voice` package version as `revision`, and callables that derive completeness from the package's own manifest (size and CRC32C per file). A size or CRC mismatch is wrapped with `mark_model_invalid`. The optional alphanumeric spelling model that `get_model_for_language` also fetches (best effort, failures swallowed) is outside the snapshot contract and is not required for a decode. Download happens under `download_phase()`; the "needs `huggingface_hub`" hint becomes conditional.
- **Pin policy.** HF SHAs do not apply. The pin is the exact `moonshine-voice` version, which fixes the model URLs. Snapshot directory names differ per model (en streaming uses `quantized_26_11_02`; non-English streaming uses `quantized_26_08_24` or `quantized_26_08_23`; non-streaming uses an undated `quantized`), so the plan reads them from the package manifest and never from a constant. An undated directory does not pin content, and CDN retention is outside the pin; both are documented.
- Model loading is lazy on first decode (like `nemotron`). A model that was not prefetched downloads on first use inside `download_phase()`, as the other backends do; only a language outside the served set is refused.
- **Latency benchmark** (feeds the go/no-go for the streaming plan): a new paced, single-endpoint script measures one-shot-stream commit-to-completed latency and the library-level `stop()` flush time for 3, 5 and 10 s utterances at 16 kHz PCM16, using the real conversion path.
- **Port.** `9965` confirmed by the user on 2026-11-14 (unassigned in `/etc/services`, nothing listening, continues the 9565/9665/9765/9865 series).

## Review Focus

- Phase 1 must not touch `server.py` session handling or `protocol.py`. Every backend runs through them, and `tests/test_stt_server.py` assertions stay unchanged (moonshine rows are additions only).
- `prefetch.py` and `snapshot.py` changes are shared by every backend: the HF path must behave as before (`tests/test_prefetch_cli.py`, `tests/test_backend_registry.py`, `tests/test_supervised_cli.py`).
- The constructor must stay import-free and non-validating (`tests/test_backend_missing_extra.py`, `tests/test_backend_registry.py`).
- Licence guard and language table: pure functions, table-tested against a fake catalog.
- Prefetch integrity: size-only acceptance of an existing file in the library (`download_file.py`) means the check must compute CRC32C itself.
- Backend enumerations in Phase 2 (`justfile`, installer, plist renderer, `scripts/smoke_launchd.py`, docs port table) must land in one commit so `tests/test_justfile_recipes.py` and `tests/test_smoke_launchd.py` stay green.

## Implementation Checklist

### Phase 1: Commit-oriented moonshine backend, prefetch and benchmark

**Impl files:** `stt_server/backends/moonshine.py, stt_server/backends/__init__.py, stt_server/prefetch.py, stt_server/snapshot.py, pyproject.toml, uv.lock, scripts/benchmark_moonshine_latency.py`
**Test files:** `tests/test_moonshine_backend.py, tests/test_backend_missing_extra.py, tests/test_backend_registry.py, tests/test_prefetch_cli.py, tests/test_decode_guard.py, tests/test_supervised_cli.py, tests/test_stt_server.py, tests/test_benchmark_moonshine_latency.py`
**Test command:** `uv run --inexact python -m pytest -q`
**Validation cmd:** `uv sync --extra moonshine --inexact && uv run --inexact python -m stt_server prefetch --backend moonshine`
**Goal:** A client sees the same event shape and error semantics as every other backend (one `delta` equal to `completed`), from a backend that needs no change to session handling, and the shared prefetch path keeps its existing contract.

- `MoonshineBackend` in `stt_server/backends/moonshine.py`, mirroring `nemotron.py`: `DecodeGuardMixin` (from `stt_server/backends/_thread_util.py`) lock pair, lazy import with an actionable `ModuleNotFoundError` naming `uv sync --extra moonshine --inexact`, `abort()`, `close()` drain, `idle()`, `backend_name = "moonshine"`.
- Stream: buffer PCM16LE, convert to float32 (`/32768`, then `.tolist()`) at `end()`, then decode through a one-shot stream (`create_stream(update_interval=0.3)`, `add_listener`, `start()`, one `add_audio(audio, 16000)` for the whole utterance, `stop()`, then `close()` in a `finally`) and collect `LineCompleted` texts; an `Error` event raises `RuntimeError` so a failed flush never becomes a truncated `completed`; join line texts with a space into one string; one `delta` equal to `completed`. The backend never calls `transcribe_without_streaming` (Decision 11). No temp WAV, so no private temp dir. Inspect the raw line text first: the plan assumes space-joining is right, and `ja`/`zh` may not want it.
- Pure functions `parse_model_spec`, `resolve_language` and `resolve_arch` (see Detailed Design), table-tested against a fake catalog.
- Transcriber cache keyed by (language, arch), populated lazily under the thread lock inside `download_phase()`, fixed to the served set.
- Registry: `DEFAULT_MOONSHINE_MODEL = "medium-streaming@en"`; `BACKENDS["moonshine"]` with `local_model_paths=False`; no `PINNED_REVISIONS` entry, with a registry docstring rule saying the package pin is the pin.
- `snapshot.py`: `SnapshotSpec` gains optional `check` and `fetch` callables (default `None`, so HF specs are unchanged). `prefetch.py`: `check_spec` and `fetch_spec` branch once on them; the `huggingface_hub` hint is conditional; the event fields (`repo`, `revision`, `kind`, progress) keep their documented meaning.
- `tests/test_backend_registry.py` is relaxed from "`repo_id == default_model`" to "exactly one spec, non-empty `repo_id`" for the default spec, with the model-identity assertion (`_make_backend(name, "x/y").model == "x/y"`) kept.
- `pyproject.toml`: extra `moonshine = ["websockets>=16,<17", "moonshine-voice==0.1.6", "numpy>=1.26"]` and keywords. Run `uv lock` and check whether a `conflicts` pair is needed (expected: none).
- Verification steps (record results in `## Findings`, adjust the plan if one fails):
  - Read the per-model directory and file list from the package manifest for the default spec; assert the exact `moonshine-voice==0.1.6` pin and the manifest-derived directory in a test.
  - Decode one clip three times on one transcriber and once on a fresh one, offline and streamed, and compare the texts. Result (2026-11-14): offline repeats fail (spurious "Mm" from the second call), one-shot streams are identical across repeats and on a fresh transcriber, hence Decision 11. The one-transcriber concurrent check is not re-run: decodes are serialised.
  - Run the licence-guard table against the live catalog when the package is installed (skipped otherwise).
- Benchmark: `scripts/benchmark_moonshine_latency.py` is a single-endpoint, real-time-paced script (not an extension of `scripts/benchmark_asr_ab.py`, which probes two endpoints and sends audio unpaced). Utterances of 3, 5 and 10 s cut from the package's `two_cities.wav` and resampled to 16 kHz PCM16; 10 runs each, the first (cold) run discarded; reports p50 for one-shot-stream commit-to-completed over the wire and for the library-level `stop()` flush after a paced streamed feed.

### Phase 2: Install, operations and docs wiring, then the go/no-go record

**Impl files:** `justfile, scripts/install_stt_agent.sh, scripts/render_stt_plist.py, scripts/smoke_launchd.py, docs/backends.md, docs/operations.md, docs/migration.md, docs/contributing.md, README.md, CHANGELOG.md, docs/dev_plans/README.md, .github/workflows/test.yml`
**Test files:** `tests/test_justfile_recipes.py, tests/test_render_stt_plist.py, tests/test_smoke_launchd.py, tests/test_docs_navigation.py, tests/test_docs_port_caveats.py, tests/test_install_migration.py`
**Test command:** `uv run --inexact python -m pytest -q`
**Goal:** `just stt-install moonshine` works like the other backends, every list that names backends stays in step, and the docs structure tests keep passing.

- **Port 9965 confirmed (2026-11-14).** No gate remains; use it in the installer, smoke list and docs.
- `justfile`: `_resolve` arms, `_agent-python` arms, `_ensure-extra` arms. Moonshine shares the default venv unless Phase 1's `uv lock` shows a conflict.
- `scripts/install_stt_agent.sh`: default-model arm (`medium-streaming@en`), port map (`9965`), the `backends with one:` error text, case validation, header docs.
- `scripts/render_stt_plist.py`: `_BACKEND_RE` and its hint string. Verify that `@` and `,` pass the renderer's and the installer's character checks for the model value (the spec grammar depends on it).
- `scripts/smoke_launchd.py`: `CANONICAL_BACKENDS`, in the same commit and the same order as the installer port arm (`tests/test_smoke_launchd.py` asserts they match).
- Tests that enumerate backends: `tests/test_justfile_recipes.py` (`BACKENDS` tuple, port and label map, the assertions that tie `_resolve`, the installer and the `docs/operations.md` port table), `tests/test_render_stt_plist.py` parametrisations, `tests/test_smoke_launchd.py`, `tests/test_docs_port_caveats.py` (reads `uv.lock`).
- Docs: `docs/backends.md` (new section; `ja`/`zh` are downloadable and servable but carry no verified-support claim, lines are joined with a space, and model licence suitability is the developer's call (we ship no models); model spec grammar, licence guard and the stderr notice, language contract and the "auto becomes en" deviation, served set and memory, model download and cache location, package pin and CDN caveat), `docs/operations.md` (port table, `stt-install`), `docs/migration.md` (port list), `docs/contributing.md` (the `SnapshotSpec` check/fetch extension), README backend list (respect the 180-line and 1200-word limits and the single-owner heading rule in `tests/test_docs_navigation.py`), CHANGELOG, `docs/dev_plans/README.md`.
- CI: add `tests/test_moonshine_backend.py` to the Linux client job's explicit test list (it uses a fake module, so it needs no wheel).
- Manual verification through an installed agent: one decode after prefetch and one with an empty model cache, both through `just stt-install moonshine` (the LaunchAgent environment sets only `PATH` and `HOME`).
- **Last step: record the go/no-go for the streaming plan.** Enter the Phase 1 benchmark numbers in `## Findings`. Start `20261114-feature-moonshine-streaming-mode.md` only if, for the 5 s utterance on the default model, one-shot commit-to-completed p50 exceeds 300 ms and the `stop()` flush p50 is below half of the one-shot p50. (Both thresholds are proposals; the user confirms them before the record is written.)

## Technical Specifications

### Files to Modify
- `stt_server/backends/__init__.py` - `DEFAULT_MOONSHINE_MODEL`, `BACKENDS` entry (module `stt_server.backends.moonshine`, class `MoonshineBackend`, `local_model_paths=False`), registry docstring (the pin rule).
- `stt_server/snapshot.py` - `SnapshotSpec` optional `check` and `fetch` callables (the class is defined here, not in `prefetch.py`; `download_phase` and `mark_model_invalid` live here too).
- `stt_server/prefetch.py` - branch once in `check_spec` and `fetch_spec`; conditional `huggingface_hub` hint.
- `pyproject.toml`, `uv.lock` - new extra.
- `justfile`, `scripts/install_stt_agent.sh`, `scripts/render_stt_plist.py`, `scripts/smoke_launchd.py` - the enumerations listed in Phase 2.
- Docs and tests as listed per phase.

### New Files to Create
- `stt_server/backends/moonshine.py` - the backend.
- `scripts/benchmark_moonshine_latency.py` and `tests/test_benchmark_moonshine_latency.py` - the paced latency benchmark.
- `tests/test_moonshine_backend.py` - fake `moonshine_voice` injected with `monkeypatch.setitem(sys.modules, ...)`, following `tests/test_nemotron_backend.py:128-145`. Cases: protocol satisfaction; one `delta` equal to `completed` at backend and wire level; empty and whitespace decode; failure propagation; cancel; decode serialisation; `idle`/`abort`/`close` drain; spec grammar table (valid and invalid); licence guard table; language table (`auto`, `""`, `None`, `AUTO`, padded, `EN`, `en-US`, `zh-CN`, `zh_CN`, `zh-Hans-CN`, `xx`, `ko`, `uk`, a non-string value, a language outside the served set); `logger.error` asserted at the call site; the downloader is never called for an unlisted language; constructor imports nothing; cache equals the served set.
- Prefetch tests (in `tests/test_prefetch_cli.py`, with a fake `moonshine_voice.download`): uncached then cached; incomplete snapshot; fetch event order; fetch failure; connection error; missing extra; corrupt snapshot reported as model-invalid; local path refused.

### Architecture Decisions
- Decision 1 (grilled): the streaming mode is split into a follow-up plan. It changes `server.py` session handling that every backend runs through, so it gets its own plan, review and PR. This plan owns the paced benchmark and the go/no-go record.
- Decision 2 (grilled): Phase 1 emits one `delta` equal to `completed`, like every other backend. Per-line deltas, their separator and the "joined deltas equal `completed`" invariant move to the streaming plan.
- Decision 3 (grilled): `prefetch_specs` remains the single prefetch contract; `SnapshotSpec` gains optional `check`/`fetch` callables; no second module hook.
- Decision 4 (grilled): the served-language set is fixed at startup and carried in the model spec `[<arch>][@<langs>]`, so no new flag, environment variable or plist plumbing is needed. An unlisted language logs an error and falls back; the downloader is never called for it; the cache equals the served set.
- Decision 5 (grilled): `DEFAULT_MOONSHINE_MODEL = "medium-streaming@en"`; the constructor stores the spec verbatim and imports nothing; `start()` validates against the live catalog and the licence guard (`ConfigError`) and logs the resolved map. Rationale: the registry and missing-extra tests require an import-free constructor.
- Decision 6 (grilled): port `9965`, confirmed by the user on 2026-11-14 (free and unassigned).
- Decision 7: the wire contract stays append-only `delta` plus `completed`. The spike showed in-progress text is rewritten constantly (46 of 63 changes on medium), so exposing it would need a replace-semantics event that nothing else in the protocol has. Revisit only in the streaming plan.
- Decision 8: serialise decodes. Concurrent runs showed no speedup and, with two transcribers, differing output; the cause is unproven and Phase 1 adds a check.
- Decision 9: the pin is the `moonshine-voice` version, not HF SHAs; directory names come from the package manifest.
- Decision 10 (user decision, 2026-11-14): `auto` or `None` maps to `en`, and an unsupported language logs an error and falls back to `en` (or the first served language). Moonshine has no language identification, so this departs from the uniform "auto means detect" contract; documented in `docs/backends.md`.
- Decision 11 (found during Phase 1, 2026-11-14): decode through a one-shot stream, not `transcribe_without_streaming`. Against moonshine-voice 0.1.6, repeated offline calls on one `Transcriber` insert a spurious leading "Mm" line from the second call on (tiny-streaming and medium-streaming, 10 s clip); a fresh transcriber and a one-shot stream are clean (30 decodes per model across 3, 5 and 10 s clips, plus one interleaved offline call: 0 mismatches). The backend keeps a long-lived transcriber per served (language, arch), so the offline call would corrupt every transcript after the first. Cost is about equal (implementer-reported, not re-measured: 10 s on medium, 1.59 s one-shot against 1.4 to 1.5 s offline; the paced benchmark in Phase 2 supplies the real numbers). The decode stays commit-oriented: one `add_audio` call at commit, then `stop()`.

### Dependencies
- `moonshine-voice==0.1.6` (MIT; the macOS wheel needs macOS 15 or later on arm64). Transitive: `numpy`, `sounddevice` (imported lazily, so PortAudio is not needed at import time on this wheel), `requests`, `tqdm`, `filelock`, `platformdirs`, `google-crc32c`.
- Runtime state outside the repo: model cache at `~/Library/Caches/moonshine_voice` (override `MOONSHINE_VOICE_CACHE`), which the LaunchAgent must be able to read and write. The plist environment sets only `PATH` and `HOME`.

### Detailed Design

#### Module and class layout (`stt_server/backends/moonshine.py`)

| Unit | Responsibility |
|---|---|
| `parse_model_spec(spec) -> (arch \| None, langs)` | Pure. Parses `[<arch>][@<lang>[,<lang>...]]`; no catalog access. Empty language list means `en`. |
| `resolve_arch(code, requested, catalog) -> ModelArch` | Pure over a catalog mapping. Applies the licence guard (streaming archs for any language, non-streaming only for `en`); an omitted arch takes the first MIT-allowed entry. Raises `ConfigError` for an unknown arch or a non-MIT combination (called from `start()`). |
| `resolve_language(lang, served) -> (code, note)` | Pure. `None`/blank/`auto` becomes the fallback (`en` if served, else the first listed) with no note; lowercase; strip region (`-` or `_`); outside the served set returns the fallback (`en` if served, else the first listed) plus an error note. The caller logs the note with `logger.error`. |
| `MoonshineBackend(DecodeGuardMixin)` | Registry entry point. Stores the spec verbatim; `start()` imports the package, validates, builds the served map, logs it. Owns the lock pair, in-flight counter, transcriber cache, `open_stream`, `abort`, `idle`, `close`. |
| `_MoonshineStream` | Buffers PCM16LE; at `end()` converts and runs the one-shot-stream decode on a guarded daemon thread under the thread lock; `events()` yields one `delta` (if non-empty) and `completed`. |
| `prefetch_specs(model)` | Module-level. One `SnapshotSpec` per served (language, arch) with `check`/`fetch` callables built from the package manifest. |

#### Language and model resolution

```mermaid
flowchart TD
    S["--model spec"] --> P["parse_model_spec (constructor stores it verbatim)"]
    P --> ST["start(): import package, read catalog"]
    ST --> V{"arch known, language known, MIT-allowed?"}
    V -->|no| CE["ConfigError"]
    V -->|yes| M["served map: lang to arch (logged once)"]
    A["client language per session"] --> B{"None, blank or auto?"}
    B -->|yes| E["code = fallback (en, else first listed), no error"]
    B -->|no| C["lowercase, strip region"]
    C --> D{"in served set?"}
    D -->|no| F["logger.error, fallback en (or first listed)"]
    D -->|yes| G["code"]
    E --> J
    F --> J["cache lookup (code, arch)"]
    G --> J
    J -->|miss| K["download_phase: get_model_for_language, then Transcriber()"]
    J -->|hit| L["use cached transcriber"]
    K --> L
```

#### Threading model
One daemon thread per decode through `_run_guarded_decode`, exactly like `nemotron.py`; the whole utterance is decoded under the thread lock, so decodes across sessions serialise. `idle()` and the `close()` drain use the mixin unchanged (`_drain_inflight`, 3 s).

#### Failure modes

| Failure | Where it surfaces | Result |
|---|---|---|
| Model download fails (offline, DNS) | first decode, inside `download_phase()` | exception from `end()`/`events()`, then `transcript.failed`; `requests` errors already classify as download errors (`stt_server/supervise.py`) |
| Cached file is corrupt (size match, bad CRC32C) | `prefetch` only (`--check` reports it; a fetch deletes the bad file and re-downloads once) | still bad after the re-download: `ModelInvalidError` (`mark_model_invalid`) naming the file path. The serve path stays size-only (no CRC at load), so an unprefetched corrupt cache is not detected there |
| Decode raises | decode thread | future carries the exception, then `transcript.failed` with a generic message; details only in the server log |
| Bad `--model` spec | `start()` | `ConfigError`, exit 1 with a clear message |
| Language outside the served set or unsupported | `resolve_language` | `logger.error`, falls back; the decode proceeds; nothing is downloaded |
| Client disconnect or `cancel` mid-decode | existing server paths | `cancel()` marks the stream; the daemon thread finishes and its result is dropped |
| SIGTERM mid-decode | `close()` | bounded drain, then `abort()` |

#### Configuration surface

| Setting | Source | Default | Note |
|---|---|---|---|
| Model spec (arch and served languages) | `--model` / `PIPECAT_STT_MODEL` | `medium-streaming@en` | `tiny-streaming` is the low-memory choice (about 290 MB RSS against about 1 GB for medium) |
| Model cache directory | `MOONSHINE_VOICE_CACHE` (library) | platformdirs user cache | the LaunchAgent must be able to write it |
| Transcriber cache | the served set | n/a | memory is the sum of the listed models |

#### Audio path
PCM16LE from the wire becomes a float32 sequence in `[-1, 1]` (`numpy.frombuffer(..., "<i2") / 32768`, then `.tolist()`), because `Stream.add_audio` builds a ctypes array from a Python sequence. The wire is pinned at 16 kHz mono (`protocol.py`), so the sample-rate argument is always 16000. The spike's numbers all used 48 kHz input; the Phase 1 benchmark measures the 16 kHz path and the list-conversion cost.

### Integration Seams

| Seam | Writer (task) | Caller (task) | Contract |
|------|---------------|---------------|----------|
| `SnapshotSpec` callables | `moonshine.py` `prefetch_specs` (Phase 1) | `prefetch.py` `check_spec`/`fetch_spec` (Phase 1) | `check` returns the same complete/missing/invalid result the HF path does; `fetch` emits the same event fields; HF specs (callables `None`) behave exactly as before |
| Constructor vs `start()` | `MoonshineBackend` (Phase 1) | registry and missing-extra tests | constructor never imports the package or validates; `start()` raises `ConfigError` or `ModuleNotFoundError` |
| Backend enumerations | Phase 1 registry | Phase 2 justfile, installer, plist renderer, smoke harness, docs port table | every list that names backends gains `moonshine`; `tests/test_justfile_recipes.py` and `tests/test_smoke_launchd.py` assert the sets are equal |
| Benchmark to go/no-go | Phase 1 benchmark | Phase 2 last step | the recorded p50 numbers are the only input to the streaming-plan decision |

## Architecture & Call Flow

Component graph:

```mermaid
graph LR
    Client["Pipecat bot / client"] -->|"WS: audio.append, commit"| Server["TranscriptionServer (server.py)"]
    Server -->|"open_stream, feed, end, events"| Backend["MoonshineBackend + stream"]
    Backend -->|"daemon thread per decode"| Runtime["moonshine-voice (libmoonshine, ONNX CPU)"]
    Server -->|"committed, delta, completed"| Client
    Prefetch["prefetch CLI / installer"] -->|"check, fetch via SnapshotSpec callables"| Cache["model cache (platformdirs)"]
    Runtime -->|"load model"| Cache
    Cache -.->|"first use or prefetch only"| CDN["download.moonshine.ai"]
    Agent["LaunchAgent (PATH, HOME only)"] -->|"runs serve"| Server
```

Trigger order for one turn (commit-oriented, this plan):

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server
    participant B as MoonshineStream
    participant R as moonshine-voice
    C->>S: append (chunks)
    Note over S: audio buffered in state.buffer
    C->>S: commit
    S-->>C: input_audio_buffer.committed
    S->>B: open_stream(language), feed(whole utterance), end()
    B->>R: one-shot stream: create_stream, add_audio, stop (decode thread, under thread lock)
    R-->>B: lines
    S->>B: events()
    B-->>S: delta (full text), completed
    S-->>C: transcription.delta, transcription.completed
```

Context lifecycle:

| Step | Trigger | Enters context | Cleared/persisted | Turn boundary |
|------|---------|----------------|-------------------|---------------|
| 1 | `start()` | package import, catalog, served map | served map lives for the process | server ready |
| 2 | Prefetch or first decode of a served language | model download and ONNX load under `download_phase()` | transcriber cached for the process; files persist in the model cache | none |
| 3 | `commit` | whole utterance, language fallback applied | buffer cleared at commit (server) | `committed` sent |
| 4 | `end()` | float32 conversion, one-shot-stream decode under the thread lock | stream freed after `events()` | `completed` sent |
| 5 | `cancel` or `close` | stream marked cancelled; bounded drain | result dropped; backend `abort()` | session closed |

## Testing Notes

### Test Approach
- [ ] Unit tests for the backend with a fake `moonshine_voice` module (no wheel needed in CI)
- [ ] Table tests for spec grammar, licence guard and language normalisation
- [ ] Prefetch tests through `SnapshotSpec` callables with a fake `moonshine_voice.download`
- [ ] Contract tests extended with moonshine rows: `tests/test_decode_guard.py`, `tests/test_supervised_cli.py` (download and model-invalid tagging, `abort()`), `tests/test_stt_server.py` (`_make_backend`, argparse, lazy import: rows added, no existing assertion changed)
- [ ] Backend-enumeration tests pass (`test_justfile_recipes`, `test_render_stt_plist`, `test_smoke_launchd`, `test_backend_registry`)
- [ ] Manual: real decode of `moonshine_voice/assets/two_cities.wav` through the running server for en medium-streaming; one non-English model with an operator-supplied clip of a known phrase (the repo has no non-English audio); one decode through an installed LaunchAgent, with the model cache populated and empty
- [ ] Run the suite with `uv run --inexact` (or `--no-sync`): plain `uv run` prunes the installed backend extras and crash-loops LaunchAgents. `--inexact` is accepted by uv 0.13.8; whether it keeps the extras is confirmed on the first run. Invoke pytest as `python -m pytest`: `uv run --inexact pytest` failed with "Failed to spawn: pytest" in this checkout (2026-11-14), while `uv run --inexact python -m pytest` ran `tests/test_docs_navigation.py` (5 passed).
- [ ] Run the full suite in the background, not the foreground

### Test Results
- [ ] All existing tests pass
- [ ] New tests added and passing
- [ ] Manual verification complete

### Edge Cases Tested
- [ ] Empty or near-silence audio (empty decode contract)
- [ ] Odd language values (see `tests/test_moonshine_backend.py` list above)
- [ ] Cancel and close mid-decode; SIGTERM drain with a decode thread alive
- [ ] Two sessions committing at once (serialised)
- [ ] First decode when the model is not cached (download failure path); corrupt cache
- [ ] Raw line text for `ja` and `zh` (space-joining check)

## Acceptance Criteria

- `uv sync --extra moonshine --inexact` followed by `python -m stt_server --backend moonshine` serves a real English utterance over the wire as `committed`, one `delta` equal to `completed`, then `completed`.
- A non-English language from the supported set loads and returns non-empty text for an operator-supplied clip. If no clip is available, the criterion is limited to "loads and decodes without error".
- `ko`, `uk` and non-streaming non-English archs are refused with `ConfigError` at `start()`. A client language outside the served set logs an error and falls back; no download is attempted for it.
- `prefetch --backend moonshine` fetches the served set and exits non-zero on a missing or corrupt snapshot, like the HF backends.
- `just stt-install moonshine` installs and probes a LaunchAgent on the confirmed port, and the backend-enumeration tests pass.
- No change to `PROTOCOL_VERSION`, `protocol.py` or `server.py` session handling. No existing assertion in `tests/test_stt_server.py` changes.
- The Phase 1 benchmark numbers (one-shot p50 and `stop()` flush p50 for 3, 5 and 10 s at 16 kHz) and the go/no-go for the streaming plan are recorded in `## Findings`.
- Code reviewed and approved; the full suite passes; docs updated (backends, operations, migration, contributing, README, CHANGELOG).

<!-- reviewed: 2026-11-15 @ 792a76dab350115b0fd43d218c5ad8d57f500f48 -->
<!-- /review-plan writes the marker line above. Everything below is the workspace: edits here do NOT invalidate the marker. -->

## Progress

- [x] Phase 0: Spike (moonshine-voice behaviour, threading, languages)
- [x] Phase 1: Commit-oriented moonshine backend, prefetch and benchmark
- [x] Phase 2: Install, operations and docs wiring, then the go/no-go record

## Findings

### Phase 0 spike (2026-11-14, Apple Silicon Mac, `moonshine-voice` 0.1.6, throwaway venv in the session scratchpad; two_cities.wav is 44.4 s at 48 kHz, passed at 48 kHz to every call)

| | tiny-streaming (en) | medium-streaming (en, default) |
|---|---|---|
| Model on disk | 43 MB | 267 MB |
| RSS after load / peak | 287 MB / 727 MB | 1000 MB / 1953 MB (two transcribers) |
| Unpaced stream (RTF) | 0.10 | 0.27 |
| `add_audio` p50 / p95 / max | 0.3 / 55 / 81 ms | 0.4 / 170 / 475 ms |
| Offline `transcribe_without_streaming` | 0.99 s | 3.35 s |
| First non-empty partial | 1.9 s of audio | 1.9 s of audio |
| Non-prefix text revisions | 43 of 59 changes | 46 of 63 changes |
| 2 threads, 1 transcriber: wall time, outputs identical | 9.2 s (single 4.7 s), yes | 28.1 s (single 12.7 s), yes |
| 2 threads, 2 transcribers: wall time, outputs identical | 8.0 s, yes | 24.9 s, no |

- Callbacks fire on the calling thread (`MainThread` in the spike). A second stream on the same transcriber works after the first one's `stop()`.
- Catalog and language errors were read from `moonshine_voice.download._stt_catalog()` and `find_model_info`.
- Not measured: tail-flush latency after `stop()`, non-English models, Linux behaviour, a soak under concurrency, and the 16 kHz path. The cause of the differing outputs with two concurrent transcribers is not known; the compared transcriber had already served six streams.
- The spike left about 310 MB in `~/Library/Caches/moonshine_voice`. The script and venv are in the session scratchpad and are not part of the repo.

### Review 2026-11-14 (run `20261114T215650Z`)
- Five lenses completed (none fell back to opus); the Contradiction Pass errored with a network error and was not retried, so the envelope is pass A's: raw 52, merged 2, unique 48, 3 Contradiction findings found by the lenses. Full JSON: `.review-plan/latest-claude.json`. The lenses read the plan from disk instead of having it embedded in the prompt.
- Finding #1 (licence guard, Critical) resolved as a fact: the upstream LICENSE enumerates the non-MIT models exhaustively and every streaming model is MIT, so the guard in Requirements matches it. The stderr notice for non-`en` models is documented.
- Decisions 1-6 were grilled and accepted; Decision 1 was clarified by the user as "split only if it impacts other models", which holds because the streaming mode changes `server.py` session handling that every backend uses.
- Findings that apply only to the streaming mode moved to `20261114-feature-moonshine-streaming-mode.md`: #4, 5, 6, 10, 11, 14, 18, 28, 29 and Minor 38, 47, 48, plus the streaming halves of #20, 21 and 27.
- Mechanical findings applied in this plan: #2, 12, 13, 15 (citations to `snapshot.py`), 19 (full-suite gate), 24, 25, 26, 34, 35, 36, 37, 39, 40, 41, 42-46, 49, 50.

### Phase 1 (2026-11-14, conduct run, 1 fix-loop iteration)
- Repeated `transcribe_without_streaming` on one `Transcriber` adds a leading "Mm" line from the second call (tiny-streaming and medium-streaming). Reproduced independently by the conductor; one-shot streams were stable over 30 decodes per model (3, 5, 10 s clips, 10 repeats each, plus an interleaved offline call). Backend switched to a one-shot stream (Decision 11). This is probably the same family as the spike's "differing outputs" observation, but that link is unproven.
- Real wheel: `uv sync --extra moonshine --inexact` then `prefetch --backend moonshine` reported the default model ready (269.3 MB); `tests/test_moonshine_backend.py`, `test_prefetch_cli.py`, `test_backend_registry.py` and `test_benchmark_moonshine_latency.py` pass with the wheel installed (321 passed, 0 skipped, so the live-catalog test ran). Full suite: 2783 passed, 24 skipped.
- Implementer-reported, not re-measured: unpaced `stop()` flush p50 on medium en about 0.13 s (3 s), 0.135 s (5 s), 0.17 s (10 s), 3 runs each; default path decoded `two_cities.wav` with language `auto` and `fr` both falling back to `en` with one logged error.
- Not run in Phase 1: `ja`/`zh` raw line text (no non-English audio, models not downloaded), the paced wire benchmark (needs a running server; Phase 2 records it), the one-transcriber concurrent check (decodes are serialised).
- `uv lock` bumped the `uv.lock` header from `revision = 3` to `revision = 5` (uv version artefact).
- Added `tests/_fake_moonshine.py` (shared fake wheel, catalog, manifest, CRC32C), outside the declared test-file slot.
- Advisory reviewer (opus, one pass, 0 Critical, 0 Important, 8 Minor; not acted on, to triage before merge):
  1. `moonshine.py:300` raises a generic `RuntimeError` and drops the library's `Error.error`; chain it (`from`) so the server log has the cause.
  2. Corrupt cache (size matches, bad CRC32C) is only detected by prefetch; the serve path relies on the library's size-only fast path, so the plan's failure-table row ("prefetch check, or first load") overstates it. Either CRC-check before the first `Transcriber()` load or narrow the row.
  3. A size-matching corrupt file makes prefetch fail with `ModelInvalidError` and re-running cannot repair it (the library accepts the size); name the path in the message or delete and re-download once.
  4. `prefetch.py:429` keys the `huggingface_hub` hint on message text, which changes the HF path for a missing transitive dependency (e.g. `filelock`); key it on the spec type instead so HF behaviour is exactly as before.
  5. On a server that does not serve `en`, every default-language (`None`/`auto`) session logs an ERROR line; map `None`/blank/`auto` straight to the fallback without the error note and keep `logger.error` for explicit unserved languages (this departs from the letter of the language contract, so it needs the user's call).
  6. No live-wheel test pins the manifest-derived directory (the fake mirrors the implementation); add an `importorskip` test. A manual check showed `medium-streaming-en/quantized_26_11_02` matches.
  7. Space-joining of line texts is unverified for `ja`/`zh`; inspect raw text before Phase 2 documents them as supported.
  8. Benchmark names the one-shot wire measurement "offline" (`run_offline_wire`, `measure_offline_wire`, `offline_p50_s`); rename to `one_shot` so the go/no-go record is not confused with the rejected offline call.
- Triage (user-approved, applied after the conduct run): 1 chained the library `Error.error` as the cause; 2 and 3 prefetch self-repairs a size-matching CRC32C-bad file (delete, re-download once, then `ModelInvalidError` naming the path), serve stays size-only, failure-table row narrowed; 4 the `huggingface_hub` hint is keyed on the spec type (`check`/`fetch` set means non-Hub); 5 `None`/blank/`auto` map to the fallback silently, `logger.error` only for explicit unserved languages; 6 live-wheel test pins the manifest directory; 8 renamed to `one_shot` (also in the streaming-mode plan's gate wording); 7 see the ja/zh verification entry below.
- ja/zh verification (finding 7, 2026-11-14, `moonshine-voice==0.1.6`, macOS `say` clips Kyoko/Tingting at 16 kHz, one-shot path through the real backend; audio not kept in the repo). Catalog: `ja` offers small-streaming, tiny-streaming, base, tiny; `zh` offers tiny-streaming and base; the backend's default arch resolves to `small-streaming` (ja, 121.8 MB, `quantized_26_08_23`) and `tiny-streaming` (zh, 32.3 MB, `quantized_26_08_24`). Single-sentence clips give one `LineCompleted` line; multi-sentence clips give 2 to 3 lines with no spaces inside a line (ja, zh text carries no word spaces; zh uses ASCII commas). The backend joins lines with `" "`, so `completed` reads `おはようございます 今日の会議は午後3時からです 資料は...`: a space joiner is right for `en` and other space-delimited languages, wrong for `ja` and `zh`. The library prints its non-commercial "Moonshine Community License" notice when loading both models. Not applied: join with `""` for `ja`/`zh` (needs the resolved language code from `_transcriber_for`).
- Decision (user, 2026-11-14): `ja`/`zh` are out of scope for verification and the joiner fix. Nothing in the implementation precludes them: a developer downloads the model and serves it with `--model <arch>@ja`. We never ship or bundle models; licence suitability for a given model (the library's non-commercial Community notice) is the developer's call, so Phase 2 docs state that plainly and make no `ja`/`zh` support claim beyond "downloadable and servable". Known limitation to document: lines are joined with a space, so `ja`/`zh` text reads with spaces. The licence guard (non-streaming non-`en` refused) stays as decided earlier.

### Phase 2 benchmarks (2026-11-15, Apple silicon laptop, `moonshine-voice==0.1.6`, default `medium-streaming@en`)
- Full suite after the Phase 2 wiring: 2823 passed, 23 skipped.
- Paced single-endpoint benchmark (`scripts/benchmark_moonshine_latency.py`, p50 of 9 warm runs, cut from `two_cities.wav`). Two runs, same machine, minutes apart:

  | utterance | one-shot commit-to-completed, run 1 | run 2 | `stop()` flush, run 1 | run 2 |
  |---|---|---|---|---|
  | 3 s | 144 ms | 144 ms | 67 ms | 66 ms |
  | 5 s | 264 ms | 2130 ms | 75 ms | 74 ms |
  | 10 s | 991 ms | 821 ms | 215 ms | 88 ms |

  The two runs disagree by 8x at 5 s. The machine was not quiet (load average about 6.9 at the time of the second run, with one browser content process at 99% CPU), and moonshine decodes on CPU through ONNX Runtime, so contention shows up directly in latency. These numbers do not settle the go/no-go (see below).
- Cross-backend comparison (one-off driver, not committed; 20 synthetic `say` utterances of 2 to 7 s with known text, four voices, audio sent unpaced; WER is indicative only because text-to-speech audio flatters it; whisper and parakeet loaded alongside moonshine; nemotron served by another app's server on port 9865):

  | backend | WER mean | latency p50 | RTF p50 |
  |---|---|---|---|
  | nemotron | 0.004 | 336 ms | 0.073 |
  | parakeet | 0.026 | 166 ms | 0.035 |
  | whisper | 0.026 | 1173 ms | 0.266 |
  | moonshine | 0.070 | 458 ms | 0.100 |

  Moonshine is the least accurate and mid-field on speed here (CPU decode against MLX for the others).
- Go/no-go: see the quiet-machine rerun below; the noisy runs above are kept for the record.

### Phase 2 quiet-machine rerun and empty-cache check (2026-11-15)
- Paced benchmark rerun at 12:01 with the 1-minute load average at 1.92 (the earlier runs had about 6.9 and a browser process at 99% CPU). p50 of 9 warm runs, `medium-streaming@en`, same script and corpus as above:

  | utterance | one-shot commit-to-completed | `stop()` flush | flush / one-shot |
  |---|---|---|---|
  | 3 s | 149 ms | 72 ms | 0.48 |
  | 5 s | 245 ms | 80 ms | 0.33 |
  | 10 s | 653 ms | 94 ms | 0.14 |

  The 1-minute load average read 8.61 after the run. Moonshine decodes on CPU, so the run's own ONNX threads are the likely cause; this was not measured separately (only start and end were logged). The five-minute average at the start was still 3.29, so "quiet" means no competing process, not an idle history.
- Cross-backend spread (one-off driver above): per-utterance real-time factor varied 1.3x (nemotron) to 2.8x (whisper), moonshine 1.9x, so in that run the load did not single moonshine out; the other backends use MLX, so a quiet-machine comparison could still shift moonshine relative to them. The four-way was not rerun: the whisper and parakeet agents were removed after the first comparison.
- Empty-cache decode through an installed agent: uninstalled the agent, moved `~/Library/Caches/moonshine_voice` to `moonshine_voice.bak-20261115` (583 MB, includes the ja/zh spike models; left in place for the user to remove), ran `just stt-install moonshine`, then decoded a 3 s clip over `ws://127.0.0.1:9965/`. The agent listens before any model exists; the first decode downloads the `medium-streaming-en` and `spelling-en` models (269 MB) and succeeds, 12 s end to end including the download. The warm decode that followed took 158 ms. The populated-cache case is covered by the benchmark runs above.
- Go/no-go rule (Phase 2 last step): start the streaming-mode plan only if the 5 s one-shot p50 exceeds 300 ms and the flush p50 is below half of it. Measured 245 ms, so the first condition is **not met** (the flush condition, 80 ms against 122 ms, holds but does not matter without the first). By the rule the streaming plan is not started: a one-shot decode of a 5 s utterance already finishes in under a third of a second after the commit. Disposition (user, 2026-11-15): `20261114-feature-moonshine-streaming-mode.md` stays unimplemented and is marked deferred.

### Review Waivers

(none)

## Issues & Solutions

(none yet)

## Final Results

(fill when complete)
