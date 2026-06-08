# Task: justfile operator layer for managing the STT LaunchAgents

**Status**: Complete
**Component**: Install & Packaging
**Assigned to**: Claude
**Priority**: Medium
**Branch**: feat/stt-agents-justfile
**Created**: 2026-06-07

## Objective

Add a `justfile` at the repo root as a thin, discoverable **operator-convenience
layer** for managing the multiple `pipecat.stt-server*` LaunchAgents (whisper /
parakeet / nemotron) that can run side by side. Provide a cross-agent `stt-list`
(the capability `install_stt_agent.sh` structurally lacks), a per-backend
`stt-status`, and a **stop-that-sticks** pair `stt-disable` / `stt-enable` that
fixes the footgun where the script's `stop` only sends `SIGTERM` and KeepAlive
immediately respawns the agent. `install` / `uninstall` recipes **delegate** to
the existing script rather than reimplementing the security-sensitive plist
rendering.

## Context

`scripts/install_stt_agent.sh` manages **exactly one agent per invocation**,
keyed by `PIPECAT_STT_LABEL` (+ socket). Its header states this explicitly:
"There is no registry or 'all' mode." In practice an operator can end up with
three agents loaded (whisper on `stt.sock`, parakeet on `parakeet.sock`,
nemotron on `nemotron.sock`) and no single command to see them or stop the idle
ones. Two concrete footguns motivated this work:

1. **No cross-agent view.** Discovering what's running means hand-running
   `launchctl list | grep pipecat.stt-server` and cross-referencing sockets.
2. **`stop` does not stick.** `install_stt_agent.sh` `stop` runs
   `launchctl kill SIGTERM` (line 191) and the script itself prints "KeepAlive
   will restart it — use 'uninstall' to disable". The only way to keep an agent
   down today is `uninstall`, which deletes the plist (heavyweight: restore
   needs a full re-`install`). There is **no `launchctl disable` anywhere in the
   script** — a `bootout`-that-keeps-the-plist is genuinely new.

`just` is the natural home for a cross-agent / "operate the listed servers"
surface because that need does not fit the script's one-agent-per-invocation
model. `just` is installed locally (`just 1.51.0`, `/opt/homebrew/bin/just`) but
is **not yet a repo convention** — this introduces it, so the README must
document it.

**Verified launchd facts (from `render_stt_plist.py:133-135`):** the rendered
plist sets `RunAtLoad: True`, `KeepAlive: True`, `ThrottleInterval: 10`. Two
consequences the plan depends on:

- `KeepAlive: True` is why `install_stt_agent.sh stop`'s `SIGTERM` respawns — the
  motivation for a `bootout`-based disable holds.
- `RunAtLoad: True` + the plist staying on disk means **`stt-disable` (bootout)
  is session-scoped**: the agent stays down for the current login session but
  launchd reloads it at next login. This is the genuine difference from
  `uninstall` (which removes the plist, so it never reloads). The README and the
  recipe help must state this — `stt-disable` is "stop until next login", not
  "stop forever". A cross-login suppression would additionally need
  `launchctl disable` (out of scope for this pass; note it as the escalation
  path).

**Koda-safety (per the cross-repo contract):** this work touches neither the
Python CLI, the wire protocol (`PROTOCOL_VERSION`, `server.hello`/
`server.status`), nor `install_stt_agent.sh`'s existing subcommands. It is purely
additive launchd tooling, so **no Koda pin bump** is required. Koda shells into
`install_stt_agent.sh`; keeping `install`/`uninstall` delegated (not
reimplemented) means there is no drift in the plist rendering Koda depends on.

## Requirements

- New `justfile` at repo root. macOS / `launchctl`-only (consistent with
  `install_stt_agent.sh`). Pin the recipe shell explicitly with
  `set shell := ["bash", "-uc"]` so error propagation is deterministic: an
  unknown-backend resolver aborts the recipe, and the best-effort probe is the
  only line allowed to swallow a non-zero exit (via `|| …`).
