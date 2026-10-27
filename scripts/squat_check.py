#!/usr/bin/env python3
"""Warn when a TCP LaunchAgent's port is held by a process that is not the agent.

Usage:
    squat_check.py --host HOST --port PORT --loaded {0,1} [--pid PID] [--label LABEL]

Read-only: it runs ``lsof`` (and, for the confirm pass, ``launchctl print``),
never starts, stops or signals anything. Stdout is always empty. Each warning is
one block on stderr, every line starting ``squat-check: warning:``.

Exit status: 0 quiet, 3 warned, 2 cannot tell (``lsof`` missing or failed; a
one-line note on stderr, no warning).

The invariant (Decision 5): the PID listening on the agent's port must equal the
PID launchd reports for the agent. Two warnings break it:

* (b) the job is running with pid P but some other pid (not P) listens on the
  port: a squatter holds the port. The message names the port, the listener
  pid(s), the launchd pid and the listener command(s).
* (c) the job has no pid (not loaded, or loaded but not running) and something
  listens on the port: ``port N held by pid X; label not running``.

A finding is only reported after a 1-second re-check, so a restart in flight (the
launchd pid or the listener changing between the two reads) never warns. The
re-check re-reads the listeners and, when ``--label`` is given, the pid with
``launchctl print gui/<uid>/<label>``; without ``--label`` it reuses ``--pid``.

Visibility limit (Decision 6): ``lsof`` run as the user lists only the user's own
processes' sockets. A listener owned by another uid is invisible, so this check
can miss a squatter running as another user; it cannot prove the port is free,
only that no visible foreign process holds it. Listeners are filtered to HOST or
a wildcard bind (``*``, ``[::]``); ``lsof``'s ``@HOST`` filter is not used.

Stdlib only: it runs under the system python3 (``python3 -I``).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

_PREFIX = "squat-check: warning:"
_WILDCARDS = ("*", "[::]")


def _lsof(port, run):
    """Return {pid: command} of listeners on PORT, or None when lsof can't tell."""
    try:
        res = run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpn"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # lsof exits 1 with no output when nothing matches; anything else with no
    # output is a failure.
    if res.returncode not in (0, 1) or (res.returncode == 1 and res.stderr.strip()):
        return None
    return res.stdout


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


def _matches(addr, host):
    # addr is "HOST:PORT", "*:PORT" or "[::]:PORT" (-n -P: numeric only).
    base = addr.rsplit(":", 1)[0]
    return base == host or base in _WILDCARDS


def _commands(pids, run):
    """Map each pid to its command name via ``lsof -Fc`` (best effort)."""
    cmds = {}
    for pid in pids:
        try:
            res = run(
                ["lsof", "-nP", "-p", pid, "-Fc"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            name = next((ln[1:] for ln in res.stdout.splitlines() if ln.startswith("c")), None)
        except (OSError, subprocess.SubprocessError):
            name = None
        cmds[pid] = name or "?"
    return cmds


def _launchd_pid(label, run):
    """Re-read the job's pid with read-only ``launchctl print``; None if none."""
    try:
        res = run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    m = re.search(r"^\s*pid = (\d+)", res.stdout, re.MULTILINE)
    return m.group(1) if m else None


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
    sleep(1)
    if label:
        pid = _launchd_pid(label, run) or ""
    out = _lsof(port, run)
    if out is None:
        print("squat-check: cannot run lsof; skipping port check", file=sys.stderr)
        return 2
    listeners = _parse(out, host)
    kind = _finding(listeners, pid)
    if kind is None:
        return 0

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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", required=True)
    ap.add_argument("--loaded", required=True, choices=("0", "1"))
    ap.add_argument("--pid", default="")
    ap.add_argument("--label", default=None)
    args = ap.parse_args(argv)
    return check(
        args.host,
        args.port,
        args.pid,
        args.loaded == "1",
        args.label or None,
    )


if __name__ == "__main__":
    sys.exit(main())
