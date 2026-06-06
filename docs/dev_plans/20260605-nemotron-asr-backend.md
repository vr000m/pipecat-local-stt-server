# Task: Add NVIDIA Nemotron 3.5 ASR streaming backend (0.3.0)

**Status**: Implemented — PR #7 open / in review (all phases 0-3 complete 2026-06-05)
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
`--backend nemotron` choice, a backend-aware default model, a temporary
`[dependency-groups]` local-install path until a PyPI-clean extra is possible,
a private-temp-dir commit-oriented decode, and the per-ASR socket
convention. Ship as a **0.3.0** minor (additive, non-breaking).

## Context

Nemotron 3.5 ASR is a **cache-aware FastConformer-RNNT**. Point-in-time model
card / PR facts to re-check during Phase 0: 600M params, 24 encoder layers, 40
language-locales via language-ID prompt conditioning, and streaming chunk sizes
around 80 ms-1.12 s. It is distinct from Parakeet TDT and is **NOT** served by
`parakeet-mlx` —
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

> **ASSUMPTION — verify at integration (the whole `mlx_audio` surface, not just
> `generate()`).** `mlx_audio` is NOT in this repo (`rg mlx_audio` hits only this
> plan), so the entire API shape above is inference from the model card / PR #774,
> not verified code: the `mlx_audio.stt` module path, the `load` entry point and
> its arity, `generate()`'s signature (kwargs, path-vs-array input), and the
> `result.text` / `AlignedResult` return shape. Treat **all** of these as
> integration-time checks — a wrong module path, kwarg, or attribute passes the
> stubbed CI but fails live. Implementation MUST mirror parakeet's defensive
> `getattr(result, "text", "") or ""` (`parakeet.py:158`) rather than asserting
> `result.text`, so an unexpected return shape degrades to empty text, not an
> `AttributeError`.

Because V1's wire is **commit-oriented** (buffer PCM → one decode → one `delta`
+ one `completed`), this backend mirrors `ParakeetBackend` almost exactly:
path-based decode (`generate()` is **assumed** to take a **file path**, not a raw
array — same as parakeet, unlike `mlx_whisper` which takes an array; part of the
integration-time check above), temp WAV in a per-process `0o700` dir, the asyncio
+ threading decode-lock pair, the in-flight drain for SIGTERM-mid-decode
Metal-command-buffer isolation, and the empty-decode contract (`delta` only on
non-empty text, `completed` always).

> **ASSUMPTION — Metal crash-class parity.** The in-flight drain is carried over
> on the premise that Nemotron is exposed to the *same* SIGTERM-mid-decode Metal
> command-buffer assertion as parakeet/mlx. That rationale is grounded in readable
> code for parakeet-mlx (`parakeet.py:198-200`) but is an **inference** for
> mlx-audio (different package, FastConformer-RNNT vs TDT, decode path unreadable
> here). It is MLX/Metal-backed, so the assumption is plausible and the drain is a
> harmless no-op if wrong — keep it, but label the rationale as *assumed-by-analogy*
> in the module comment, not "identical", and confirm the failure mode at integration.

**One material difference from Parakeet:** Nemotron's `generate()` **accepts a
`language` parameter** (target-lang prompting, e.g. `"en-US"`, or `"auto"` for
LID). Parakeet ignores the client `language`; Nemotron forwards it. See
"Language handling" below.

### Dependency reality (BLOCKER — read before implementing)

