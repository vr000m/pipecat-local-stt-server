# Task: Port-squat detection, one smoke entry point and launchd verification (0.5.1)

**Status**: Planning
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
3. **One smoke entry point.** `just smoke` runs every smoke check (supervised echo, peer-cred, token-file policy, optional real-agent decode and launchd checks) and prints one PASS/FAIL table.

Not in scope: token by default (issue #17 proposal 1), which breaks the same-as-TTS default and needs the TTS server to move first. Login persistence needs a logout and cannot be automated; it stays a documented manual check.

## Context

- 0.5.0 shipped loopback TCP agents with no token by default. Another local process can connect, and a process that binds the port first can answer in place of the agent. `docs/operations.md` already states there is no peer-uid check.
- Verified at 0.5.0: token-file refusals and acceptance on a spare port without launchd; `smoke_supervised.py --backend echo` (12 scenarios); same-uid peer-cred; a decode through each real agent. Not verified against real launchd: the port-collision loop, socket rollback, login persistence.
- The installer takes `PIPECAT_STT_LABEL`, `PIPECAT_STT_PORT` and `PIPECAT_STT_SOCKET`, so a spare label such as `pipecat.stt-server.smoke` on a spare port exercises real launchd without touching `pipecat.stt-server*`. A fixer once ran the installer against the real label and broke the Whisper agent; nothing in this plan may do that. Every launchd check must refuse to run against a canonical label.
- `justfile` `smoke-peercred` and `scripts/smoke_peercred.py` use plain `uv run`, which prunes the optional backend extras and sends LaunchAgents into a crash loop (see the `uv run` extras memory). They must run from `.venv/bin/python` or `uv run --no-sync`.
- `docs/dev_plans/README.md` still lists the 0.5.0 plan as "in review — PR #16 open"; PR #16 is merged.

## Decisions

| # | Decision | Why |
|---|---|---|
| 1 | Squat detection **warns, never fails**: `stt-status` and `stt-list` print a `warning:` line and keep their exit code | A status command that starts failing would break scripts; the finding is advisory |
| 2 | Compare the **listener PID** (`lsof -nP -iTCP:<port> -sTCP:LISTEN -Fp`) with the **launchd PID** (`launchctl print gui/<uid>/<label>`, `pid = N`) | Both are available without privileges for the same user; other users' processes may not show in `lsof`, so a missing listener PID is "cannot tell", not a squat |
| 3 | Squat check covers TCP agents only | A socket agent keeps the peer-uid check and its `.lock`; there is no port to squat |
| 4 | launchd checks are **opt-in** (`just smoke --launchd`), spare label and port only | They bootstrap and bootout real launchd jobs; never run them in CI or by default |
| 5 | Token by default stays out; revisit with the TTS server | Breaks every consumer's no-config default |

## Phases

### Phase 1: Squat detection (issue #17, proposals 3 + 2)

- [ ] `just _squat_check LABEL HOST PORT`: helper that prints nothing on match, a `warning:` on mismatch, nothing (with a debug note) when either PID cannot be read. Called from `stt-list` and `stt-status` for TCP plists only; the endpoint comes from the installed plist via the existing keyed reader.
- [ ] Tests in `tests/test_justfile_recipes.py` with stubbed `lsof` and `launchctl`: match, mismatch, listener absent, launchd PID absent (job not running), socket agent (no check), unreadable `lsof`.
- [ ] `docs/operations.md`: state the trust boundary as the decision for issue #17, document the warning and what to do (`launchctl print`, `lsof -nP -iTCP:<port>`, kill the squatter or change the port).
- [ ] CHANGELOG `[0.5.1]` entry; comment the decision on issue #17 and keep it open only for proposal 1 under a narrower title, or close it.

### Phase 2: launchd verification harness

- [ ] `scripts/smoke_launchd.py`: installs a spare-label agent through the real installer using `.venv/bin/python` (never `uv run`), with the `echo` backend; refuses any label that starts with `pipecat.stt-server` unless it ends in `.smoke`; always boots out and removes its own plist and logs on exit, including on failure.
- [ ] Check: **port collision.** Bind the spare port first, install, and assert launchd retries about every 10 s (`ThrottleInterval`), that `<label>.err` names the address in use, and that the squat warning from Phase 1 fires for the occupier. Free the port and assert the agent comes up.
- [ ] Check: **socket rollback.** Install as TCP, reinstall as socket, then reinstall as TCP, asserting after each step that the plist records the right transport, the old socket and `.lock` are gone only after the old job unloaded, and the server answers on the new endpoint.
- [ ] Check: **token file through launchd.** Install with a valid 0600 token file in a private directory, assert 401 without a token and success with it.
- [ ] Manual, documented only: persistence across logout and login.

### Phase 3: Unified smoke

- [ ] `scripts/smoke.py` (run as `.venv/bin/python scripts/smoke.py`) and `just smoke [--agents] [--launchd]`: runs `smoke_supervised.py --backend echo`, `smoke_peercred.py`, the token-file refuse/accept checks (spare port, `echo` backend) and prints one table with PASS, FAIL or SKIPPED plus the reason. `--agents` adds a decode through each installed agent from its plist endpoint (read-only). `--launchd` adds Phase 2. Exit non-zero only on FAIL.
- [ ] Replace plain `uv run` in `smoke-peercred` and the `smoke_peercred.py` docstring with the venv interpreter.
- [ ] Tests: the table renderer and the exit-code rule, with the individual smoke steps stubbed.
- [ ] README and `docs/operations.md`: one `just smoke` section replacing the scattered smoke instructions.

### Phase 4: Release housekeeping

- [ ] Mark `20261025-feature-stt-agents-loopback-tcp.md` shipped (0.5.0, PR #16) and update its row in `docs/dev_plans/README.md`; add this plan's row.
- [ ] Bump `pyproject.toml` and `uv.lock` to 0.5.1 after the CHANGELOG entry is final.

## Files to Modify

- `justfile` (squat helper, `stt-list`, `stt-status`, `smoke`, `smoke-peercred`)
- `scripts/smoke_launchd.py` (new), `scripts/smoke.py` (new), `scripts/smoke_peercred.py` (docstring)
- `tests/test_justfile_recipes.py`, `tests/test_smoke_*.py` (new)
- `docs/operations.md`, `README.md`, `CHANGELOG.md`, `docs/dev_plans/README.md`, `docs/dev_plans/20261025-feature-stt-agents-loopback-tcp.md`
- `pyproject.toml`, `uv.lock`

## Verification

- Unit tests with stubbed `lsof` and `launchctl` pass; full suite and `ruff format --check`, `ruff check`, `shellcheck` clean.
- `just smoke` all PASS on this machine; `just smoke --agents` decodes through all four running agents; `just smoke --launchd` passes with the four real agents running before, during and after (same PIDs, same listeners).
- A deliberate squat (a process bound to a spare agent's port before launchd starts it) is reported by `just stt-status`.

## Risks

- `launchctl print` output is not a stable API; the PID parse must tolerate a missing `pid =` line (job loaded but not running) and fail to "cannot tell", never to a false squat warning.
- `lsof` cannot see other users' sockets; a squatter run by another user may be invisible. Document this limit; it is the reason proposal 2 is advisory.
- The launchd harness touches real launchd. The spare-label guard and unconditional cleanup are the safety; a test must assert the guard refuses canonical labels.

## Open Questions

1. ~~Does the installer accept the `echo` backend?~~ RESOLVED: the renderer's allowlist is `echo|mlx|parakeet|nemotron|kyutai` (`scripts/render_stt_plist.py:52`) and the installer checks `PIPECAT_STT_BACKEND` against the same names, so the harness uses `echo` and needs no model. Still to confirm in Phase 2: the installer's default interpreter check accepts a `.venv` without any backend extra.
2. Close issue #17 after Phase 1, or keep it open for proposal 1? (Needs the TTS server's answer.)
