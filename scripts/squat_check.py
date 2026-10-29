#!/usr/bin/env python3
"""Warn when a TCP LaunchAgent's port is held by a process that is not the agent.

Usage:
    launchctl print gui/<uid>/<label> 2>&1 | squat_check.py --host HOST --port PORT --launchctl-rc RC [--label LABEL]

The one CLI form, used by the recipes (``stt-list``, ``stt-status``) and by the
launchd harness alike: RC is the exit status of the caller's own ``launchctl
print gui/<uid>/<label>`` and its output (stdout and stderr) arrives on STDIN,
never on argv. That output carries the job's ``environment`` block, where a plist
from before 0.5.0 keeps its bearer token (``KODA_STT_AUTH_TOKEN`` /
``PIPECAT_STT_AUTH_TOKEN``), and every local user can read a process's argv
with ``ps``. The answer is classified by ``classify_print``, the same rule
``launchd_state`` uses. An answer that is neither loaded nor launchctl's "could
not find service" is "cannot tell" (exit 2, a note on stderr), never "not
loaded".

Read-only: it runs ``lsof`` (and, for the confirm pass, ``launchctl print``),
never starts, stops or signals anything. Stdout is always empty. Each warning is
one block on stderr, every line starting ``squat-check: warning:``.

Exit status: 0 quiet, 3 warned, 2 cannot tell (``lsof`` missing or failed, the
caller's ``launchctl print`` answer unreadable, or the confirm pass could not
re-read the launchd job or saw its pid or ``runs`` change; a one-line note on
stderr, no warning).

The invariant (Decision 5): the PID listening on the agent's port must equal the
PID launchd reports for the agent. Two warnings break it:

* (b) the job is running with pid P but some other pid (not P) listens on the
  port: a squatter holds the port. The message names the port, the listener
  pid(s), the launchd pid and the listener command(s).
* (c) the job is loaded but has no pid (not running) and something listens on
  the port: ``port N held by pid X; label not running``. A job that is not
  loaded at all is quiet (Decision 2): a disabled agent holds no claim on its
  port.

A finding is only reported after a 1-second re-check, so a restart in flight (the
launchd pid or the listener changing between the two reads) never warns. The
re-check re-reads the listeners and, when ``--label`` is given, the pid with
``launchctl print gui/<uid>/<label>``; without ``--label`` it reuses the pid
from the caller's answer.
When that re-read fails, a finding that persists is "cannot tell" (exit 2), never a
warning resting on the first pass's pid. With ``--label`` the pid is read once more
after the second listener read; a pid or ``runs`` counter that changed across that
listener read (the job restarted while lsof ran, or started, listened and exited
with no pid on either side), a running job whose answer has no ``runs =`` line,
or a read that failed is also "cannot tell", so a warning always compares the
listeners with a job that held for the whole read.

Visibility limit (Decision 6): ``lsof`` run as the user lists only the user's own
processes' sockets. A listener owned by another uid is invisible, so this check
can miss a squatter running as another user; it cannot prove the port is free,
only that no visible foreign process holds it. Listeners are filtered to HOST or
a wildcard bind (``*``, ``[::]``, ``0.0.0.0``); ``lsof``'s ``@HOST`` filter is not
used. A wildcard HOST (``0.0.0.0``, ``::``, ``*``) keeps every listener on the
port, ``localhost`` matches ``127.0.0.1`` and ``::1``, and IPv6 brackets are
ignored on both sides.

The confirm pass re-reads the job's state with ``--label``: a job that became
unloaded within the second is quiet again (Decision 2, case a).

``lsof`` runs with ``-w`` (warnings off). Its "nothing matched" answer is exit 1
with no stderr, or with only ``lsof: WARNING:`` lines; any other exit 1 message,
or an exit of 2 or more, is "cannot tell".

Import surface (besides the CLI), exactly what each importer uses:

* ``scripts/smoke_launchd.py``: ``read_print`` and ``parse_print``
  (``read_job``, ``squat_warns``), ``endpoint_holders``, ``launchd_state``
  (with the ``uid`` keyword), ``listener_pids``, ``lsof_pids``,
  ``parse_job_argv`` (``job_endpoint``), the bounds
  ``LSOF_TIMEOUT`` / ``LAUNCHCTL_TIMEOUT`` (its time budget sums them) and
  ``cli_bound`` (the CLI's own worst case, which its squat-check timeout and
  budget derive from).
* ``scripts/_smoke_lib.py``: ``UNKNOWN`` (re-exported as
  ``_smoke_lib.UNKNOWN``), ``read_job_print`` (``launchd_identity``, which
  ``smoke.py`` uses) and ``lsof_output`` (``tcp_server_end_pids``, which
  ``connect_verified`` uses).

So the harness, the runner and the recipes share one listener definition and
ONE ``launchctl print`` reader (``read_print`` + ``parse_print``, see "the one
``launchctl print`` reader" below); ``launchd_state``, ``launchd_identity``
and ``read_job`` are thin wrappers over it. This module never imports
``_smoke_lib`` (it runs alone under ``python3 -I``).

Stdlib only: it runs under the system python3 (``python3 -I``).
"""

