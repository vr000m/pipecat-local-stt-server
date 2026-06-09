# Dev Plans

Status index for the development plans in this directory. One row per plan;
update the row in the same change that updates the plan's `**Status**` line.

| Plan | Component | Status |
|---|---|---|
| [De-brand the runtime surface (0.2.0)](20260530-refactor-debrand-runtime-surface.md) | Install & Packaging | ✅ Complete (2026-05-30) |
| [Nemotron 3.5 ASR backend (0.3.0)](20260605-nemotron-asr-backend.md) | ASR Backends | ✅ Shipped — PR #7 merged 2026-06-06; packaged as `nemotron` extra in 0.3.2 |
| [justfile operator layer for STT LaunchAgents](20260607-feature-stt-agents-justfile.md) | Install & Packaging | ✅ Complete |
| [STT vocabulary/prompt biasing on the wire protocol](20260607-feature-stt-prompt-biasing.md) | Wire Protocol / ASR Backends | ⬜ Not started — analysis/handoff only |

## Conventions

- Plans are named `YYYYMMDD-<slug>.md` (creation date).
- Each plan carries a `**Status**` line near the top — that line is the source of
  truth; this table summarizes it.
- When adding a new plan, add a row here. When a plan's status changes, update both.

> For a richer, auto-generated view (git-derived timeline, cross-references,
> per-plan drill-downs), run the `skein:plan-view` skill.
