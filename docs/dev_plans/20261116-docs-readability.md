# Task: Make the documentation easier to navigate

**Status**: Complete
**Component**: Documentation
**Branch**: docs/readability-guides
**Created**: 2026-11-16
**Completed**: 2026-11-16

## Objective

Give new users and contributors a short path to their first working command,
and separate everyday operation from detailed security and live-test contracts.

## Review findings and decisions

- The 170-line README fits the existing size budget, but does not identify
  `echo` as a transport test or explain the checkout prerequisite.
- The 744-line operations guide mixes installation, security, and test-harness
  implementation. Move detailed security to `docs/security.md` and live tests
  and benchmarks to `docs/testing.md`; retain forwarding sections for old links.
- The contributor guide assumes every contribution adds a backend. Add setup,
  a source map, and contribution checks; organise the backend checklist by task.
- Preserve backend defaults, protocol behaviour, authentication caveats, and the
  canonical port table in operations. Preserve historical plan evidence.
- Keep the backend guide intact as the model reference; give its long Moonshine
  section subheadings. Clarify examples and navigation in the other guides.

## Scope and validation

Edit `README.md`, the top-level Markdown guides in `docs/`, this plan, and the
plan index. Append a correction to the earlier README restructuring plan only
for the changed ownership of security and testing. No runtime, dependency, or
release changes, commits, or publication are needed for this editing request.

**Scope update (2026-11-16):** The subsequent user requests authorise committing
the documentation and opening a PR with a visual guide to the split. This
supersedes the original commit/publication boundary; runtime scope is unchanged.

Use the existing documentation link/anchor, ownership, size, and port checks.
Review the diff for lost contracts and run the full local test suite. Do not
install models or modify LaunchAgents to validate prose changes.

## Progress

- [x] Read the guides, previous documentation plan, and executable contracts.
- [x] Rewrite and split guides; check navigation and preserved contracts.
- [x] Run checks and record results.

## Results

- README: 166 lines. Operations: 744 → 318 lines; security and testing now
  have focused guides. Contributor setup, source map, and backend checklist
  remain together.
- Existing operations headings are retained; the canonical port table is
  byte-identical to the base. Relative-link/anchor checks pass across all docs.
- Focused documentation and recipe suite: 283 passed. Ruff formatting and
  lint checks pass; Python examples parse, and Markdown fences/tables balance.
- Full local suite with socket permissions: **2,951 passed, 23 skipped**
  (23m 40s). The sandboxed attempt was stopped after socket creation failed
  with `EPERM`; the rerun completed without failures.
- No runtime, script, test, manifest, or lockfile changes.