`mlx-audio` Nemotron STT support landed in
[Blaizzy/mlx-audio#774](https://github.com/Blaizzy/mlx-audio/pull/774), merged
to `main` ~2026-06-05. The **PyPI** `mlx-audio` release has **not** been cut
since ~end of April 2026, so **no released version on PyPI contains Nemotron
support**.

> **POINT-IN-TIME FACTS — re-verify before pinning (as of 2026-06-05).** The
> PR-#774 merge state, the merge commit SHA the dev group will pin, and the
> "no PyPI release since ~end of April" claim are external facts that may drift
> before implementation. Re-check #774's merged SHA and `pip index versions
> mlx-audio` (or PyPI) immediately before writing the pin; if a release now
> contains #774, prefer a clean version pin over the git SHA.

Consequences:

- A `git+https://github.com/Blaizzy/mlx-audio` pin works for local
  `uv sync`/source installs but **cannot** be declared in a PyPI-published
  package's extra metadata — PyPI rejects projects whose `Requires-Dist`
  carries direct-URL (PEP 508 `@ <url>`) dependencies (see the PyPI
  "Forbidden: direct dependencies" upload error / `pypa/packaging-problems`).
  If this policy claim becomes load-bearing for a changed option, verify it
  against Warehouse/PyPI or TestPyPI before altering the dependency strategy.
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
  - lazy `mlx_audio` imports only inside `start()` / `_get_model()` (never at
    module load — preserves the lean-base invariant so `echo`/`mlx`/`parakeet`
    construct without `mlx-audio` installed);
  - `start()` does an eager **non-loading** STT entrypoint check (for example
    `from mlx_audio.stt import load`) so the server fails before socket bind if
    the installed package lacks the required module/callable, but does **not**
    load the model;
  - `_get_model()` lazy-loads via the Phase-0-verified `load(self._model_id)` under
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
  passes CI but fails live. The module docstring must state the **three-way
  `language` contract across backends** so the divergence is discoverable, not
  just the parakeet contrast: `parakeet` accepts-and-ignores
  (`parakeet.py:62-68`), `mlx_whisper` forwards (`mlx_whisper.py:141`), and
  `nemotron` forwards with a `DEFAULT_NEMOTRON_LANGUAGE` fallback. No shared
  abstraction/registry is introduced — deliberately; the named constant is the
  minimal call for three backends.
- `pyproject.toml`:
  - version `0.2.0` → `0.3.0`;
  - **Option 1 (decided):** add a git-pinned `mlx-audio` to a
    `[dependency-groups]` **`nemotron`** dev group (NOT
    `[project.optional-dependencies]`), so published extras stay PyPI-clean.
    Pin to the merge SHA of PR #774 (or `@main`), e.g.
    `mlx-audio @ git+https://github.com/Blaizzy/mlx-audio.git@<sha>`. **Do not**
    add a `nemotron` *extra* or an unresolvable version pin yet — that follow-up
    lands when `mlx-audio` publishes a release containing #774.
  - **Verify PyPI-cleanliness with a concrete check, don't assume it.** PEP 735
    `[dependency-groups]` are not emitted into wheel/sdist `Requires-Dist`, but
    confirm: `uv build && unzip -p dist/*.whl '*/METADATA' | grep -i mlx-audio`
    must return **empty**. Add this to the packaging validation.
- Docs (same pass as code, per workflow):
  - README: backend bullet (mirror the parakeet bullet incl. the PII/temp-WAV
    note), `--backend {echo,mlx,parakeet,nemotron}` everywhere it is enumerated,
    a per-ASR socket-convention table row
    (`pipecat.stt-server.nemotron` / `~/Library/Caches/pipecat-stt/nemotron.sock`),
    an install/smoke block mirroring parakeet's (l.99-103) but using
    `uv sync --group nemotron` (dev group, not an extra — per Option 1);
    the stale `stt-server-mlx`/`stt-server-parakeet` extra-command fix is
    **already done on this branch** (see the FIXED note above);
  - CHANGELOG `[0.3.0]` "Added" entry; footer link target; explicitly state
    Option 1 landed, why no published `nemotron` extra is included yet, and how
    PyPI-installability was verified. Update the PR description with the same
    dependency/publishability decision and final validation evidence.
  - **`scripts/render_stt_plist.py` `_BACKEND_RE` allowlist (MANDATORY, not an
    audit).** `_BACKEND_RE = re.compile(r"^(echo|mlx|parakeet)$")`
    (`render_stt_plist.py:44`) is a hard `sys.exit(2)` gate at `:102` — a
    `nemotron` render/install is **rejected** until it is widened to
    `^(echo|mlx|parakeet|nemotron)$`. Land this **in lockstep, one commit**, with
    its parametrized test: extend `tests/test_render_stt_plist.py` (parametrize
    `:255`) and add a `BACKEND=nemotron` allowlist-pass case mirroring the
    `parakeet` / `bogus` cases (`:12`). **`_log_basename` needs NO nemotron
    branch** — `parakeet` is not special-cased; the generic `.`→`-` fallthrough
    (`render_stt_plist.py:48-68`) already yields `pipecat-stt-server-nemotron`,
    so do **not** add a two-literal branch here (the 0.2.0 `_log_basename` rule
    does not apply to nemotron). `_MODEL_RE` is generic and needs no change.
  - **`scripts/install_stt_agent.sh` `DEFAULT_MODEL` branch (MANDATORY — silent
    misconfig otherwise).** The script selects the default model by backend:
    `if [[ "$BACKEND" == "parakeet" ]] … else <Whisper>` (`install_stt_agent.sh:57-67`).
    With `nemotron` it falls to the `else` and installs a **Whisper** repo id for
    a Nemotron agent — a silent wrong-model install, not a fast failure. Add a
    `nemotron` arm pointing at `DEFAULT_NEMOTRON_MODEL`'s value (mirror the
    existing "must agree with `DEFAULT_PARAKEET_MODEL`" comment), and update the
    header-comment backend enumeration (`echo|mlx|parakeet` → `…|nemotron`).
    This is a **model-default** fix, not a label/socket one.
