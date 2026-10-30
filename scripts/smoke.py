#!/usr/bin/env python3
"""One-command smoke runner: every local check in one table.

    .venv/bin/python scripts/smoke.py [--agents] [--launchd]

Run it as ``just smoke [--agents] [--launchd]``. Steps, in order:

* ``supervised``: ``smoke_supervised.py --backend echo``.
* ``peercred``: ``smoke_peercred.py`` (SKIPPED when there is no second uid, the
  usual dev case, where the same-uid leg still ran; or when the platform is not
  macOS or Linux, where nothing ran).
* ``token-file``: a spare-port echo server with a token file; a request with no
  token or a wrong one must get 401 and the right one 101 (``assert_token_gate``).
* ``agents`` (``--agents``): decode a short generated clip through every installed
  ``pipecat.stt-server*`` agent from its plist endpoint. The plists are read from
  ``_smoke_lib.agents_home()`` (the account home, the same one the launchd
  harness snapshots; ``$HOME`` does not move it). Installed plists only: a loaded
  job with no plist is not decoded (the launchd harness's real-agent snapshot,
  which also reads ``launchctl list``, does cover it). Each plist is read once,
  in-process (``read_stt_plist.load``), and every endpoint field and the token
  come from that one read; the token is resolved as the server resolves it
  (``--auth-token-file``, else ``PIPECAT_STT_AUTH_TOKEN``, else
  ``KODA_STT_AUTH_TOKEN`` in the plist's ``EnvironmentVariables``) and handed to
  the decode child on stdin, never on argv. The token goes only over a
  connection whose server end is the agent's own launchd job
  (``_smoke_lib.connect_verified``): one read-only ``launchctl print`` gives its
  pid P and spawn counter ``runs`` R; the child opens the connection, sends
  nothing until the process holding the server end of THAT connection is P
  (``LOCAL_PEERPID`` for a socket, ``lsof`` for TCP) and a second print still
  gives (P, R), then decodes over that same socket. After the decode a fresh
  print must still give (P, R) for a PASS. Otherwise the agent's row is FAIL,
  and when the check refused, nothing was sent. An installed plist whose job
  is not loaded in launchd (servers run by another app) is SKIPPED, not
  decoded, before its plist is read; an answer that cannot be read is FAIL,
  never SKIPPED. The step is FAIL on any FAIL, else PASS on any PASS, else
  SKIPPED. Strictly read-only: it never calls bootout, bootstrap, kickstart or
  the installer.
* ``launchd`` (``--launchd``): ``smoke_launchd.py`` on the spare label and port.
  macOS only, never in CI, and it needs both ``PIPECAT_STT_LABEL`` and
  ``KODA_STT_LABEL`` set to the spare label, which this runner does explicitly.

Each step is PASS, FAIL or SKIPPED with a reason. The exit status is 1 if any
step FAILED, else 0 (SKIPPED never fails). A step that crashes is a FAIL row and
the next step still runs. Stdlib only; every step runs in a subprocess through
``run_command`` with ``sys.executable`` (never ``uv run``, which would prune the
backend extras).

Each step runs in its own session (``_smoke_lib.run_owned``). On a step timeout
the step gets SIGTERM, on Ctrl-C (or a SIGINT or SIGTERM sent to this runner)
SIGINT; either way it has ``TEARDOWN_GRACE`` to exit. A signal to the runner
during that grace is held until the step has stopped, then stops the run. When
the step has exited (or the grace ran out) its whole process group is SIGKILLed
before the step is reaped, so nothing it started outlives it and the kill can
never reach a reused pid. An interrupted run marks the current step FAIL and
the later steps SKIPPED (not run), prints the table and exits 1.
"""

import argparse
import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _smoke_lib  # noqa: E402
import read_stt_plist  # noqa: E402

PASS, FAIL, SKIPPED = _smoke_lib.PASS, _smoke_lib.FAIL, _smoke_lib.SKIPPED
SPARE_LABEL = _smoke_lib.SPARE_LABEL
CLIP_TEXT = "Hello, this is a smoke test of the speech to text server."
CLIP_KEYWORD = "hello"

