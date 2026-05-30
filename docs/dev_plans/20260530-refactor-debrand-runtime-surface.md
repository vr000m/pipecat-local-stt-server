# Task: De-brand the runtime surface (0.2.0)

**Status**: Not Started
**Component**: Install & Packaging
**Assigned to**: Claude
**Priority**: Medium
**Branch**: refactor/debrand-runtime-surface
**Created**: 2026-05-30
**Completed**: (fill when done)

## Objective

Rename the residual `koda` branding in **default runtime values** (LaunchAgent label, default socket path, default log dir/basenames) to a `pipecat`-namespaced default, with a safe upgrade path for existing v0.1.x installs. This is a breaking change shipped as **0.2.0**. The deprecated `KODA_STT_*` env-var *names* are out of scope and stay as honoured aliases.

## Context

The package was extracted from the private `koda-pipecat` monorepo. The 0.1.x line de-branded the configuration *names* (canonical `PIPECAT_STT_*`, deprecated-but-honoured `KODA_STT_*`) and stripped dangling monorepo doc references. What remains is `koda` baked into the **default values** a fresh install actually uses:

- the LaunchAgent label `koda.stt-server` (and the two-agent variant `koda.stt-server.parakeet`),
- the default UDS socket `~/Library/Caches/koda-stt/stt.sock` (+ `parakeet.sock`),
- the default log dir `~/Library/Logs/koda-stt/` and the `koda-stt.log`/`.err` basenames.

These are *live, correct defaults*, not dead links — so renaming is a genuine breaking change for anyone who already ran `install_stt_agent.sh`: an upgrade must not leave the old `koda.stt-server` (or `koda.stt-server.parakeet`) launchd agents running alongside the new one, and consumers pinned to the old socket path must be told. This plan does the rename and bakes the migration into the install path.

Distinct from this change (explicitly **not** touched): the `KODA_STT_*` env-var *names* (deprecated aliases, still read in the shell resolver chains), the `koda-pipecat` provenance line in `README.md:8`, and the `koda-pipecat` sibling-repo source path in `tests/test_wire_schema_compat.py:54` (load-bearing — it `git archive`s the `stt-extraction-base` tag from that repo; overridable via `KODA_REPO_PATH`).

## Requirements

- Rename the default label `koda.stt-server` → `pipecat.stt-server` everywhere it is a **default value** (not where `KODA_STT_LABEL` is an env-var *name*).
- Rename default socket `~/Library/Caches/koda-stt/` → `~/Library/Caches/pipecat-stt/` and log dir `~/Library/Logs/koda-stt/` → `~/Library/Logs/pipecat-stt/`, basenames `koda-stt.{log,err}` → `pipecat-stt.{log,err}`.
- `_log_basename` (Python + shell copies) MUST use **two explicit literal branches** — `pipecat.stt-server → pipecat-stt` (new default) and `koda.stt-server → koda-stt` (retained legacy) — NOT `if label == DEFAULT_LABEL` (which would otherwise map the new default to the old basename). Keep the two copies in lockstep.
- `install_stt_agent.sh install` MUST migrate: `launchctl bootout` **both** legacy labels — `koda.stt-server` AND `koda.stt-server.parakeet` — (if present) and remove their `*.plist` files before bootstrapping the renamed agent. Idempotent; no-op on a fresh machine; never boots out the new agent.
- `KODA_STT_*` env-var NAMES remain honoured aliases (untouched). `KODA_STT_LABEL` / `KODA_STT_SOCKET` / `KODA_STT_LOG_DIR` must still override the new defaults.
- Regenerate the byte-exact plist snapshot and update **all** affected tests/assertions/docstrings (whole-file audit, not an enumerated subset); pin both the `_log_basename` parametrize cases and a **full-plist** explicit-legacy-label render test.
- Update **every** value-context `koda` occurrence in README (the README is already half-migrated — `:252` uses `pipecat-stt`), verified by grep-and-assert-zero, including the cross-repo `STT_WS_DEFAULT_SOCKET` wrapper note at `README.md:392`.
- `pyproject.toml` → `0.2.0`; `CHANGELOG [0.2.0]` documents the breaking change; README carries an explicit **Upgrade** note (incl. `STT_WS_DEFAULT_SOCKET` / `STT_WS_SOCKET` guidance), with mechanical presence checks.
- Full suite green (`ruff format`, `ruff check`, `pytest`) before merge.
- Do NOT change `README.md:8` provenance or `tests/test_wire_schema_compat.py:54` source path.