- **README is the canonical source for the backend→(label, socket) map; the
  justfile map is a checked mirror.** The map covers the three canonical
  backends:
  - `whisper`  → label `pipecat.stt-server`           → socket `~/Library/Caches/pipecat-stt/stt.sock`
  - `parakeet` → label `pipecat.stt-server.parakeet`  → socket `~/Library/Caches/pipecat-stt/parakeet.sock`
  - `nemotron` → label `pipecat.stt-server.nemotron`  → socket `~/Library/Caches/pipecat-stt/nemotron.sock`
  This is a **third** copy of values that already live in the README per-ASR
  table (`README.md:79-81`) and the script's usage comments
  (`install_stt_agent.sh:31-32`) — only whisper's socket is an actual script
  *default* (`:57`); parakeet/nemotron sockets are operator-supplied env,
  documented only in prose. Because "must match exactly" is otherwise
  unenforced, a test parses the README table and asserts the justfile map equals
  it (see Phase 1).
- Recipes:
  - `stt-list` — sweep `~/Library/LaunchAgents/pipecat.stt-server*.plist`
    (**prefix-based, not map-based**, so a custom-labelled agent still surfaces),
    show each agent's loaded/running state + pid by reusing the script's exact
    grep (`state|last exit|pid`, `install_stt_agent.sh:199`). Then, for each of
    the **three canonical sockets only**, append the **live** backend/model via
    `uv run python -m stt_server status --socket-path <sock>`. A custom-labelled
    agent surfaces as loaded/running with **no** live line (its socket is not
    derivable from its label) — this is an intended, documented degradation.
  - `stt-status <backend>` — wire `status` probe for one backend.
  - `stt-disable <backend>` — `launchctl bootout` (stop until next login;
    **keeps** the plist). Idempotent: if not loaded, print a clear message and
    exit 0.
  - `stt-enable <backend>` — `launchctl bootstrap` + `kickstart` from the
    existing plist. If the plist is absent, point the operator at
    `stt-install <backend>`.
  - `stt-install <backend>` / `stt-uninstall <backend>` — DELEGATE to
    `scripts/install_stt_agent.sh` by setting `PIPECAT_STT_LABEL` /
    `PIPECAT_STT_SOCKET` / `PIPECAT_STT_BACKEND` per backend.
- **Probe correctness:** every `status` invocation passes `--socket-path`
  explicitly so it cannot fall back to a stale `STT_WS_*` / `STT_WS_DEFAULT_SOCKET`
  and probe the wrong agent (`__main__.py:171-177`).
- **`status` exit-code handling:** `python -m stt_server status` raises
  `SystemExit(1)` on an unreachable/stopped socket (`__main__.py:288-303`) — it
  does **not** print "stopped"/"unreachable". So `stt-list`'s per-socket probe
  MUST wrap the call to absorb the non-zero exit and render the status itself
  (e.g. `… || echo "stopped/unreachable"`). The 3.0 s default timeout
  (`__main__.py` status subparser) bounds the wait.
- **Locked decision 1:** all `disable`/`enable` launchctl logic lives in the
  justfile. Do NOT add subcommands to `scripts/install_stt_agent.sh`.
- **Locked decision 2:** `install`/`uninstall` recipes delegate to the script;
  do not reimplement plist rendering.
- README must gain a section documenting `just` + the recipes, matching house
  style (`##` / `###`, the existing per-ASR table format), including the
  `stt-disable` (until next login) vs `stt-uninstall` (removes plist)
  distinction.
- An unknown backend name must fail fast (non-zero exit) with a clear error
  whose text lists all three valid backends (`whisper`/`parakeet`/`nemotron`),
  not silently no-op or operate the wrong agent.
- Legacy `koda.stt-server*` agents (still recognised by the script's migration
  arm, `install_stt_agent.sh:142`) are **outside** the `pipecat.stt-server*`
  sweep; the README notes they must be checked manually during migration.

## Implementation Checklist

### Phase 1 — justfile with the map + read-only recipes (`stt-list`, `stt-status`)

**Impl files:** `justfile`
**Test files:** `tests/test_justfile_recipes.py` (new)
**Test command:** `uv run pytest tests/test_justfile_recipes.py -q`

- Add `justfile` at repo root with `set shell := ["bash", "-uc"]`, the
  backend→(label, socket) map, and a `_label` / `_socket` resolver that errors
  (non-zero, message listing all three backends) on an unknown backend.