SUPERVISED_TIMEOUT = 600.0
PEERCRED_TIMEOUT = 180.0
TOKEN_TIMEOUT = 90.0
CLIP_TIMEOUT = 60.0
DECODE_TIMEOUT = 180.0
# The decode child's exit status when connect_verified refused the endpoint:
# nothing (no token) was sent.
DECODE_REFUSED = 3
# The launchd step's timeout and grace are not numbers here: they are the
# harness's own budget (smoke_launchd.run_budget / teardown_budget, summed
# from the bounds it waits on, for the installed agents), so they cannot drift.
# After a step's timeout (SIGTERM) or Ctrl-C (SIGINT) the child gets this long to
# run its own cleanup before its process group is SIGKILLed.
TEARDOWN_GRACE = 180.0

# smoke_peercred.py's own skip lines (it exits 0 on both); it prints these
# same constants, so a rewording cannot turn a SKIP into a PASS.
PEERCRED_NO_UID_MARK = _smoke_lib.PEERCRED_NO_UID_MARK
PEERCRED_PLATFORM_MARK = _smoke_lib.PEERCRED_PLATFORM_MARK


@dataclass
class StepResult:
    name: str
    outcome: str
    detail: str = ""


def render_table(results: list[StepResult]) -> str:
    """One table: name, outcome, detail; every step present."""
    rows = [("STEP", "OUTCOME", "DETAIL")]
    for r in results:
        rows.append((r.name, r.outcome, " ".join(r.detail.split())))
    w0 = max(len(r[0]) for r in rows)
    w1 = max(len(r[1]) for r in rows)
    lines = [f"{a:<{w0}}  {b:<{w1}}  {c}".rstrip() for a, b, c in rows]
    lines.insert(1, f"{'-' * w0}  {'-' * w1}  {'-' * 6}")
    return "\n".join(lines)


def exit_code(results: list[StepResult]) -> int:
    return 1 if any(r.outcome == FAIL for r in results) else 0


def run_command(
    cmd: list[str],
    timeout: float,
    env: dict | None = None,
    *,
    input: bytes | None = None,
    grace: float | None = None,
) -> subprocess.CompletedProcess:
    """The only place this module spawns a subprocess for a step.

    The step runs in its own session, so a terminal Ctrl-C reaches only this
    runner, through ``_smoke_lib.run_owned``'s one lifecycle: SIGINT and SIGTERM
    to the runner are held while the step runs. On TIMEOUT the step gets
    SIGTERM, on the first held signal SIGINT; either way it has
    ``TEARDOWN_GRACE`` to run its own cleanup, and a second signal cannot cut
    that short. Then, however the step ended (its own exit included), its whole
    process group is SIGKILLed while the step is still unreaped, so the signal
    can only reach the step's own group, never a reused pid or group. Afterwards
    a held signal is raised as KeyboardInterrupt (``main`` turns a SIGTERM into
    one too) and a timeout as ``subprocess.TimeoutExpired``. INPUT goes to the
    step's stdin (a token travels this way, never on argv). GRACE replaces
    ``TEARDOWN_GRACE`` for a step with its own teardown budget (``launchd``).
    """
    res, timed_out = _smoke_lib.run_owned(
        cmd,
        timeout,
        env=env,
        cwd=str(REPO),
        grace=TEARDOWN_GRACE if grace is None else grace,
        input=input,
    )
    if timed_out:
        raise subprocess.TimeoutExpired(cmd, timeout, output=res.stdout, stderr=res.stderr)
    return res


