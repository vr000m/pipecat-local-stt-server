#!/usr/bin/env python3
"""Opt-in launchd verification: port collision, socket rollback, token file.

Runs the checks 0.5.0 covered only with stubs against REAL launchd, using a spare
LaunchAgent (label ``pipecat.stt-server.smoke``, the ``echo`` backend, port 9998)
installed through ``scripts/install_stt_agent.sh``. No real ``pipecat.stt-server*``
agent is touched, and the harness checks it by comparing a snapshot of every real
agent (its loaded job's state, its plist and the holder of the endpoint the job
serves; see "real-agent snapshot" below) with the baseline before every check
and after teardown.

Usage::

    PIPECAT_STT_LABEL=pipecat.stt-server.smoke KODA_STT_LABEL=pipecat.stt-server.smoke .venv/bin/python scripts/smoke_launchd.py

Both ``PIPECAT_STT_LABEL`` and ``KODA_STT_LABEL`` must be set explicitly to the
spare label; an unset or different value is refused (never filled in).

Never run it with plain ``uv run`` (that prunes the backend extras); the installer
is handed the running interpreter through ``PIPECAT_STT_PYTHON``.

Order: preconditions (darwin, ``CI`` unset; otherwise skipped, exit 0) ->
run lock (an exclusive non-blocking ``flock`` on
``<agents home>/Library/Caches/pipecat-stt-smoke-launchd.lock``; a second run
refuses and touches nothing; the lock is held until the process exits) ->
baseline snapshot of the real agents -> guard (exact spare label in both
``PIPECAT_STT_LABEL`` and ``KODA_STT_LABEL``, a port that is not canonical, not
served by a real job or recorded in a real plist (both from the baseline) and
not already listening) -> start state (no spare plist, loaded job, socket or
``.lock``) -> the three checks inside a try/finally, each preceded by a
re-snapshot compared with the baseline -> teardown -> re-snapshot and compare. A guard
or start-state refusal is a FAIL and happens before any ``launchctl`` bootstrap or
bootout and before any installer call. Teardown always runs after that point
(failure, exception, SIGINT, SIGTERM): the installer's ``uninstall`` for the spare
label, then the spare socket and ``.lock`` only while the harness itself holds
that ``.lock``'s ``flock`` (so no live server owns them), then the spare plist
and logs. It never touches another label, leaves (and reports) anything it
cannot prove is its own and unheld, and any removal that fails, or any artifact
still present afterwards, makes the teardown row FAIL.

The real agents are every installed ``pipecat.stt-server*`` plist under
``_smoke_lib.agents_home()`` (the account's home from the password database, not
``$HOME``, so an overridden ``HOME`` cannot empty the before/after snapshot) plus
every loaded ``pipecat.stt-server*`` job ``launchctl list`` reports, plist or not;
the spare is excluded. Each one's loaded state, pid, ``runs`` counter and served
endpoint come from one ``launchctl print`` of the job (its ``pid =``, ``runs =``
and ``arguments`` block), plist or not. No environment variable moves the agents
home. A field the harness cannot read never compares as unchanged, so one in the
baseline snapshot refuses the run before the first install.

The spare port is held for the whole run: if the collision check cannot bind it,
or a later check finds a listener on it that is not the spare job, the run FAILs
there (the remaining checks are not run) and teardown still runs.
Listeners (``lsof``) and a labelled job's (loaded, pid) state (``launchctl
print``) are read through ``squat_check.py``'s helpers (one definition each).
Two reads live here: the job list (``launchctl list``) and a real job's
``launchctl print`` (``read_job``: classified by ``squat_check.classify_print``,
plus its runs counter and arguments block). A ``launchctl`` or ``lsof`` answer the
harness cannot read refuses or FAILs, never reads as "not loaded" or "free".
The token check sends its throwaway token (and the wrong one) only over
``_smoke_lib.connect_verified`` connections whose server end is the spare job
(its pid and ``runs`` from one ``read_job``), and PASSes only when a fresh
``read_job`` afterwards shows the same pair; until then it waits for the agent
with requests that carry no token. Every answer that proves the SPARE is up
(the collision check's recovery, each rollback transport) goes over such a
verified connection too (``Harness.verified_status``), with no token; a plain
no-token probe is only ever a readiness wait, never the proof.

Every installer call runs in its own process group through
``_smoke_lib.run_owned`` (``run_installer``): a SIGINT/SIGTERM during an install
SIGKILLs the installer's whole group before the interrupt reaches the harness,
so teardown never runs beside an installer descendant. Any installer
``warning:`` line not in the short benign list (``_BENIGN_WARNINGS``) FAILs the
install or the teardown. Plists are read in-process through
``read_stt_plist`` (``load``, ``read_field``), the one shared plist reader. The
start state refuses a spare plist under ``$HOME`` or under the agents home, and
prints every cleanup command shell-quoted. An unreadable launchd state of the
spare during the checks stops the remaining checks, like a lost port.

When a precondition is absent the harness prints
``_smoke_lib.LAUNCHD_SKIP_MARK`` + the reason and exits 0; ``smoke.py`` maps
that line to SKIPPED. After its table it prints one
``_smoke_lib.LAUNCHD_FAIL_MARK`` + "<check>: <detail>" line per FAIL row, which
``smoke.py`` uses as its launchd row's detail. Exit 0 unless a requested step FAILed.

Environment: after the preconditions the harness replaces its own
environment with ``_smoke_lib.harness_env`` (``scrubbed_environ``) for the
rest of the run, so every subprocess inherits only HOME, USER, TMPDIR, the
spare label and log-dir names and ``PATH=_smoke_lib.SAFE_PATH``
(``/usr/bin:/bin:/usr/sbin:/sbin``): no ``BASH_ENV``, ``ENV``, exported
``BASH_FUNC_*``, ``PYTHON*``, ``DYLD_*`` or ``LD_*``. The installer gets
``install_env`` (the same, with the installer's own names set per call).
Bash is ``/bin/bash``; ``launchctl``, ``lsof`` and ``python3`` resolve through
that fixed PATH.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fnmatch
import hashlib
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
INSTALLER = SCRIPTS / "install_stt_agent.sh"
BASH = "/bin/bash"  # never PATH-resolved
SQUAT_CHECK = SCRIPTS / "squat_check.py"

# Importing the shared helpers (and, lazily, stt_server.auth) needs these on the
# path; same pattern as smoke_peercred.py.
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SCRIPTS))

import _smoke_lib  # noqa: E402
import read_stt_plist  # noqa: E402
import squat_check  # noqa: E402

SPARE_LABEL = _smoke_lib.SPARE_LABEL
AGENT_LABEL_GLOB = _smoke_lib.AGENT_GLOB[: -len(".plist")]
# A snapshot listener the harness could not read; never equal to anything.
UNKNOWN = "unknown"
DEFAULT_PORT = 9998
HOST = "127.0.0.1"
# Installer backends with a canonical port (the justfile's "whisper" is "mlx").
CANONICAL_BACKENDS = ("mlx", "kyutai", "parakeet", "nemotron")
UNLOAD_WAIT = 30  # PIPECAT_STT_UNLOAD_WAIT given to the installer, in seconds
INSTALL_TIMEOUT = UNLOAD_WAIT + 90
UP_TIMEOUT = 60  # how long an agent gets to answer after an install
COLLISION_TIMEOUT = 30
CMD_TIMEOUT = 15
SOCKET_LOCK_WAIT = 10.0  # teardown's wait for the spare socket's .lock
LOCK_UNHELD_WAIT = 10  # the rollback's wait for the .lock to be released
WAIT_EVERY = 1.0  # Harness.wait_for's poll interval
# Python start-up, imports, file work and the table: what no named bound covers.
BUDGET_MARGIN = 30.0


# --- the harness's time budget ------------------------------------------------
#
# smoke.py's launchd step gives the harness run_budget(n) before SIGTERM and
# then teardown_budget(n) before SIGKILL, n = the installed real-agent plists
# (each one adds a launchctl print and an lsof to every snapshot). Both are
# sums of the bounds the code really waits on, named here; a new wait must be
# added to them (tests/test_smoke_launchd.py pins the runner to these
# functions and each bound to the constant the code uses). Residual: a loaded
# real job with NO plist is in every snapshot but not in n; it can only cut
# short the final re-snapshot, which follows the uninstall and file removal.


def install_bound() -> float:
    """One installer call: its timeout, one poll, the kill-and-reap drain."""
    return INSTALL_TIMEOUT + _smoke_lib.GROUP_POLL + _smoke_lib.GROUP_DRAIN


def snapshot_bound(n_agents: int) -> float:
    """One real-agent snapshot: ``launchctl list``, then per agent one
    ``launchctl print`` (read_job) and at most one lsof (endpoint_listener)."""
    return CMD_TIMEOUT + n_agents * (CMD_TIMEOUT + squat_check.LSOF_TIMEOUT)


def _handshake_bound() -> float:
    return 2 * _smoke_lib.HANDSHAKE_TIMEOUT  # the connect, then the status line


def _connect_verified_bound() -> float:
    """connect, the lsof settle plus one last lsof, the launchd re-read."""
    return (
        _smoke_lib.HANDSHAKE_TIMEOUT
        + _smoke_lib.SERVER_END_SETTLE
        + squat_check.LSOF_TIMEOUT
        + CMD_TIMEOUT
    )


def _verified_status_bound() -> float:
    """Harness.verified_status: read_job, connect_verified, the handshake."""
    return CMD_TIMEOUT + _connect_verified_bound() + _handshake_bound()


def _wait_bound(timeout: float, pred: float) -> float:
    """Harness.wait_for: a predicate may start just before the deadline."""
    return timeout + WAIT_EVERY + pred


def teardown_budget(n_agents: int) -> float:
    """From a SIGTERM to the harness's exit: an in-flight installer group's
    kill and reap, the uninstall, the socket lock wait, the launchd check,
    the final snapshot and the margin."""
    return (
        _smoke_lib.GROUP_DRAIN
        + install_bound()
        + SOCKET_LOCK_WAIT
        + squat_check.LAUNCHCTL_TIMEOUT
        + snapshot_bound(n_agents)
        + BUDGET_MARGIN
    )


def run_budget(n_agents: int) -> float:
    """The harness's worst case from start to exit, teardown included."""
    snap = snapshot_bound(n_agents)
    setup = (
        snap  # baseline
        + len(CANONICAL_BACKENDS) * CMD_TIMEOUT  # canonical_ports
        + squat_check.LSOF_TIMEOUT + 1  # is_listening: lsof, loopback connect
        + squat_check.LAUNCHCTL_TIMEOUT  # start state
        + 2 * CMD_TIMEOUT  # the stt_server import check
    )  # fmt: skip
    boundaries = 3 * snap + 2 * (squat_check.LSOF_TIMEOUT + squat_check.LAUNCHCTL_TIMEOUT)
    up = _wait_bound(UP_TIMEOUT, _verified_status_bound())
    squat = CMD_TIMEOUT + 2 * CMD_TIMEOUT  # squat_warns: print, then the CLI
    collision = install_bound() + COLLISION_TIMEOUT + squat + WAIT_EVERY + up
    rollback = 3 * (install_bound() + up) + _wait_bound(LOCK_UNHELD_WAIT, squat_check.LSOF_TIMEOUT)
    token = (
        install_bound()
        + _wait_bound(UP_TIMEOUT, _handshake_bound())  # _refuses
        + CMD_TIMEOUT  # read_job
        + 3 * (_connect_verified_bound() + _handshake_bound())  # 401 / 401 / 101
        + CMD_TIMEOUT  # the fresh read afterwards
    )
    return setup + boundaries + collision + rollback + token + teardown_budget(n_agents)