- Tests — the "mirror point-for-point" claim is binding, so the list below
  enumerates **every** `tests/test_parakeet_backend.py` case (audited against the
  real file), not a subset:
  - `tests/test_nemotron_backend.py` mirroring `tests/test_parakeet_backend.py`
    (stub `mlx_audio` via `sys.modules` injection before import; synthetic
    PCM16LE, no binary fixtures): protocol conformance; **identity** —
    `backend_name == "nemotron"` and `model` set, the fields that feed
    `server.hello`/`server.status` (mirrors `test_backend_exposes_identity`,
    `test_parakeet_backend.py:203`); non-empty → `delta`+`completed`; empty →
    `completed` only; **whitespace-only decode → `completed` only, no `delta`**
    (mirrors `test_whitespace_only_decode_yields_completed_only`,
    `test_parakeet_backend.py:277`; directly exercises the `result.text.strip()`
    requirement above); mid-decode raise propagates (no swallowed `completed`);
    **no `kind="failed"` event invented** — source-grep guard mirroring
    `test_no_failed_event_kind_defined` (`test_parakeet_backend.py:210`);
    model-load failure raises from its distinct call site; `cancel()` pre-`end()`
    → no events; `cancel()` mid-decode bounded; 60 s+ utterance not truncated;
    overlapping decodes serialize; `DEFAULT_NEMOTRON_MODEL` non-empty.
  - **Language tests split into two** (the `None`-default is an open question —
    do NOT couple a deterministic test to an undecided value):
    - **(a) forwarding (deterministic, choice-independent):** a client-supplied
      `language` (e.g. `"es-ES"`) reaches the stub `generate`'s `language` kwarg.
      Asserts the forwarded-not-ignored contract regardless of the default.
    - **(b) `None`-default (gated on the constant):** with the client sending
      `None`, assert the stub `generate` receives exactly what
      `DEFAULT_NEMOTRON_LANGUAGE` resolves to (or that the kwarg is omitted, if
      the constant encodes "omit"). The expected value is settled when the
      constant is, not at plan time — the test pins whatever the constant says.
  - **Lean-base no-import (subprocess):**
    `test_make_backend_and_resolve_model_nemotron_do_not_import_mlx_audio`
    mirroring `test_make_backend_parakeet_arm_does_not_import_parakeet_mlx`
    (`test_stt_server.py:973`) — block `mlx_audio` import in a clean subprocess
    and assert both `_make_backend("nemotron", …)` and
    `_resolve_model("nemotron", None)` work without importing it. Without this
    the lean-base `mlx_audio` claim is asserted but unproven. (Phase 2.)
  - **CLI choice enumeration (executable, not just grep):**
    `test_argparse_backend_choices_include_nemotron` +
    `test_argparse_rejects_unknown_backend` parity, mirroring
    `test_stt_server.py:921,929`. This is the executable proof of CLI
    enumeration completeness; reserve grep for docs/scripts. (Phase 2.)
  - **Plist allowlist:** a `BACKEND=nemotron` allowlist-pass case in
    `tests/test_render_stt_plist.py` mirroring the existing `parakeet` / `bogus`
    cases (`test_render_stt_plist.py:12`, parametrize `:255`). (Phase 2 — see the
    `_BACKEND_RE` task.)
  - **Installer model default:** add a `tests/test_install_migration.py` case
    that runs the real installer harness with `PIPECAT_STT_BACKEND=nemotron`
    and asserts the rendered plist/model path uses `DEFAULT_NEMOTRON_MODEL`, not
    the Whisper default. This pins the silent-misconfig regression at the shell
    boundary, not just in renderer unit tests.
  - **Nemotron PII/temp-dir + shutdown invariants:** add focused
    `tests/test_nemotron_backend.py` cases for owner-only temp-dir permissions,
    temp WAV creation under that private dir plus `os.unlink` after decode, and
    `close()` waiting/bounding while a decode is in flight. Keep these alongside
    the existing event/serialization parity tests so the Metal-safety and PII
    claims are executable, not comment-only.
  - Any other test that enumerates backend choices — grep for `"parakeet"` /
    `choices` and extend.
