# Testing and benchmarks

Start with the automated suite when changing code. Use smoke tests to exercise
real processes and optional backends; use benchmarks to compare transcription
quality and latency. For development setup, see [Contributing](contributing.md).

- [Automated checks](#automated-checks)
- [Smoke tests](#smoke-tests-just-smoke)
- [Supervised lifecycle checks](#supervised-smoke-test)
- [Whisper vs Parakeet benchmark](#ab-benchmark-whisper-vs-parakeet)

## Automated checks

From a checkout with `uv sync --inexact` completed:

```bash
uv run --no-sync ruff format
uv run --no-sync ruff check
uv run --no-sync python -m pytest -q
```

The suite uses fake backend modules, so it does not require model downloads.
Some checks need local sockets or platform-specific tools; inspect skips and
failures before treating a run as complete.

## Smoke tests (`just smoke`)

`just smoke [--agents] [--launchd]` runs the local checks in one command and
prints one table of PASS, FAIL or SKIPPED with a reason per step; the exit status
is 1 if any step FAILED (SKIPPED never fails). It runs `.venv/bin/python
scripts/smoke.py`, never `uv run` (which prunes the backend extras).

| Step | What it runs |
| --- | --- |
| `supervised` | `scripts/smoke_supervised.py --backend echo` (see below) |
| `peercred` | `scripts/smoke_peercred.py`; SKIPPED when no second uid is reachable via passwordless `sudo` (the same-uid leg still ran) |
| `token-file` | an echo server on a spare port with a token file; a request without the token (or with a wrong one) must get `401`, with it `101`; each request goes over a connection whose server end `lsof` shows is that server's own process |
| `agents` | `--agents` only: verify each installed agent's identity, then decode a generated clip; see [Agent decode checks](#agent-decode-checks). |
| `launchd` | `--launchd` only: `scripts/smoke_launchd.py` on the spare label `pipecat.stt-server.smoke` and spare port 9998 |

`--launchd` is opt-in and macOS-only (SKIPPED elsewhere and when `CI` is set).
The harness refuses to run unless both `PIPECAT_STT_LABEL` and `KODA_STT_LABEL`
name the spare label; the runner sets both itself, so do the same when calling
`scripts/smoke_launchd.py` directly. The individual scripts below still run on
their own, but `just smoke` is the entry point.

### Agent decode checks

`just smoke --agents` is read-only: it never calls the installer, `bootout`,
`bootstrap`, or `kickstart`. It decodes through installed
`pipecat.stt-server*` plists whose jobs are loaded in launchd.

For each agent, the runner:

1. Reads launchd's pid and `runs` counter. A job that is not loaded is SKIPPED
   without reading its plist or token; a loaded job that is not running fails.
2. Reads the installed plist once for its endpoint and authentication. Token
   precedence is `--auth-token-file`, then `PIPECAT_STT_AUTH_TOKEN`, then
   `KODA_STT_AUTH_TOKEN` in the plist environment. Token files must pass the
   server's own policy; a refused file makes the agent FAIL without sending it.
   The token reaches the decode process on stdin, never on its command line.
3. Opens a connection and verifies its server-side owner before sending
   anything: `LOCAL_PEERPID` for a socket or `lsof` for TCP. The owner must be
   the launchd pid, and a fresh launchd read must show the same pid and `runs`.
4. Uses the production client's `connect`, headers, and
   `server.hello` / `session.created` handshake over that verified connection.
   A clip generated with `say` and `afconvert` (16 kHz mono) must produce a
   transcript containing the spoken key word.
5. Reads launchd again after decoding. A changed pid or `runs` makes the
   result FAIL, even if transcription succeeded.

The connection swap fails closed if `connect` runs outside the client module's
namespace, uses names outside the allowlist checked against `client.py`
(including `ws_unix_connect` and `_socket_path`), or stops looking up the
global `ws_connect`. At runtime, the replacement `ws_connect` must be called
exactly once for the client's own URI.

An unreadable launchd state, an invisible or different server-side owner, or a
restart during verification fails the agent. A rejected identity check sends
no token. The checks do not cover root-owned processes. Loaded jobs without a
plist are covered by the launchd harness's snapshot, not by agent decoding.

The step is FAIL if any agent fails, otherwise PASS if any passes, otherwise
SKIPPED. No installed agents, no loaded installed jobs, or missing `say` also
produce SKIPPED. Services run by another app are outside this check.

### Timeouts and interruption

Every step has a timeout (`scripts/smoke.py`): `supervised` 600 s, `peercred`
180 s, `token-file` 90 s; in `agents`, 60 s for each `say` / `afconvert` call
and 180 s for each decode. The `launchd` step's timeout is the harness's own
worst case, teardown included (`smoke_launchd.run_budget`, summed from the
bounds it waits on, for every real agent its snapshot reads: installed plists
plus loaded jobs with no plist, from `launchctl list`; about 31 minutes with
none, plus about 2 minutes per agent). A
timed-out step gets SIGTERM, and Ctrl-C (or a SIGINT or SIGTERM sent to the
runner) sends the running step SIGINT; the step then has 180 s to clean up (the
`launchd` step: the harness's whole teardown, `smoke_launchd.teardown_budget`,
about 3 minutes plus 25 s per agent). Once the step has exited, or that grace ran out, its
whole process group is killed, before the step's own process is reaped, so the
kill can only reach that step's group. A further signal to the runner during
that grace is held until the step has stopped and then stops the run: no later
step starts, the interrupted step is FAIL, the rest are SKIPPED, and the table
still prints. A step that crashes is a FAIL row and the run goes on to the next
step.

### Launchd harness isolation

Inside the launchd harness each installer call runs in its own process group,
with a 120 s timeout: a SIGINT or SIGTERM during an install kills the
installer's whole group before teardown starts, so teardown never runs beside a
half-finished install. An installer group that cannot be shown stopped (its
kill failed, or its leader was not reaped) is a failure that stops the
remaining checks, never a result, also when a signal arrived during it. That
installer may still bootstrap the spare or rewrite its plist, so the teardown
then changes nothing (no uninstall, nothing removed): its row FAILs with the
spare's launchd state, what is left behind and the cleanup commands
(shell-quoted) to run once no installer process is left. The same holds when
the teardown's own uninstall cannot be shown stopped. The teardown's uninstall
is not interrupted.
The harness runs everything (the installer included) with a scrubbed
environment: `HOME`, `USER`, `TMPDIR`, the spare label and log-dir variables
and the fixed `PATH=/usr/bin:/bin:/usr/sbin:/sbin`; `BASH_ENV`, `ENV`,
exported shell functions, `PYTHON*`, `DYLD_*` and `LD_*` never reach it, and
bash is `/bin/bash`. An agent
gets 60 s to answer after each install, and the collision check polls its log
for 30 s.

### Checking real agents remain unchanged

The harness's proof that it left the real agents alone is a snapshot of each
one (every installed `pipecat.stt-server*` plist and every loaded such job):
from one `launchctl print` of the job, its loaded state, pid, `runs` counter
(which moves on a restart even when the pid is reused) and the endpoint the job
is serving; a digest of its plist (the plist is only the intended endpoint and
can change without a reload) and the port recorded in those same bytes (parsed
as the server parses `--port`, so `+9998` or `09998` is 9998; a value the
server would refuse is unreadable and refuses the run); and the
pid holding the job's endpoint (a socket job's `<socket>.lock`). A job with no
pid serves nothing; anything it cannot read on a running job refuses the run
before the first install, a running job whose `launchctl print` has no `runs =`
line included (the live run is what confirms launchctl prints one). The
baseline is taken before the guard (which refuses every port a real job serves
or a real plist recorded, both taken from the baseline) and compared before every check and after teardown; a difference stops
the remaining checks. A real agent that was not loaded, then loaded and unloaded
again between two of those comparisons with its plist untouched, leaves nothing
to compare. A real agent that restarts on its own during the run (a crash loop)
fails the comparison too: fix it before the run.

Every `launchctl print` answer here, in `smoke.py --agents` and in the
recipes' squat check is read by one parser, in `scripts/squat_check.py`
(`read_print` / `parse_print`): only "could not find service" is "not
loaded", any other failure is "cannot tell", and a running job whose answer
has no `runs =` line is unreadable everywhere (never "no restart counter").

The token check sends its throwaway token (and a wrong one) only over
connections whose server end `lsof` shows is the spare job's pid, with the same
pid and `runs` before and after; it waits for the agent with requests that carry
no token. The collision check's "agent up" and every rollback transport are
proved the same way: their no-token request goes over a connection whose server
end is the spare job, so a listener that is not the spare cannot pass them.

### Teardown and spare resources

Teardown removes the spare socket and its `.lock` only while it holds that
`.lock`'s `flock` itself, so no live server can own them; anything it cannot prove
is its own and unheld is left and reported. A removal that fails, or anything
still there afterwards, makes the `teardown` row FAIL. On a FAIL the harness
prints one `FAIL: launchd check failed: <check>: <detail>` line per failed row,
and `just smoke` shows those in its `launchd` row.

Only one `smoke_launchd.py` run at a time: it takes an exclusive lock on
`~/Library/Caches/pipecat-stt-smoke-launchd.lock` (in the account home, not
`$HOME`) before touching anything, and a second run is refused. The lock is
released when the run exits; the file stays.

The launchd harness uses spare port 9998 by default; when that port is busy, run
it on another spare port with `scripts/smoke_launchd.py --port <port>` (the guard
still refuses a canonical port, a port recorded by an installed or loaded agent,
and a port that is already listening). Its socket-rollback check always uses
the fixed spare socket `$HOME/Library/Caches/pipecat-stt-smoke-launchd/spare.sock`
(`PIPECAT_STT_SOCKET` is not read), so it can never be a real agent's socket.

Before it installs anything the harness refuses leftover spare state, and
prints the cleanup commands (shell-quoted, safe to paste): a loaded spare job, a
spare plist under `$HOME` or under the account home's `~/Library/LaunchAgents`
(the label is one launchd job per user, whatever `HOME` says), the spare socket
or its `.lock`, and spare logs. An installer `warning:` line fails the install
or the teardown unless it is one of three known harmless ones (socket mode
ignoring TCP settings, a dropped token for a socket agent, a retried bootstrap).

### Coverage boundary
 The harness, collision, rollback, token-file,
and teardown paths have live coverage. The `--agents` child is fully exercised
against stubbed `launchctl`/`lsof` and a fake agent, but still needs a live run
with a loaded LaunchAgent. A service managed by another app is SKIPPED rather
than treated as a launchd-backed agent. Historical run evidence lives in the
[0.5.1 development plan](dev_plans/20261027-feature-stt-squat-detection-unified-smoke.md).

## Supervised smoke test

`scripts/smoke_supervised.py` drives `serve --supervised` the way a menubar or
other supervising app does: it spawns real server processes and checks the JSON
`ready`/`error` events, stdin-EOF and SIGTERM stops (including during warm-up),
a parent that dies, a second instance on a live socket, a stale socket after
`kill -9`, concurrent clients, and a real transcription. It is opt-in (not in CI
or pytest) and needs the backend's extra installed; run it with that
environment's interpreter:

```bash
.venv/bin/python scripts/smoke_supervised.py --backend echo
.venv/bin/python scripts/smoke_supervised.py --backend mlx \
    --model mlx-community/whisper-large-v3-turbo-q4
```

It exits 0 only if every scenario passes, and 2 (running nothing) when the
model is not cached: `prefetch` it first.

## A/B benchmark: Whisper vs Parakeet

With both agents installed and live, `scripts/benchmark_asr_ab.py`
replays a corpus of utterances through **both** servers and reports
per-utterance Word Error Rate (WER) + decode latency, plus aggregates. It is
a pure V1 client — no protocol surface added — and a one-off operator tool
(no REST counterpart, not a CI gate).

```bash
# 0.5.0 default agents are loopback TCP, so pass the URIs. --whisper-uri /
# --parakeet-uri override the socket flags.
uv run --no-sync python scripts/benchmark_asr_ab.py --corpus path/to/corpus \
    --whisper-uri ws://127.0.0.1:9565 --parakeet-uri ws://127.0.0.1:9765

# Socket agents: with no endpoint flags the script defaults to
# ~/Library/Caches/pipecat-stt/stt.sock and .../parakeet.sock
# (override with --whisper-socket / --parakeet-socket).
uv run --no-sync python scripts/benchmark_asr_ab.py --corpus path/to/corpus

# Write a full JSON report alongside the console summary.
uv run --no-sync python scripts/benchmark_asr_ab.py --corpus path/to/corpus \
    --whisper-uri ws://127.0.0.1:9565 --parakeet-uri ws://127.0.0.1:9765 \
    --json-out benchmarks/results/asr_ab.json
```

The corpus is a directory of `<stem>.wav` (16 kHz mono PCM16) + `<stem>.txt`
reference-transcript pairs, named explicitly on the command line. The
benchmark **fails fast** if only one of the two endpoints answers — it never
silently benchmarks a single ASR. The corpus is never baked into the script;
`docs/benchmarks` / `~/koda-data` JSON corpora carry real names and
financials, so the script refuses a `--corpus` under those roots unless
`--allow-pii-corpus` is passed. Use a synthetic or consented-recording corpus
and keep it outside the repo.