# Env names the installer reads. All are scrubbed from the child env so nothing
# inherited steers an install; the harness sets what it needs per call.
_SCRUB_PREFIXES = ("PIPECAT_STT_", "KODA_STT_")
_KEEP = ("PIPECAT_STT_LOG_DIR", "KODA_STT_LOG_DIR")
# The installer's stderr "warning:" lines that do NOT mean something was left
# behind. Every other warning line fails the install (fail closed: a new or
# reworded installer warning is a FAIL until it is classified here), which is
# what makes the rollback's "removed on the unload-confirmed path" claim hold.
# tests/test_smoke_launchd.py classifies every warning in the installer source.
_BENIGN_WARNINGS = (
    "warning: socket mode (PIPECAT_STT_SOCKET set): ignoring ",
    "warning: launchctl bootstrap failed for ",  # "...; retrying once" (a failed retry exits non-zero)
    "a socket agent carries none",  # the installed agent required a token
)
# The installer's stdout note when an install retires the old socket.
OLD_SOCKET_NOTE = "removing the old socket"
_IN_USE_RE = re.compile(r"address already in use|errno 48", re.IGNORECASE)

PASS, FAIL, SKIPPED = _smoke_lib.PASS, _smoke_lib.FAIL, _smoke_lib.SKIPPED


class GuardRefused(Exception):
    """The harness refused to run: the label, port or environment is not safe."""


class CheckFailed(Exception):
    """A check's assertion did not hold."""


class PortLost(CheckFailed):
    """Something other than the spare job holds (or may hold) the spare port."""


class LaunchdUnreadable(RuntimeError):
    """``launchctl print`` gave an answer that is neither loaded nor launchctl's
    own "could not find service": the harness cannot tell the job's state."""


class RealAgentsChanged(CheckFailed):
    """A real agent's snapshot differs from the baseline (or cannot be re-read)."""


class InstallerNotStopped(CheckFailed):
    """An installer call's process group could not be shown stopped
    (``_smoke_lib.GroupNotStopped``): it may still be changing the spare."""


# A check that raises one of these leaves the harness unable to prove the spare
# port and label are still its own, or that the real agents are untouched, so no
# later (mutating) check may run.
STOP_CHECKS = (PortLost, LaunchdUnreadable, RealAgentsChanged, InstallerNotStopped)
_STOP_REASONS = {
    InstallerNotStopped: ("installer not stopped", "an installer may still be running"),
    PortLost: ("port lost", "the spare port was lost"),
    LaunchdUnreadable: ("launchd state unreadable", "the spare's launchd state cannot be read"),
    RealAgentsChanged: ("real agents changed", "a real agent changed"),
}


# --- environment, paths ----------------------------------------------------


def _home() -> Path:
    """``$HOME``: where the installer puts the spare's plist, logs and socket."""
    return _smoke_lib.env_home()


def _plist_path(label: str) -> Path:
    return _smoke_lib.launch_agents_dir(_home()) / f"{label}.plist"


def _log_dir() -> Path:
    custom = os.environ.get("PIPECAT_STT_LOG_DIR") or os.environ.get("KODA_STT_LOG_DIR")
    return Path(custom) if custom else _home() / "Library" / "Logs" / "pipecat-stt"


def _log_paths(label: str) -> list[Path]:
    """[err log, out log] for the spare: StandardErrorPath / StandardOutPath from
    the installed plist; for a key the plist lacks, or before there is a plist,
    the name ``render_stt_plist.log_basename`` (the public function that
    writes those keys) gives the label. Imported here, not at module load: the
    renderer exits when ``stt_server`` does not import, which the start state
    reports."""
    from render_stt_plist import log_basename

    base = log_basename(label)
    paths = {
        "StandardErrorPath": _log_dir() / f"{base}.err",
        "StandardOutPath": _log_dir() / f"{base}.log",
    }
    try:
        data = read_stt_plist.load(str(_plist_path(label)))
    except read_stt_plist.MalformedPlist:
        data = {}
    for key in paths:
        val = data.get(key)
        if isinstance(val, str) and val:
            paths[key] = Path(val)
    err, out = paths["StandardErrorPath"], paths["StandardOutPath"]
    return [err] if out == err else [err, out]


def _err_log(label: str) -> Path:
    return _log_paths(label)[0]


def spare_socket_path() -> Path:
    """A spare-specific socket path, so it can never be a real agent's socket."""
    return _home() / "Library" / "Caches" / "pipecat-stt-smoke-launchd" / "spare.sock"


