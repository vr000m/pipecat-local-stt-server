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

- the LaunchAgent label `koda.stt-server`,
- the default UDS socket `~/Library/Caches/koda-stt/stt.sock` (+ `parakeet.sock`),
- the default log dir `~/Library/Logs/koda-stt/` and the `koda-stt.log`/`.err` basenames.

These are *live, correct defaults*, not dead links — so renaming is a genuine breaking change for anyone who already ran `install_stt_agent.sh`: an upgrade must not leave the old `koda.stt-server` launchd agent running alongside the new one, and consumers pinned to the old socket path must be told. This plan does the rename and bakes the migration into the install path.

Distinct from this change (explicitly **not** touched): the `KODA_STT_*` env-var *names* (deprecated aliases, still read in the shell resolver chains), the `koda-pipecat` provenance line in `README.md:8`, and the `koda-pipecat` sibling-repo source path in `tests/test_wire_schema_compat.py:54` (load-bearing — it `git archive`s the `stt-extraction-base` tag from that repo; overridable via `KODA_REPO_PATH`).

## Requirements

- Rename the default label `koda.stt-server` → `pipecat.stt-server` everywhere it is a **default value** (not where `KODA_STT_LABEL` is an env-var *name*).
- Rename default socket `~/Library/Caches/koda-stt/` → `~/Library/Caches/pipecat-stt/` and log dir `~/Library/Logs/koda-stt/` → `~/Library/Logs/pipecat-stt/`, basenames `koda-stt.{log,err}` → `pipecat-stt.{log,err}`.
- Keep the two `_log_basename` copies (`render_stt_plist.py`, `install_stt_agent.sh`) in lockstep, and **preserve** the legacy `koda.stt-server → koda-stt` mapping so a user who explicitly passes the old label still gets stable paths.
- `install_stt_agent.sh install` MUST migrate: `launchctl bootout gui/$(id -u)/koda.stt-server` (if present) and remove the legacy `koda.stt-server.plist` before bootstrapping the renamed agent — no double-running agents after upgrade.
- `KODA_STT_*` env-var NAMES remain honoured aliases (untouched). Setting `KODA_STT_LABEL`/`KODA_STT_SOCKET`/`KODA_STT_LOG_DIR` must still work.
- Regenerate the byte-exact plist snapshot and update its paired inputs/assertions; pin a backward-compat test for the legacy label mapping.
- `pyproject.toml` → `0.2.0`; `CHANGELOG [0.2.0]` documents the breaking change; README carries an explicit **Upgrade** note.
- Full suite green (`ruff format`, `ruff check`, `pytest`) before merge.
- Do NOT change `README.md:8` provenance or `tests/test_wire_schema_compat.py:54` source path.

## Review Focus

- **Backward compatibility of the label resolver**: a user who explicitly sets `KODA_STT_LABEL=koda.stt-server` (or any custom label) must still render a valid plist; the legacy `koda.stt-server → koda-stt` basename mapping must survive in *both* `render_stt_plist.py:48-59` and `install_stt_agent.sh:72-75`.
- **Migration correctness / idempotency**: the `install` bootout-of-legacy step must be idempotent and must not fail when no legacy agent exists (`|| true`), and must not bootout the *new* agent it is about to install.
- **`_log_basename` lockstep**: the Python and shell copies are a known duplication seam (pinned by `test_log_basename_mapping_is_pinned`); verify both are updated identically and the parametrize covers new-default + legacy.
- **Snapshot drift**: the byte-for-byte snapshot test (`test_render_stt_plist.py:88-98`) and `SNAPSHOT_ENV` inputs must be regenerated together; confirm the committed snapshot is produced by the renderer, not hand-edited.
- **Scope discipline**: confirm no `KODA_STT_*` env-var *name* alias was removed, and the `koda-pipecat` provenance/source-path references are untouched.
- **`mlx_teardown_spike.sh`**: it hardcodes the label (line 41) and concatenates `koda-stt.err` literally 4× (no `LOG_BASENAME` var) — easy to miss; verify it matches the new default.

## Implementation Checklist

### Phase 1: Rename default values in the renderer and scripts

**Impl files:** `scripts/render_stt_plist.py, scripts/install_stt_agent.sh, scripts/mlx_teardown_spike.sh, scripts/benchmark_asr_ab.py`
**Test files:** `tests/test_render_stt_plist.py`
**Test command:** `uv run python -m pytest tests/test_render_stt_plist.py -q`