@contextlib.contextmanager
def _term_as_interrupt():
    """Make a SIGTERM to this runner raise KeyboardInterrupt, like Ctrl-C, so the
    active step is stopped through ``run_command`` instead of being orphaned."""

    def handler(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")

    old = signal.signal(signal.SIGTERM, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


def _tail(proc: subprocess.CompletedProcess) -> str:
    text = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""


def _execute(cmd, timeout, env=None, input=None, grace=None):
    """Run one command; return (outcome, detail, proc-or-None)."""
    kw = {k: v for k, v in (("input", input), ("grace", grace)) if v is not None}
    try:
        proc = run_command(cmd, timeout, env, **kw)
    except subprocess.TimeoutExpired:
        return FAIL, f"timed out after {timeout:g}s", None
    except Exception as exc:  # noqa: BLE001 - a crash is a FAIL, never a traceback
        return FAIL, f"crashed: {type(exc).__name__}: {exc}", None
    if proc.returncode != 0:
        detail = f"exit {proc.returncode}"
        tail = _tail(proc)
        return FAIL, f"{detail}: {tail}" if tail else detail, proc
    return PASS, "", proc


def run_step(
    name: str,
    cmd: list[str],
    timeout: float,
    *,
    requested: bool = True,
    skip_reason: str | None = None,
    env: dict | None = None,
) -> StepResult:
    if not requested:
        return StepResult(name, SKIPPED, "not requested")
    if skip_reason:
        return StepResult(name, SKIPPED, skip_reason)
    outcome, detail, _ = _execute(cmd, timeout, env)
    return StepResult(name, outcome, detail)


# --- step builders ----------------------------------------------------------
def supervised_cmd() -> list[str]:
    return [sys.executable, str(SCRIPTS / "smoke_supervised.py"), "--backend", "echo"]


def peercred_cmd() -> list[str]:
    return [sys.executable, str(SCRIPTS / "smoke_peercred.py")]


def token_file_cmd() -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), "--_token-check"]


def launchd_cmd() -> list[str]:
    return [sys.executable, str(SCRIPTS / "smoke_launchd.py")]


def launchd_env(base: dict | None = None) -> dict:
    env = dict(os.environ if base is None else base)
    env["PIPECAT_STT_LABEL"] = SPARE_LABEL
    env["KODA_STT_LABEL"] = SPARE_LABEL
    return env


def launchd_budget() -> tuple[float, float]:
    """(timeout, grace) for the launchd step: the harness's own worst-case run
    with its teardown, then its teardown again, for the installed agents it
    will snapshot (``smoke_launchd.run_budget`` / ``teardown_budget``), so a
    SIGTERM can never SIGKILL the harness mid-uninstall."""
    import smoke_launchd

    n = len(agent_plists())
    return smoke_launchd.run_budget(n), smoke_launchd.teardown_budget(n)


def launchd_skip_reason() -> str | None:
    return _smoke_lib.launchd_skip_reason(os.environ, sys.platform)


def supervised_step() -> StepResult:
    return run_step("supervised", supervised_cmd(), SUPERVISED_TIMEOUT)


def token_file_step() -> StepResult:
    return run_step("token-file", token_file_cmd(), TOKEN_TIMEOUT)


def launchd_step(requested: bool) -> StepResult:
    """The harness exits 0 with ``LAUNCHD_SKIP_MARK`` + reason as its first
    stdout line when a precondition is absent: that is SKIPPED, never PASS. The
    runner also checks the same preconditions itself and skips without spawning.
    On a FAIL the detail is the harness's ``LAUNCHD_FAIL_MARK`` lines (the
    checks that failed); without any (a crash) it is the output's last line."""
    if not requested:
        return StepResult("launchd", SKIPPED, "not requested")
    reason = launchd_skip_reason()
    if reason:
        return StepResult("launchd", SKIPPED, reason)
    timeout, grace = launchd_budget()
    outcome, detail, proc = _execute(launchd_cmd(), timeout, launchd_env(), grace=grace)
    first = ((proc.stdout or "") if proc is not None else "").lstrip().split("\n", 1)[0]
    if outcome == PASS and first.startswith(_smoke_lib.LAUNCHD_SKIP_MARK):
        return StepResult("launchd", SKIPPED, first[len(_smoke_lib.LAUNCHD_SKIP_MARK) :].strip())
    failures = _smoke_lib.launchd_failures(proc.stdout if proc is not None else "")
    if outcome == FAIL and failures:
        # The harness's failure channel names the checks that failed; the last
        # output line is only the last table row.
        detail = f"exit {proc.returncode}: " + "; ".join(failures)
    return StepResult("launchd", outcome, detail)