def _lock_path(sock: Path | str) -> Path:
    return Path(f"{sock}.lock")


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _read_from(path: Path, offset: int) -> str:
    """PATH's text past OFFSET (from 0 if it was truncated); '' when unreadable."""
    try:
        with open(path, "rb") as fh:
            if _size(path) >= offset:
                fh.seek(offset)
            return fh.read().decode(errors="replace")
    except OSError:
        return ""


def _in_use_lines(text: str) -> int:
    """Address-in-use LINES (one traceback line can name both forms)."""
    return sum(1 for ln in text.splitlines() if _IN_USE_RE.search(ln))


def _within(path: Path, root: Path) -> bool:
    """True when PATH's nearest existing ancestor (itself if it exists) resolves
    inside ROOT, so no symlinked component can carry a new directory out of it."""
    probe = path
    while not os.path.lexists(probe) and probe != probe.parent:
        probe = probe.parent
    real, real_root = os.path.realpath(probe), os.path.realpath(root)
    return real == real_root or real.startswith(real_root.rstrip(os.sep) + os.sep)


def prepare_private_dir(path: Path, root: Path | None = None) -> bool:
    """Create PATH 0700, or make an existing directory of ours 0700.

    ``lstat`` first: a symlink or a non-directory is refused, so a planted link's
    target is never chmod'ed or used. With ROOT, PATH must also stay inside ROOT
    after resolving every existing ancestor (``mkdir(parents=True)`` follows a
    symlinked ancestor). Returns True when it created PATH."""
    if root is not None and not _within(path, root):
        raise CheckFailed(f"{path} resolves outside {root} (a symlinked ancestor); refusing it")
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        path.mkdir(parents=True, mode=0o700)
        os.chmod(path, 0o700)
        return True
    if stat.S_ISLNK(st.st_mode):
        raise CheckFailed(f"{path} is a symlink; refusing to use it for the spare socket")
    if not stat.S_ISDIR(st.st_mode):
        raise CheckFailed(f"{path} exists and is not a directory")
    if st.st_uid != os.getuid():
        raise CheckFailed(f"{path} is not owned by the current user")
    os.chmod(path, 0o700)
    return False


# --- process helpers (all by bare name through PATH) ----------------------


def _run(cmd, run=subprocess.run, timeout=CMD_TIMEOUT, **kw):
    return run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def current_uid() -> str:
    """This process's uid (the per-uid launchd domain ``gui/<uid>``), from the
    kernel (``os.getuid``), never from a PATH-resolved ``id``."""
    return str(os.getuid())


def require_launchd_state(label: str, uid: str, run=subprocess.run) -> tuple[bool, str | None]:
    """(loaded, pid) from read-only ``launchctl print`` (squat_check's reader).

    Raises LaunchdUnreadable (a RuntimeError) when launchctl's answer is not
    "loaded" or launchctl's own "could not find service": an unreadable state
    never reads as unloaded."""
    state = squat_check.launchd_state(label, run, uid=uid)
    if state is None:
        raise LaunchdUnreadable(
            f"cannot read the launchd state of {label} (launchctl print failed)"
        )
    return state


def tcp_listener_pids(port: int | str, run=subprocess.run) -> list[str] | None:
    """Pids listening on PORT on any address; None when lsof can't tell."""
    return squat_check.listener_pids(port, None, run)


def loaded_agent_labels(run=subprocess.run) -> set[str]:
    """Loaded ``pipecat.stt-server*`` job labels (the spare excluded) from
    read-only ``launchctl list``. Raises RuntimeError when it cannot be read."""
    try:
        res = _run(["launchctl", "list"], run)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"cannot list the loaded launchd jobs: {exc}") from exc
    if res.returncode != 0:
        raise RuntimeError(
            f"cannot list the loaded launchd jobs (launchctl list exit {res.returncode})"
        )
    labels = set()
    for line in (res.stdout or "").splitlines():
        parts = line.split()
        if parts and fnmatch.fnmatchcase(parts[-1], AGENT_LABEL_GLOB):
            if parts[-1] != SPARE_LABEL:
                labels.add(parts[-1])
    return labels


def squat_cli(host: str, port: int | str, label: str, launchctl_rc: int) -> list[str]:
    """The one ``squat_check.py`` command line, the recipes' ``_squat_check``
    form: the ``launchctl print`` answer itself goes on stdin, never argv."""
    return [
        "python3",
        "-I",
        str(SQUAT_CHECK),
        "--host",
        host,
        "--port",
        str(port),
        "--label",
        label,
        "--launchctl-rc",
        str(launchctl_rc),
    ]


def lock_holders(lock: Path, run=subprocess.run) -> list[str] | None:
    """Pids holding LOCK open; None when lsof can't tell."""
    return squat_check.lsof_pids(["--", str(lock)], run)


def lock_unheld(lock: Path, run=subprocess.run) -> bool:
    """True only when lsof answered and listed no holder (fail closed)."""
    return lock_holders(lock, run) == []


def is_listening(port: int, run=subprocess.run) -> bool:
    """True when something listens on PORT (lsof, then a loopback connect).

    Raises GuardRefused when lsof can't tell."""
    pids = tcp_listener_pids(port, run)
    if pids is None:
        raise GuardRefused(f"cannot tell whether port {port} is free (lsof failed)")
    if pids:
        return True
    import socket

    try:
        socket.create_connection((HOST, port), timeout=1).close()
    except OSError:
        return False
    return True


def read_plist_field(label: str, field: str) -> str:
    return read_plist_field_path(_plist_path(label), field)


def read_plist_field_path(plist: Path, field: str) -> str:
    """One field from PLIST, exactly as recorded, through ``read_stt_plist`` (the
    one plist reader; in-process). '' when the plist is absent, malformed or
    lacks it."""
    try:
        data = read_stt_plist.load(str(plist))
    except read_stt_plist.MalformedPlist:
        data = {}
    return read_stt_plist.read_field(data, field)


# --- guard ------------------------------------------------------------------


def preconditions(env=os.environ, platform=sys.platform) -> str | None:
    """A reason to skip (not fail) on this machine, or None."""
    return _smoke_lib.launchd_skip_reason(env, platform)


def run_lock_path() -> Path:
    """The run lock. Under the agents home (the account home), not ``$HOME``: the spare label lives in the per-uid launchd
    domain, so two runs with different ``$HOME`` values still share it."""
    return _smoke_lib.agents_home() / "Library" / "Caches" / "pipecat-stt-smoke-launchd.lock"


def acquire_run_lock(path: Path) -> int:
    """Take an exclusive, non-blocking ``flock`` on PATH and return its fd; the
    lock lasts until the fd is closed or the process exits, however it exits.

    Raises GuardRefused when another run holds it, or PATH is a symlink, not a
    regular file or not ours. The file is never unlinked: unlinking a lock file
    lets two runs lock different inodes of the same path."""
    import fcntl

    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise GuardRefused(f"cannot open the run lock {path}: {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise GuardRefused(f"the run lock {path} is not a regular file owned by this user")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise GuardRefused(
                f"another smoke_launchd.py run holds {path}; wait for it to finish"
            ) from exc
    except BaseException:
        os.close(fd)
        raise
    return fd


def canonical_ports(run=subprocess.run) -> set[int]:
    """The four canonical ports, read from ``install_stt_agent.sh port <backend>``."""
    ports: set[int] = set()
    for backend in CANONICAL_BACKENDS:
        try:
            res = _run([BASH, str(INSTALLER), "port", backend], run)
        except (OSError, subprocess.SubprocessError) as exc:
            raise GuardRefused(f"cannot read the canonical port of {backend}: {exc}") from exc
        out = (res.stdout or "").strip()
        if res.returncode != 0 or not out.isdigit():
            raise GuardRefused(f"cannot read the canonical port of {backend} from the installer")
        ports.add(int(out))
    return ports


def real_agent_plists(home: Path | None = None) -> list[Path]:
    """Installed real-agent plists under ``agents_home()`` (never ``$HOME``)."""
    return _smoke_lib.agent_plists(home if home is not None else _smoke_lib.agents_home())