from __future__ import annotations

import argparse
import collections
import os
import re
import subprocess
import sys
import time

_PREFIX = "squat-check: warning:"
# Normalised (brackets stripped) addresses that mean "every interface".
_WILDCARDS = frozenset({"*", "::", "0.0.0.0", ""})
_LOCALHOST = frozenset({"127.0.0.1", "::1"})
# launchctl print's answer for a label that is not loaded (exit 113).
_NOT_LOADED_RC = 113
_NOT_LOADED_RE = re.compile(r"could not find service", re.IGNORECASE)
_LSOF_WARNING = "lsof: WARNING"
# Each lsof / launchctl call's own bound (the launchd harness's budget adds them).
LSOF_TIMEOUT = 10
LAUNCHCTL_TIMEOUT = 10
CONFIRM_SLEEP = 1  # the confirm pass's wait before it re-reads


def cli_bound(listeners):
    """The CLI's worst-case wall time (``main`` -> ``check``, interpreter
    start-up excluded) with LISTENERS pids listening on the port: the first
    lsof, the confirm sleep, the ``--label`` pid re-read, the second lsof, the
    bracketing pid read, then one ``lsof -Fc`` per listener for its command.
    Every wait ``check`` makes is one of these bounds (tests pin the sum to a
    run of the slowest path)."""
    return 2 * LSOF_TIMEOUT + CONFIRM_SLEEP + 2 * LAUNCHCTL_TIMEOUT + listeners * LSOF_TIMEOUT