def peercred_step() -> StepResult:
    outcome, detail, proc = _execute(peercred_cmd(), PEERCRED_TIMEOUT)
    out = (proc.stdout or "") if proc is not None else ""
    if outcome == PASS and PEERCRED_PLATFORM_MARK in out:
        return StepResult(
            "peercred", SKIPPED, f"peer-cred smoke needs macOS or Linux, not {sys.platform}"
        )
    if outcome == PASS and PEERCRED_NO_UID_MARK in out:
        return StepResult(
            "peercred",
            SKIPPED,
            "cross-uid leg unavailable: no second uid via passwordless sudo (same-uid leg passed)",
        )
    return StepResult("peercred", outcome, detail)


def agent_plists() -> list[Path]:
    """Installed real-agent plists, from the home the launchd harness snapshots.
    Plists only: a loaded job with no plist has no endpoint to decode through
    here (the launchd harness's snapshot does cover such jobs)."""
    return _smoke_lib.agent_plists(_smoke_lib.agents_home())


ENDPOINT_FIELDS = ("socket-path", "host", "port", "auth-token-file")


def read_endpoint(plist: Path) -> tuple[dict[str, str] | None, str | None, str]:
    """(endpoint, token, error): every endpoint field AND the bearer token from
    ONE in-process read of PLIST (``read_stt_plist.load``), so they always come
    from the same version of the file even while an installer replaces it.

    The token is resolved the way the server resolves it: the file named by
    ``--auth-token-file``, read through the server's own policy
    (``stt_server.auth.load_token_file``: absolute path, ``O_NOFOLLOW`` and
    ``O_NONBLOCK`` so a symlink or a FIFO is refused at once, a regular file
    this user owns with no group/other access, the token's characters
    checked); a file the server would refuse is an error, never "no token",
    and nothing is sent to that agent. Else the plist's legacy environment token
    (``read_stt_plist.env_token``: ``PIPECAT_STT_AUTH_TOKEN``, then
    ``KODA_STT_AUTH_TOKEN``); None when the agent requires none."""
    try:
        data = read_stt_plist.load(str(plist))
    except read_stt_plist.MalformedPlist as exc:
        return None, None, f"cannot read {plist.name}: {exc}"
    if not data:
        return None, None, f"{plist.name} is gone"
    # Exactly as recorded: read_field adds nothing.
    endpoint = {f: read_stt_plist.read_field(data, f) for f in ENDPOINT_FIELDS}
    if "--port" in read_stt_plist.read_flags(data):
        # The port as the server parses it (int(): "+9998" is 9998); one it
        # would refuse is an error, never "no port".
        port = read_stt_plist.parse_port(endpoint["port"])
        if port is None:
            return None, None, f"cannot parse its --port {endpoint['port']!r}"
        endpoint["port"] = str(port)
    if not endpoint["socket-path"] and not (endpoint["host"] and endpoint["port"]):
        return None, None, "plist has no endpoint"
    token_file = endpoint["auth-token-file"]
    if token_file:
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        from stt_server.auth import load_token_file
        from stt_server.errors import ConfigError

        try:
            _resolved, token = load_token_file(token_file, uid=os.getuid())
        except ConfigError as exc:  # its message never carries the token
            return None, None, f"its token file is refused by the server's policy: {exc}"
        return endpoint, token, ""
    return endpoint, read_stt_plist.env_token(data) or None, ""


# An installed plist whose job launchd does not have loaded is a leftover (the
# servers may be run by another app, not launchd): SKIPPED, never decoded.
NOT_LOADED_DETAIL = (
    "installed, not loaded in launchd (not decoded; servers run by another app "
    "are not covered by --agents)"
)