def check_guard(
    label, port, *, env=os.environ, canonical_ports, is_listening, agent_ports=None
) -> None:
    """Raise GuardRefused unless this is the spare label on a spare, free port."""
    if label != SPARE_LABEL:
        raise GuardRefused(f"label {label!r} is not the spare label {SPARE_LABEL!r}")
    for name in ("PIPECAT_STT_LABEL", "KODA_STT_LABEL"):
        val = env.get(name)
        if val != SPARE_LABEL:
            shown = "unset" if val is None else repr(val)
            raise GuardRefused(f"{name} must be set to {SPARE_LABEL!r} (it is {shown})")
    if port in canonical_ports:
        raise GuardRefused(f"port {port} is a canonical agent port")
    if agent_ports and port in agent_ports:
        raise GuardRefused(f"port {port} is recorded in the installed {agent_ports[port]} plist")
    if is_listening(port):
        raise GuardRefused(f"port {port} already has a listener")


# --- real-agent snapshot ----------------------------------------------------
#
# The contract (one place). For each real agent (every installed
# pipecat.stt-server* plist under agents_home() and every loaded
# pipecat.stt-server* job `launchctl list` reports, the spare excluded) the
# snapshot records an AgentState:
#
#   loaded, pid, runs, endpoint  from ONE read-only `launchctl print` of the job,
#       the source of truth for what launchd is running: `loaded`/`pid` through
#       squat_check.classify_print (the recipes' rule), `runs` (launchd's spawn
#       counter, which moves on every restart even when a pid is reused) and the
#       endpoint the job is SERVING, from its `arguments` block (its port
#       canonical, as the server parses it; a --port int() refuses is UNKNOWN).
#       An unreadable answer raises LaunchdUnreadable (never "not loaded").
#       A loaded job with a pid whose answer has no `runs =` line is UNKNOWN
#       (None would compare equal to None and blind the restart check).
#   plist  sha256 of the installed plist's bytes (None: no plist). The plist is
#       only the INTENDED endpoint (it can change without a reload), so it is
#       never where the listener is read; any rewrite of it is a change.
#   plist_port  the TCP port recorded in those same bytes, as the server would
#       parse it (read_stt_plist.parse_port: int(), so `--port +9998` and
#       `--port=09998` are 9998), stored canonical (None: no --port, or no
#       plist; UNKNOWN: unreadable, or a --port value int() refuses). The guard's port set comes from the
#       baseline's `endpoint` and `plist_port`, never from a later plist read.
#   listener  the pids holding the JOB's endpoint (squat_check.endpoint_holders:
#       TCP listeners on its port and host; a socket job's `<socket>.lock`
#       holders, the file the server flocks, since lsof on an AF_UNIX path lists
#       nothing). A job that is not loaded, or loaded with no pid, serves
#       nothing: None, and no lsof call (an absent socket there is not UNKNOWN).
#       A running job whose endpoint cannot be read (lsof failed, no endpoint in
#       its arguments) is UNKNOWN.
#
# Invariant: two snapshots are equal only when every field of every agent (all
# of dataclasses.fields(AgentState), no hand-kept list) is equal and neither
# holds UNKNOWN; a baseline holding UNKNOWN anywhere refuses the run. The harness takes the baseline before the
# guard (whose port set is derived from it: job and plist ports alike), and
# compares a fresh snapshot with it before EVERY check (so a mutation is caught
# at the next boundary, and the later, mutating checks do not run) and after
# teardown. `runs` catches a restart that came back to the same pid and
# listener. Residual: an agent that was NOT loaded, loaded and unloaded again
# entirely between two boundaries with its plist untouched, leaves no launchd
# state to observe.

_JOB_ARGS_RE = re.compile(r"^\s*arguments = \{\s*\n(.*?)^\s*\}", re.MULTILINE | re.DOTALL)


@dataclasses.dataclass(frozen=True)
class AgentState:
    loaded: bool
    pid: str | None
    runs: str | None
    endpoint: tuple[str, ...] | str | None  # ("tcp", host, port) | ("unix", path) | None | UNKNOWN
    plist: str | None
    listener: str | None
    plist_port: str | None = None


def canonical_port(flags: dict[str, str]) -> str | None:
    """The ``--port`` in serve FLAGS (``read_stt_plist.read_argv_flags``) as
    the server parses it, canonical (``str(int(value))``): None when there is
    no ``--port``; UNKNOWN when its value is one ``int()`` refuses (an empty
    one included), never "no port"."""
    if "--port" not in flags:
        return None
    port = read_stt_plist.parse_port(flags["--port"])
    return UNKNOWN if port is None else str(port)


def job_endpoint(text: str) -> tuple[str, ...] | str | None:
    """The endpoint a job serves, from its ``launchctl print`` arguments block:
    ("unix", path), ("tcp", host, canonical port), None when it names neither,
    or UNKNOWN when its ``--port`` cannot be parsed the server's way."""
    m = _JOB_ARGS_RE.search(text or "")
    args = [ln.strip() for ln in m.group(1).splitlines() if ln.strip()] if m else []
    flags = read_stt_plist.read_argv_flags(args)
    if flags.get("--socket-path"):
        return ("unix", flags["--socket-path"])
    port = canonical_port(flags)
    if port == UNKNOWN:
        return UNKNOWN
    if port is not None:
        return ("tcp", flags.get("--host", ""), port)
    return None


def read_job(
    label: str, uid: str, run=subprocess.run
) -> tuple[bool, str | None, str | None, tuple[str, ...] | None]:
    """(loaded, pid, runs, endpoint) from ONE read-only ``launchctl print``.
    Raises LaunchdUnreadable when the answer is neither loaded nor launchctl's
    own "could not find service"."""
    try:
        res = _run(["launchctl", "print", f"gui/{uid}/{label}"], run)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LaunchdUnreadable(f"cannot read {label} with launchctl print: {exc}") from exc
    out = res.stdout or ""
    state = squat_check.classify_print(res.returncode, out + "\n" + (res.stderr or ""))
    if state is None:
        raise LaunchdUnreadable(f"cannot read {label} with launchctl print (exit {res.returncode})")
    loaded, pid = state
    if not loaded:
        return False, None, None, None
    runs = _smoke_lib.parse_runs(out)
    if runs is None and pid:
        # A running job always has a spawn counter; an answer without one
        # cannot prove a restart did not happen (None == None), so it is UNKNOWN.
        runs = UNKNOWN
    return True, pid, runs, job_endpoint(out)


def endpoint_listener(loaded: bool, pid, endpoint, run=subprocess.run) -> str | None:
    """Comma-joined holder pids of a job's endpoint, None when there is none,
    UNKNOWN when it cannot be read. A job with no pid serves nothing: None."""
    if not loaded or not pid:
        return None
    if endpoint is None or endpoint == UNKNOWN:
        return UNKNOWN
    if endpoint[0] == "unix":
        pids = squat_check.endpoint_holders(None, None, endpoint[1], run)
    else:
        pids = squat_check.endpoint_holders(endpoint[1] or None, endpoint[2], "", run)
    return UNKNOWN if pids is None else (",".join(pids) or None)


def _plist_record(plist: Path) -> tuple[str | None, str | None]:
    """(sha256, recorded TCP port) from ONE read of PLIST's bytes: (None, None)
    when absent; UNKNOWN for what cannot be read."""
    try:
        raw = read_stt_plist.read_bytes(str(plist))  # never blocks on a FIFO
    except read_stt_plist.MalformedPlist:
        return UNKNOWN, UNKNOWN
    if raw is None:
        return None, None
    try:
        port = canonical_port(read_stt_plist.read_flags(read_stt_plist.loads(raw)))
    except read_stt_plist.MalformedPlist:
        port = UNKNOWN
    return hashlib.sha256(raw).hexdigest(), port


