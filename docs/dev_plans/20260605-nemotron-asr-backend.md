# Task: Add NVIDIA Nemotron 3.5 ASR streaming backend (0.3.0)

**Status**: Draft (awaiting review)
**Component**: ASR Backends
**Assigned to**: Claude
**Priority**: Medium
**Branch**: feat/nemotron-asr-backend
**Created**: 2026-06-05

## Objective

Add a fourth ASR backend, `nemotron`, backed by NVIDIA's
**Nemotron 3.5 ASR streaming 0.6b** (`mlx-community/nemotron-3.5-asr-streaming-0.6b`)
running on Apple Silicon via the **`mlx-audio`** package. Wire it into the
existing single-backend-per-process model exactly like `parakeet`: a new
`--backend nemotron` choice, a backend-aware default model, an optional install
extra, a private-temp-dir commit-oriented decode, and the per-ASR socket
convention. Ship as a **0.3.0** minor (additive, non-breaking).

## Context

Nemotron 3.5 ASR is a **cache-aware FastConformer-RNNT** (600M params, 24
encoder layers, 40 language-locales via language-ID prompt conditioning). It is
distinct from Parakeet TDT and is **NOT** served by `parakeet-mlx` —
`parakeet_mlx.from_pretrained` only returns Parakeet variants
(`ParakeetTDT/RNNT/CTC/TDTCTC`). The MLX runtime for Nemotron is
**`mlx-audio`** (Blaizzy/mlx-audio), API:

```python
from mlx_audio.stt import load
model = load("mlx-community/nemotron-3.5-asr-streaming-0.6b")
result = model.generate("audio.wav", language="en-US")   # offline, full-file
text = result.text
# (also exposes model.stream_generate(path, language=...) yielding cumulative
#  AlignedResult per chunk — NOT used in V1; see "Streaming deferred" below.)
```

Because V1's wire is **commit-oriented** (buffer PCM → one decode → one `delta`
+ one `completed`), this backend mirrors `ParakeetBackend` almost exactly:
path-based decode (`generate()` takes a **file path**, not a raw array — same as
parakeet, unlike `mlx_whisper` which takes an array), temp WAV in a per-process
`0o700` dir, the asyncio + threading decode-lock pair, the in-flight drain for
SIGTERM-mid-decode Metal-command-buffer isolation, and the empty-decode
contract (`delta` only on non-empty text, `completed` always).

**One material difference from Parakeet:** Nemotron's `generate()` **accepts a
`language` parameter** (target-lang prompting, e.g. `"en-US"`, or `"auto"` for
LID). Parakeet ignores the client `language`; Nemotron forwards it. See
"Language handling" below.

### Dependency reality (BLOCKER — read before implementing)