## Review Focus

- **Atomicity of the rename**: the byte-for-byte snapshot test and the label/log-path assertions go red the instant `DEFAULT_LABEL` flips; the rename, snapshot regen, and ALL test/docstring updates MUST land in one commit (Phase 1) — no green-suite gap between them.
- **`_log_basename` two-literal rewrite + lockstep**: confirm the Python copy stops keying on `DEFAULT_LABEL` and uses explicit `pipecat.stt-server`/`koda.stt-server` literals matching the shell copy; pinned by `test_log_basename_mapping_is_pinned` (new-default + legacy + multi-instance).
- **Migration correctness/idempotency, both agents**: the `install` bootout must retire `koda.stt-server` *and* `koda.stt-server.parakeet`, be idempotent (no legacy → no-op), and never bootout the new agent. This must be **executed in a test** (stubbed `launchctl`), not only `bash -n`/`shellcheck`.
- **Backward-compat coverage**: a full-plist render under explicit `KODA_STT_LABEL=koda.stt-server` must still yield `koda.stt-server` + `/koda-stt.{log,err}`; `KODA_STT_SOCKET`/`KODA_STT_LOG_DIR` must still override the new defaults (shell-level test).
- **Cross-repo consumer contract**: the rename breaks `STT_WS_DEFAULT_SOCKET` (README:392); the upgrade note must address it. Decide document-only vs out-of-scope-to-fix.
- **Scope discipline**: no `KODA_STT_*` env-var *name* alias removed; `koda-pipecat` provenance (README:8) and wire-compat source path (test_wire_schema_compat.py:54) untouched.
- **`mlx_teardown_spike.sh`**: hardcodes the label (l.41) and concatenates `koda-stt.err` literally 4× (no `LOG_BASENAME` var) — no automated test guards it; rename is grep-verified, not unit-tested.

## Implementation Checklist

### Phase 1: Rename defaults + regenerate snapshot + update all tests (ATOMIC — single commit)

**Impl files:** `scripts/render_stt_plist.py, scripts/mlx_teardown_spike.sh, scripts/benchmark_asr_ab.py, tests/snapshots/pipecat-stt.plist, tests/test_render_stt_plist.py`
**Test files:** `tests/test_render_stt_plist.py`
**Test command:** `uv run python -m pytest tests/test_render_stt_plist.py -q`
**Validation cmd:** `! rg -n 'koda-stt|koda\.stt-server' scripts/render_stt_plist.py scripts/mlx_teardown_spike.sh scripts/benchmark_asr_ab.py | rg -v 'koda\.stt-server.*->.*koda-stt|legacy'`

This phase is **one commit** because flipping `DEFAULT_LABEL` immediately reddens the byte-for-byte snapshot test (`test_render_stt_plist.py:88-98`) and the legacy-label/log assertions (`:108-110`, parametrize `:170`); they cannot be green again until the snapshot is regenerated and the assertions rewritten. Do not split.