- `render_stt_plist.py`: `DEFAULT_LABEL = "pipecat.stt-server"` (line 40); update the module docstring (line 1).
- `render_stt_plist.py` `_log_basename()` (lines 48-59): map the new default `pipecat.stt-server → "pipecat-stt"`; **retain** `koda.stt-server → "koda-stt"` as an explicit legacy branch; default `label.replace(".", "-")` for anything else.
- `install_stt_agent.sh`: LABEL default (line 48) `…:-koda.stt-server` → `…:-pipecat.stt-server`; LOG_DIR default (line 52) `…/Logs/koda-stt` → `…/Logs/pipecat-stt`; SOCKET_PATH default (line 56) `…/Caches/koda-stt/stt.sock` → `…/Caches/pipecat-stt/stt.sock`; the shell `_log_basename` (lines 72-75) gains the new-default + retains legacy branch; usage comments (lines 2,12,14,30,31,40,43,70).
- `mlx_teardown_spike.sh`: LABEL literal (line 41); socket/log defaults (lines 42-43); the four `"$LOG_DIR/koda-stt.err"` literals (lines 89,90,100,105) → `pipecat-stt.err`.
- `benchmark_asr_ab.py`: argparse defaults (lines 490,495) and docstring examples (lines 25-26).

### Phase 2: Install-time migration of the legacy agent

**Impl files:** `scripts/install_stt_agent.sh`
**Test files:** `tests/test_render_stt_plist.py` (or a new `tests/test_install_migration.py` for the bootout sequencing, if shellcheck-style coverage is added)
**Test command:** `uv run python -m pytest tests/test_render_stt_plist.py -q`
**Validation cmd:** `bash -n scripts/install_stt_agent.sh && shellcheck scripts/install_stt_agent.sh`

- In the `install` subcommand (around lines 112-116), before bootstrapping the new agent: if `$LABEL != koda.stt-server`, run `launchctl bootout "gui/$(id -u)/koda.stt-server" 2>/dev/null || true` and `rm -f "$HOME/Library/LaunchAgents/koda.stt-server.plist"` to retire the legacy agent.
- Emit a one-line notice when a legacy agent/plist was found and retired.
- Document (comment + README) that the old socket (`~/Library/Caches/koda-stt/`) and logs are left in place (harmless; new agent uses new paths) and that consumers pinned to the old socket path must set `STT_WS_SOCKET` or update.
- Keep the step idempotent and guarded so a fresh machine (no legacy agent) is unaffected.

### Phase 3: Regenerate the plist snapshot and update tests

**Impl files:** `tests/snapshots/pipecat-stt.plist` (renamed from `koda-stt.plist`), `tests/test_render_stt_plist.py`
**Test files:** `tests/test_render_stt_plist.py`
**Test command:** `uv run python -m pytest tests/test_render_stt_plist.py -q`

- Rename `tests/snapshots/koda-stt.plist` → `tests/snapshots/pipecat-stt.plist` and regenerate it from the renderer (do not hand-edit) with the updated `SNAPSHOT_ENV`.
- `test_render_stt_plist.py`: update `SNAPSHOT` path (line 34); `SNAPSHOT_ENV` socket/log inputs (lines 42,46) to the `pipecat-stt` paths (REPO_ROOT at line 41 is arbitrary fixture cwd — update to a neutral `/Users/test/pipecat-local-stt-server` for cleanliness); label assertion (line 108) → `pipecat.stt-server`; log-suffix assertions (lines 109-110) → `/pipecat-stt.log` / `.err`.
- `test_log_basename_mapping_is_pinned` parametrize (lines 167-188): default `(None → "pipecat-stt")`; **add** a legacy case `("koda.stt-server" → "koda-stt")` to pin backward compat; keep the multi-instance case (`pipecat.stt-server.parakeet → pipecat-stt-server-parakeet`).

### Phase 4: Docs, version bump, and upgrade note

**Impl files:** `README.md, CHANGELOG.md, pyproject.toml`
**Test files:** (docs-only)
**Test command:** `uv run python -m pytest -q`

- README: update default label/socket/log in examples and tables (lines 37,41,73-74,101-102,153,215,411).
- README: add an **Upgrading from 0.1.x to 0.2.0** subsection — the rename, that re-running `install_stt_agent.sh install` retires the legacy agent automatically, and the `STT_WS_SOCKET` note for pinned consumers.
- `CHANGELOG.md`: `## [0.2.0]` with a **Changed (BREAKING)** entry for the default rename + migration, and a note that `KODA_STT_*` names remain honoured.
- `pyproject.toml`: `version = "0.2.0"`.
- Do not touch `README.md:8` provenance.

## Technical Specifications

### Files to Modify
- `scripts/render_stt_plist.py` — `DEFAULT_LABEL` (l.40), `_log_basename` legacy+new mapping (l.48-59), docstring (l.1).
- `scripts/install_stt_agent.sh` — LABEL/LOG_DIR/SOCKET_PATH defaults (l.48,52,56), shell `_log_basename` (l.72-75), `install` migration (l.112-116), usage comments.
- `scripts/mlx_teardown_spike.sh` — hardcoded LABEL (l.41), socket/log defaults (l.42-43), four `koda-stt.err` literals (l.89,90,100,105).
- `scripts/benchmark_asr_ab.py` — argparse socket defaults (l.490,495), docstring (l.25-26).
- `tests/test_render_stt_plist.py` — SNAPSHOT path, SNAPSHOT_ENV inputs, label/log assertions, `_log_basename` parametrize.
- `README.md`, `CHANGELOG.md`, `pyproject.toml` — docs, changelog, version.

### New / Renamed Files
- `tests/snapshots/koda-stt.plist` → `tests/snapshots/pipecat-stt.plist` (regenerated bytes).