- `stt-list`: glob `~/Library/LaunchAgents/pipecat.stt-server*.plist`
  (prefix-based), derive label from each basename, report loaded/running + pid
  via `launchctl print gui/$(id -u)/<label>` reusing the script's grep
  (`state|last exit|pid`). Then per **canonical** socket append the live
  backend/model from `uv run python -m stt_server status --socket-path <sock>`,
  wrapping the call so its `SystemExit(1)` on a stopped/absent socket is absorbed
  and rendered as "stopped/unreachable" (the recipe never aborts on it). A
  custom-labelled agent surfaces as loaded/running with no live line.
- `stt-status <backend>`: resolve socket from the map, run the wire `status`
  probe with explicit `--socket-path` (honored verbatim by
  `_resolve_probe_endpoint`).
- Tests (use the repo's existing stub-`launchctl` harness pattern from
  `tests/test_install_migration.py` — stub `launchctl`/`id`/`uv` on `PATH` and a
  temp `HOME`/LaunchAgents dir; this keeps CI launchd-free while still asserting
  real behavior):
  - `just --list` exposes the expected recipe names.
  - Unknown backend exits non-zero and the error names all three valid backends.
  - The resolver maps each canonical backend to the correct label + socket.
  - **Map mirrors README:** parse the `README.md` per-ASR table rows and assert
    the justfile map equals them (drift fails CI — closes the third-source-of-
    truth gap).
  - **Probe passes `--socket-path`:** shim `python -m stt_server status` to echo
    its argv; assert `stt-status <backend>` invokes it with the mapped
    `--socket-path`, and that a bogus `STT_WS_SOCKET` in the env is ignored.
  - **Prefix sweep surfaces custom agents:** drop a
    `pipecat.stt-server.custom.plist` into the stubbed LaunchAgents dir; assert
    `stt-list` enumerates it (state-only, no live line).
  - **`stt-list` tolerates a stopped socket:** stub `status` to exit 1; assert
    the recipe still exits 0 and renders "stopped/unreachable".

### Phase 2 — lifecycle recipes (`stt-disable`, `stt-enable`, delegated install/uninstall)

**Impl files:** `justfile`
**Test files:** `tests/test_justfile_recipes.py`
**Test command:** `uv run pytest tests/test_justfile_recipes.py -q`

- `stt-disable <backend>`: `launchctl bootout gui/$(id -u)/<label>` (keeps the
  plist — stop until next login). Guard: if not loaded, print a clear message and
  exit 0 (idempotent).
- `stt-enable <backend>`: `launchctl bootstrap gui/$(id -u) <plist>` then
  `launchctl kickstart gui/$(id -u)/<label>`. Guard: if the plist is absent,
  point the operator at `stt-install <backend>`.
- `stt-install <backend>` / `stt-uninstall <backend>`: set
  `PIPECAT_STT_LABEL` / `PIPECAT_STT_SOCKET` / `PIPECAT_STT_BACKEND` from the map
  and exec `scripts/install_stt_agent.sh install|uninstall`.
- Tests (same stub-`launchctl` harness):
  - **disable ≠ uninstall (the headline invariant):** pre-create the agent's
    plist; run `just stt-disable <backend>`; assert a `bootout` for the mapped
    label fired AND the plist file still exists (no `rm`).
  - **delegation env is exact:** assert `stt-install`/`stt-uninstall` invoke the
    script with the full `PIPECAT_STT_LABEL` / `PIPECAT_STT_SOCKET` /
    `PIPECAT_STT_BACKEND` triple for at least one non-default backend (parakeet
    or nemotron), pinned to the map values (catches socket-filename drift).
  - **idempotency / missing-plist guards:** stub `launchctl print` to return
    non-zero; assert `stt-disable` on a not-loaded agent exits 0 with a message,
    and `stt-enable` with no plist prints an actionable message pointing at
    `stt-install`.
  - lifecycle recipe names exist; unknown-backend still fails fast.

### Phase 3 — docs (README section) + plan completion

**Impl files:** `README.md`, `docs/dev_plans/20260607-feature-stt-agents-justfile.md`
**Test files:** `tests/test_justfile_recipes.py` (Koda-safety diff guard)
**Test command:** `uv run pytest tests/test_justfile_recipes.py -q`
**Validation cmd:** `just --list`