- `ruff format` + `ruff check` clean; **full** suite green before PR.

## Review Focus

- **Right runtime package**: confirm `mlx-audio` (`from mlx_audio.stt import
  load`), NOT `parakeet-mlx`, and that the real (post-#774) `generate()`
  signature matches what the backend calls — the stub will pass CI regardless,
  so this is an **integration-time** verification, called out, not assumed.
  This probe is Phase 0 and blocks backend implementation.
- **Dependency/publishability**: the chosen option (1/2/3) must keep — or
  consciously forgo — PyPI-installability of 0.3.0; no unresolvable pin shipped.
  State which option landed and why in the CHANGELOG/PR.
- **Lean-base invariant intact**: `echo`/`mlx`/`parakeet` still construct with
  `mlx-audio` absent; the `nemotron` import is lazy in BOTH `_make_backend` and
  `_resolve_model`; no module-load-time `import mlx_audio`. **Proven** by the new
  subprocess test that blocks `mlx_audio` and calls both
  `_make_backend("nemotron", ...)` and `_resolve_model("nemotron", None)`
  (mirrors `test_stt_server.py:973`) — not merely asserted.
- **Metal-safety parity**: lock pair, model-load-inside-thread-lock, in-flight
  drain, and `0o700` temp dir are present; the drain's crash-class rationale is
  labelled *assumed-by-analogy* for mlx-audio (not "identical"), per the Metal
  parity ASSUMPTION in Context. PII temp-WAV handling matches and is proven by
  tests for private-dir permissions, temp WAV unlinking, and bounded `close()`
  drain under an in-flight decode.
- **Empty-decode + cancel contract** byte-identical to parakeet/mlx semantics;
  `result.text` read defensively via `getattr(result, "text", "") or ""`.
- **Language contract**: forwarded-not-ignored is proven by the deterministic
  forwarding test (a); the `None` default is gated on `DEFAULT_NEMOTRON_LANGUAGE`
  and pinned by test (b); the module docstring states the **three-way** contract
  (parakeet ignores / mlx_whisper forwards / nemotron forwards-with-default).
- **Enumeration completeness**: every `{echo,mlx,parakeet}` site gains
  `nemotron` — CLI choices (`__main__.py:350`), README prose+tables, **the
  `_BACKEND_RE` allowlist gate** (`render_stt_plist.py:44`) and **the
  install-script `DEFAULT_MODEL` branch** (`install_stt_agent.sh:57-67`) — the
  two hard gates, not just docs. Grep-verified zero stragglers; the CLI half is
  proven by `test_argparse_backend_choices_include_nemotron`, the renderer by
  `tests/test_render_stt_plist.py`, and the installer default-model shell seam by
  `tests/test_install_migration.py`. Sibling dev plans referencing backend
  enumeration checked and updated if needed.
- **Streaming deferred is explicit**: the module docstring states V1 uses
  `generate()` (offline) and that `stream_generate()` is intentionally unused
  until a streaming wire protocol lands — so a reader doesn't assume the
  cache-aware streaming is active.

## Streaming deferred

Nemotron's headline feature is cache-aware **streaming** (model-card / PR
point-in-time claim: 80 ms-1.12 s chunks; re-check in Phase 0).
V1's wire is commit-oriented and runs ASR **after** smart-turn, so the backend
always sees a complete utterance — identical to why Parakeet streaming is
deferred. We call `generate()` (full-file offline), not `stream_generate()`.
Activating streaming is a separate, larger change (new wire events, partial
`delta`s, VAD/turn interplay) tracked outside this plan.

## Implementation Checklist

### Phase 0: Dependency pin + real mlx-audio API verification