- `render_stt_plist.py`: `DEFAULT_LABEL = "pipecat.stt-server"` (l.40); update the module docstring (l.1).
- `render_stt_plist.py` `_log_basename()` (l.48-59): rewrite to **two explicit literal branches** — `if label == "pipecat.stt-server": return "pipecat-stt"`; `if label == "koda.stt-server": return "koda-stt"` (retained legacy); else `label.replace(".", "-")`. Do NOT key on `DEFAULT_LABEL`.
- `mlx_teardown_spike.sh`: hardcoded LABEL (l.41) → `pipecat.stt-server`; socket/log defaults (l.42-43); the four `"$LOG_DIR/koda-stt.err"` literals (l.89,90,100,105) → `pipecat-stt.err`.
- `benchmark_asr_ab.py`: argparse socket defaults (l.490,495) and docstring examples (l.25-26).
- Rename `tests/snapshots/koda-stt.plist` → `tests/snapshots/pipecat-stt.plist`, **regenerated from the renderer** (never hand-edited) with the updated `SNAPSHOT_ENV`.
- `test_render_stt_plist.py`: `SNAPSHOT` path (l.34); `SNAPSHOT_ENV` socket/log inputs (l.42,46) → `pipecat-stt`; **REPO_ROOT (l.41) is itself koda-branded (`/Users/test/koda-pipecat`) — change it to `/Users/test/pipecat-local-stt-server` as a required fixture de-brand** (this is why the snapshot bytes change, expected not drift); label assertion (l.108) → `pipecat.stt-server`; log-suffix assertions (l.109-110) → `/pipecat-stt.{log,err}`.
- `test_render_stt_plist.py` whole-file audit: also update `test_default_env_plist_has_legacy_label_and_log_paths` (l.101-110) and every module/function docstring referencing the legacy default (l.1-15, 89, 102-103). Grep the file for residual `koda-stt`/`koda.stt-server` *default* expectations — do not rely on the enumerated lines.
- `test_log_basename_mapping_is_pinned` parametrize (l.167-188): new default `(None → "pipecat-stt")`; **add** legacy `("koda.stt-server" → "koda-stt")`; the multi-instance case **changes** `("pipecat.stt-server.parakeet" → "pipecat-stt-server-parakeet")` (was koda) — this is an edit, not a keep.
- **Add a full-plist explicit-legacy-label test**: `_run_render({"KODA_STT_LABEL": "koda.stt-server"})` (or `PIPECAT_STT_LABEL`) yields `Label == "koda.stt-server"` and `StandardOut/ErrPath` ending `/koda-stt.{log,err}` — the end-to-end analogue of the parametrize unit.

### Phase 2: Install-time migration of legacy agents (with an executable test)

**Impl files:** `scripts/install_stt_agent.sh`
**Test files:** `tests/test_install_migration.py`
**Test command:** `uv run python -m pytest tests/test_install_migration.py -q`
**Validation cmd:** `bash -n scripts/install_stt_agent.sh && shellcheck scripts/install_stt_agent.sh`

- `install_stt_agent.sh`: LABEL default (l.48) → `pipecat.stt-server`; LOG_DIR default (l.52) → `…/Logs/pipecat-stt`; SOCKET_PATH default (l.56) → `…/Caches/pipecat-stt/stt.sock`; shell `_log_basename` (l.72-75) gains the new-default literal + retains the legacy `koda.stt-server → koda-stt` branch; usage comments (l.2,12,14,30,31,40,43,70).
- Migration block, inserted **immediately after `render_plist` and before the existing `bootout "$LABEL"`** (so the new agent is never retired), guarded by `if [[ "$LABEL" == "pipecat.stt-server" ]]`: for each legacy label in `koda.stt-server` and `koda.stt-server.parakeet`, run `launchctl bootout "gui/$(id -u)/<legacy>" 2>/dev/null || true` and `rm -f "$HOME/Library/LaunchAgents/<legacy>.plist"`. Emit a one-line notice when a legacy agent/plist was retired. Do **not** retire legacy agents during arbitrary custom-label installs; this script's established contract is one selected agent per invocation, and only the renamed default install is the upgrade path for retiring old defaults.
- Document (comment + README) that the old socket (`~/Library/Caches/koda-stt/`) and logs are left in place (harmless; new agent uses new paths) and that consumers pinned to the old socket must set `STT_WS_SOCKET`.
- **`tests/test_install_migration.py`** (new): put a stub `launchctl` and `id` on `PATH` (record argv to a temp file), run `install_stt_agent.sh install` against a temp `HOME`, and assert: (1) bootout invoked for `gui/<uid>/koda.stt-server` *and* `gui/<uid>/koda.stt-server.parakeet` when `LABEL` is the new default; (2) exit-0 no-op when no legacy plist exists; (3) the new label is never passed to bootout before bootstrap; (4) a non-default custom label such as `PIPECAT_STT_LABEL=pipecat.stt-server.test` does **not** bootout either legacy label; (5) `KODA_STT_SOCKET`/`KODA_STT_LOG_DIR` set in the env override the new `pipecat-stt` defaults (covers the shell-only alias-resolution requirement). Skip/guard cleanly if `launchctl` stubbing is not feasible on the CI runner, but keep at least the alias-override assertions runnable.