- Add a README section (after `### Two-agent install` / near `## Checking server
  health`) documenting: that `just` is now used, the recipe list, the
  backend→label→socket table (reuse the existing per-ASR table style), and the
  `stt-disable` (down **until next login** — `RunAtLoad` reloads it) vs
  `stt-uninstall` (removes the plist — stays gone) distinction. Note the
  `launchctl disable` escalation for cross-login suppression.
- Note the `stop`-respawns footgun (`KeepAlive: True`) and that `stt-disable` is
  the session-scoped fix.
- Note that `stt-list` covers `pipecat.stt-server*` agents only; legacy
  `koda.stt-server*` agents must be checked manually during migration.
- **Koda-safety guard (test):** assert `git diff --name-only main...HEAD` is a
  subset of `{justfile, tests/test_justfile_recipes.py, README.md, the plan
  file}` — i.e. the diff touches neither `stt_server/`, the wire protocol, nor
  `scripts/install_stt_agent.sh`, so the no-pin-bump claim is mechanically true.
- Keep all three phases in a single PR; do not partial-merge before Phase 3
  (the `justfile` would otherwise ship undocumented).
- Mark this plan Complete and fill Final Results.

## Technical Specifications

### Files to modify / create

- **`justfile`** (new, repo root) — the entire feature surface. Holds the map and
  all recipes. macOS/launchctl-only.
- **`tests/test_justfile_recipes.py`** (new) — launchd-free assertions on recipe
  presence, unknown-backend handling, and map resolution.
- **`README.md`** — new operator section documenting `just` + recipes.
- **`scripts/install_stt_agent.sh`** — **NOT modified** (locked decision 1).
- **`stt_server/__main__.py`** — **NOT modified** (host-agnostic Python CLI stays
  launchd-free).

### Verified integration facts (from codebase exploration)

- `install_stt_agent.sh` env derivation: `LABEL` default `pipecat.stt-server`
  (`install_stt_agent.sh:49`), `SOCKET_PATH` default
  `~/Library/Caches/pipecat-stt/stt.sock` (`:57`), `BACKEND` default `mlx`
  (`:58`), `PLIST_DST = ~/Library/LaunchAgents/$LABEL.plist` (`:52`). The justfile
  sets these per backend before delegating.
- launchctl idioms already used by the script (mirror them for consistency):
  `bootout` (`:152`, `:161`), `bootstrap` (`:153`), `enable` (`:154`),
  `kickstart -k` (`:155`, `:195`), `kickstart` no-`-k` (`:183`),
  `print` (`:144`, `:179`, `:187`, `:199`). **No `launchctl disable` exists** —
  `stt-disable` uses `bootout` (keeps plist), distinct from `uninstall`'s
  `bootout` + `rm plist`.
- Backend→default-model map in the script (`:67–73`): parakeet →
  `mlx-community/parakeet-tdt-0.6b-v3`; nemotron →
  `mlx-community/nemotron-3.5-asr-streaming-0.6b`; mlx/echo →
  `mlx-community/whisper-large-v3-turbo`. The justfile does **not** duplicate the
  model map — `stt-install` lets the script apply its backend-aware default.
- `python -m stt_server status` endpoint resolution (`__main__.py:_resolve_probe_endpoint`,
  ~`:150–186`): explicit `--socket-path` is honored **verbatim** (priority
  `uri > socket_path > host+port`); only with no CLI flag does it fall back to
  `STT_WS_*` env then `STT_WS_DEFAULT_SOCKET`. So passing `--socket-path <sock>`
  per the map deterministically probes the intended agent. `--socket-path` is
  registered on the `status` subparser (`_add_endpoint_flags(p_status,
  include_uri=True)`, ~`:378`).
- `status` failure shape (`__main__.py:_cmd_status`, ~`:288-303`): on
  `FileNotFoundError` / `ConnectionRefusedError` / `asyncio.TimeoutError` /
  `OSError` it prints a stderr line and `raise SystemExit(1)`. It never prints
  the literal "stopped"/"unreachable". The `stt-list` recipe therefore owns the
  stopped-agent display and MUST absorb the non-zero exit. Default probe timeout
  is 3.0 s, so a stopped socket fails fast rather than hanging.