**Impl files:** `pyproject.toml`, `uv.lock`
**Test files:** none, but record command output in the PR description / plan
workspace if needed
**Validation commands:** re-check #774 merge state and PyPI releases; add/update
the `nemotron` dependency group; run `uv lock` or `uv sync --group nemotron`;
then run a real-package probe against the pinned dependency.
**Validation:** the dependency pin is resolved and locked; `from mlx_audio.stt
import load` succeeds; the model loads or the probe explicitly records why a
full load is not possible on the machine; the callable used by the backend is
confirmed (`generate` kwarg name, file-path input, return text shape, language
value vocabulary including `"auto"` / `"en-US"` / omit behavior, and any
`strip_lang_tags` cleanup flag); the final `DEFAULT_NEMOTRON_LANGUAGE` decision
is recorded before tests are written. If a PyPI release now contains PR #774,
prefer a clean version pin over the git SHA and reconsider whether the follow-up
published extra can land in this same release.

Phase 0 is a blocking integration gate. Do not implement `nemotron.py` or its
language tests until the real package probe has settled the call shape and the
default-language behavior; stubbed tests can make the wrong API look green.

### Phase 1: NemotronBackend + tests (stubbed mlx_audio)

**Impl files:** `stt_server/backends/nemotron.py`
**Test files:** `tests/test_nemotron_backend.py`
**Test command:** `uv run python -m pytest tests/test_nemotron_backend.py -q`
**Validation:** backend satisfies protocols; full parakeet-parity contract green
with `mlx_audio` fully stubbed; language-forwarding assertion green; private
temp-dir permissions, temp WAV unlinking, and bounded close-drain tests green.

Mirror `stt_server/backends/parakeet.py` structurally; swap the runtime
(`from mlx_audio.stt import load` in `start()` / `_get_model()` +
Phase-0-verified `model.generate(path, language=…)` or omit-language variant),
the `backend_name`, the default-model constant, the temp-dir prefix, and the
language-forwarding behavior. Comments describe Nemotron's own rationale, not
parakeet's.

### Phase 2: CLI wiring + choice-enumeration tests

**Impl files:** `stt_server/__main__.py`, `scripts/render_stt_plist.py`
(**`_BACKEND_RE` — mandatory**), `scripts/install_stt_agent.sh`
(**`DEFAULT_MODEL` nemotron arm — mandatory**)
**Test files:** `tests/test_stt_server.py`, `tests/test_render_stt_plist.py`,
`tests/test_install_migration.py` (+ any enumerating backend choices)
**Test command:** `uv run python -m pytest tests/test_stt_server.py tests/test_render_stt_plist.py tests/test_install_migration.py -q`
**New tests (mandatory, land in the same commit as the choice-tuple change):**
`test_argparse_backend_choices_include_nemotron`, `test_resolve_model` returns
`DEFAULT_NEMOTRON_MODEL` when `--model` unset, and
`test_make_backend_and_resolve_model_nemotron_do_not_import_mlx_audio`
(subprocess, blocks `mlx_audio`, calls both `_make_backend("nemotron", ...)`
and `_resolve_model("nemotron", None)`) — mirror `test_stt_server.py:921,929,973`.
Add the `BACKEND=nemotron` renderer allowlist test and the real-installer
`PIPECAT_STT_BACKEND=nemotron` default-model regression test in this same phase.
**Validation:** `--backend nemotron` constructs the backend; `_resolve_model`
returns `DEFAULT_NEMOTRON_MODEL` when `--model` unset; lazy-import (lean base)
preserved and **proven** by the no-import subprocess test; `_BACKEND_RE` accepts
`nemotron`; `install_stt_agent.sh` installs `DEFAULT_NEMOTRON_MODEL` for
`BACKEND=nemotron` (not Whisper); `echo`/`mlx`/`parakeet` unaffected.

### Phase 3: Packaging + docs

**Impl files:** `pyproject.toml`, `README.md`, `CHANGELOG.md`
**Test files:** packaging/docs grep checks only; backend/CLI/installer tests
already landed in Phases 1-2
**Test command:** `uv run python -m pytest -q`
**Validation:** version `0.3.0`; Phase-0 dependency group remains locked in
`uv.lock`; **`uv build && unzip -p dist/*.whl '*/METADATA' | grep -i mlx-audio`
returns empty** (no direct-URL leak into `Requires-Dist`); README/CHANGELOG/socket
table updates land with grep-verified enumeration completeness; CHANGELOG and PR
description state Option 1, why no published `nemotron` extra exists yet, and
the PyPI-clean verification result; full suite + `ruff format`/`ruff check`
clean.