### Phase 3: Docs, version bump, and upgrade note

**Impl files:** `README.md, CHANGELOG.md, pyproject.toml`
**Test files:** (docs-only)
**Test command:** `uv run python -m pytest -q`
**Validation cmd:** `rg -q '^## Upgrading from 0\.1\.x to 0\.2\.0' README.md && rg -q 'STT_WS_SOCKET' README.md && rg -q '^## \[0\.2\.0\]' CHANGELOG.md && ! rg -n 'koda-stt|koda\.stt-server' README.md | rg -v ':8:'`

- README: convert **every** value-context `koda` occurrence to `pipecat` by grep, not by line list — `rg -n 'koda-stt|koda\.stt-server' README.md` and update each (current hits incl. l.37,41,73-74,101-102,153,215,392,411); reconcile the already-migrated l.252 so the two-agent recipe is internally consistent. Leave only `README.md:8` provenance.
- README: add `STT_WS_DEFAULT_SOCKET` note (l.392 region) — the rename does not touch the koda-pipecat wrapper; pinned consumers must re-point `STT_WS_DEFAULT_SOCKET` or set `STT_WS_SOCKET`.
- README: add an **Upgrading from 0.1.x to 0.2.0** subsection — the rename, that re-running `install_stt_agent.sh install` auto-retires the legacy whisper **and** parakeet agents, and the socket guidance.
- `CHANGELOG.md`: `## [0.2.0]` with a **Changed (BREAKING)** entry for the default rename + migration, and a note that `KODA_STT_*` names remain honoured.
- `pyproject.toml`: `version = "0.2.0"`.
- Do not touch `README.md:8` provenance.

## Technical Specifications

### Files to Modify
- `scripts/render_stt_plist.py` — `DEFAULT_LABEL` (l.40), `_log_basename` two-literal rewrite (l.48-59), docstring (l.1).
- `scripts/install_stt_agent.sh` — LABEL/LOG_DIR/SOCKET_PATH defaults (l.48,52,56), shell `_log_basename` (l.72-75), dual-agent `install` migration (after l.110, before l.112), usage comments.
- `scripts/mlx_teardown_spike.sh` — hardcoded LABEL (l.41), socket/log defaults (l.42-43), four `koda-stt.err` literals (l.89,90,100,105).
- `scripts/benchmark_asr_ab.py` — argparse socket defaults (l.490,495), docstring (l.25-26).
- `tests/test_render_stt_plist.py` — SNAPSHOT path, SNAPSHOT_ENV (incl. REPO_ROOT de-brand), label/log assertions, `test_default_env_plist_has_legacy_label_and_log_paths`, docstrings, `_log_basename` parametrize, new full-plist legacy test.
- `README.md`, `CHANGELOG.md`, `pyproject.toml` — docs (grep-swept), changelog, version.

### New / Renamed Files
- `tests/snapshots/koda-stt.plist` → `tests/snapshots/pipecat-stt.plist` (regenerated bytes).
- `tests/test_install_migration.py` (new) — executable migration test with stubbed `launchctl`/`id`.

