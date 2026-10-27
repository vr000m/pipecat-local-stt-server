# Task: Port-squat detection, one smoke entry point and launchd verification (0.5.1)

**Status**: Planning (review findings folded 2026-10-27)
**Component**: Install & Packaging
**Assigned to**: Varun Singh
**Priority**: Medium
**Branch**: feat/stt-squat-detection-unified-smoke
**Created**: 2026-10-27
**Completed**:
**Review Gates**: none

## Objective

Close the follow-ups left open by 0.5.0 (PR #16) in one 0.5.1 release:

1. **Issue #17, proposals 3 + 2.** Document the loopback-TCP trust boundary as the decision, and make `just stt-status` / `just stt-list` warn when the process listening on an agent's port is not the one launchd started (port squatting).
2. **launchd verification.** Run, once and repeatably, the checks that 0.5.0 covered only with stubs: the port-collision restart loop and the TCP-to-socket-and-back rollback, against real launchd with a spare label and port so no real agent is touched.
3. **One smoke entry point.** `just smoke` runs every smoke check (supervised echo, peer-cred, token-file policy, optional real-agent decode and launchd checks) and prints one PASS/FAIL/SKIPPED table.

Not in scope: token by default (issue #17 proposal 1), which breaks the same-as-TTS default and needs the TTS server to move first. Login persistence needs a logout and cannot be automated; it stays a documented manual check.

## Context

- 0.5.0 shipped loopback TCP agents with no token by default. Another local process can connect, and a process that binds the port first can answer in place of the agent. `docs/operations.md` already states there is no peer-uid check.
- Verified at 0.5.0: token-file refusals and acceptance on a spare port without launchd; `smoke_supervised.py --backend echo` (12 scenarios); same-uid peer-cred; a decode through each real agent. Not verified against real launchd: the port-collision loop, socket rollback, login persistence.
- The installer takes `PIPECAT_STT_LABEL`, `PIPECAT_STT_PORT` and `PIPECAT_STT_SOCKET`, so a spare label such as `pipecat.stt-server.smoke` on a spare port exercises real launchd without touching `pipecat.stt-server*`. A fixer once ran the installer against the real label and broke the Whisper agent; nothing in this plan may do that. Every launchd check must refuse to run against a canonical label or port.
- `justfile` `smoke-peercred` (line 316) and `scripts/smoke_peercred.py` (docstring lines 30-31) use plain `uv run`, which prunes the optional backend extras and sends LaunchAgents into a crash loop (see the `uv run` extras memory). They must run from `.venv/bin/python` or `uv run --no-sync`. This fix lands before any `just smoke` run (Phase 1, first commit).
- The 0.5.0 bookkeeping (plan header shipped, `docs/dev_plans/README.md` row) was completed in commit 41fcb65; Phase 4 only closes this plan.
- The installer never creates or unlinks `<socket>.lock` (`install_stt_agent.sh:391,492`; 0.5.0 plan line 270; `docs/operations.md:191`). Socket cleanup removes only the stale socket.
- The smoke plist lands in the real `~/Library/LaunchAgents` and matches `stt-list`'s `pipecat.stt-server*` glob (`justfile:93`) by design. `_resolve` (`justfile:55-66`) accepts only whisper|parakeet|nemotron|kyutai, so the spare label is reachable only through `stt-list` or bare `stt-status`, never `stt-status smoke`.

## Architecture & Call Flow

```
just smoke [--agents] [--launchd]
  └─ scripts/smoke.py  (.venv/bin/python; table, exit-code rule, per-step timeout)
       ├─ scripts/smoke_supervised.py --backend echo
       ├─ scripts/smoke_peercred.py
       ├─ token-file refuse/accept  ── scripts/_smoke_lib.assert_token_gate()
       ├─ --agents : read each installed plist (read_stt_plist.py) → status/decode probe (read-only)
       └─ --launchd: scripts/smoke_launchd.py
            ├─ guard (label, port, platform, start state)
            ├─ snapshot real pipecat.stt-server* jobs (launchctl print pid + listener pid)
            ├─ scripts/install_stt_agent.sh  (spare label, echo backend)  → launchd → stt_server
            ├─ scripts/squat_check.py  (called directly, never via `just`)
            ├─ _smoke_lib.assert_token_gate()
            └─ teardown: installer `uninstall`, remove spare .lock, re-snapshot and compare

just stt-list / just stt-status (no target or known backend)
  └─ per TCP agent: one `launchctl print` (already read for pid) → scripts/squat_check.py
       (system python3 -I, stdlib only, same pattern as read_stt_plist.py)
       └─ lsof -nP -iTCP:PORT -sTCP:LISTEN -Fpn  (keep HOST or wildcard addresses)
```

Rules: Python orchestrators call scripts, never `just`. `squat_check.py` is the single squat verdict for the recipes and the harness. Warnings go to stderr only.

## Decisions

| # | Decision | Why |
|---|---|---|
| 1 | Squat detection **warns, never fails**: `stt-list` exits as before; `stt-status` keeps the probe's exit code. Warnings go to **stderr**, before any `exec` | A status command that starts failing would break scripts; stdout is the machine-readable probe surface |
| 2 | **Three-state verdict** in `scripts/squat_check.py`. Inputs: host, port, launchd pid (empty if none), job-loaded flag. Listener set = the pids from `lsof -nP -iTCP:PORT -sTCP:LISTEN -Fpn`, keeping only entries whose address is HOST or a wildcard (`*`, `[::]`). A host-filtered `-iTCP@HOST:PORT` query is **not** used: probed on this machine, it returns nothing for a `0.0.0.0` listener, and a wildcard squatter is the case that blocks the agent's bind. (a) no listener, or job not loaded → quiet; (b) job running with pid P and any listener ≠ P → warn squat; (c) job loaded with no pid and a listener present → warn "port N held by pid X; label not running". Exit 0 quiet, 3 warned, 2 cannot tell (lsof failed). A would-be warning is re-checked once after 1 s (re-read the pid, then the listeners) and printed only if it persists, so a restart between the two reads cannot warn. On a warning print the listener command (`lsof -Fc`) | A KeepAlive job that fails to bind has no `pid =` for most of each 10 s window, so (c) is what makes the collision case observable. Any differing PID warns; no parent-PID exemption |
| 3 | Squat check covers TCP agents only, and only when an installed plist exists | A socket agent keeps the peer-uid check and its `.lock`; the no-plist canonical-fallback path of `stt-status` makes no squat call |
| 4 | The helper is `scripts/squat_check.py` (stdlib, system `python3 -I`); `_squat_check` in the justfile only wraps it. It receives the pid and loaded flag the caller already read, not a label | Matches the one-shared-reader pattern of `read_stt_plist.py`; one `launchctl print` per agent per command, no PID race |
| 5 | The helper relies on the invariant listener PID == launchd PID (ProgramArguments[0] is the venv python serving in-process). Documented in its docstring and pinned by a test. A `PIPECAT_STT_PYTHON` wrapper that forks makes every agent warn; the printed listener command makes that recognisable | Verified: no fork/Popen in `stt_server/` |
| 6 | `lsof` is the deliberate PID source; its limit (other users' sockets may be invisible, so exit 2 / quiet) is documented. `netstat -anv` could close it but its column layout is undocumented, so it is not used | Advisory by design |
| 7 | launchd checks are **opt-in** (`just smoke --launchd`). The guard allows only the exact label `pipecat.stt-server.smoke` (both `PIPECAT_STT_LABEL` and the `KODA_STT_LABEL` alias set explicitly), and a port that is not one of the four canonical ports (read from `install_stt_agent.sh port <backend>`) and not already LISTENing. The port check runs before the collision step binds its occupier. Separately, the harness has two *preconditions*, `sys.platform == "darwin"` and `CI` unset (Decision 8) | An allowlist, not a prefix denylist; the label alone did not protect the real ports |
| 8 | Smoke step outcomes: a step that is not requested, or whose precondition is absent on this machine (non-darwin, `CI` set, no installed agents for `--agents`, no second uid for cross-uid peer-cred) → **SKIPPED** with the reason. A requested step that a *guard* (Decision 7: label, port, start state) refuses, cannot start, crashes, times out or exits non-zero → **FAIL**. Exit non-zero only on FAIL. Every step has a timeout | Defines the table's exit code unambiguously |
| 9 | One `assert_token_gate(host, port, token_file)` in `scripts/_smoke_lib.py` (401 without a token, success with it), used by `smoke.py` and `smoke_launchd.py` | Avoids two copies of the same assertion |
| 10 | Token by default stays out; revisit with the TTS server | Breaks every consumer's no-config default |

## Implementation Checklist

### Phase 1a: Replace plain `uv run` in the smoke recipes

**Impl files:** justfile, scripts/smoke_peercred.py
**Test files:** tests/test_justfile_recipes.py
**Test command:** `.venv/bin/python -m pytest tests/test_justfile_recipes.py -q`

- [ ] Replace plain `uv run` in the `smoke-peercred` recipe and the `smoke_peercred.py` docstring with `.venv/bin/python`. This phase is its own commit, and no `just smoke` runs before it lands. Add a regression test that greps the justfile, `scripts/smoke_*.py` and `README.md` for `uv run python` without `--no-sync`.

### Phase 1b: Squat detection (issue #17, proposals 3 + 2)

**Impl files:** scripts/squat_check.py, justfile, docs/operations.md, CHANGELOG.md
**Test files:** tests/test_squat_check.py, tests/test_justfile_recipes.py
**Test command:** `.venv/bin/python -m pytest tests/test_squat_check.py tests/test_justfile_recipes.py tests/test_docs_port_caveats.py -q`

- [ ] `scripts/squat_check.py` per Decisions 2-6, plus the `_squat_check` wrapper recipe, called from the `stt-list` loop (TCP plists only, reusing the pid it already read) and from `stt-status` (known backend with a plist, and the no-target path through `stt-list`), before the `exec`.
- [ ] Tests (`tests/test_squat_check.py` for the helper, `tests/test_justfile_recipes.py` for the recipes), stubbing `lsof` and `launchctl`: match; mismatch; listener absent; job loaded with no pid and a listener; job not loaded; two listener pids where one matches (warns); a wildcard (`*:PORT`) listener with a different pid (warns) and one bound to another host (ignored); lsof unreadable (exit 2, quiet); socket agent (no call); no plist (no call).
- [ ] Exit-code and stream tests: on a mismatch `stt-list` exits 0 and `stt-status` returns the probe's exit code for both 0 and 1; the warning is on stderr and stdout is byte-identical with and without a squat.
- [ ] Add the `lsof` stub (default: no listener) to the shared fixture in `tests/test_justfile_recipes.py` in the same commit as the helper, so existing tests stay hermetic on the macos-14 runner. Add `_squat_check` to the private list in `test_just_list_exposes_public_recipes` (`smoke` joins the public list in Phase 3).
- [ ] `docs/operations.md`: state the trust boundary as the decision for issue #17; document the warning, the lsof limit, and what to do (`launchctl print`, `lsof -nP -iTCP:<port>`, kill the squatter or change the port).
- [ ] CHANGELOG `[0.5.1]` entry (later phases extend it); when the PR lands, comment the decision on issue #17 (trust boundary documented, squat detection shipped, token-by-default deferred) and close it, then open a new issue "token by default for loopback TCP agents" that waits on the TTS server moving first.

### Phase 2: launchd verification harness

**Impl files:** scripts/smoke_launchd.py, scripts/_smoke_lib.py, CHANGELOG.md
**Test files:** tests/test_smoke_launchd.py
**Test command:** `.venv/bin/python -m pytest tests/test_smoke_launchd.py tests/test_docs_port_caveats.py -q`

This phase writes code and stubbed tests only. The first live run against real launchd happens after the user reviews `scripts/smoke_launchd.py`.

- [ ] `scripts/smoke_launchd.py`: installs a spare-label agent through the real installer using `.venv/bin/python` (never `uv run`), with the `echo` backend and `PIPECAT_STT_UNLOAD_WAIT` set explicitly (the installer's "leaving socket" warning is a FAIL). Applies the Decision 7 guard. Spare port default 9998.
- [ ] **Start state:** fail early, printing the bootout/rm commands, if the spare plist, a loaded spare job, the spare socket or the spare `.lock` already exists. Reading the log path from the installed plist's `StandardErrorPath` via `read_stt_plist.py` (it is `pipecat-stt-server-smoke.err`, not `<label>.err`).
- [ ] **Real-agent snapshot:** record `launchctl print` pid and the lsof listener pid for every `pipecat.stt-server*` label (excluding the spare) before and after; FAIL on any change.
- [ ] **Teardown (always, including failure and SIGINT):** the installer's `uninstall` for the spare label (bootout, wait for unload, socket removal in that order), then remove the spare `.lock` once lsof shows no holder, then remove the spare plist and logs.
- [ ] Check: **port collision.** Bind the spare port first, install, and poll up to 30 s for at least two address-in-use lines in the err log (launchd's `ThrottleInterval` of 10 is a minimum spacing, not a period; no fixed cadence is asserted) and at least one warning from `squat_check.py`, called directly. Free the port and assert the agent comes up.
- [ ] Check: **socket rollback.** Install as TCP, reinstall as socket, then reinstall as TCP. After each step assert the plist records the right transport and the server answers on the new endpoint; assert the old **socket** is gone only after the old job unloaded, and that `<socket>.lock` is left in place and unheld.
- [ ] Check: **token file through launchd.** Install with a valid 0600 token file in a 0700 directory created under `$HOME` (`/tmp` and the scratchpad fail the ancestor-directory policy in `stt_server/auth.py`); assert the policy accepts it first, then `assert_token_gate`.
- [ ] Tests (stubbed `launchctl`/`id`, no real launchd; the installer runs for real as in `tests/test_install_migration.py`): the guard refuses `pipecat.stt-server`, `pipecat.stt-server.parakeet`, `koda.stt-server`, an unset label, a `KODA_STT_LABEL` override and each canonical port, and accepts `pipecat.stt-server.smoke` on a free port, logging no bootout or bootstrap on refusal; a forced failing check still runs teardown (argv log shows `bootout gui/<uid>/<label>`, plist and logs gone), including on SIGINT; the real-agent snapshot comparison flags a changed pid; the harness refuses when `CI` is set or the platform is not darwin.
- [ ] Extend the CHANGELOG `[0.5.1]` entry.
- [ ] Manual, documented only: persistence across logout and login.

### Phase 3: Unified smoke

**Impl files:** scripts/smoke.py, justfile, README.md, docs/operations.md, CHANGELOG.md
**Test files:** tests/test_smoke_runner.py, tests/test_justfile_recipes.py
**Test command:** `.venv/bin/python -m pytest tests/test_smoke_runner.py tests/test_justfile_recipes.py tests/test_docs_port_caveats.py -q`

- [ ] `scripts/smoke.py` (run as `.venv/bin/python scripts/smoke.py`) and `just smoke [--agents] [--launchd]` (positional-arguments pattern as `stt-probe`): runs `smoke_supervised.py --backend echo`, `smoke_peercred.py`, the token-file refuse/accept checks (spare port, `echo` backend) and prints one table with PASS, FAIL or SKIPPED plus the reason, per Decision 8. `--agents` adds a decode through each installed agent from its plist endpoint (read-only). The repo ships no audio, so the clip is generated at run time with `say` and `afconvert` (16 kHz mono wav); the step asserts the transcript contains the spoken key word, and is SKIPPED when `say` is absent. `--launchd` adds Phase 2.
- [ ] `scripts/_smoke_lib.py`: add `assert_token_gate()` (Decision 9).
- [ ] Tests, with the individual smoke steps stubbed: the table renderer; the exit-code rule for every Decision 8 outcome (not requested, precondition absent, guard refused, crash, timeout, non-zero); `--agents` under stubbed `launchctl`/`id`/`uv` with rendered TCP and socket plists, asserting the argv log has only `print`/status calls, no bootout, bootstrap, kickstart or installer call, and no `uv run` without `--no-sync`.
- [ ] Add `smoke` to the public-recipe list in `test_just_list_exposes_public_recipes`.
- [ ] README and `docs/operations.md`: one `just smoke` section replacing the scattered smoke instructions.
- [ ] Extend the CHANGELOG `[0.5.1]` entry.

### Phase 4: Release housekeeping

**Impl files:** pyproject.toml, uv.lock, CHANGELOG.md, docs/dev_plans/README.md
**Test files:** tests/test_docs_port_caveats.py
**Test command:** `.venv/bin/python -m pytest tests/test_docs_port_caveats.py -q`

- [ ] Finalise the CHANGELOG `[0.5.1]` entry.
- [ ] Bump `pyproject.toml` to 0.5.1 and regenerate `uv.lock` with `uv lock` (never `uv sync`, which prunes the extras). Do this **before** the final `just smoke --launchd` so the same-PID check covers it.
- [ ] Mark this plan Shipped and flip its row in `docs/dev_plans/README.md`.

## Files to Modify

- `justfile` (`_squat_check` wrapper, `stt-list`, `stt-status`, `smoke`, `smoke-peercred`)
- `scripts/squat_check.py` (new), `scripts/smoke_launchd.py` (new), `scripts/smoke.py` (new), `scripts/_smoke_lib.py` (`assert_token_gate`), `scripts/smoke_peercred.py` (docstring)
- `tests/test_squat_check.py` (new), `tests/test_justfile_recipes.py`, `tests/test_smoke_*.py` (new)
- `docs/operations.md`, `README.md`, `CHANGELOG.md`, `docs/dev_plans/README.md`
- `pyproject.toml`, `uv.lock`

## Verification

- Unit tests with stubbed `lsof` and `launchctl` pass; full suite (run in the background) and `ruff format --check`, `ruff check`, `shellcheck` clean.
- `just smoke` all PASS on this machine; `just smoke --agents` decodes through all four running agents; `just smoke --launchd` passes, and its own before/after snapshot shows the four real agents with the same PIDs and listeners.
- A deliberate squat (a process bound to a spare agent's port before launchd starts it) is reported by `just stt-list`.

## Risks

- `launchctl print` output is not a stable API; the PID parse must tolerate a missing `pid =` line (job loaded but not running) and fall to the "loaded, no pid" state (Decision 2c) or "cannot tell", never to a false squat warning.
- `lsof` cannot see other users' sockets; a squatter run by another user may be invisible. Documented; it is the reason proposal 2 is advisory.
- A forking `PIPECAT_STT_PYTHON` wrapper warns on every agent (Decision 5); the printed listener command identifies it.
- The launchd harness touches real launchd. The guard, the start-state check, unconditional teardown and the real-agent snapshot are the safety, and each has a test in Phase 2.
- A failed teardown leaves a KeepAlive echo agent visible in every `stt-list` and at next login; the start-state check on the next run prints the cleanup commands.

## Open Questions

1. ~~Does the installer accept the `echo` backend and a `.venv` without extras?~~ RESOLVED: the renderer's allowlist is `echo|mlx|parakeet|nemotron|kyutai` (`scripts/render_stt_plist.py:52`); the installer's interpreter check is only `-x "$PYTHON"` (`install_stt_agent.sh:229`) and `_ensure-extra` runs only from `just stt-install`, which the harness bypasses. The harness asserts at start that `stt_server` imports under `python -I` (editable install).
2. ~~Close issue #17 after Phase 1, or keep it open for proposal 1?~~ RESOLVED 2026-10-27 (grilled): close #17 with the decision comment when the PR lands and open a narrower token-by-default issue that waits on the TTS server.

## Review

Grilled 2026-10-27: helper home (`squat_check.py`), three-state warn rule with 1 s re-check, any-differing-pid listener rule, allowlist harness guard with real-agent snapshot, SKIPPED/FAIL outcomes, issue #17 handling and the 0.5.1 version were each accepted as recommended.

Plan review 2026-10-27 (5 lenses plus a contradiction pass, 43 findings, 1 Critical, 19 Important, 23 Minor) folded in above:
- the collision assertion now rests on the three-state verdict (Decision 2) and a tolerant timing bound
- the `.lock` assertion matches the installer's contract
- the guard is an allowlist covering label, port, platform and start state, and has tests
- teardown, real-agent snapshot, exit-code rule, helper design and call flow are specified
- the `uv run` fix moves to its own Phase 1a, ahead of everything else

Conduct-format edit 2026-10-27 (user-approved): `## Phases` renamed `## Implementation Checklist`, Phase 1 split into 1a and 1b so the `uv run` fix is its own commit, per-phase `Impl files` / `Test files` / `Test command` slots added, and the `smoke` public-recipe test entry moved to Phase 3. No design decision changed.

<!-- reviewed: 2026-10-27 @ 2c4ec68126d00b7e2985481643f549b8dc2a45e0 -->

<!-- /review-plan writes the marker line above. Everything below is the workspace: edits here do NOT invalidate the marker. -->

## Progress

- [ ] Phase 1a: Replace plain `uv run` in the smoke recipes
- [ ] Phase 1b: Squat detection
- [ ] Phase 2: launchd verification harness
- [ ] Phase 3: Unified smoke
- [ ] Phase 4: Release housekeeping

## Findings

- (append findings here as work proceeds)
- 2026-10-27: probed `lsof -nP -iTCP@127.0.0.1:PORT -sTCP:LISTEN -Fp` against a `0.0.0.0` listener: no output. Unfiltered `-iTCP:PORT` shows it. Decision 2 queries by port and filters addresses in the helper.
- 2026-10-27: the user booted out all four real agents with `just stt-disable` (plists kept); re-enable with `just stt-enable <backend>` before any `--agents` or `--launchd` run.

## Review Waivers