## Out of scope

- Activating cache-aware **streaming** / partial `delta`s (separate plan).
- Changing the V1 commit-oriented wire protocol or VAD/turn handling.
- Removing or renaming the `KODA_STT_*` env-var aliases.
- Cutting the upstream `mlx-audio` PyPI release (external dependency).

## Known follow-ups (tracked, not done here)

- **Extract the shared Metal-decode machinery.** With `nemotron`, the
  in-flight-counter + 3.0 s `close()` drain + condition block becomes the **third**
  near-identical copy (`parakeet.py:210-270`, `mlx_whisper.py:249-294`), and the
  `0o700` temp-dir + WAV-write + `os.unlink` block the **second**. Extraction now
  would be premature (the plan deliberately mirrors parakeet for review-diffability,
  and only `_thread_util` is shared today), but once the three copies land and are
  confirmed byte-identical, a single `_MetalDecodeBackend`-style consolidation pass
  is the right cleanup — a tracked decision, not silent 3× copy-paste drift.
- **Promote the `nemotron` dev group to a published extra** once `mlx-audio`
  releases a version containing PR #774 (clean version pin, see Dependency reality).
<!-- reviewed: 2026-06-05 @ e0593b7cb82365782de90dc37f2c3bc3041eedd3 -->

## Progress

- [x] Phase 0: Dependency pin + real mlx-audio API verification
- [x] Phase 1: NemotronBackend + tests (stubbed mlx_audio)
- [x] Phase 2: CLI wiring + choice-enumeration tests
- [x] Phase 3: Packaging + docs

## Findings

### Phase 0 — integration probe (settled 2026-06-05, real post-#774 package)

External facts re-verified:

- **PR #774 MERGED**, merge commit `14add666b5313cadff94a231ee11979f6ac1adf7`
  (merged 2026-06-05T17:11Z). Pinned in the `[dependency-groups]` `nemotron`
  group.
- **PyPI `mlx-audio` latest is 0.4.3** (uploaded 2026-04-28) — predates #774, so
  no released version carries Nemotron STT. Git-SHA pin (Option 1) is required;
  no PyPI release to switch to.

API verified against the **installed** package source (authoritative) and a real
load + synthetic decode on this machine:

- Entry point `from mlx_audio.stt import load` works; `load(model_id)` returns
  `mlx_audio.stt.models.nemotron_asr.nemotron_asr.Model`.
- `Model.generate(audio, *, language=None, att_context_size=None, dtype=float32,
  verbose=False, **kwargs) -> AlignedResult`. The kwarg is **`language`** (NOT
  `target_lang`). `audio` accepts a **file path** (`str`/`Path`); `load_audio`
  runs internally — same shape as parakeet, unlike `mlx_whisper`'s array input.
- Return is `AlignedResult` (the *same* dataclass `parakeet-mlx` returns, from
  `mlx_audio.stt.models.parakeet.alignment`); `.text` is present. The defensive
  `getattr(result, "text", "") or ""` read carries over unchanged.
- **Language vocabulary**: `prompt_dictionary` has 121 keys including
  `"auto"` (→101), `"en"`/`"en-US"` (→0), `"es-ES"` (→2), …
  `default_language = "auto"`. `_resolve_prompt_index` falls back to
  `default_language` then index 0 for unknown values — a wrong/unsupported
  language string degrades gracefully, never raises.
- **No `strip_lang_tags` flag** exists on `generate`/`decode` — that integration
  concern is closed (nothing to wire).
- **`DEFAULT_NEMOTRON_LANGUAGE = "auto"`** — decided. `"auto"` is a verified
  accepted prompt key AND the model's own `default_language`; it is the headline
  40-locale LID mode advertised for this multilingual local server. The backend
  forwards a client-supplied `language` and falls back to this constant when the
  client sends `None`. One-line-swappable to `"en-US"` (also verified accepted)
  if LID proves unreliable in practice.
- **End-to-end smoke**: `load(...).generate(<temp WAV>, language="auto")`
  returned `AlignedResult` with `.text == ""` for a 1 s 16 kHz sine tone —
  exercising the empty-decode (`completed`-only) path. Full load + decode runs
  on this machine; no partial-completion caveat needed.