def job_identity(state) -> tuple[tuple[str, str] | None, str]:
    """((pid, runs), "") of a running launchd job from STATE, the answer of ONE
    read-only ``launchctl print`` (``_smoke_lib.launchd_identity``), else
    (None, why). None (the answer cannot be read) is "cannot tell", never
    "not loaded".

    A NOT LOADED job never gets here: ``agents_step`` reports it SKIPPED
    (``NOT_LOADED_DETAIL``) before its plist is read, the one rule for that
    state, so a not-loaded STATE is a caller's bug and raises ValueError
    (the step's crash row, a FAIL) rather than a second, contradictory FAIL
    verdict for it."""
    if state is None:
        return None, "cannot read its launchd state (launchctl print failed)"
    loaded, pid, runs = state
    if not loaded:
        raise ValueError("a not-loaded job is SKIPPED by agents_step, never given to job_identity")
    if not pid:
        return None, "loaded but not running (launchd has no pid)"
    if not _smoke_lib.runs_readable(runs):
        return None, f"launchctl print shows pid {pid} but no `runs =` line"
    return (pid, runs), ""


def decode_cmd(label: str, endpoint: dict, wav: str, pid: str, runs: str) -> list[str]:
    """The decode child's argv: the endpoint and the identity it must verify
    (label, pid, runs). Never the token: that goes on stdin."""
    cmd = [sys.executable, str(Path(__file__).resolve()), "--_decode", "--wav", wav]
    cmd += ["--keyword", CLIP_KEYWORD, "--label", label, "--pid", pid, "--runs", runs]
    if endpoint.get("socket-path"):
        cmd += ["--socket-path", endpoint["socket-path"]]
    else:
        cmd += ["--host", endpoint["host"], "--port", endpoint["port"]]
    return cmd


def make_clip(directory: str) -> tuple[str | None, str]:
    """Generate the clip with say + afconvert. Returns (wav path, error)."""
    aiff = os.path.join(directory, "clip.aiff")
    wav = os.path.join(directory, "clip.wav")
    steps = [
        ["say", "-o", aiff, CLIP_TEXT],
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", aiff, wav],
    ]
    for cmd in steps:
        outcome, detail, _ = _execute(cmd, CLIP_TIMEOUT)
        if outcome != PASS:
            return None, f"{cmd[0]} {detail}"
    return wav, ""


def agents_step(requested: bool) -> StepResult:
    if not requested:
        return StepResult("agents", SKIPPED, "not requested")
    plists = agent_plists()
    if not plists:
        return StepResult("agents", SKIPPED, "no installed pipecat.stt-server* agents")
    if shutil.which("say") is None:
        return StepResult("agents", SKIPPED, "macOS `say` is not available to generate the clip")
    parts: list[str] = []
    failed = passed = False
    with tempfile.TemporaryDirectory(prefix="stt-smoke-agents.") as tmp:
        wav, err = make_clip(tmp)
        if wav is None:
            return StepResult("agents", FAIL, err)
        for plist in plists:
            label = plist.name[: -len(".plist")]
            # The identity read comes first: a job that is NOT LOADED (a readable
            # answer, loaded False) is SKIPPED before its plist or token file is
            # read. An unreadable answer (None) is "cannot tell" and goes on to
            # the FAIL below, never SKIPPED; a loaded job's plist and token-file
            # errors are reported exactly as before.
            state = _smoke_lib.launchd_identity(label)
            if state is not None and not state[0]:
                parts.append(f"{label}: {SKIPPED} {NOT_LOADED_DETAIL}")
                continue
            endpoint, token, bad = read_endpoint(plist)
            ident = None
            if endpoint is not None:
                ident, why = job_identity(state)
                if ident is None:
                    bad = f"{why}; not decoded, no token sent"
            if bad:
                failed = True
                parts.append(f"{label}: FAIL {bad}")
                continue
            pid, runs = ident
            outcome, detail, proc = _execute(
                decode_cmd(label, endpoint, wav, pid, runs),
                DECODE_TIMEOUT,
                input=token.encode() if token else None,
            )
            if outcome == FAIL and proc is not None and proc.returncode == DECODE_REFUSED:
                detail = f"{detail}; not decoded, no token sent"
            # The PASS must come from the agent: a FRESH launchctl print after
            # the decode still shows the same pid and runs (a restart, even
            # one back to the same pid, or an unload during it is a FAIL).
            if outcome == PASS:
                changed = _smoke_lib.confirm_identity_unchanged(
                    lambda: _smoke_lib.launchd_pid_runs(label),  # noqa: B023 - called here
                    pid,
                    runs,
                    "the launchd job",
                    "during the decode",
                )
                if changed:
                    outcome, detail = FAIL, changed
            failed = failed or outcome != PASS
            passed = passed or outcome == PASS
            parts.append(f"{label}: {outcome}" + (f" {detail}" if detail else ""))
    # Any FAIL fails the step; else a PASS passes it; else every installed agent
    # was SKIPPED (not loaded) and so is the step.
    step = FAIL if failed else PASS if passed else SKIPPED
    return StepResult("agents", step, "; ".join(parts))