### Architecture Decisions
- **Preserve the legacy basename mapping.** `_log_basename` keeps an explicit `koda.stt-server → koda-stt` branch alongside the new `pipecat.stt-server → pipecat-stt`. Rationale: a user who still passes the old label (via `KODA_STT_LABEL` or `--label`) gets stable, collision-free paths; only the *default* moves.
- **Migrate by retiring, not relocating.** On `install`, bootout + remove the legacy launchd agent/plist; leave the old socket/log files in place (orphaned but harmless — the new agent uses new paths). Rationale: launchd double-running is the real hazard; copying/symlinking data dirs adds risk for no benefit. Consumers pinned to the old socket are handled via `STT_WS_SOCKET` + the README upgrade note.
- **Names vs values stay decoupled.** `KODA_STT_*` env-var *names* are untouched; only default *values* change. The package never reads `*_STT_SOCKET`/`*_STT_LABEL` names (it receives `--socket-path`), so the blast radius is `scripts/` + `tests/` + docs only.
- **`mlx_teardown_spike.sh` updated for consistency** even though it hardcodes the label and is a diagnostic spike, so a future reader does not resurrect `koda-stt`.

### Dependencies
- No new dependencies. `pyproject.toml`: `websockets>=13.0`; dev `pytest>=8.0.0`, `pytest-asyncio>=0.24.0`, `ruff>=0.8.0`; build backend `hatchling`. pytest config: `asyncio_mode="auto"`, `testpaths=["tests"]`, `pythonpath=["."]`.

### Integration Seams

| Seam | Writer | Caller | Contract |
|------|--------|--------|----------|
| Resolved label → renderer | `install_stt_agent.sh` (l.97-105, injects `PIPECAT_STT_LABEL="$LABEL"`) | `render_stt_plist.py` (l.79, `env_first(...) or DEFAULT_LABEL`) | Shell pre-resolves the label and passes it under the canonical key; renderer's `DEFAULT_LABEL` only applies on direct invocation. Both defaults must be `pipecat.stt-server`. |
| `_log_basename` duplication | `render_stt_plist.py:48-59` | `install_stt_agent.sh:72-75` | Identical mapping in two languages; pinned by `test_log_basename_mapping_is_pinned`. Must update both with new-default + legacy branches. |
| Default values → snapshot | renderer defaults | `tests/snapshots/pipecat-stt.plist` + `SNAPSHOT_ENV` | Snapshot is the byte-exact render of the default env; regenerate from the renderer, never hand-edit. |
| Legacy agent → migration | prior install (`koda.stt-server`) | `install_stt_agent.sh install` | New install must bootout + rm the legacy agent/plist idempotently before bootstrapping the renamed one. |

## Testing Notes

### Test Approach
- [ ] `test_render_stt_plist.py` byte-for-byte snapshot regenerated and passing against the renamed default.
- [ ] `_log_basename` parametrize covers new default + legacy `koda.stt-server` + multi-instance.
- [ ] Backward-compat: explicit `KODA_STT_LABEL=koda.stt-server` still renders a valid plist with `koda-stt` basenames.
- [ ] `install_stt_agent.sh` passes `bash -n` and `shellcheck`; migration branch is idempotent (no legacy agent → no-op).
- [ ] Full suite (`uv run python -m pytest -q`) green; `ruff format` + `ruff check` clean.

### Edge Cases Tested
- [ ] Fresh machine with no legacy agent (migration is a no-op).
- [ ] Custom label still collision-free (basename = `label.replace(".", "-")`).
- [ ] `mlx_teardown_spike.sh` log path matches the renamed default.

## Acceptance Criteria

- Default label/socket/log are `pipecat`-namespaced everywhere they are *values*; no `koda` default value remains in `scripts/` or `tests/` except the deliberately-retained legacy `_log_basename` branch and its pinned test.
- Re-running `install_stt_agent.sh install` on a v0.1.x machine retires the legacy `koda.stt-server` agent and runs only the renamed agent.
- `KODA_STT_*` env-var names still resolve; `koda-pipecat` provenance (`README.md:8`) and wire-compat source path (`test_wire_schema_compat.py:54`) unchanged.
- Snapshot regenerated from the renderer; all tests pass; ruff clean.
- `0.2.0` in `pyproject.toml`, `CHANGELOG [0.2.0]` with the breaking-change + migration note, README upgrade section present.
- `/review-plan` and `/deep-review` findings addressed before merge.

## Final Results

[Fill when complete]

<!-- reviewed: YYYY-MM-DD @ <hash> -->
<!-- /review-plan writes the marker line above. Everything below is the workspace: edits here do NOT invalidate the marker. -->

## Progress

- [ ] Phase 1: Rename default values in the renderer and scripts
- [ ] Phase 2: Install-time migration of the legacy agent
- [ ] Phase 3: Regenerate the plist snapshot and update tests
- [ ] Phase 4: Docs, version bump, and upgrade note

## Findings

- (append findings here as work proceeds)

## Issues & Solutions

(none yet)