- Rendered plist keys (`render_stt_plist.py:133-135`): `RunAtLoad: True`,
  `KeepAlive: True`, `ThrottleInterval: 10`. `KeepAlive` makes `SIGTERM` respawn
  (motivates bootout); `RunAtLoad` makes `bootout` session-scoped (reloads at
  next login while the plist remains).
- Verified socket directory is `~/Library/Caches/pipecat-stt/` (NOT a per-label
  subdir). Canonical sockets: `stt.sock`, `parakeet.sock`, `nemotron.sock`.
- README house style: top sections `##`, subsections `###`; existing per-ASR
  map is a markdown table (`### Per-ASR socket convention`); operator snippets
  use fenced ```bash with env-prefix style; `## Checking server health` already
  documents the `status` invocation.

### Dependency / version facts

- `pyproject.toml`: `version = 0.3.1`, `requires-python >= 3.12`,
  `target-version = py312`; dev tooling `ruff >= 0.8.0`, `pytest >= 8.0.0`,
  `pytest-asyncio >= 0.24.0`; only hard runtime dep `websockets >= 13.0`.
- `just` does not appear anywhere in the repo today (no `pyproject.toml` entry,
  no README/script mention) — this PR introduces it as a new convention.

## Testing Notes

- **The invariants are CI-testable — "launchd-free" did not mean "manual".** The
  repo already ships a hermetic stub-`launchctl` harness:
  `tests/test_install_migration.py` stubs `launchctl`/`id` on `PATH` under a temp
  `HOME`, logs argv, and asserts exact `bootout` targets AND plist file
  presence/absence (e.g. `test_default_install_removes_legacy_plists:195-208`,
  `test_custom_label_install…:311-342`; `launchctl print` non-zero stub at
  `:93-95`; LaunchAgents glob against stub HOME at `:148-154`). Phase 1/2 tests
  reuse this pattern to assert, in CI: bootout-not-rm, exact delegation env,
  explicit `--socket-path`, prefix-sweep custom-agent surfacing,
  stopped-socket tolerance, and idempotency guards. Extend the same approach to
  stub `uv`/`python -m stt_server status` (an argv-echoing shim) for probe
  assertions.
- **What stays genuinely manual** (real launchd, not stubbable): that a real
  agent *actually* stays down after `bootout` and that `bootstrap`+`kickstart`
  restores from the on-disk plist with no re-render. These are documented
  launchctl behaviors (`bootout` removes the service from the domain without
  touching the plist; `bootstrap` loads from the plist file) — named here as
  assumptions, gated by the manual acceptance below.
- Manual acceptance on the dev machine: with whisper + parakeet + nemotron
  loaded, `just stt-list` shows all three with correct live backends;
  `just stt-disable whisper` removes pid 67359 and `launchctl list | grep
  pipecat.stt-server` no longer lists it; the plist at
  `~/Library/LaunchAgents/pipecat.stt-server.plist` **still exists** after
  disable; `just stt-enable whisper` brings it back from the existing plist (no
  re-render); `just stt-status nemotron` reports `backend=nemotron`.

## Issues & Solutions

_(to be filled during implementation)_

## Acceptance Criteria

- [ ] `justfile` exists at repo root with `set shell := ["bash", "-uc"]`;
      `just --list` shows `stt-list`, `stt-status`, `stt-disable`, `stt-enable`,
      `stt-install`, `stt-uninstall`.
- [ ] `stt-list` reports all `pipecat.stt-server*` agents (prefix sweep,
      including a custom-labelled one) with loaded/running + pid, and live
      backend/model for canonical sockets; a stopped/absent socket renders
      "stopped/unreachable" and the recipe still exits 0.
- [ ] `stt-disable <backend>` takes the agent down for the session and
      **preserves** the plist (asserted in CI via the stub harness: `bootout`
      fired, plist file survives); README states it lasts until next login.
- [ ] `stt-enable <backend>` restores from the existing plist without a
      re-render (manual acceptance).