### Architecture Decisions
- **Preserve the legacy basename mapping as an indefinitely-retained shim.** `_log_basename` keeps an explicit `koda.stt-server → koda-stt` branch (in *both* `render_stt_plist.py` and `install_stt_agent.sh`) alongside the new `pipecat.stt-server → pipecat-stt`. This is a deliberate compat shim — **do not "clean it up"**; it is removable only in the same release that drops the `KODA_STT_*` env-var aliases. Pinned by `test_log_basename_mapping_is_pinned`.
- **`_log_basename` must use literals, not `DEFAULT_LABEL`.** Keying the legacy branch on `DEFAULT_LABEL` would silently map the *new* default to `koda-stt` once the constant moves; the two branches are explicit string literals, matching the shell copy.
- **Migrate by retiring both legacy agents only during the renamed-default install, not relocating data.** On `install` with `LABEL=pipecat.stt-server`, bootout + rm the legacy `koda.stt-server` and `koda.stt-server.parakeet` agents/plists; leave old socket/log files in place (orphaned, harmless). Custom-label installs continue to manage only their selected label and must not retire unrelated legacy agents. Rationale: launchd double-running is the upgrade hazard for the new default; copying/symlinking data dirs adds risk for no benefit.
- **`STT_WS_DEFAULT_SOCKET` is document-only, not fixed here.** The koda-pipecat `./koda stt` wrapper that exports the old socket path lives in another repo; this plan documents the break in the upgrade note (re-point or use `STT_WS_SOCKET`) rather than reaching across repos.
- **Names vs values stay decoupled.** `KODA_STT_*` env-var *names* are untouched; only default *values* change. The package never reads `*_STT_SOCKET`/`*_STT_LABEL` names (it receives `--socket-path`), so the blast radius is `scripts/` + `tests/` + docs only.

### Dependencies
- No new dependencies. `pyproject.toml`: `websockets>=13.0`; dev `pytest>=8.0.0`, `pytest-asyncio>=0.24.0`, `ruff>=0.8.0`; build backend `hatchling`. pytest config: `asyncio_mode="auto"`, `testpaths=["tests"]`, `pythonpath=["."]`.

### Integration Seams

| Seam | Writer | Caller | Contract |
|------|--------|--------|----------|
| Resolved label → renderer | `install_stt_agent.sh` (l.97-105, injects `PIPECAT_STT_LABEL="$LABEL"`) | `render_stt_plist.py` (l.79, `env_first(...) or DEFAULT_LABEL`) | Shell pre-resolves the label and passes it under the canonical key; renderer's `DEFAULT_LABEL` only applies on direct invocation. Both defaults must be `pipecat.stt-server`. |
| `_log_basename` duplication | `render_stt_plist.py:48-59` | `install_stt_agent.sh:72-75` | Identical two-literal mapping in two languages; pinned by `test_log_basename_mapping_is_pinned`. Update both with new-default + legacy literal branches. |
| Default values → snapshot | renderer defaults + `SNAPSHOT_ENV` | `tests/snapshots/pipecat-stt.plist` | Snapshot is the byte-exact render of the default env; regenerate from the renderer, never hand-edit. REPO_ROOT fixture de-brand is part of the expected byte change. |
| Legacy agents → migration | prior install (`koda.stt-server`, `koda.stt-server.parakeet`) | `install_stt_agent.sh install` with `LABEL=pipecat.stt-server` | Renamed-default install must bootout + rm BOTH legacy agents/plists idempotently before bootstrapping the renamed one; never bootout the new label or retire legacy agents for arbitrary custom-label installs. Tested via stubbed `launchctl`. |
| Old socket path → external wrapper | this rename | koda-pipecat `./koda stt` wrapper (`STT_WS_DEFAULT_SOCKET`, README:392) | Cross-repo; NOT fixed here. Upgrade note instructs re-pointing `STT_WS_DEFAULT_SOCKET` or setting `STT_WS_SOCKET`. |

## Testing Notes