def snapshot_real_agents(run=subprocess.run, home: Path | None = None) -> dict[str, AgentState]:
    """label -> AgentState for each real agent (see the contract above).
    Read-only calls only."""
    uid = current_uid()
    agents = _smoke_lib.launch_agents_dir(home if home is not None else _smoke_lib.agents_home())
    labels = {p.name[: -len(".plist")] for p in real_agent_plists(home)} | loaded_agent_labels(run)
    snap: dict[str, AgentState] = {}
    for label in sorted(labels):
        loaded, pid, runs, endpoint = read_job(label, uid, run)
        digest, plist_port = _plist_record(agents / f"{label}.plist")
        snap[label] = AgentState(
            loaded,
            pid,
            runs,
            endpoint,
            digest,
            endpoint_listener(loaded, pid, endpoint, run),
            plist_port,
        )
    return snap


def snapshot_ports(snap: dict[str, AgentState]) -> dict[int, str]:
    """port -> label for every TCP port a real agent serves (its loaded job's
    arguments) or intends to (the port its installed plist recorded when the
    snapshot was taken): the guard's port set, from the snapshot alone, so the
    guard and the baseline cannot read the real agents differently."""
    ports: dict[int, str] = {}
    for label, st in sorted(snap.items()):
        if st.plist_port == UNKNOWN:
            raise GuardRefused(f"cannot read the port recorded in the {label} plist")
        if st.endpoint == UNKNOWN:
            raise GuardRefused(f"cannot parse the port the loaded {label} job serves")
        # Both are canonical (canonical_port): int() is the server's own parse.
        if st.endpoint and st.endpoint[0] == "tcp":
            ports[int(st.endpoint[2])] = label
        if st.plist_port:
            ports[int(st.plist_port)] = label
    return ports


def snapshot_unknowns(snap) -> list[str]:
    """ "label (field, ...)" for each agent with a field the snapshot could not
    read (sorted by label); every field of AgentState counts."""
    out = []
    for label, st in sorted(snap.items()):
        bad = [f.name for f in dataclasses.fields(st) if getattr(st, f.name) == UNKNOWN]
        if bad:
            out.append(f"{label} ({', '.join(bad)})")
    return out


def compare_snapshots(before, after) -> list[str]:
    """Human-readable differences between two snapshots; empty means unchanged.
    A field that is UNKNOWN on either side is a difference."""
    diffs = []
    for label in sorted(set(before) | set(after)):
        if label not in after:
            diffs.append(f"{label}: disappeared (was {before[label]})")
            continue
        if label not in before:
            diffs.append(f"{label}: appeared ({after[label]})")
            continue
        b, a = before[label], after[label]
        for name in (f.name for f in dataclasses.fields(AgentState)):
            bv, av = getattr(b, name), getattr(a, name)
            if UNKNOWN in (bv, av):
                diffs.append(
                    f"{label}: {name} unreadable ({bv} -> {av}); cannot prove it unchanged"
                )
            elif bv != av:
                diffs.append(f"{label}: {name} {bv} -> {av}")
    return diffs


# --- start state ------------------------------------------------------------


def spare_plist_paths(label: str) -> list[Path]:
    """Every place a spare plist can define the spare's launchd identity: under
    ``$HOME`` (where the installer writes it) and under the agents home (the
    account home, whose LaunchAgents launchd loads at login). The label lives
    in the per-uid launchd domain, so a plist in either one is the same job."""
    paths = [_plist_path(label)]
    other = _smoke_lib.launch_agents_dir(_smoke_lib.agents_home()) / f"{label}.plist"
    if os.path.abspath(other) != os.path.abspath(paths[0]):
        paths.append(other)
    return paths


def shell_command(*argv: str | Path) -> str:
    """ARGV as one line a user can paste into a shell: every word quoted."""
    return " ".join(shlex.quote(str(a)) for a in argv)


def start_state_problems(label: str, uid: str, sock: Path, run=subprocess.run) -> list[str]:
    """Cleanup commands (each word shell-quoted) for each piece of spare state
    that already exists."""
    cmds = []
    loaded, _pid = require_launchd_state(label, uid, run)
    if loaded:
        cmds.append(shell_command("launchctl", "bootout", f"gui/{uid}/{label}"))
    # Teardown unlinks the spare's logs, so they must be the harness's own: logs
    # already at those paths (an earlier run, or a label sharing the basename)
    # refuse the run instead.
    for path in (*spare_plist_paths(label), sock, _lock_path(sock), *_log_paths(label)):
        if path.exists() or path.is_symlink():
            cmds.append(shell_command("rm", "-f", path))
    return cmds


# --- installer --------------------------------------------------------------


def install_env(label: str, port: int, *, socket_path=None, token_file=None, base=None) -> dict:
    """The installer's whole environment: ``_smoke_lib.harness_env`` of BASE
    (the fixed PATH, HOME, USER, TMPDIR, the label and log-dir names; nothing
    else inherited), the inherited labels dropped and the spare's own values
    set."""
    env = {
        k: v
        for k, v in _smoke_lib.harness_env(base if base is not None else os.environ).items()
        if not k.startswith(_SCRUB_PREFIXES) or k in _KEEP
    }
    env.update(
        PIPECAT_STT_LABEL=label,
        KODA_STT_LABEL=label,
        PIPECAT_STT_BACKEND="echo",
        PIPECAT_STT_PORT=str(port),
        PIPECAT_STT_HOST=HOST,
        PIPECAT_STT_PYTHON=sys.executable,
        PIPECAT_STT_UNLOAD_WAIT=str(UNLOAD_WAIT),
    )
    if socket_path is not None:
        env["PIPECAT_STT_SOCKET"] = str(socket_path)
    if token_file is not None:
        env["PIPECAT_STT_AUTH_TOKEN_FILE"] = str(token_file)
    return env


def run_installer(sub: str, env: dict, *, teardown: bool = False) -> subprocess.CompletedProcess:
    """``install_stt_agent.sh SUB`` in its own process group, through
    ``_smoke_lib.run_owned`` (the runner's step lifecycle): when this returns or
    raises, no installer process, nor anything it started, is still running, so
    teardown can never race a late ``launchctl bootstrap``.

    A terminal Ctrl-C reaches only the harness. A SIGINT/SIGTERM to the harness
    during an install is held, the installer's whole group is SIGKILLed while
    its leader is unreaped, and only then does the signal reach the harness's
    handler (KeyboardInterrupt, teardown follows). With ``teardown`` (the
    uninstall) a signal does not stop the installer. INSTALL_TIMEOUT kills the
    group and raises subprocess.TimeoutExpired. A group that cannot be shown
    stopped (the SIGKILL failed, or the leader was not reaped) raises
    InstallerNotStopped, never a result."""
    cmd = [BASH, str(INSTALLER), sub]
    try:
        res, timed_out = _smoke_lib.run_owned(
            cmd,
            INSTALL_TIMEOUT,
            env=env,
            cwd=str(REPO_ROOT),
            stop_on_signal=not teardown,
            interrupt_signal=None,
            timeout_signal=None,
        )
    except _smoke_lib.GroupNotStopped as exc:
        raise InstallerNotStopped(f"installer {sub}: {exc}") from exc
    if timed_out:
        raise subprocess.TimeoutExpired(cmd, INSTALL_TIMEOUT, output=res.stdout, stderr=res.stderr)
    return res


def leave_behind_warnings(stderr: str) -> list[str]:
    """The installer's ``warning:`` lines on STDERR that are not known benign."""
    return [
        ln.strip()
        for ln in (stderr or "").splitlines()
        if ln.strip().startswith("warning:") and not any(b in ln for b in _BENIGN_WARNINGS)
    ]


def install(env: dict, what: str) -> str:
    """Run the installer's install; a non-zero exit or a leave-behind warning FAILs."""
    res = run_installer("install", env)
    err = res.stderr or ""
    if res.returncode != 0:
        raise CheckFailed(f"install ({what}) exited {res.returncode}: {err.strip()[-300:]}")
    left = leave_behind_warnings(err)
    if left:
        raise CheckFailed(f"install ({what}) warned about a leftover: {left[-1][-300:]}")
    return res.stdout or ""


# --- checks -----------------------------------------------------------------