`mlx-audio` Nemotron STT support landed in
[Blaizzy/mlx-audio#774](https://github.com/Blaizzy/mlx-audio/pull/774), merged
to `main` ~2026-06-05. The **PyPI** `mlx-audio` release has **not** been cut
since ~end of April 2026, so **no released version on PyPI contains Nemotron
support**. Consequences:

- A `git+https://github.com/Blaizzy/mlx-audio` pin works for local
  `uv sync`/source installs but **cannot** be declared in a PyPI-published
  package's extra metadata (PyPI rejects direct-URL deps).
- Until `mlx-audio` cuts a release with PR #774, this backend **cannot ship a
  clean PyPI extra**. **DECIDED (2026-06-05): Option 1 — dev dependency-group,
  git-pinned.**
  1. **[CHOSEN] Wait** — hold the published `pyproject` *extra* (and a
     PyPI-publishable 0.3.0) until `mlx-audio` releases; land backend code +
     tests now behind a git-pinned **`[dependency-groups]` dev group** so it's
     runnable locally (`uv sync --group nemotron`) but absent from published
     `[project.optional-dependencies]`, keeping 0.3.0 PyPI-installable. A
     follow-up adds the real `nemotron` extra (clean version pin) once
     `mlx-audio` releases.
  2. ~~Git-pin the extra now~~ — rejected: would block publishing 0.3.0 to PyPI.
  3. ~~Pin a future version optimistically~~ — rejected: uninstallable extra,
     no git fallback.

Tests stub `mlx_audio` entirely (per the parakeet pattern) so CI never needs
the real package and the dependency situation does not block landing the code.
The dev group is a developer/local-install convenience; document `uv sync
--group nemotron` (or a `git+https` one-liner) in the README install block.

### Pre-existing discrepancy — FIXED (2026-06-05, this branch)

The README documented `uv sync --extra stt-server-mlx` / `--extra
stt-server-parakeet` but `pyproject.toml` names the extras **`mlx`** /
**`parakeet`** — so those documented commands **failed** (`uv` resolves extras
by their pyproject key). This was leftover monorepo-extraction drift
(`stt_server/__init__.py` even said "extras split once extracted").

**Resolved in this branch (commit "docs: fix stale extra names"):** corrected
the README executable commands to `--extra mlx` / `--extra parakeet`, plus the
prose and the stray `stt-server-{mlx,parakeet,client}` references in
`__init__.py`, `client.py`, `__main__.py`, `parakeet.py`, `mlx_teardown_spike.sh`,
and `test_stt_server.py`. Direction chosen: **fix docs to match the shipped
pyproject** (non-breaking) rather than rename the extras (which would break
consumers like Koda that already `uv sync --extra parakeet`). Launchd *labels*
`pipecat.stt-server.parakeet` were left untouched (they are not extras).
Verified `rg 'stt-server-(mlx|parakeet|client)'` returns only label hits.

The Nemotron work therefore uses the correct convention from the start: a
`[dependency-groups]` **`nemotron`** dev group now (per the Option-1 decision
above), and a future `nemotron` *extra* with the same key once `mlx-audio`
releases — never a `stt-server-`-prefixed name.

## Requirements

- New `stt_server/backends/nemotron.py` with `NemotronBackend` +
  `_NemotronStream`, structurally satisfying `TranscriptionBackend` /
  `BackendStream`, mirroring `ParakeetBackend`:
  - lazy `import mlx_audio` only inside `start()` / `_get_model()` (never at
    module load — preserves the lean-base invariant so `echo`/`mlx`/`parakeet`
    construct without `mlx-audio` installed);
  - `start()` does an eager `import mlx_audio` (fail-fast on missing extra), but
    does **not** load the model;
  - `_get_model()` lazy-loads via `mlx_audio.stt.load(self._model_id)` under
    `_model_lock` (load-once);
  - commit-oriented decode in a daemon thread; PCM16LE buffer → temp WAV in a
    per-process `tempfile.mkdtemp(prefix="pipecat-stt-nemotron-")` `0o700` dir,
    written with the protocol-pinned `AUDIO_CHANNELS` / `AUDIO_SAMPLE_WIDTH_BYTES`
    / `AUDIO_SAMPLE_RATE_HZ`; `os.unlink` in `finally`; dir removed in `close()`;
  - asyncio `_decode_lock` + backend-scope threading `_thread_lock` pair, model
    loaded **inside** the thread lock (same Metal-safety rationale as parakeet);
  - backend-scope in-flight counter + condition + `close()` drain (3.0 s
    bounded wait, warn-on-timeout) — identical Metal SIGTERM rationale;
  - empty-decode contract: `delta` only when `result.text.strip()` is non-empty,
    `completed` always; `cancel()` before `end()` yields no events; `cancel()`
    mid-decode is bounded/crash-free;
  - `backend_name = "nemotron"`; `self.model = model` for the
    `server.hello`/`server.status` `backend.name`/`.model` identity fields.
- `DEFAULT_NEMOTRON_MODEL = "mlx-community/nemotron-3.5-asr-streaming-0.6b"`
  exported from `backends/nemotron.py` (single source of truth, imported lazily
  by `__main__._resolve_model`, never hardcoded twice — mirrors
  `DEFAULT_PARAKEET_MODEL`).
- `stt_server/__main__.py`:
  - `_make_backend`: add `name == "nemotron"` arm → lazy `from
    .backends.nemotron import NemotronBackend` → `NemotronBackend(model=model)`;
  - `_resolve_model`: add `backend == "nemotron"` arm → lazy-import
    `DEFAULT_NEMOTRON_MODEL`;
  - `--backend` choices `("echo", "mlx", "parakeet")` →
    `("echo", "mlx", "parakeet", "nemotron")`; update the adjacent comment that
    enumerates per-backend defaults.
- **Language handling — OPEN DESIGN QUESTION (resolve at integration).**
  Nemotron `generate()` accepts `language` (target-lang prompting; the model
  card cites `target_lang` values like `"en-US"` and `"auto"` for LID). The
  protocol already plumbs the client `language` to `open_stream(language=…)`,
  so a client-supplied value is **forwarded** to `generate(...)` (unlike
  Parakeet, which ignores it). The unresolved part is **the default when the
  client sends `None`**. Three candidates, with trade-offs:

  | Default        | Pro | Con |
  |----------------|-----|-----|
  | **Omit kwarg** | Most conservative; asserts no value we can't verify against the real signature; model uses its own built-in default. | That built-in default is undocumented — could itself be LID or could be `en`; we'd be deferring, not deciding. |
  | **`"auto"` (LID)** | Uses Nemotron's headline 40-locale language-ID; "right" for a multilingual local server; matches the model card's advertised mode. | Assumes `"auto"` is the *exact* accepted token (unverified until integration); LID adds latency and can mis-route short/silent utterances → wrong-language garbage. |
  | **Explicit `"en"` / `"en-US"`** | Deterministic, lowest-latency for the dominant local use case; no LID mis-fire on short utterances. | Wrong kwarg *value* form unknown (`"en"` vs `"en-US"` vs ISO `"eng"`); silently wrong for non-English speakers; bakes an English assumption into a "local STT server" that Parakeet's `-v3` multilingual default does not.|

  **Recommendation to carry into implementation:** make the `None`-default a
  single named module constant (e.g. `DEFAULT_NEMOTRON_LANGUAGE`) so the choice
  is one-line-swappable, and **default it to `"auto"`** *iff* integration
  confirms `"auto"` is accepted and LID latency is acceptable on the target
  hardware; otherwise fall back to **omit-the-kwarg**. Do NOT hardcode `"en"`
  for a server that otherwise ships a multilingual default — but record `"en-US"`
  as the escape hatch if LID proves unreliable in practice. **All three forms
  pass the stubbed tests**, so the exact accepted kwarg name (`language` vs
  `target_lang`) and value vocabulary (`"auto"`/`"en-US"`/…) and the
  `strip_lang_tags` output-cleanup flag are **integration-time verifications
  against the real post-#774 `generate()` signature** — a wrong name/value
  passes CI but fails live. Document the forwarded-vs-ignored contrast with
  Parakeet in the module docstring.
- `pyproject.toml`:
  - version `0.2.0` → `0.3.0`;
  - **Option 1 (decided):** add a git-pinned `mlx-audio` to a
    `[dependency-groups]` **`nemotron`** dev group (NOT
    `[project.optional-dependencies]`), so published extras stay PyPI-clean.
    Pin to the merge SHA of PR #774 (or `@main`), e.g.
    `mlx-audio @ git+https://github.com/Blaizzy/mlx-audio.git@<sha>`. **Do not**
    add a `nemotron` *extra* or an unresolvable version pin yet — that follow-up
    lands when `mlx-audio` publishes a release containing #774.
- Docs (same pass as code, per workflow):
  - README: backend bullet (mirror the parakeet bullet incl. the PII/temp-WAV
    note), `--backend {echo,mlx,parakeet,nemotron}` everywhere it is enumerated,
    a per-ASR socket-convention table row
    (`pipecat.stt-server.nemotron` / `~/Library/Caches/pipecat-stt/nemotron.sock`),
    an install/smoke block mirroring parakeet's (l.99-103) but using
    `uv sync --group nemotron` (dev group, not an extra — per Option 1);
    the stale `stt-server-mlx`/`stt-server-parakeet` extra-command fix is
    **already done on this branch** (see the FIXED note above);
  - CHANGELOG `[0.3.0]` "Added" entry; footer link target;
  - `scripts/render_stt_plist.py` label-mapping docstring/`_log_basename` if it
    enumerates per-backend labels (audit: does it special-case `parakeet`? if a
    `nemotron` label needs a basename branch, add it in lockstep Python+shell —
    see the 0.2.0 plan's `_log_basename` two-literal rule);
  - `scripts/install_stt_agent.sh` if it enumerates backend labels/sockets.
- Tests:
  - `tests/test_nemotron_backend.py` mirroring `tests/test_parakeet_backend.py`
    point-for-point (stub `mlx_audio` via `sys.modules` injection before import;
    synthetic PCM16LE, no binary fixtures): protocol conformance; non-empty →
    `delta`+`completed`; empty → `completed` only; mid-decode raise propagates
    (no swallowed `completed`, no `failed` kind); model-load failure raises from
    its distinct call site; `cancel()` pre-`end()` → no events; `cancel()`
    mid-decode bounded; 60 s+ utterance not truncated; overlapping decodes
    serialize; `DEFAULT_NEMOTRON_MODEL` non-empty; **plus** a Nemotron-specific
    assertion that the stub's `generate` receives the forwarded `language`
    (and that `language=None` omits/`"auto"`s it per the chosen default).
  - Any test that enumerates backend choices (`test_stt_server.py`,
    `test_backend_protocol.py`?) — grep for `"parakeet"` / `choices` and extend.
- `ruff format` + `ruff check` clean; **full** suite green before PR.

## Review Focus

- **Right runtime package**: confirm `mlx-audio` (`from mlx_audio.stt import
  load`), NOT `parakeet-mlx`, and that the real (post-#774) `generate()`
  signature matches what the backend calls — the stub will pass CI regardless,
  so this is an **integration-time** verification, called out, not assumed.
- **Dependency/publishability**: the chosen option (1/2/3) must keep — or
  consciously forgo — PyPI-installability of 0.3.0; no unresolvable pin shipped.
  State which option landed and why in the CHANGELOG/PR.
- **Lean-base invariant intact**: `echo`/`mlx`/`parakeet` still construct with
  `mlx-audio` absent; the `nemotron` import is lazy in BOTH `_make_backend` and
  `_resolve_model`; no module-load-time `import mlx_audio`. Verify with a
  no-extra import test if one exists for parakeet.
- **Metal-safety parity**: lock pair, model-load-inside-thread-lock, in-flight
  drain, and `0o700` temp dir are present and rationale-accurate (not
  copy-paste comments referencing parakeet). PII temp-WAV handling matches.
- **Empty-decode + cancel contract** byte-identical to parakeet/mlx semantics.
- **Language contract**: forwarded-not-ignored is correct and tested; the
  `None` default is the conservative one chosen; docstring states the
  parakeet contrast so the protocol match doesn't hide a semantic difference.
- **Enumeration completeness**: every `{echo,mlx,parakeet}` site (CLI choices,
  README prose+tables, install/plist scripts) gains `nemotron`; grep-verified
  zero stragglers. Sibling dev plans referencing backend enumeration updated.
- **Streaming deferred is explicit**: the module docstring states V1 uses
  `generate()` (offline) and that `stream_generate()` is intentionally unused
  until a streaming wire protocol lands — so a reader doesn't assume the
  cache-aware streaming is active.

## Streaming deferred

Nemotron's headline feature is cache-aware **streaming** (80 ms–1.12 s chunks).
V1's wire is commit-oriented and runs ASR **after** smart-turn, so the backend
always sees a complete utterance — identical to why Parakeet streaming is
deferred. We call `generate()` (full-file offline), not `stream_generate()`.
Activating streaming is a separate, larger change (new wire events, partial
`delta`s, VAD/turn interplay) tracked outside this plan.

## Implementation Checklist

### Phase 1: NemotronBackend + tests (stubbed mlx_audio)

**Impl files:** `stt_server/backends/nemotron.py`
**Test files:** `tests/test_nemotron_backend.py`
**Test command:** `uv run python -m pytest tests/test_nemotron_backend.py -q`
**Validation:** backend satisfies protocols; full parakeet-parity contract green
with `mlx_audio` fully stubbed; language-forwarding assertion green.

Mirror `stt_server/backends/parakeet.py` structurally; swap the runtime
(`mlx_audio.stt.load` + `model.generate(path, language=…)`), the `backend_name`,
the default-model constant, the temp-dir prefix, and the language-forwarding
behavior. Comments describe Nemotron's own rationale, not parakeet's.

### Phase 2: CLI wiring + choice-enumeration tests

**Impl files:** `stt_server/__main__.py`
**Test files:** `tests/test_stt_server.py` (+ any enumerating backend choices)
**Test command:** `uv run python -m pytest tests/test_stt_server.py -q`
**Validation:** `--backend nemotron` constructs the backend; `_resolve_model`
returns `DEFAULT_NEMOTRON_MODEL` when `--model` unset; lazy-import (lean base)
preserved; `echo`/`mlx`/`parakeet` unaffected.

### Phase 3: Packaging + docs

**Impl files:** `pyproject.toml`, `README.md`, `CHANGELOG.md`,
`scripts/render_stt_plist.py` (+ `install_stt_agent.sh` if it enumerates labels)
**Test files:** `tests/test_render_stt_plist.py` (if a nemotron label/basename
branch is added)
**Test command:** `uv run python -m pytest -q`
**Validation:** version `0.3.0`; dependency handled per chosen option;
README/CHANGELOG/socket-table/extra-command updates land with grep-verified
enumeration completeness; pre-existing `stt-server-*` extra-command mismatch
fixed; full suite + `ruff format`/`ruff check` clean.

## Out of scope

- Activating cache-aware **streaming** / partial `delta`s (separate plan).
- Changing the V1 commit-oriented wire protocol or VAD/turn handling.
- Removing or renaming the `KODA_STT_*` env-var aliases.
- Cutting the upstream `mlx-audio` PyPI release (external dependency).