### Test Approach
- [ ] Phase 1 byte-for-byte snapshot regenerated and passing; whole-file audit of `test_render_stt_plist.py` for residual koda default expectations.
- [ ] `_log_basename` parametrize covers new default + legacy `koda.stt-server` + multi-instance (parakeet, now pipecat).
- [ ] Full-plist explicit-legacy-label test: `KODA_STT_LABEL=koda.stt-server` → `Label==koda.stt-server`, log paths `/koda-stt.{log,err}`.
- [ ] `tests/test_install_migration.py`: stubbed-`launchctl` assertions for dual-agent bootout during renamed-default install, fresh-machine no-op, new-label-never-booted-out, custom-label install does not retire legacy agents, and `KODA_STT_SOCKET`/`KODA_STT_LOG_DIR` override.
- [ ] `install_stt_agent.sh` passes `bash -n` and `shellcheck`.
- [ ] README/CHANGELOG presence checks (Phase 3 Validation cmd) pass; grep shows zero residual `koda` value-paths in README except `:8`.
- [ ] Full suite (`uv run python -m pytest -q`) green; `ruff format` + `ruff check` clean.

### Edge Cases Tested
- [ ] Fresh machine with no legacy agent (migration is a no-op, exit 0).
- [ ] Two-agent 0.1.x user: both `koda.stt-server` and `koda.stt-server.parakeet` retired.
- [ ] Custom label still collision-free (basename = `label.replace(".", "-")`).
- [ ] `mlx_teardown_spike.sh` log path matches the renamed default — **grep-verified, not unit-tested** (the existing `test_mlx_teardown_spike.py` asserts nothing about the script's literals).

## Acceptance Criteria

- Default label/socket/log are `pipecat`-namespaced everywhere they are *values*; no `koda` default value remains in `scripts/` or `tests/` except the deliberately-retained legacy `_log_basename` literal branches and their pinned tests.
- Re-running default `install_stt_agent.sh install` on a v0.1.x machine retires the legacy `koda.stt-server` **and** `koda.stt-server.parakeet` agents and runs only the renamed agent; custom-label installs do not retire unrelated legacy agents; verified by `tests/test_install_migration.py`.
- `KODA_STT_LABEL`/`KODA_STT_SOCKET`/`KODA_STT_LOG_DIR` still override the new defaults; explicit `koda.stt-server` label renders `koda-stt` basenames (full-plist test).
- `koda-pipecat` provenance (`README.md:8`) and wire-compat source path (`test_wire_schema_compat.py:54`) unchanged; no `KODA_STT_*` env-var *name* removed.
- Snapshot regenerated from the renderer; README grep shows zero residual `koda` value-paths except `:8`; all tests pass; ruff clean.
- `0.2.0` in `pyproject.toml`, `CHANGELOG [0.2.0]` with the breaking-change + migration note, README upgrade section + `STT_WS_DEFAULT_SOCKET` guidance present (grep-verified).
- `/deep-review` findings addressed before merge.

## Final Results

[Fill when complete]

<!-- reviewed: 2026-05-30 @ ce00ffc9fee21fa6c627a8b34d9c371872b4f9a5 -->
<!-- /review-plan writes the marker line above. Everything below is the workspace: edits here do NOT invalidate the marker. -->

## Progress

- [ ] Phase 1: Rename defaults + regenerate snapshot + update all tests (ATOMIC)
- [ ] Phase 2: Install-time migration of legacy agents (with an executable test)
- [ ] Phase 3: Docs, version bump, and upgrade note

## Findings

- 2026-05-30 `/review-plan` (4 lenses, 13 findings: 2 Critical, 6 Important, 5 Minor) incorporated into this revision. Criticals: phases merged into one atomic rename commit (was 1+3 split that left the suite red); install migration now has an executable stubbed-`launchctl` test (was lint-only). Importants: dual-agent migration (parakeet too), `STT_WS_DEFAULT_SOCKET` consumer note, whole-file test audit, `_log_basename` two-literal rewrite, `KODA_STT_SOCKET`/`LOG_DIR` test, full-plist legacy render test. Minors: grep-based README sweep, shim-lifespan decision, REPO_ROOT de-brand reframe, mlx grep guard, doc presence checks. codebase-claims lens verified all references.

## Issues & Solutions

(none yet)