class Harness:
    def __init__(self, label: str, port: int, baseline=None):
        self.label = label
        self.baseline = baseline if baseline is not None else {}
        self.port = port
        self.uid = current_uid()
        self.sock = spare_socket_path()
        self.sock_dir_made = False
        self.token_dir: Path | None = None
        self.holder = None  # the collision check's port occupier

    # -- waits

    def wait_for(self, what: str, pred, timeout: float = UP_TIMEOUT, every: float = WAIT_EVERY):
        deadline = time.monotonic() + timeout
        last = None
        while True:
            try:
                got = pred()
                if got:
                    return got
            except (OSError, CheckFailed) as exc:
                last = exc
            if time.monotonic() >= deadline:
                extra = f" (last error: {last})" if last else ""
                raise CheckFailed(f"timed out after {timeout:.0f}s waiting for {what}{extra}")
            time.sleep(every)

    def tcp_status(self) -> int:
        """The spare port's answer to an upgrade with NO token, over a plain
        connection: a readiness probe only (nothing secret is sent, and it
        proves nothing about who answered)."""
        return _smoke_lib.ws_handshake_status(host=HOST, port=self.port)

    def verified_status(self, endpoint: tuple) -> int:
        """ENDPOINT's answer to an upgrade with no token, over a
        ``_smoke_lib.connect_verified`` connection whose server end is the
        spare job (its pid and runs from one ``read_job``; the same rule as
        the token check): what proves the SPARE answered, not any listener.
        A job without a readable pid and runs, or a server end that is not
        its, is CheckFailed (``wait_for`` retries it); an unreadable launchd
        state raises LaunchdUnreadable."""
        loaded, pid, runs, _ep = read_job(self.label, self.uid)
        if not (loaded and pid and runs and runs != UNKNOWN):
            raise CheckFailed(
                f"the spare job is not running with a readable pid and runs "
                f"(loaded {loaded}, pid {pid}, runs {runs})"
            )
        try:
            sock = _smoke_lib.connect_verified(endpoint, pid, runs, self._identity)
        except _smoke_lib.EndpointRefused as exc:
            raise CheckFailed(f"not the spare job: {exc}") from exc
        return _smoke_lib.ws_handshake_status(sock=sock)

    def tcp_up(self) -> bool:
        """The spare JOB answers 101 on the spare port (server end verified)."""
        return self.verified_status(("tcp", HOST, str(self.port))) == 101

    def sock_up(self) -> bool:
        """The spare JOB answers 101 on the spare socket (server end verified)."""
        return self.verified_status(("unix", str(self.sock))) == 101

    def expect_transport(self, want: str) -> None:
        port = read_plist_field(self.label, "port")
        sock = read_plist_field(self.label, "socket-path")
        if want == "tcp":
            if port != str(self.port) or sock:
                raise CheckFailed(
                    f"plist should record TCP {self.port}; port={port!r} socket={sock!r}"
                )
        elif sock != str(self.sock) or port:
            raise CheckFailed(
                f"plist should record socket {self.sock}; port={port!r} socket={sock!r}"
            )

    # -- check: port collision

    def check_collision(self) -> str:
        import socket

        # The guard's port check already ran; now the harness holds the port itself.
        self.holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.holder.bind((HOST, self.port))
            self.holder.listen(1)
        except OSError as exc:
            self.release_holder()
            raise PortLost(f"cannot hold port {self.port} after the guard: {exc}") from exc
        # Only lines written after this install count: an earlier run's
        # address-in-use lines must not satisfy the assertion.
        offsets = {p: _size(p) for p in _log_paths(self.label)}  # no plist yet: naming rule
        install(install_env(self.label, self.port), "collision")
        errlog = _err_log(self.label)  # now from the plist's StandardErrorPath
        offset = offsets.get(errlog, _size(errlog))
        deadline = time.monotonic() + COLLISION_TIMEOUT
        in_use = 0
        warned = False
        while time.monotonic() < deadline:
            text = _read_from(errlog, offset)
            in_use = _in_use_lines(text)
            if in_use >= 2:
                warned = self.squat_warns()
                if warned:
                    break
            time.sleep(1)
        if in_use < 2:
            raise CheckFailed(
                f"saw {in_use} address-in-use lines in {errlog} within {COLLISION_TIMEOUT}s"
            )
        if not warned:
            raise CheckFailed(f"squat_check.py gave no warning within {COLLISION_TIMEOUT}s")
        self.holder.close()
        self.holder = None
        self.wait_for("the agent to answer after the port was freed", self.tcp_up)
        return (
            f"{in_use} address-in-use lines, squat warning, agent up after free "
            "(server end verified)"
        )

    def ensure_port_ours(self) -> None:
        """Raise PortLost unless every listener on the spare port is the spare job
        (or there is none); an lsof answer it cannot read is a loss too."""
        pids = tcp_listener_pids(self.port)
        if pids is None:
            raise PortLost(f"cannot tell who listens on port {self.port} (lsof failed)")
        _loaded, pid = require_launchd_state(self.label, self.uid)
        foreign = [p for p in pids if p != pid]
        if foreign:
            raise PortLost(
                f"port {self.port} is held by pid {', '.join(foreign)}, not the spare job"
                f" (pid {pid})"
            )

    def ensure_real_agents_unchanged(self) -> None:
        """Raise RealAgentsChanged unless a fresh snapshot of the real agents
        equals the baseline (the snapshot contract above); run before every
        check, so a mutation stops the later checks at the next boundary."""
        try:
            now = snapshot_real_agents()
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            raise RealAgentsChanged(f"cannot re-snapshot the real agents: {exc}") from exc
        diffs = compare_snapshots(self.baseline, now)
        if diffs:
            raise RealAgentsChanged("; ".join(diffs))

    def release_holder(self) -> None:
        if self.holder is not None:
            with contextlib.suppress(OSError):
                self.holder.close()
            self.holder = None

    def squat_warns(self) -> bool:
        """Run ``squat_check.py`` exactly as the recipes do: one read-only
        ``launchctl print`` of the spare, its exit status on argv
        (``--launchctl-rc``) and its output on stdin, classified there by
        ``classify_print``. Raises LaunchdUnreadable when that same rule cannot
        tell the spare's state (the harness's own stop rule)."""
        target = f"gui/{self.uid}/{self.label}"
        try:
            res = _run(["launchctl", "print", target])
        except (OSError, subprocess.SubprocessError) as exc:
            raise LaunchdUnreadable(
                f"cannot read the launchd state of {self.label}: {exc}"
            ) from exc
        answer = (res.stdout or "") + "\n" + (res.stderr or "")
        if squat_check.classify_print(res.returncode, answer) is None:
            raise LaunchdUnreadable(
                f"cannot read the launchd state of {self.label} "
                f"(launchctl print exit {res.returncode})"
            )
        sq = _run(
            squat_cli(HOST, self.port, self.label, res.returncode),
            input=answer,
            timeout=CMD_TIMEOUT * 2,
        )
        return sq.returncode == 3 and "squat-check: warning:" in (sq.stderr or "")

    # -- check: socket rollback

    def check_rollback(self) -> str:
        sock = self.sock
        lock = _lock_path(sock)
        self.sock_dir_made = prepare_private_dir(sock.parent, _home())

        install(install_env(self.label, self.port), "rollback: TCP")
        self.expect_transport("tcp")
        self.wait_for("the TCP agent to answer", self.tcp_up)

        install(install_env(self.label, self.port, socket_path=sock), "rollback: socket")
        self.expect_transport("socket")
        self.wait_for("the socket agent to answer", self.sock_up)
        if not lock.exists():
            raise CheckFailed(f"{lock} was not created by the socket server")

        # The harness cannot watch the unload and the unlink race from outside
        # (the old server may unlink its own socket while it exits). What it
        # proves: install() refused any "still loaded" / "may still hold" /
        # "leaving" warning, so the installer took its unload-confirmed removal
        # path (wait_unloaded returned 0), reported it, and the socket is gone.
        out = install(install_env(self.label, self.port), "rollback: back to TCP")
        self.expect_transport("tcp")
        self.wait_for("the TCP agent to answer again", self.tcp_up)
        if OLD_SOCKET_NOTE not in out:
            raise CheckFailed("installer did not report removing the old socket")
        if sock.exists() or sock.is_symlink():
            raise CheckFailed(f"old socket {sock} still exists after the move back to TCP")
        if not lock.exists():
            raise CheckFailed(f"{lock} was removed; the installer must leave it")
        self.wait_for(f"{lock} to be unheld", lambda: lock_unheld(lock), timeout=LOCK_UNHELD_WAIT)
        return (
            "tcp -> socket -> tcp (each answer's server end verified); old socket "
            "removed on the installer's unload-confirmed path, .lock left and unheld"
        )

    # -- check: token file

    def check_token(self) -> str:
        from stt_server.auth import load_token_file

        self.token_dir, token_file = _smoke_lib.write_policy_token_file(
            _home(), "pipecat-smoke-token-"
        )
        # The server's own policy must accept it before launchd is asked to.
        try:
            resolved, _token = load_token_file(str(token_file), uid=int(self.uid))
        except ValueError as exc:
            raise CheckFailed(f"token file policy refused the harness file: {exc}") from exc

        install(install_env(self.label, self.port, token_file=token_file), "token")
        if read_plist_field(self.label, "auth-token-file") != resolved:
            raise CheckFailed("plist does not record the resolved token file path")
        # Up = refuses a request with no token; nothing secret is sent yet.
        self.wait_for("the token agent to refuse a request with no token", self._refuses)
        # The token (and the wrong one, nearly the token) goes only over a
        # connection whose server end is the spare job (pid P, runs R from one
        # launchctl print), and a fresh print after the gate must still give
        # (P, R): _smoke_lib.connect_verified's contract.
        loaded, pid, runs, _ep = read_job(self.label, self.uid)
        if not (loaded and pid and runs and runs != UNKNOWN):
            raise CheckFailed(
                f"the spare job is not running with a readable pid and runs "
                f"(loaded {loaded}, pid {pid}, runs {runs}); no token sent"
            )

        def connect():
            return _smoke_lib.connect_verified(
                ("tcp", HOST, str(self.port)), pid, runs, self._identity
            )

        try:
            _smoke_lib.assert_token_gate(HOST, self.port, str(token_file), connect=connect)
        except _smoke_lib.EndpointRefused as exc:
            raise CheckFailed(f"token not sent: {exc}") from exc
        except AssertionError as exc:
            raise CheckFailed(str(exc)) from exc
        now = self._identity()
        if now != (pid, runs):
            raise CheckFailed(
                f"the spare job changed during the token check: {(pid, runs)} -> {now}"
            )
        return "no token -> 401, wrong token -> 401, token -> 101 (server end verified)"

    def _refuses(self) -> bool:
        return self.tcp_status() == 401

    def _identity(self):
        """(pid, runs) of the spare job from one fresh ``launchctl print``; None
        when it is not loaded. An unreadable answer raises LaunchdUnreadable."""
        loaded, pid, runs, _ep = read_job(self.label, self.uid)
        return (pid, runs) if loaded else None

    # -- teardown

    def _unlink(self, path: Path, problems: list[str]) -> None:
        """Unlink PATH; a failure other than "already gone" is a problem."""
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            problems.append(f"cannot remove {path}: {exc}")

    def remove_spare_socket(self, problems: list[str], wait: float = SOCKET_LOCK_WAIT) -> None:
        """Remove the spare socket and its ``.lock``, only while proving no live
        server owns them.

        The start state proved neither existed when the run began, and the path
        is the harness's own. A server holds an exclusive ``flock`` on
        ``<socket>.lock`` for its whole life and takes it before it binds, so the
        harness takes that same lock itself (non-blocking, retried for WAIT
        seconds): while it holds the lock no server can be serving the socket or
        start to. Holding it, the harness unlinks the socket (only an AF_UNIX
        socket owned by this user), then the ``.lock`` (only while the path still
        names the inode it holds), then lets go. A lock it cannot take (held by a
        live process), a path of the wrong type or owner, or a socket with no
        ``.lock`` (nothing can prove it unserved) is left in place and reported."""
        import fcntl

        sock, lock = self.sock, _lock_path(self.sock)
        if not (os.path.lexists(sock) or os.path.lexists(lock)):
            return
        if not os.path.lexists(lock):
            problems.append(f"{sock} has no {lock.name} to prove it unserved; left in place")
            return
        try:
            fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as exc:
            problems.append(f"cannot open {lock} ({exc}); {sock} and {lock} left in place")
            return
        try:
            held = os.fstat(fd)
            if not stat.S_ISREG(held.st_mode) or held.st_uid != os.getuid():
                problems.append(f"{lock} is not a regular file owned by this user; left in place")
                return
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        problems.append(
                            f"{lock} is still held by a live process after {wait:.0f}s; "
                            f"{sock} and {lock} left in place"
                        )
                        return
                    time.sleep(0.2)
            if os.path.lexists(sock):
                st = os.lstat(sock)
                if stat.S_ISSOCK(st.st_mode) and st.st_uid == os.getuid():
                    self._unlink(sock, problems)
                else:
                    problems.append(f"{sock} is not a socket owned by this user; left in place")
            try:
                now = os.lstat(lock)
            except FileNotFoundError:
                now = None
            if now is not None and (now.st_dev, now.st_ino) == (held.st_dev, held.st_ino):
                self._unlink(lock, problems)
            elif now is not None:
                problems.append(f"{lock} was replaced during teardown; left in place")
        finally:
            os.close(fd)

    def teardown(self) -> list[str]:
        """Remove everything the harness created; touches only the spare label.
        Every removal that fails, and every artifact still present at the end
        (``lexists``: a dangling symlink counts), is a problem, so a teardown
        that did not clean up can never PASS."""
        problems: list[str] = []
        label = self.label
        self.release_holder()
        logs = _log_paths(label)  # read StandardErrorPath before the plist goes
        try:
            socket_in_plist = read_plist_field(label, "socket-path")
        except (CheckFailed, OSError, subprocess.SubprocessError):
            socket_in_plist = ""

        env = install_env(label, self.port)
        try:
            res = run_installer("uninstall", env, teardown=True)
            if res.returncode != 0 or leave_behind_warnings(res.stderr):
                problems.append(
                    f"uninstall: exit {res.returncode}: {(res.stderr or '').strip()[-200:]}"
                )
        except (OSError, subprocess.SubprocessError, InstallerNotStopped) as exc:
            problems.append(f"uninstall failed: {exc}")

        self.remove_spare_socket(problems)
        # Only the harness's own socket path is ever removed: a different one in
        # the plist is not provably the harness's, so it is reported, not touched.
        if socket_in_plist and socket_in_plist != str(self.sock):
            for path in (Path(socket_in_plist), _lock_path(socket_in_plist)):
                if os.path.lexists(path):
                    problems.append(f"{path} (recorded in the spare plist) left in place")
        if self.sock_dir_made and os.path.lexists(self.sock.parent):
            try:
                self.sock.parent.rmdir()
            except OSError as exc:
                problems.append(f"cannot remove {self.sock.parent}: {exc}")
        if self.token_dir is not None:
            try:
                shutil.rmtree(self.token_dir)
            except FileNotFoundError:
                pass
            except OSError as exc:
                problems.append(f"cannot remove {self.token_dir}: {exc}")
        plist = _plist_path(label)
        for path in [plist, *logs]:
            self._unlink(path, problems)
        try:
            loaded, _pid = require_launchd_state(label, self.uid)
        except RuntimeError as exc:
            hint = shell_command("launchctl", "print", f"gui/{self.uid}/{label}")
            problems.append(f"{exc}; check by hand: {hint}")
        else:
            if loaded:
                hint = shell_command("launchctl", "bootout", f"gui/{self.uid}/{label}")
                problems.append(f"{label} is still loaded; run: {hint}")
        residue = [plist, *logs, self.sock, _lock_path(self.sock)]
        if self.sock_dir_made:
            residue.append(self.sock.parent)
        if self.token_dir is not None:
            residue.append(self.token_dir)
        left = [p for p in residue if os.path.lexists(p)]
        if left:
            problems.append("left behind: " + ", ".join(str(p) for p in left))
        return problems