def build_and_run(agents: bool, launchd: bool) -> list[StepResult]:
    """Run every step in order, one row each (Decision 8). A step that raises is
    its FAIL row and the next step still runs; an interrupt (Ctrl-C, or SIGTERM
    under ``main``) is the current step's FAIL row and the rest are not run."""
    steps = [
        ("supervised", supervised_step),
        ("peercred", peercred_step),
        ("token-file", token_file_step),
        ("agents", lambda: agents_step(agents)),
        ("launchd", lambda: launchd_step(launchd)),
    ]
    results: list[StepResult] = []
    for i, (name, step) in enumerate(steps):
        try:
            results.append(step())
        except KeyboardInterrupt as exc:
            detail = f"interrupted ({exc})" if str(exc) else "interrupted"
            not_stopped = _smoke_lib.group_not_stopped(exc)
            if not_stopped is not None:  # the step's group could not be shown stopped
                detail = f"{detail}; {not_stopped}"
            results.append(StepResult(name, FAIL, detail))
            results += [
                StepResult(n, SKIPPED, "not run: the run was interrupted")
                for n, _s in steps[i + 1 :]
            ]
            break
        except Exception as exc:  # noqa: BLE001 - a crash is a FAIL row, never a traceback
            results.append(StepResult(name, FAIL, f"crashed: {type(exc).__name__}: {exc}"))
    return results