- [ ] Probe always passes `--socket-path` (asserted via argv shim; bogus
      `STT_WS_SOCKET` ignored).
- [ ] `install`/`uninstall` recipes delegate with the exact `PIPECAT_STT_*`
      triple per backend (asserted for a non-default backend); the script is
      unmodified.
- [ ] justfile map equals the README per-ASR table (asserted in CI).
- [ ] Unknown backend name fails fast (non-zero) and the error names all three
      valid backends.
- [ ] `stt_server/__main__.py`, the wire protocol, and
      `scripts/install_stt_agent.sh`'s existing subcommands are unmodified — and
      a CI test asserts the branch diff is a subset of
      `{justfile, tests/test_justfile_recipes.py, README.md, the plan}` (no Koda
      pin bump needed).
- [ ] README documents `just` + the recipes + the disable-vs-uninstall
      distinction + the legacy-koda manual-check note, matching house style.
- [ ] `uv run pytest` green; `ruff format` + `ruff check` clean.

## Review Focus

- **Koda contract:** confirm the diff touches neither `stt_server/` Python CLI,
  the wire protocol, nor `install_stt_agent.sh`'s existing subcommands — only the
  new `justfile`, tests, and README. If any of those are touched, the no-pin-bump
  claim is void.
- **Second source of truth:** the backend→label/socket map in the justfile must
  match `install_stt_agent.sh`'s defaults and the README table exactly. Flag any
  drift (e.g. a socket filename or label that disagrees).
- **Custom-label blind spot:** named recipes (`stt-disable parakeet`) only know
  the three canonical backends. `stt-list`'s `pipecat.stt-server*` prefix sweep
  should still surface a custom-labelled agent (e.g. a fourth operator-installed
  one) as loaded/running even though no named recipe targets it — verify the
  sweep is prefix-based, not map-based, so it does not hide unknown agents.
- **disable ≠ uninstall:** `stt-disable` must NOT delete the plist (that's
  `stt-uninstall`'s job). Verify `bootout` without `rm`.
- **Probe correctness:** `stt-status` / `stt-list` must pass `--socket-path`
  explicitly so the probe targets the intended agent and cannot fall back to a
  stale `STT_WS_*`/default socket.
- **`status` exit-code absorption:** `stt-list`'s per-socket probe must wrap the
  `status` call so its `SystemExit(1)` on a stopped/absent socket (`__main__.py:
  288-303`) does not abort the recipe. Verify the recipe still exits 0 with a
  stopped agent present.
- **`stt-disable` scope honesty:** confirm the README/help describe disable as
  "down until next login" (RunAtLoad reloads), not permanent — only
  `stt-uninstall` (plist removed) is durable.
- **Map-vs-README mirror test:** confirm a test actually parses the README table
  and fails on drift; without it "must match exactly" is unenforced.

## Final Results

Delivered all three phases:

- **`justfile`** — read-only recipes (`stt-list`, `stt-status`) and lifecycle
  recipes (`stt-disable`, `stt-enable`, `stt-install`, `stt-uninstall`), with a
  private `_resolve` helper holding the backend → (label, socket, backend-name)
  map. Install/uninstall delegate to `scripts/install_stt_agent.sh` (no plist
  reimplementation); `stt-list` prefix-sweeps `pipecat.stt-server*` so custom
  labels surface. `stt-disable` ≠ `stt-uninstall` (bootout keeps the plist).
- **`tests/test_justfile_recipes.py`** — hermetic recipe tests (stub
  `launchctl`/`id`/`uv`, temp `HOME`), a README↔justfile map mirror test, and a
  Koda-safety negative-contract test.
- **README** — "Managing agents with `just`" section + the per-ASR socket table
  the mirror test parses.

Post-review hardening (deep-review findings): `{{backend}}` is shell-escaped via
`just`'s `quote()` at every call site (closes a command-injection vector);
`_resolve` emits one field per line so spaced socket paths parse correctly; the
resolution/mirror tests drive the public `stt-install` recipe instead of the
private `_resolve`; the `uv` stub dispatch is argv-position-aware; and the
README-table parser anchors on the header before indexing columns.

<!-- reviewed: 2026-06-07 @ f144271594d3cf4660fb40d36db9ac4637b4e1e8 -->