# --- main -------------------------------------------------------------------


def _print_table(results) -> None:
    """The table, then one failure-channel line per FAIL row
    (``_smoke_lib.LAUNCHD_FAIL_MARK``), which ``smoke.py`` reads."""
    width = max((len(n) for n, _s, _d in results), default=4)
    print()
    for name, status, detail in results:
        print(f"{status:<8} {name:<{width}}  {detail}".rstrip())
    for line in _smoke_lib.launchd_fail_lines(results):
        print(line)


def run_checks(harness, checks, rows: list) -> list:
    """Run CHECKS ((name, fn) pairs) in order, appending one row each to ROWS as
    it finishes, so an interrupt (which propagates) keeps the finished rows. Any
    exception is that check's FAIL row and the next check still runs, except a
    STOP_CHECKS one (a real agent changed, the spare port was lost, or the
    spare's launchd state cannot be read): the harness can no longer prove it is
    touching only its own port and label, so the remaining checks, which install
    the spare, are not run. Before every check the real agents are compared with
    the baseline, and before every check but the first the spare port must be
    the spare job's."""
    for i, (name, fn) in enumerate(checks):
        try:
            harness.ensure_real_agents_unchanged()
            if i:
                harness.ensure_port_ours()
            rows.append((name, PASS, fn()))
        except STOP_CHECKS as exc:
            prefix, why = next(v for k, v in _STOP_REASONS.items() if isinstance(exc, k))
            rows.append((name, FAIL, f"{prefix}: {exc}"))
            rows += [(n, SKIPPED, f"not run: {why}") for n, _f in checks[i + 1 :]]
            break
        except Exception as exc:  # noqa: BLE001 - any crash is this check's FAIL row
            detail = str(exc) or type(exc).__name__
            if not isinstance(exc, (CheckFailed, AssertionError)):
                detail = f"{type(exc).__name__}: {detail}"
            rows.append((name, FAIL, detail))
        finally:
            harness.release_holder()
    return rows


