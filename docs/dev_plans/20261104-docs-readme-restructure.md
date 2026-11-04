# Task: Shorten the README and make focused guides canonical

**Status**: Complete
**Component**: Documentation
**Assigned to**: Codex
**Priority**: Medium
**Branch**: docs/shorter-readme
**Created**: 2026-11-04
**Completed**: 2026-11-04
**Review Gates**: none

## Objective

Turn the root README into a concise project landing page and move detailed
backend, model, prefetch, supervision, and contributor reference material into
focused guides under `docs/`.

## Context

The root README is 446 lines and 3,691 words. It duplicates material in the
827-line operations guide and remains the canonical target for several deep
links from architecture and operations. Readers must scan implementation,
historical verification, and deployment detail before reaching the
documentation index.

Current guide ownership is otherwise clear: operations owns deployed-server
management, integration owns Python/Pipecat clients, architecture owns the
server lifecycle and supervision, protocol owns wire events, and migration
owns version transitions. The missing homes are backend/model reference and
contributor guidance.

## Requirements

- Keep `README.md` as a useful standalone landing page with install, one UDS
  quick start, one TCP quick start, a compact backend table, representative
  agent commands, and task-oriented documentation links.
- Keep the README at or below 180 lines and 1,200 words without dropping the
  endpoint, optional-extra, or local-only security caveats needed for a safe
  first run.
- Add `docs/backends.md` as the canonical home for backend selection, default
  and custom models, pinned revisions, prefetch/cache semantics, and Whisper
  tuning.
- Add `docs/contributing.md` as the canonical home for the add-a-backend
  contract.
- Keep the tested per-backend LaunchAgent port table in `docs/operations.md` as
  the canonical documentation mirror of the executable map in
  `scripts/install_stt_agent.sh`; do not create a second documentation table.
- Expand `docs/architecture.md` so supervised-process output and stable error
  codes no longer depend on README anchors.
- Remove dated smoke-run/commit chronology from evergreen guidance while
  preserving current limitations and links to durable plan evidence where
  useful.
- Make detailed guides link to one another for canonical reference material;
  a guide may still link to the README as a package breadcrumb.
- Preserve migration history and unrelated documentation decisions.

## Review Focus

- Confirm commands, backend defaults, extras, ports, security caveats, and
  error codes remain unchanged during relocation.
- Confirm there is one canonical owner for each extracted topic and no
  detailed guide points to a README section for reference semantics.
- Confirm existing relative Markdown links and anchors resolve after headings
  move.

## Implementation Checklist

### Phase 1: Establish canonical focused guides

**Impl files:** `docs/backends.md, docs/contributing.md, docs/operations.md, docs/architecture.md, docs/integration.md`
**Test files:** `tests/test_docs_navigation.py`
**Test command:** `uv run python -m pytest tests/test_docs_navigation.py tests/test_docs_port_caveats.py tests/test_justfile_recipes.py -q`
**Goal:** Each detailed subject has one canonical guide; relocation preserves commands and behavioural contracts while removing dated run chronology.

- Create the backend/model and contributor guides from current canonical text.
- Move supervised-process and error-code reference material into architecture.
- Replace duplicated operations sections with contextual links and concise
  current limitations.
- Update cross-guide links so details do not depend on README anchors.
- Add focused regression checks for canonical topic ownership, preserved
  behavioural tokens, and relative links/anchors.

### Phase 2: Rewrite the landing page and index it

**Impl files:** `README.md, docs/dev_plans/README.md, docs/dev_plans/20261104-docs-readme-restructure.md`
**Test files:** `tests/test_docs_navigation.py`
**Test command:** `uv run python -m pytest tests/test_docs_navigation.py -q`
**Goal:** The README remains enough for a safe first run, then routes each deeper task to its canonical guide in 180 lines and 1,200 words or fewer.

- Rewrite the README around overview, quick start, compact backend and agent
  summaries, integration entry points, and a task-oriented docs table.
- Add this plan to the dev-plan index.
- Verify size, internal links, forbidden README-anchor dependencies, and diff
  whitespace before completion.
- Smoke-test the README's echo-backend command shapes over temporary UDS and
  loopback TCP endpoints, probe each endpoint with `status`, then stop it.

## Technical Specifications

### Files to Modify

- `README.md` — concise landing page and documentation router.
- `docs/architecture.md` — supervised process contract and stable error codes.
- `docs/integration.md` — backend-selection link to the canonical guide.
- `docs/operations.md` — canonical operator recipes, reduced backend/tuning
  duplication, and evergreen smoke-test coverage language.
- `docs/dev_plans/README.md` — plan index row.
- `docs/dev_plans/20261104-docs-readme-restructure.md` — progress and results.

### New Files to Create

- `docs/backends.md` — backend/model/prefetch/tuning reference.
- `docs/contributing.md` — backend contribution checklist.
- `tests/test_docs_navigation.py` — relative-link, anchor, README-size,
  ownership, and preserved-token regression checks.

### Architecture Decisions

- The README optimizes for first success and navigation, not exhaustive
  reference coverage.
- Existing subject guides remain canonical where ownership already exists;
  new guides are added only for subjects without a coherent home.
- `scripts/install_stt_agent.sh` remains the executable source of truth for
  backend ports. `docs/operations.md` retains the canonical documentation
  table as a checked mirror; existing tests compare it with the installer and
  justfile, and `docs/backends.md` links to it.
- Historical validation chronology stays in dev plans/changelog history, not
  in evergreen user instructions. Current unsupported or unverified coverage
  remains stated without commit-by-commit narration.
- Explore facts are part of this immutable contract. Correcting a verified
  path, observed pattern, or dependency fact above the review marker requires
  another plan review.