def _only_warnings(stderr):
    """True when STDERR is empty or holds only ``lsof: WARNING:`` lines."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    return all(ln.startswith(_LSOF_WARNING) for ln in lines)


def lsof_output(args, run=subprocess.run):
    """``lsof -nP -w <args>`` stdout, or None when lsof can't tell.

    ``args`` must carry its ``-F`` fields before any ``--`` (after ``--`` lsof
    reads every word as a file name)."""
    try:
        res = run(
            ["lsof", "-nP", "-w", *args], capture_output=True, text=True, timeout=LSOF_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # lsof exits 1 when nothing matches, with no stderr or (should -w not cover
    # one) only warnings; exit 1 with any other message (a missing file, a bad
    # option) or an exit of 2 or more is a failure.
    if res.returncode == 0 or (res.returncode == 1 and _only_warnings(res.stderr)):
        return res.stdout or ""
    return None


def _lsof(port, run):
    """``-Fpn`` listener output for PORT, or None when lsof can't tell."""
    return lsof_output([f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpn"], run)


def lsof_pids(args, run=subprocess.run):
    """Sorted pids from ``lsof -nP -Fp <args>``; [] when none, None when lsof can't tell."""
    out = lsof_output(["-Fp", *args], run)
    if out is None:
        return None
    return sorted({ln[1:] for ln in out.splitlines() if ln.startswith("p")})


def listener_pids(port, host=None, run=subprocess.run):
    """Pids listening on PORT whose address matches HOST (None: any address);
    None when lsof can't tell."""
    out = _lsof(port, run)
    return None if out is None else _parse(out, host)


def endpoint_holders(host, port, socket_path, run=subprocess.run):
    """Pids serving an agent endpoint; [] when none, None when lsof can't tell.

    TCP (no SOCKET_PATH): the listeners on PORT whose address matches HOST
    (``listener_pids``). A Unix socket: the holders of ``<SOCKET_PATH>.lock``,
    the file the server opens and ``flock``s for its whole life (``lsof`` on
    the AF_UNIX path itself lists nothing on macOS). A ``.lock`` that does not
    exist has no holder ([]); one that vanishes during the read is None."""
    if socket_path:
        lock = f"{socket_path}.lock"
        if not os.path.lexists(lock):
            return []
        return lsof_pids(["--", lock], run)
    return listener_pids(port, host, run)


def _parse(out, host):
    """Parse ``-Fpn`` output into the pids that listen on HOST or a wildcard."""
    pids = []
    cur = None
    addrs = []

    def flush():
        if cur is not None and any(_matches(a, host) for a in addrs):
            if cur not in pids:
                pids.append(cur)

    for line in out.splitlines():
        if line.startswith("p"):
            flush()
            cur = line[1:]
            addrs = []
        elif line.startswith("n"):
            addrs.append(line[1:])
    flush()
    return pids


def _norm(addr):
    return addr.strip().strip("[]").lower()


def _matches(addr, host):
    # addr is "HOST:PORT", "*:PORT" or "[::1]:PORT" (-n -P: numeric only). A
    # listener blocks the agent's bind when either side is a wildcard or both
    # name the same address; None means any address.
    base = _norm(addr.rsplit(":", 1)[0])
    if host is None:
        return True
    want = _norm(str(host))
    if want in _WILDCARDS or base in _WILDCARDS:
        return True
    if want == "localhost":
        return base in _LOCALHOST
    return base == want


def _commands(pids, run):
    """Map each pid to its command name via ``lsof -Fc`` (best effort)."""
    cmds = {}
    for pid in pids:
        out = lsof_output(["-p", pid, "-Fc"], run)
        name = next((ln[1:] for ln in (out or "").splitlines() if ln.startswith("c")), None)
        cmds[pid] = name or "?"
    return cmds


# --- the one `launchctl print` reader -------------------------------------------
#
# Every reader of a job's `launchctl print` answer goes through read_print (the
# call) and parse_print (the rule): this module's own re-check
# (read_job_print), the recipes' CLI form (classify_print, the loaded/pid half
# of parse_print), _smoke_lib.launchd_identity (smoke.py --agents) and
# smoke_launchd.read_job / Harness.squat_warns. One rule each for:
#
#   loaded   exit 0. Not loaded is only launchctl's own "could not find
#            service" (exit 113, or that message); any other answer, or a call
#            that could not run, is "cannot tell" (None), never "not loaded".
#   pid      the `pid = N` line; None when absent (loaded, not running).
#   runs     the `runs = N` line (launchd's spawn counter, which moves on every
#            restart even when a pid is reused). A RUNNING job (a pid) whose
#            answer has no such line is UNKNOWN, never None: None == None
#            would let a restart compare as unchanged. A job with no pid and no
#            line is None; a job that is not loaded has none.
#   argv     the job's `arguments = { ... }` block, one word per line; () when
#            absent or not loaded. The endpoint is derived from it by the
#            caller (smoke_launchd.job_endpoint parses it the way the server
#            parses its flags; that parser is not stdlib-only).
#
# The text is the answer's stdout and stderr together (the CLI's `2>&1`).

# A value a reader could not read (a running job's runs counter, a snapshot
# field): never equal to anything, and never an identity to verify against.
# The one definition; _smoke_lib.UNKNOWN and smoke_launchd.UNKNOWN are this
# same object.
UNKNOWN = "unknown"

_PID_RE = re.compile(r"^\s*pid = (\d+)", re.MULTILINE)
_RUNS_RE = re.compile(r"^\s*runs = (\d+)", re.MULTILINE)
_ARGS_RE = re.compile(r"^\s*arguments = \{\s*\n(.*?)^\s*\}", re.MULTILINE | re.DOTALL)


# One parsed `launchctl print` answer.
JobPrint = collections.namedtuple("JobPrint", "loaded pid runs argv")


def parse_launchd_pid(text):
    """The ``pid = N`` of a ``launchctl print`` body, or None."""
    m = _PID_RE.search(text or "")
    return m.group(1) if m else None


def parse_runs(text):
    """The ``runs = N`` of a ``launchctl print`` body, or None (the raw line;
    ``parse_print`` turns a running job's missing line into UNKNOWN)."""
    m = _RUNS_RE.search(text or "")
    return m.group(1) if m else None


def parse_job_argv(text):
    """The words of a ``launchctl print`` body's ``arguments`` block; ()."""
    m = _ARGS_RE.search(text or "")
    return tuple(ln.strip() for ln in m.group(1).splitlines() if ln.strip()) if m else ()


def parse_print(rc, text):
    """JobPrint(loaded, pid, runs, argv) from one ``launchctl print`` answer:
    its exit status RC and its output TEXT (stdout and stderr); None when it
    can't tell. The rules are in the block above."""
    if rc == 0:
        pid = parse_launchd_pid(text)
        runs = parse_runs(text)
        if runs is None and pid:
            runs = UNKNOWN
        return JobPrint(True, pid, runs, parse_job_argv(text))
    if rc == _NOT_LOADED_RC or _NOT_LOADED_RE.search(text or ""):
        return JobPrint(False, None, None, ())
    return None


def classify_print(rc, text):
    """(loaded, pid) from one ``launchctl print`` answer (``parse_print``'s
    loaded/pid); None when it can't tell. The recipes' rule."""
    job = parse_print(rc, text)
    return None if job is None else (job.loaded, job.pid)


def read_print(label, run=subprocess.run, uid=None):
    """(rc, text) of one read-only ``launchctl print gui/<uid>/LABEL`` (TEXT is
    stdout and stderr), bounded by LAUNCHCTL_TIMEOUT; None when it could not
    run. The one call every reader makes."""
    uid = os.getuid() if uid is None else uid
    try:
        res = run(
            ["launchctl", "print", f"gui/{uid}/{label}"],
            capture_output=True,
            text=True,
            timeout=LAUNCHCTL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return res.returncode, (res.stdout or "") + "\n" + (res.stderr or "")


def read_job_print(label, run=subprocess.run, uid=None):
    """``parse_print`` of ``read_print``: a JobPrint, or None when it can't tell."""
    got = read_print(label, run, uid)
    return None if got is None else parse_print(*got)


def launchd_state(label, run=subprocess.run, uid=None):
    """(loaded, pid) from one read-only ``launchctl print`` (``read_job_print``);
    None when it can't tell."""
    job = read_job_print(label, run, uid)
    return None if job is None else (job.loaded, job.pid)


def _finding(listeners, pid):
    """Classify one observation: None (fine), ('b'|'c')."""
    if not listeners:
        return None
    if pid:
        return "b" if any(p != pid for p in listeners) else None
    return "c"


def check(host, port, pid, loaded, label=None, *, run=subprocess.run, sleep=time.sleep):
    """Return the exit code (0 quiet, 3 warned, 2 cannot tell); warn on stderr."""
    pid = str(pid) if pid else ""
    if not loaded:
        return 0  # Decision 2: a job that is not loaded is quiet.
    out = _lsof(port, run)
    if out is None:
        print("squat-check: cannot run lsof; skipping port check", file=sys.stderr)
        return 2
    listeners = _parse(out, host)
    kind = _finding(listeners, pid)
    if kind is None:
        return 0

    # Confirm: a restart in flight changes the pid or the listener within a second.
    sleep(CONFIRM_SLEEP)
    pid_current = True
    runs = None
    if label:
        job = read_job_print(label, run)
        if job is None:
            # Can't tell: the caller's pid may be stale, so it can only clear a
            # finding (a listener that now matches it), never confirm one.
            pid_current = False
        else:
            if not job.loaded:
                return 0  # unloaded within the second: quiet, as in the first pass
            pid, runs = job.pid or "", job.runs
    out = _lsof(port, run)
    if out is None:
        print("squat-check: cannot run lsof; skipping port check", file=sys.stderr)
        return 2
    listeners = _parse(out, host)
    kind = _finding(listeners, pid)
    if kind is None:
        return 0
    if not pid_current:
        print("squat-check: cannot re-read the launchd pid; skipping port check", file=sys.stderr)
        return 2
    if label:
        # Bracket the listener read: a job that restarted between the pid read
        # above and lsof would have its new pid compared with the old one, and
        # a job that started, listened and exited in between (no pid on either
        # side) would have its own listener reported. The spawn counter `runs`
        # moves on every start, so it must hold too; an UNKNOWN one (a running
        # job with no `runs =` line) never compares as unchanged.
        after = read_job_print(label, run)
        if after is not None and not after.loaded:
            return 0  # unloaded during the read: quiet, as above
        if after is None or (after.pid or "") != pid or runs == UNKNOWN or after.runs != runs:
            print(
                "squat-check: the launchd job changed (pid or runs) during the re-check; "
                "skipping port check",
                file=sys.stderr,
            )
            return 2

    cmds = _commands(listeners, run)
    if kind == "b":
        listeners = [p for p in listeners if p != pid]
    held = ", ".join(f"{p} ({cmds[p]})" for p in listeners)
    if kind == "b":
        print(
            f"{_PREFIX} port {port} is held by pid {held}, "
            f"not the launchd job (pid {pid})\n"
            f"{_PREFIX} another process may be squatting the port; "
            f"see docs/operations.md",
            file=sys.stderr,
        )
    else:
        print(
            f"{_PREFIX} port {port} held by pid {held}; label not running\n"
            f"{_PREFIX} another process may be squatting the port; "
            f"see docs/operations.md",
            file=sys.stderr,
        )
    return 3


def main(argv=None, stdin=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", required=True)
    ap.add_argument(
        "--launchctl-rc",
        type=int,
        required=True,
        help="exit status of the caller's launchctl print; its output is read from stdin",
    )
    ap.add_argument("--label", default=None)
    args = ap.parse_args(argv)
    text = (sys.stdin if stdin is None else stdin).read()
    got = classify_print(args.launchctl_rc, text)
    if got is None:
        print(
            "squat-check: cannot read the launchd state "
            f"(launchctl print exit {args.launchctl_rc}); skipping port check",
            file=sys.stderr,
        )
        return 2
    loaded, pid = got
    return check(args.host, args.port, pid, loaded, args.label or None)


if __name__ == "__main__":
    sys.exit(main())