def signal_handler(state: dict):
    """SIGINT/SIGTERM handler: the first signal sets ``tearing_down`` and raises
    KeyboardInterrupt; every later one is ignored, so a second signal cannot
    escape main or cut the teardown short."""

    def on_signal(signum, _frame):
        if not state["tearing_down"]:
            state["tearing_down"] = True
            raise KeyboardInterrupt(f"signal {signum}")

    return on_signal


@contextlib.contextmanager
def scrubbed_environ():
    """Replace this process's environment with ``_smoke_lib.harness_env`` of
    it for the block (so every subprocess inherits only that), and restore it
    afterwards."""
    saved = dict(os.environ)
    clean = _smoke_lib.harness_env(saved)
    os.environ.clear()
    os.environ.update(clean)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Opt-in launchd verification on a spare label and port. "
        "PIPECAT_STT_LABEL and KODA_STT_LABEL must both be set explicitly to "
        f"{SPARE_LABEL}, e.g. PIPECAT_STT_LABEL={SPARE_LABEL} "
        f"KODA_STT_LABEL={SPARE_LABEL} .venv/bin/python scripts/smoke_launchd.py",
    )
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="spare TCP port (default 9998)")
    ap.add_argument("--label", default=SPARE_LABEL, help=f"must be {SPARE_LABEL} (guarded)")
    args = ap.parse_args(argv)

    reason = preconditions()
    if reason:
        print(_smoke_lib.LAUNCHD_SKIP_MARK + reason)
        return 0
    with scrubbed_environ():
        return _scrubbed_main(args)


def _scrubbed_main(args) -> int:
    """main after the preconditions, in the scrubbed environment."""
    # Both label variables must already name the spare; the environment goes to
    # the guard as-is (unset or differing is refused, never filled in).
    env = dict(os.environ)

    results: list[tuple[str, str, str]] = []

    def refuse(what: str, detail: str, cmds=()) -> int:
        results.append((what, FAIL, detail))
        _print_table(results)
        for cmd in cmds:
            print(f"  cleanup: {cmd}")
        return 1

    home, agents_at = _home(), _smoke_lib.agents_home()
    if home.resolve() != agents_at.resolve():
        print(
            f"warning: HOME={home} is not the account home {agents_at}; the spare goes "
            f"under $HOME, the real-agent snapshot and port guard read {agents_at}, and "
            "the start-state check refuses a spare plist under either",
            file=sys.stderr,
        )

    # One run at a time: two runs share the spare label, plist, socket and logs,
    # and one's teardown would remove the other's agent.
    try:
        lock_fd = acquire_run_lock(run_lock_path())
    except GuardRefused as exc:
        return refuse("lock", str(exc))
    try:
        return _locked_main(args, env, results, refuse, agents_at)
    finally:
        os.close(lock_fd)


def _locked_main(args, env, results, refuse, agents_at) -> int:
    """Everything after the run lock: guard, start state, snapshot, checks,
    teardown, re-snapshot."""
    # The baseline comes first: the guard's port set is derived from it (every
    # port a real job serves or a real plist intends), so the two cannot read
    # the real agents differently.
    try:
        uid = current_uid()
        before = snapshot_real_agents()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return refuse("snapshot", f"cannot snapshot the real agents: {exc}")
    # An unreadable baseline field can never compare as unchanged, so the run
    # could not prove it left that agent alone: refuse before the first install.
    unknown = snapshot_unknowns(before)
    if unknown:
        return refuse(
            "snapshot",
            f"cannot read {'; '.join(unknown)} (listener: lsof failed or a running job "
            "with no endpoint in its arguments; runs: launchctl print showed a pid but "
            "no `runs =` line; plist/plist_port: an unreadable plist); nothing was "
            "installed",
        )
    try:
        ports = snapshot_ports(before)
    except GuardRefused as exc:
        return refuse("guard", str(exc))
    try:
        check_guard(
            args.label,
            args.port,
            env=env,
            canonical_ports=canonical_ports(),
            is_listening=is_listening,
            agent_ports=ports,
        )
    except GuardRefused as exc:
        return refuse("guard", str(exc))

    try:
        cmds = start_state_problems(args.label, uid, spare_socket_path())
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return refuse("start state", f"cannot read the start state: {exc}")
    if cmds:
        return refuse("start state", "spare state already exists; clean it up first", cmds)
    imp = subprocess.run(
        [sys.executable, "-I", "-c", "import stt_server"],
        capture_output=True,
        text=True,
        timeout=CMD_TIMEOUT * 2,
    )
    if imp.returncode != 0:
        return refuse(
            "start state", "stt_server does not import under python -I (editable install?)"
        )

    state = {"tearing_down": False}
    old = {s: signal.signal(s, signal_handler(state)) for s in (signal.SIGINT, signal.SIGTERM)}
    harness = Harness(args.label, args.port, baseline=before)
    interrupted = False
    try:
        run_checks(
            harness,
            (
                ("port collision", harness.check_collision),
                ("socket rollback", harness.check_rollback),
                ("token file", harness.check_token),
            ),
            results,
        )
    except (KeyboardInterrupt, SystemExit) as exc:
        interrupted = True
        results.append(("interrupted", FAIL, str(exc) or type(exc).__name__))
    finally:
        state["tearing_down"] = True
        try:
            problems = harness.teardown()
        except Exception as exc:  # teardown must not mask the checks' results
            problems = [f"teardown crashed: {exc}"]
        results.append(
            ("teardown", FAIL if problems else PASS, "; ".join(problems) or "spare agent removed")
        )
        try:
            diffs = compare_snapshots(before, snapshot_real_agents())
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            diffs = [f"cannot re-snapshot the real agents: {exc}"]
        results.append(
            (
                "real agents unchanged",
                FAIL if diffs else PASS,
                "; ".join(diffs)
                or f"{len(before)} agents (plists under {agents_at}, plus loaded jobs): "
                "same launchd state, plists and listeners",
            )
        )
        for s, h in old.items():
            signal.signal(s, h)

    _print_table(results)
    failed = interrupted or any(s == FAIL for _n, s, _d in results)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