### Content Ownership Inventory

| Topic | Current source | Canonical destination | Allowed summary |
|---|---|---|---|
| First-run endpoints and safety | `README.md:52-112` | README quick start; operational details in `docs/operations.md` | README keeps one UDS and one TCP command plus local-only caveats. |
| LaunchAgent and `just` operation | `README.md:113-208`, `docs/operations.md:5-369` | `docs/operations.md` | README keeps three representative recipes. |
| Backend and model selection | `README.md:209-295` | `docs/backends.md` | README keeps one compact defaults table. |
| Prefetch and cache event semantics | `README.md:296-345`, `docs/operations.md:404-432` | `docs/backends.md` | README keeps one prefetch command; operations links to the guide. |
| Supervised process and stable errors | `README.md:347-397`, `docs/architecture.md:76-128` | `docs/architecture.md` | README keeps one sentence and link. |
| Adding a backend | `README.md:398-435` | `docs/contributing.md` | README and architecture link only. |
| Whisper tuning | `docs/operations.md:775-827` | `docs/backends.md` | Operations links only. |
| Dated smoke-run chronology | `README.md:149-162`, `docs/operations.md:615-640` | Existing dev-plan history; current limitations remain in operations | No commit-by-commit evergreen summary. |

### Dependencies

- No new runtime or documentation dependencies.
- Current relevant manifest facts: Python `>=3.12`, `websockets>=16,<17`,
  `mlx-whisper>=0.4.0`, `parakeet-mlx>=0.5.1`, `mlx-audio>=0.4.4`, and
  `moshi_mlx==0.3.0` in `pyproject.toml`.

### Integration Seams

| Seam | Writer | Caller | Contract |
|---|---|---|---|
| Backend defaults and prefetch semantics | `docs/backends.md` | README, operations, integration | Other docs summarize and link; they do not duplicate full tables or event semantics. |
| LaunchAgent port map | `scripts/install_stt_agent.sh` | `docs/operations.md`, justfile, tests | Installer is executable truth; operations is the canonical documentation mirror checked against it. |
| Supervised process contract | `docs/architecture.md` | README, operations | Ready/error/stdin semantics and stable codes have one canonical reference. |
| Contributor checklist | `docs/contributing.md` | README, architecture | Backend interface, registry, packaging, tests, and docs steps remain complete. |

## Testing Notes

### Test Approach

- [x] Run the full Python test suite.
- [x] Run `uv run python -m pytest tests/test_docs_navigation.py -q` to check
  all relative Markdown links, local heading anchors, canonical topic
  ownership, preserved behavioural tokens, and README size limits.
- [x] Run `rg -n 'README\.md#|\.\./README\.md#' docs/*.md`; expect no output.
- [x] Run `wc -l -w README.md`; expect at most 180 lines and 1,200 words.
- [x] Start the echo backend once with the README UDS command shape and once
  with its TCP command shape, probe each with `python -m stt_server status`,
  and stop both cleanly.
- [x] Run `git diff --check`.

### Test Results

- [x] Full test suite passes: 2,537 passed, 23 skipped.
- [x] Documentation link validation passes.
- [x] README size target passes: 149 lines, 710 words.
- [x] Echo-backend UDS and TCP quick-start smoke checks pass.

### Edge Cases Tested

- [x] Links containing explicit Markdown anchors resolve after headings move.
- [x] The operations port table remains byte-shape compatible with its parser.
- [x] Legacy aliases and backend-specific limitations remain documented.

## Acceptance Criteria

- `README.md` is no more than 180 lines and 1,200 words.
- A new user can install and start a safe local server using only the README.
- Backend/model/prefetch/tuning reference lives in `docs/backends.md`.
- Backend contribution guidance lives in `docs/contributing.md`.
- Supervised-process error semantics live in `docs/architecture.md`.
- Existing operations, integration, protocol, and migration guidance remains
  reachable through task-oriented README links.
- No detailed guide depends on a README heading for canonical semantics.
- Relative links, anchors, full tests, and `git diff --check` pass.

<!-- reviewed: 2026-11-04 @ 6f9d5f318263957863555f4a0dc3686b556ea7bd -->

## Progress

- [x] Phase 1: Establish canonical focused guides
- [x] Phase 2: Rewrite the landing page and index it

## Findings

- Explore confirmed that `docs/operations.md` already duplicates prefetch
  progress/pinning semantics and Whisper tuning, while `docs/architecture.md`
  points to README anchors for error codes and backend contribution.
- No git refs were named by the user; no ref verification was required.
- Focused documentation and operator checks passed 271 tests. The complete
  socket-enabled suite passed 2,537 tests with 23 expected skips.
- Both README echo quick starts returned `protocol_version: 0.1` and
  `backend: echo` through `status`, over UDS and loopback TCP respectively.

## Issues & Solutions

- The sandboxed full suite denied two loopback binds with `EPERM`. A rerun with
  local socket permissions passed the complete suite; no product change was
  required.

## Final Results

### Summary

Replaced the long README with a 149-line landing page and made focused guides
canonical for backend/model reference, contributor mechanics, supervision, and
operations.

### Outcomes

- Reduced the README from 446 lines and 3,691 words to 149 lines and 710 words.
- Added regression coverage for relative links, heading anchors, canonical
  topic ownership, size limits, and relocated behavioural tokens.
- Removed dated smoke-run chronology from evergreen guidance while preserving
  the current live-coverage boundary and durable plan link.

### Learnings

- The installer owns executable backend-port truth; the operations table is its
  canonical documentation mirror and remains protected by existing tests.
- Detailed guides should link to canonical peers rather than use README anchors
  as reference storage.

### Follow-up Work

- None required for this documentation split.