# --- child roles (run as subprocesses of the steps above) --------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _token_check() -> int:
    """Start an echo server with a token file on a spare port; run the gate."""
    sys.path.insert(0, str(REPO))
    from _smoke_lib import assert_token_gate

    port = _free_port()

    # The runner's timeout sends SIGTERM; its default action would skip the
    # finally below and leave the server and the token directory behind.
    def on_term(signum, _frame):
        raise SystemExit(128 + signum)

    old_term = signal.signal(signal.SIGTERM, on_term)
    tmp = None
    server = None
    # The server's stderr goes to an anonymous temp file, never a pipe nobody
    # drains after start-up (more than a pipe buffer of log lines during the
    # gate would block the server on write); read back only for an early exit.
    errlog = tempfile.TemporaryFile()
    try:
        # The token file must pass stt_server.auth's policy (_smoke_lib's one
        # fixture, which the launchd harness also checks against load_token_file).
        tmp, token_path = _smoke_lib.write_policy_token_file(Path.home(), "stt-smoke-token.")
        token_file = str(token_path)
        server = subprocess.Popen(
            [
                sys.executable, "-m", "stt_server", "serve", "--backend", "echo",
                "--host", "127.0.0.1", "--port", str(port),
                "--auth-token-file", token_file,
            ],
            cwd=str(REPO),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=errlog,
        )  # fmt: skip
        deadline = time.time() + 30
        while time.time() < deadline:
            if server.poll() is not None:
                errlog.seek(0)
                err = errlog.read().decode(errors="replace")[-300:]
                print(f"server exited early ({server.returncode}): {err}", file=sys.stderr)
                return 1
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.2)
        else:
            print(f"server did not listen on 127.0.0.1:{port} within 30s", file=sys.stderr)
            return 1

        # The token goes only over a connection whose server end is the server
        # this child started (lsof): its own unreaped child, so its pid is the
        # identity (connect_verified_child), the same contract as the decode.
        def connect():
            return _smoke_lib.connect_verified_child(("tcp", "127.0.0.1", str(port)), server)

        try:
            assert_token_gate("127.0.0.1", port, token_file, connect=connect)
        except (AssertionError, OSError, _smoke_lib.EndpointRefused) as exc:
            print(f"token gate: {exc}", file=sys.stderr)
            return 1
        print(f"token gate OK on 127.0.0.1:{port}")
        return 0
    finally:
        # Held, then dropped: this child is exiting anyway, and a second
        # SIGTERM must not cut the cleanup short (_smoke_lib's policy table).
        with _smoke_lib.held_signals():
            if server is not None and server.poll() is None:
                server.terminate()
                try:
                    server.wait(10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
            errlog.close()
        signal.signal(signal.SIGTERM, old_term)


# Every name stt_server.client.TranscriptionClient.connect uses (its globals,
# attributes and methods), as client.py has it: byte-identical to main, the
# surface Koda pins by SHA. The swap in _verified_client is sound only while
# connect opens its uri-branch websocket through the module-global ws_connect
# and nothing else, so before anything is sent the decode requires connect to
# run in that module's globals, to look ws_connect up there, and to use no
# name outside this set (a direct websockets call or a new helper would add
# one): otherwise it refuses, until this set is re-checked against client.py.
_CONNECT_NAMES = frozenset(
    {
        "_auth_token", "_uri", "ws_connect", "P", "MAX_APPEND_BYTES", "_ws",
        "_socket_path", "ws_unix_connect", "format_host_for_uri", "_host", "_port",
        "_recv_json", "get", "EVT_SERVER_HELLO", "RuntimeError", "EVT_SESSION_CREATED",
    }
)  # fmt: skip


def _connect_swap_problem(client_mod) -> str | None:
    """Why the verified-socket swap cannot be trusted to carry
    ``TranscriptionClient.connect``'s connection (see ``_CONNECT_NAMES``), or
    None when it can."""
    import dis
    import types

    fn = client_mod.TranscriptionClient.connect
    code = getattr(fn, "__code__", None)
    if code is None or getattr(fn, "__globals__", None) is not vars(client_mod):
        return "TranscriptionClient.connect does not run in stt_server.client's globals"
    names: set[str] = set()
    stack = [code]
    while stack:
        co = stack.pop()
        names.update(co.co_names)
        stack += [k for k in co.co_consts if isinstance(k, types.CodeType)]
    new = sorted(names - _CONNECT_NAMES)
    if new:
        return (
            "TranscriptionClient.connect uses names the verified-socket swap was not "
            f"checked against: {', '.join(new)}"
        )
    if "ws_connect" not in {
        i.argval for i in dis.get_instructions(code) if i.opname == "LOAD_GLOBAL"
    }:
        return "TranscriptionClient.connect no longer looks up the module-global ws_connect"
    return None


def _verified_client(sock, uri: str, token: str | None):
    """A ``TranscriptionClient`` that speaks over SOCK, a connection
    ``connect_verified`` already checked, instead of opening its own.

    It runs the production ``TranscriptionClient.connect`` itself (its
    headers, the bearer token, and its ``server.hello`` / ``session.created``
    handshake), replacing ONLY that method's transport call: for the call, the
    client module's ``ws_connect`` is one that opens the websocket on SOCK.
    ``stt_server/client.py`` stays unchanged (it is the surface Koda pins by
    SHA). The client is built with ``uri=URI``, so the uri branch, the one that
    calls ``ws_connect``, is the one connect takes; a call for any other uri,
    or any other transport, is refused rather than opened elsewhere. A
    production connect the swap cannot be shown to carry
    (``_connect_swap_problem``) is refused before anything is sent, and a
    connect that returns without calling the swap exactly once is an error."""
    import stt_server.client as client_mod

    class VerifiedSocketClient(client_mod.TranscriptionClient):
        async def connect(self) -> dict:
            problem = _connect_swap_problem(client_mod)
            if problem:
                raise RuntimeError(f"refusing to connect, nothing sent: {problem}")
            real = client_mod.ws_connect
            calls = []

            def over_verified_sock(target, **kw):
                if target != uri or "sock" in kw:
                    raise RuntimeError(
                        f"refusing a transport other than the verified socket: {target}"
                    )
                calls.append(target)
                return real(target, sock=sock, **kw)

            client_mod.ws_connect = over_verified_sock
            try:
                hello = await super().connect()
            finally:
                client_mod.ws_connect = real
            if len(calls) != 1:
                raise RuntimeError(
                    f"connect opened the verified socket {len(calls)} times, not once"
                )
            return hello

    return VerifiedSocketClient(uri=uri, auth_token=token)


def _decode(args: argparse.Namespace) -> int:
    """Decode a wav through one endpoint; require the key word in the transcript.

    The token arrives on stdin. Before anything is sent, the connection's
    server end must be the launchd job (ARGS.pid, ARGS.runs) of ARGS.label
    (``_smoke_lib.connect_verified``), and first of all the production
    connect must be one the verified-socket swap carries
    (``_connect_swap_problem``); a refusal exits ``DECODE_REFUSED``."""
    import asyncio
    import wave

    sys.path.insert(0, str(REPO))
    from stt_server.client import format_host_for_uri
    from stt_server.protocol import EVT_TRANSCRIPT_COMPLETED

    token = sys.stdin.read().strip() or None
    import stt_server.client as client_mod

    problem = _connect_swap_problem(client_mod)
    if problem:
        print(f"refused: {problem}", file=sys.stderr)
        return DECODE_REFUSED
    with wave.open(args.wav) as w:
        data = w.readframes(w.getnframes())
    if args.socket_path:
        endpoint, uri = ("unix", args.socket_path), "ws://localhost/"
    else:
        endpoint = ("tcp", args.host, args.port)
        uri = f"ws://{format_host_for_uri(args.host)}:{args.port}/"

    def identity():
        return _smoke_lib.launchd_pid_runs(args.label)

    try:
        sock = _smoke_lib.connect_verified(endpoint, args.pid, args.runs, identity)
    except _smoke_lib.EndpointRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return DECODE_REFUSED

    async def go() -> str:
        async with _verified_client(sock, uri, token) as c:
            await c.update_session(turn_detection=None)
            for i in range(0, len(data), 3200):
                await c.send_audio(data[i : i + 3200])
            await c.commit()
            async for ev in c.events():
                if ev.get("type") == EVT_TRANSCRIPT_COMPLETED:
                    return ev.get("transcript", "")
        return ""

    try:
        text = asyncio.run(asyncio.wait_for(go(), 150))
    except Exception as exc:  # noqa: BLE001
        print(f"decode failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.keyword.lower() not in text.lower():
        print(f"transcript lacks {args.keyword!r}: {text!r}", file=sys.stderr)
        return 1
    print(f"transcript: {text!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--_token-check"]:
        return _token_check()
    if argv[:1] == ["--_decode"]:
        ap = argparse.ArgumentParser()
        ap.add_argument("--_decode", action="store_true")
        ap.add_argument("--wav", required=True)
        ap.add_argument("--keyword", required=True)
        ap.add_argument("--label", required=True)
        ap.add_argument("--pid", required=True)
        ap.add_argument("--runs", required=True)
        ap.add_argument("--socket-path")
        ap.add_argument("--host")
        ap.add_argument("--port")
        return _decode(ap.parse_args(argv))

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--agents",
        action="store_true",
        help="decode a clip through each installed agent (read-only)",
    )
    ap.add_argument(
        "--launchd", action="store_true", help="run the launchd harness on the spare label (macOS)"
    )
    args = ap.parse_args(argv)
    with _term_as_interrupt():
        results = build_and_run(args.agents, args.launchd)
    print(render_table(results))
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
