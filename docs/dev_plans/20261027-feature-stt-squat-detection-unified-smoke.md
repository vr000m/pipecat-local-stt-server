# Task: Port-squat detection, one smoke entry point and launchd verification (0.5.1)

**Status**: Shipped in 0.5.1 (PR #18; the launchd harness ran live 2026-10-28 and again via `just smoke --launchd` 2026-10-29; `--agents` decode through a real launchd agent is stub-tested only)
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
- Verified live at 0.5.0, by hand before `smoke.py` existed (0.5.0 plan, manual verification): token-file refusals and acceptance on a spare port without launchd; `smoke_supervised.py --backend echo` (12 scenarios); same-uid peer-cred; a decode through each real agent. The 0.5.1 code that automates these (`smoke.py`'s `token-file` child, `--agents` decode and `peercred` SKIPPED mapping) is new and, like the launchd harness, has only stubbed tests until the first live `just smoke` run. Not verified against real launchd: the port-collision loop, socket rollback, login persistence.
- The installer takes `PIPECAT_STT_LABEL`, `PIPECAT_STT_PORT` and `PIPECAT_STT_SOCKET`, so a spare label such as `pipecat.stt-server.smoke` on a spare port exercises real launchd without touching `pipecat.stt-server*`. A fixer once ran the installer against the real label and broke the Whisper agent; nothing in this plan may do that. Every launchd check must refuse to run against a canonical label or port.
- `justfile` `smoke-peercred` and the `scripts/smoke_peercred.py` docstring used plain `uv run`, which prunes the optional backend extras and sends LaunchAgents into a crash loop (see the `uv run` extras memory). They must run from `.venv/bin/python` or `uv run --no-sync`. This fix lands before any `just smoke` run (Phase 1, first commit).
- The 0.5.0 bookkeeping (plan header shipped, `docs/dev_plans/README.md` row) was completed in commit 41fcb65; Phase 4 only closes this plan.
- The installer never creates or unlinks `<socket>.lock` (`install_stt_agent.sh`'s socket cleanup; the 0.5.0 plan; `docs/operations.md`, Socket agents). Socket cleanup removes only the stale socket.
- The smoke plist lands in the real `~/Library/LaunchAgents` and matches `stt-list`'s `pipecat.stt-server*` glob by design. The justfile's `_resolve` accepts only whisper|parakeet|nemotron|kyutai, so the spare label is reachable only through `stt-list` or bare `stt-status`, never `stt-status smoke`.

## Architecture & Call Flow

```
just smoke [--agents] [--launchd]
  └─ scripts/smoke.py  (.venv/bin/python; table, exit-code rule, per-step timeout)
       ├─ scripts/smoke_supervised.py --backend echo
       ├─ scripts/smoke_peercred.py
       ├─ token-file refuse/accept  ── scripts/_smoke_lib.assert_token_gate(connect=connect_verified)
       ├─ --agents : read each installed plist once (read_stt_plist.load: endpoint + token)
       │             → launchctl print (pid P, runs R) → decode child: connect_verified
       │             (server end of THAT connection is P, re-print still P/R) → token and
       │             decode over the same socket → fresh print still P/R (read-only)
       └─ --launchd: scripts/smoke_launchd.py
            ├─ baseline snapshot of real pipecat.stt-server* agents (Decision 11)
            ├─ guard (label, port from the baseline, platform, start state)
            ├─ scripts/install_stt_agent.sh  (spare label, echo backend)  → launchd → stt_server
            ├─ scripts/squat_check.py  (called directly, never via `just`; the recipes' form)
            ├─ _smoke_lib.assert_token_gate() over connect_verified (spare job's pid/runs from read_job)
            ├─ re-snapshot and compare before every check
            └─ teardown: installer `uninstall`, spare socket and .lock under the harness's own flock, re-snapshot and compare

just stt-list / just stt-status (no target or known backend)
  └─ per TCP agent: one `launchctl print` (already read for pid) → scripts/squat_check.py
       (exit status on argv, the answer on stdin)
       (system python3 -I, stdlib only, same pattern as read_stt_plist.py)
       └─ lsof -nP -iTCP:PORT -sTCP:LISTEN -Fpn  (keep HOST or wildcard addresses)
```

Rules: Python orchestrators call scripts, never `just`. `squat_check.py` is the single squat verdict for the recipes and the harness. Warnings go to stderr only.

## Decisions

| # | Decision | Why |
|---|---|---|
| 1 | Squat detection **warns, never fails**: `stt-list` exits as before; `stt-status` keeps the probe's exit code. Warnings go to **stderr**, before any `exec` | A status command that starts failing would break scripts; stdout is the machine-readable probe surface |
| 2 | **Three-state verdict** in `scripts/squat_check.py`. Inputs: host, port, launchd pid (empty if none), job-loaded flag. Listener set = the pids from `lsof -nP -w -iTCP:PORT -sTCP:LISTEN -Fpn`, keeping each entry whose address can block the agent's bind: IPv6 brackets are stripped on both sides; either side a wildcard (`*`, `::`, `0.0.0.0` or empty) matches; a HOST of `localhost` matches `127.0.0.1` and `::1`; otherwise the addresses must be equal. A host-filtered `-iTCP@HOST:PORT` query is **not** used: probed on this machine, it returns nothing for a `0.0.0.0` listener, and a wildcard squatter is the case that blocks the agent's bind. (a) no listener, or job not loaded → quiet; (b) job running with pid P and any listener ≠ P → warn squat; (c) job loaded with no pid and a listener present → warn "port N held by pid X; label not running". Exit 0 quiet, 3 warned, 2 cannot tell (lsof failed, or the re-check could not re-read the pid or saw it change across the second listener read; lsof's exit 1 with no stderr or only `lsof: WARNING:` lines is "nothing matched"). A would-be warning is re-checked once after 1 s (re-read the pid, then the listeners, then the pid again) and printed only if it persists with the same pid on both sides of that listener read, so a restart between the two reads, or during the second, cannot warn. On a warning print the listener command (`lsof -Fc`) | A KeepAlive job that fails to bind has no `pid =` for most of each 10 s window, so (c) is what makes the collision case observable. Any differing PID warns; no parent-PID exemption |
| 3 | Squat check covers TCP agents only, and only when an installed plist exists | A socket agent keeps the peer-uid check and its `.lock`; the no-plist canonical-fallback path of `stt-status` makes no squat call |
| 4 | The helper is `scripts/squat_check.py` (stdlib, system `python3 -I`); `_squat_check` in the justfile only wraps it. It receives what the caller already read, in ONE form for the recipes and the harness alike: the caller's one `launchctl print` answer, its exit status on argv (`--launchctl-rc`) and its output on **stdin, never argv** (the output carries the job's environment block, where a pre-0.5.0 plist keeps its bearer token, and argv is readable by every local user through `ps`), which the helper classifies with the same rule as its own re-check (`classify_print`: only "could not find service" is not loaded, any other failure is "cannot tell", exit 2). Changed in review round 5 from a second `--loaded`/`--pid` form for the harness, so the harness exercises the recipes' path against real launchd. An optional `--label` is used only by the 1-second re-check to re-read the pid (user-approved during conduct, Phase 1b) | Matches the one-shared-reader pattern of `read_stt_plist.py`; one `launchctl print` per agent per command, no PID race |
| 5 | The helper relies on the invariant listener PID == launchd PID (ProgramArguments[0] is the venv python serving in-process). Documented in its docstring and pinned by a test. A `PIPECAT_STT_PYTHON` wrapper that forks makes every agent warn; the printed listener command makes that recognisable | Verified: no fork/Popen in `stt_server/` |
| 6 | `lsof` is the deliberate PID source; its limit (other users' sockets may be invisible, so exit 2 / quiet) is documented. `netstat -anv` could close it but its column layout is undocumented, so it is not used | Advisory by design |
| 7 | launchd checks are **opt-in** (`just smoke --launchd`). The guard allows only the exact label `pipecat.stt-server.smoke` (both `PIPECAT_STT_LABEL` and the `KODA_STT_LABEL` alias set explicitly), and a port that is not one of the four canonical ports (read from `install_stt_agent.sh port <backend>`) and not already LISTENing. The port check runs before the collision step binds its occupier. Separately, the harness has two *preconditions*, `sys.platform == "darwin"` and `CI` unset (Decision 8) | An allowlist, not a prefix denylist; the label alone did not protect the real ports |
| 8 | Smoke step outcomes: a step that is not requested, or whose precondition is absent on this machine (non-darwin, `CI` set, no installed agents for `--agents`, no second uid for cross-uid peer-cred) → **SKIPPED** with the reason. A requested step that a *guard* (Decision 7: label, port, start state) refuses, cannot start, crashes, times out or exits non-zero → **FAIL**. Exit non-zero only on FAIL. Every step has a timeout | Defines the table's exit code unambiguously |
| 9 | One `assert_token_gate(host, port, token_file, connect=...)` in `scripts/_smoke_lib.py` (401 without a token, success with it), used by `smoke.py` and `smoke_launchd.py`; every request goes over a `connect_verified` socket (Decision 12) | Avoids two copies of the same assertion |
| 10 | Token by default stays out; revisit with the TTS server | Breaks every consumer's no-config default |
| 11 | **Real-agent snapshot contract** (review round 5). Per real agent (installed plist or loaded job, spare excluded): `loaded`, `pid`, `runs` and the served endpoint from ONE `launchctl print` of the job (the source of truth; unreadable raises), parsed by the one reader in `squat_check.py` (`read_print` / `parse_print`, closing pass) that `launchd_state`, `_smoke_lib.launchd_identity` and `read_job` wrap, so every reader treats a running job's missing `runs =` line as UNKNOWN; a sha256 of the plist (only the intended endpoint, never where the listener is read); both ports parsed as the server parses `--port` (`int()`; a value it refuses is UNKNOWN, review round 7); and the holders of the JOB's endpoint (`squat_check.endpoint_holders`: TCP listeners on its host and port, or a socket job's `<socket>.lock` holders, since `lsof` on an AF_UNIX path lists nothing). A job not loaded or with no pid serves nothing (no `lsof`); anything unreadable on a running job is UNKNOWN and refuses the run, a running job's missing `runs =` line included (review round 6). Every `AgentState` field is compared (`dataclasses.fields`). The baseline precedes the guard (whose port set is derived from it alone: the job's served port and the port its plist recorded when the baseline was read, from the same bytes as the digest) and is compared before every check and after teardown; a difference stops the later checks | The plist can change without a reload, a restart can come back to the same pid (`runs` moves), and a before/after pair misses transient changes. Residual, documented: an unloaded agent loaded and unloaded between two comparisons with its plist untouched leaves nothing to compare |
| 12 | **A token only over a verified connection** (review round 6, replacing round 5's check-then-connect gate). One helper, `_smoke_lib.connect_verified`, used by `--agents`, the harness's token check and (review round 7) every harness answer that proves the spare is up (the collision recovery, each rollback transport; with no token); the `token-file` step's server is its own child, so it uses `connect_verified_child` (identity = the unreaped child's pid, no launchd `runs` value): (1) one `launchctl print` gives the job's pid P and `runs` R; (2) the connection is opened and nothing is sent; (3) the process holding the server end of THAT connection must be exactly P (`LOCAL_PEERPID` on a socket, `lsof` for the TCP connection from the server address to our ephemeral port; invisible, unreadable or different refuses and closes the socket); (4) a second print must still give P and R; only then is the token written, on that same socket; (5) after the exchange a fresh print must still give P and R for a PASS. Each plist is read once, in-process, for the endpoint and the token (the token file, else `PIPECAT_STT_AUTH_TOKEN`, else `KODA_STT_AUTH_TOKEN` in its environment, as the server resolves it), and the token reaches the decode child on stdin, which runs the production `TranscriptionClient.connect` (headers, `server.hello` / `session.created`) over the verified socket, swapping only its transport call; `stt_server/client.py` stays unchanged, being the surface Koda pins (closing pass). A refusal is that agent's FAIL (Decision 8) with no token sent. Residuals: a same-uid process cannot hold P; a root process is out of scope; `LOCAL_PEERPID` and lsof's view of an accepted socket are tested against child-process listeners, not yet against launchd agents | Round 5 checked ownership and then sent on a new connection, leaving a window in which a squatter could take the endpoint; checking the connection that carries the token closes it |

## Implementation Checklist

### Phase 1a: Replace plain `uv run` in the smoke recipes

**Impl files:** justfile, scripts/smoke_peercred.py
**Test files:** tests/test_justfile_recipes.py
**Test command:** `.venv/bin/python -m pytest tests/test_justfile_recipes.py -q`

- [x] Replace plain `uv run` in the `smoke-peercred` recipe and the `smoke_peercred.py` docstring with `.venv/bin/python`. This phase is its own commit, and no `just smoke` runs before it lands. Add a regression test that greps the justfile, `scripts/smoke_*.py` and `README.md` for `uv run python` without `--no-sync`.

### Phase 1b: Squat detection (issue #17, proposals 3 + 2)

**Impl files:** scripts/squat_check.py, justfile, docs/operations.md, CHANGELOG.md
**Test files:** tests/test_squat_check.py, tests/test_justfile_recipes.py
**Test command:** `.venv/bin/python -m pytest tests/test_squat_check.py tests/test_justfile_recipes.py tests/test_docs_port_caveats.py -q`

- [x] `scripts/squat_check.py` per Decisions 2-6, plus the `_squat_check` wrapper recipe, called from the `stt-list` loop (TCP plists only, reusing the pid it already read) and from `stt-status` (known backend with a plist, and the no-target path through `stt-list`), before the `exec`.
- [x] Tests (`tests/test_squat_check.py` for the helper, `tests/test_justfile_recipes.py` for the recipes), stubbing `lsof` and `launchctl`: match; mismatch; listener absent; job loaded with no pid and a listener; job not loaded; two listener pids where one matches (warns); a wildcard (`*:PORT`) listener with a different pid (warns) and one bound to another host (ignored); lsof unreadable (exit 2, quiet); socket agent (no call); no plist (no call).
- [x] Exit-code and stream tests: on a mismatch `stt-list` exits 0 and `stt-status` returns the probe's exit code for both 0 and 1; the warning is on stderr and stdout is byte-identical with and without a squat.
- [x] Add the `lsof` stub (default: no listener) to the shared fixture in `tests/test_justfile_recipes.py` in the same commit as the helper, so existing tests stay hermetic on the macos-14 runner. Add `_squat_check` to the private list in `test_just_list_exposes_public_recipes` (`smoke` joins the public list in Phase 3).
- [x] `docs/operations.md`: state the trust boundary as the decision for issue #17; document the warning, the lsof limit, and what to do (`launchctl print`, `lsof -nP -iTCP:<port>`, kill the squatter or change the port).
- [x] CHANGELOG `[0.5.1]` entry (later phases extend it).
- [x] When the PR lands, comment the decision on issue #17 (trust boundary documented, squat detection shipped, token-by-default deferred) and close it, then open a new issue "token by default for loopback TCP agents" that waits on the TTS server moving first. Done: the decision comment is posted on #17, #17 is closed, and #19 is open.

### Phase 2: launchd verification harness

**Impl files:** scripts/smoke_launchd.py, scripts/_smoke_lib.py, CHANGELOG.md
**Test files:** tests/test_smoke_launchd.py
**Test command:** `.venv/bin/python -m pytest tests/test_smoke_launchd.py tests/test_docs_port_caveats.py -q`

This phase wrote code and stubbed tests. The harness then ran live three times (2026-10-28, spare label and port 9998); the last, on the closing-pass code, had all five rows PASS, and the real agents were unchanged.

- [x] `scripts/smoke_launchd.py`: installs a spare-label agent through the real installer using `.venv/bin/python` (never `uv run`), with the `echo` backend and `PIPECAT_STT_UNLOAD_WAIT` set explicitly (the installer's "leaving socket" warning is a FAIL). Applies the Decision 7 guard. Spare port default 9998.
- [x] **Start state:** fail early, printing the bootout/rm commands, if the spare plist, a loaded spare job, the spare socket or the spare `.lock` already exists. Reading the log path from the installed plist's `StandardErrorPath` via `read_stt_plist.py` (it is `pipecat-stt-server-smoke.err`, not `<label>.err`).
- [x] **Real-agent snapshot:** per Decision 11, for every `pipecat.stt-server*` label (excluding the spare), a baseline before the guard and a comparison before every check and after teardown; FAIL on any change.
- [x] **Teardown (always, including failure and SIGINT):** the installer's `uninstall` for the spare label (bootout, wait for unload, socket removal in that order), then the spare socket and `.lock` only while the harness holds the `.lock`'s `flock` itself (anything it cannot prove its own and unheld is left and reported), then the spare plist and logs; any failed removal or leftover FAILs the teardown row.
- [x] Check: **port collision** (code and stubbed tests; passed live 2026-10-28). Bind the spare port first, install, and poll up to 30 s for at least two address-in-use lines in the err log (launchd's `ThrottleInterval` of 10 is a minimum spacing, not a period; no fixed cadence is asserted) and at least one warning from `squat_check.py`, called directly. Free the port and assert the agent comes up.
- [x] Check: **socket rollback** (code and stubbed tests; passed live 2026-10-28). Install as TCP, reinstall as socket, then reinstall as TCP. After each step assert the plist records the right transport and the server answers on the new endpoint; assert the old **socket** is gone only after the old job unloaded, and that `<socket>.lock` is left in place and unheld.
- [x] Check: **token file through launchd** (code and stubbed tests; passed live 2026-10-28). Install with a valid 0600 token file in a 0700 directory created under `$HOME` (`/tmp` and the scratchpad fail the ancestor-directory policy in `stt_server/auth.py`); assert the policy accepts it first, then `assert_token_gate`.
- [x] Tests (stubbed `launchctl`/`id`, no real launchd; the installer runs for real as in `tests/test_install_migration.py`): the guard refuses `pipecat.stt-server`, `pipecat.stt-server.parakeet`, `koda.stt-server`, an unset label, a `KODA_STT_LABEL` override and each canonical port, and accepts `pipecat.stt-server.smoke` on a free port, logging no bootout or bootstrap on refusal; a forced failing check still runs teardown (argv log shows `bootout gui/<uid>/<label>`, plist and logs gone), including on SIGINT; the real-agent snapshot comparison flags a changed pid; the harness refuses when `CI` is set or the platform is not darwin.
- [x] Extend the CHANGELOG `[0.5.1]` entry.
- [ ] Manual, documented only: persistence across logout and login.

### Phase 3: Unified smoke

**Impl files:** scripts/smoke.py, justfile, README.md, docs/operations.md, CHANGELOG.md
**Test files:** tests/test_smoke_runner.py, tests/test_justfile_recipes.py
**Test command:** `.venv/bin/python -m pytest tests/test_smoke_runner.py tests/test_justfile_recipes.py tests/test_docs_port_caveats.py -q`

- [x] `scripts/smoke.py` (run as `.venv/bin/python scripts/smoke.py`) and `just smoke [--agents] [--launchd]` (positional-arguments pattern as `stt-probe`): runs `smoke_supervised.py --backend echo`, `smoke_peercred.py`, the token-file refuse/accept checks (spare port, `echo` backend) and prints one table with PASS, FAIL or SKIPPED plus the reason, per Decision 8. `--agents` adds a decode through each installed agent from its plist endpoint (read-only; Decision 12). The repo ships no audio, so the clip is generated at run time with `say` and `afconvert` (16 kHz mono wav); the step asserts the transcript contains the spoken key word, and is SKIPPED when `say` is absent. `--launchd` adds Phase 2.
- [x] `scripts/_smoke_lib.py`: add `assert_token_gate()` (Decision 9).
- [x] Tests, with the individual smoke steps stubbed: the table renderer; the exit-code rule for every Decision 8 outcome (not requested, precondition absent, guard refused, crash, timeout, non-zero); `--agents` under stubbed `launchctl`/`id`/`uv` with rendered TCP and socket plists, asserting the argv log has only `print`/status calls, no bootout, bootstrap, kickstart or installer call, and no `uv run` without `--no-sync`.
- [x] Add `smoke` to the public-recipe list in `test_just_list_exposes_public_recipes`.
- [x] README and `docs/operations.md`: one `just smoke` section replacing the scattered smoke instructions.
- [x] Extend the CHANGELOG `[0.5.1]` entry.

### Phase 4: Release housekeeping

**Impl files:** pyproject.toml, uv.lock, CHANGELOG.md, docs/dev_plans/README.md
**Test files:** tests/test_docs_port_caveats.py
**Test command:** `.venv/bin/python -m pytest tests/test_docs_port_caveats.py -q`

- [x] Finalise the CHANGELOG `[0.5.1]` entry.
- [x] Bump `pyproject.toml` to 0.5.1 and regenerate `uv.lock` with `uv lock` (never `uv sync`, which prunes the extras). Do this **before** the final `just smoke --launchd` so the same-PID check covers it.
- [x] Mark this plan Shipped and flip its row in `docs/dev_plans/README.md`.

## Files to Modify

- `justfile` (`_squat_check` wrapper, `stt-list`, `stt-status`, `smoke`, `smoke-peercred`)
- `scripts/squat_check.py` (new), `scripts/smoke_launchd.py` (new), `scripts/smoke.py` (new), `scripts/_smoke_lib.py` (`assert_token_gate`), `scripts/smoke_peercred.py` (docstring)
- `tests/test_squat_check.py` (new), `tests/test_justfile_recipes.py`, `tests/test_smoke_*.py` (new), `tests/_audit_library_reads.py` (new: the audit hook for the harness's no-real-`~/Library` test)
- `scripts/read_stt_plist.py` (public `load` for the harness's in-process plist reads)
- `tests/test_stt_server.py` (pins the client's transports; `stt_server/client.py` itself is not modified)
- `scripts/smoke_supervised.py` (server stderr to a temp file, not an undrained pipe)
- Incidental: `scripts/smoke_test_parakeet.py` (docstring: `.venv/bin/python`, not plain `uv run`; Phase 1a), `scripts/benchmark_asr_ab.py` (docstring: `uv run --no-sync python`, Phase 3 docs pass)
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

1. ~~Does the installer accept the `echo` backend and a `.venv` without extras?~~ RESOLVED: the renderer's backend allowlist is `echo|mlx|parakeet|nemotron|kyutai` (`scripts/render_stt_plist.py`); the installer's interpreter check is only `-x "$PYTHON"` (`install_stt_agent.sh`) and `_ensure-extra` runs only from `just stt-install`, which the harness bypasses. The harness asserts at start that `stt_server` imports under `python -I` (editable install).
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

<!-- reviewed: 2026-10-30 @ 783a1925230ee85fe2600cd4803a1ffd8de4034a -->

<!-- /review-plan writes the marker line above. Everything below is the workspace: edits here do NOT invalidate the marker. -->

## Progress

- [x] Phase 1a: Replace plain `uv run` in the smoke recipes
- [x] Phase 1b: Squat detection
- [x] Phase 2: launchd verification harness
- [x] Phase 3: Unified smoke
- [x] Phase 4: Release housekeeping

## Findings

- (append findings here as work proceeds)
- 2026-10-27: probed `lsof -nP -iTCP@127.0.0.1:PORT -sTCP:LISTEN -Fp` against a `0.0.0.0` listener: no output. Unfiltered `-iTCP:PORT` shows it. Decision 2 queries by port and filters addresses in the helper.
- 2026-10-27: the user booted out all four real agents with `just stt-disable` (plists kept); re-enable with `just stt-enable <backend>` before any `--agents` or `--launchd` run.
- 2026-10-28 (review round 5): `squat_check.py` has one CLI form (answer on stdin; Decision 4); the real-agent snapshot follows Decision 11; `--agents` makes one read-only `launchctl print` per agent for its identity gate (Decision 12), superseding the Phase 3 note that it makes no launchctl call; the test-only `PIPECAT_STT_SMOKE_TEST_AGENTS_HOME` is gone (tests patch `_smoke_lib.agents_home`, `tests/_with_agents_home.py` for subprocesses). Unverified until the live run: that `launchctl print` shows a `runs =` line (since review round 6 a running job without one is UNKNOWN and refuses the run, so a machine whose launchctl omits it fails closed). The reviewer verified on this Mac that `lsof -Fp` on a live AF_UNIX path lists nothing and on the flocked `<socket>.lock` lists the holder.

## Review Waivers
- 2026-10-27 Phase 1a (conduct): the regression test scans `justfile` and `scripts/smoke_*.py` only; the plan said `README.md` too. `README.md` (about 14 lines) and `docs/operations.md` (lines 334, 340, 343, 399, 402, 405, 576) still show plain `uv run python` in foreground `serve`/`prefetch`/benchmark examples. Same extras-pruning hazard, outside Phase 1a's files. Follow-up: switch them to `uv run --no-sync python` (or `.venv/bin/python`) in Phase 3's docs pass and extend the test to `README.md` then. Also fixed `scripts/smoke_test_parakeet.py` (docstring), which matches the `smoke_*.py` glob.
- 2026-10-27 Phase 1b (conduct): Decisions 2 and 4 contradicted each other (re-check re-reads the pid vs helper gets no label). User chose to keep an optional `--label`, used only by the confirm pass; Decision 4 reworded and the marker refreshed. One fix-loop iteration: `loaded=0` with a listener warned instead of staying quiet, and `pid in listeners` hid a second non-matching listener. Mid-phase reviewer (>3 files) skipped; the later /code-review pass covers it.
- 2026-10-27 Phase 2 (conduct): code and stubbed tests only, 47 passed; the first live launchd run came later (2026-10-28, all checks passed). One fix iteration: `main` filled an unset `PIPECAT_STT_LABEL` from `--label`, defeating the Decision 7 guard; removed, both label variables must be set explicitly. Choices to review: the spare socket is fixed at `~/Library/Caches/pipecat-stt-smoke-launchd/spare.sock` (`PIPECAT_STT_SOCKET` is not read); log paths come from the installed plist's `StandardErrorPath` / `StandardOutPath` keys (read through `read_stt_plist.load`, since `read_field` has no such field), falling back to `render_stt_plist.log_basename(label)` under the log directory for a key the plist lacks or before there is a plist; `canonical_ports` asks the installer for `mlx` (its name for whisper); an extra `stt_server` import check under `python -I` runs after the start-state check; SIGINT/SIGTERM record a FAIL, run teardown and exit 1; `assert_token_gate` uses a raw websocket upgrade (401 without or with a wrong token, 101 with a good one) via `ws_handshake_status`. Unverified until the live run: two address-in-use lines within 30 s, lsof showing the harness-held port as a listener, and the strict old-job-unloaded-before-socket-removal check (may false-FAIL). Success path of `assert_token_gate` is untested (needs the real server).
- 2026-10-27 Phase 3 (conduct, autonomous): 277 passed (+1 after the regression test was extended to `README.md` and `docs/operations.md`, closing the Phase 1a follow-up; all plain `uv run python` examples there are now `uv run --no-sync python`). `assert_token_gate` already existed from Phase 2. `smoke.py` runs `token-file` and `agents` through hidden child modes (`--_token-check`, `--_decode`) of itself via `run_command`; `agents` made no launchctl call at all (superseded 2026-10-28: it now makes two read-only `launchctl print` calls per agent in the runner and one in the decode, see Decision 12); a plist whose token is only in an env var is decoded without a token; the `peercred` SKIPPED mapping relies on the string `skipped: needs a second local uid` in `smoke_peercred.py` output (unverified against real output). Live behaviour of the token-file child, the decode and the timeouts (supervised 600 s, launchd 900 s; the launchd timeout is superseded: `smoke.py` now derives it from `smoke_launchd.run_budget`, see docs/operations.md) is unverified until `just smoke` is run. (Still true for the token-file child, the `--agents` decode and the runner's timeouts; the launchd harness itself has since run live.) The test-writer's `say` stub touched the spoken text as a file in the repo root; fixed to touch the `-o` path.
- 2026-10-27 Phase 4 (conduct, done directly): pyproject and uv.lock at 0.5.1. `uv lock` also raised the lockfile revision 3 to 5 with the local uv, so the committed lockfile was kept and only the package version line changed (`uv lock --check` passes). Plan status is Implemented, not Shipped: the live `smoke_launchd.py` run and the PR are still to do; flip to Shipped when the PR merges.
- 2026-10-28 review gauntlet: 7 rounds (findings 26, 21, 14, 19, 13, 17, 14); it ended `non-converge` by operator decision after a last fix round. Live run of `scripts/smoke_launchd.py` on the spare label (port 9998): port collision (squat warning seen), socket rollback, token file and teardown passed, and the real agents were unchanged. Not run live: `--agents` and the `just smoke --launchd` wiring. A `/code-review xhigh --fix` pass then fixed seven more (teardown signal window, squat-check timeout and budget, renderer import exit, one `UNKNOWN` sentinel, drain readers, one running-identity precondition, token-check stderr), and the harness was re-run live with the same result.
- 2026-10-29 review round 8: 12 findings fixed, in `6907162` (ws_handshake_status: one deadline and a byte cap), `cb5c2e7` (verified_status re-reads pid and runs after the exchange), `77a6ff2` (the `--agents` decode refuses a connect the swap cannot carry), `9e47a6f` (squat confirm pass brackets with pid and runs), `a907f47` (import-surface docstring pinned), `cbb0947` (run_owned decides GroupNotStopped before redelivering; teardown after an installer not shown stopped), `7b358f0` (live-run docs, plan re-stamped), `2e42099` (autouse log-dir and HOME isolation), `5ac536a` (squat-CLI stub prints a runs line), `9f54b5d` (one hold helper) `cc852a6` (no runs line on either side is cannot tell; the squat_check one-file docstring) and `57a2e0c` (the recipe tests' launchctl stubs print a runs line; they had failed since `9e47a6f`). Dispositions: E1 is closed as by design: `squat_check.py` stays one file, because the recipes run it as `python3 -I`, which keeps its own directory off `sys.path`, so a sibling import would fail. J8b: the two hold-then-redeliver helpers (`smoke._signals_held`, `_smoke_lib.held_signals`) are merged into `_smoke_lib.held_signals(reraise=)`. The other three policies (`smoke._term_as_interrupt`, the token-check `on_term`, the harness's raise-once `signal_handler` / `begin_teardown`) do different jobs, so they stay separate, and the `_smoke_lib` docstring tables all four. The `python -I` plist-reader split is moot, because the harness reads plists in-process through `read_stt_plist`. The load-then-unload residual (an unloaded agent that is loaded and unloaded again between two comparisons, plist untouched) is accepted and documented in Decision 11. The squat confirm pass's None == None case (a job with no pid and no `runs =` line on both reads) now fails closed: cannot tell, not a warning. Not verified live: that launchd prints `runs =` for a loaded job with no pid.
- 2026-10-28 closing pass (three design causes): one `launchctl print` reader in `squat_check.py` (`read_print` / `parse_print`) behind `launchd_state`, `launchd_identity` and `read_job`, with the one `UNKNOWN` sentinel defined there (a running job's missing `runs =` line is UNKNOWN in all of them; every print is bounded by `LAUNCHCTL_TIMEOUT` and the harness budget sums that); the `--agents` client runs the unchanged production `TranscriptionClient.connect`, swapping only its transport call (a split of `client.py` was tried and reverted: the branch must not touch the Koda-pinned client); end-to-end stub tests for the collision success path, the rollback body and the `--agents` decode contract (success and every refusal cell, nothing sent when refused); `smoke_supervised.py` server stderr to a temp file. Not run live: `--agents` and the `just smoke --launchd` wiring. The harness then ran live a third time on the final code, all five rows PASS. Full suite 2374 passed, 23 skipped.
- 2026-10-29 `--agents` not-loaded leftovers: an installed plist whose job launchd reports not loaded is that agent's SKIPPED (checked before its plist or token file is read; nothing sent), so a machine whose servers run under another app no longer always FAILs; an unreadable answer stays FAIL, and the step is FAIL on any FAIL, else PASS on any PASS, else SKIPPED.
