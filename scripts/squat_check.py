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
re-read the launchd pid or saw it change; a one-line note on stderr, no warning).

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
``launchctl print gui/<uid>/<label>``; without ``--label`` it reuses the pid
from the caller's answer.
When that re-read fails, a finding that persists is "cannot tell" (exit 2), never a
warning resting on the first pass's pid. With ``--label`` the pid is read once more
after the second listener read; a pid that changed across that listener read (the
job restarted while lsof ran) or a read that failed is also "cannot tell", so a
warning always compares the listeners with a pid that held for the whole read.

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

Import surface: besides the CLI, ``scripts/smoke_launchd.py`` imports
``lsof_output``, ``lsof_pids``, ``listener_pids``, ``endpoint_holders``,
``parse_launchd_pid``, ``classify_print`` and ``launchd_state`` (with the
``uid`` keyword), and ``scripts/_smoke_lib.py`` (``launchd_identity``,
``connect_verified``, which ``smoke.py`` uses) imports ``classify_print`` and
``lsof_output``, so the harness, the runner and the recipes share one listener
and one launchd-state definition. Everything else
here is private to this module.

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
# Normalised (brackets stripped) addresses that mean "every interface".
_WILDCARDS = frozenset({"*", "::", "0.0.0.0", ""})
_LOCALHOST = frozenset({"127.0.0.1", "::1"})
# launchctl print's answer for a label that is not loaded (exit 113).
_NOT_LOADED_RC = 113
_NOT_LOADED_RE = re.compile(r"could not find service", re.IGNORECASE)
_LSOF_WARNING = "lsof: WARNING"


def _only_warnings(stderr):
    """True when STDERR is empty or holds only ``lsof: WARNING:`` lines."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    return all(ln.startswith(_LSOF_WARNING) for ln in lines)


def lsof_output(args, run=subprocess.run):
    """``lsof -nP -w <args>`` stdout, or None when lsof can't tell.

    ``args`` must carry its ``-F`` fields before any ``--`` (after ``--`` lsof
    reads every word as a file name)."""
    try:
        res = run(["lsof", "-nP", "-w", *args], capture_output=True, text=True, timeout=10)
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
        try:
            res = run(
                ["lsof", "-nP", "-w", "-p", pid, "-Fc"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            name = next((ln[1:] for ln in res.stdout.splitlines() if ln.startswith("c")), None)
        except (OSError, subprocess.SubprocessError):
            name = None
        cmds[pid] = name or "?"
    return cmds


def parse_launchd_pid(text):
    """The ``pid = N`` of a ``launchctl print`` body, or None."""
    m = re.search(r"^\s*pid = (\d+)", text or "", re.MULTILINE)
    return m.group(1) if m else None


def classify_print(rc, text):
    """(loaded, pid) from one ``launchctl print`` answer: its exit status RC and
    its output TEXT (stdout and stderr). None when it can't tell.

    Not loaded is only launchctl's own "could not find service" answer (exit
    113, or that message); any other failure is "can't tell", never "not
    loaded". The one rule for the recipes' read and this module's own."""
    if rc == 0:
        return True, parse_launchd_pid(text)
    if rc == _NOT_LOADED_RC or _NOT_LOADED_RE.search(text or ""):
        return False, None
    return None


def launchd_state(label, run=subprocess.run, uid=None):
    """(loaded, pid) from read-only ``launchctl print`` (``classify_print``);
    None when it can't tell."""
    uid = os.getuid() if uid is None else uid
    try:
        res = run(
            ["launchctl", "print", f"gui/{uid}/{label}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return classify_print(res.returncode, (res.stdout or "") + "\n" + (res.stderr or ""))


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
    pid_current = True
    if label:
        state = launchd_state(label, run)
        if state is None:
            # Can't tell: the caller's pid may be stale, so it can only clear a
            # finding (a listener that now matches it), never confirm one.
            pid_current = False
        else:
            if not state[0]:
                return 0  # unloaded within the second: quiet, as in the first pass
            pid = state[1] or ""
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
        # above and lsof would have its new pid compared with the old one.
        after = launchd_state(label, run)
        if after is not None and not after[0]:
            return 0  # unloaded during the read: quiet, as above
        if after is None or (after[1] or "") != pid:
            print(
                "squat-check: the launchd pid changed during the re-check; skipping port check",
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
