#!/usr/bin/env python3
"""Opt-in launchd verification: port collision, socket rollback, token file.

Runs the checks 0.5.0 covered only with stubs against REAL launchd, using a spare
LaunchAgent (label ``pipecat.stt-server.smoke``, the ``echo`` backend, port 9998)
installed through ``scripts/install_stt_agent.sh``. No real ``pipecat.stt-server*``
agent is touched, and the harness proves it by comparing each real agent's
launchd pid and listener pid before and after.

Usage::

    PIPECAT_STT_LABEL=pipecat.stt-server.smoke KODA_STT_LABEL=pipecat.stt-server.smoke .venv/bin/python scripts/smoke_launchd.py

Both ``PIPECAT_STT_LABEL`` and ``KODA_STT_LABEL`` must be set explicitly to the
spare label; an unset or different value is refused (never filled in).

Never run it with plain ``uv run`` (that prunes the backend extras); the installer
is handed the running interpreter through ``PIPECAT_STT_PYTHON``.

Order: preconditions (darwin, ``CI`` unset; otherwise ``SKIPPED``, exit 0) ->
run lock (an exclusive non-blocking ``flock`` on
``<agents home>/Library/Caches/pipecat-stt-smoke-launchd.lock``; a second run
refuses and touches nothing; the lock is held until the process exits) ->
guard (exact spare label in both ``PIPECAT_STT_LABEL`` and ``KODA_STT_LABEL``, a
port that is not canonical, not recorded in an installed real agent's plist and
not already listening) -> start state (no spare
plist, loaded job, socket or ``.lock``) -> snapshot of the real agents -> the
three checks inside a try/finally -> teardown -> re-snapshot and compare. A guard
or start-state refusal is a FAIL and happens before any ``launchctl`` bootstrap or
bootout and before any installer call. Teardown always runs after that point
(failure, exception, SIGINT, SIGTERM): the installer's ``uninstall`` for the spare
label, then the spare ``.lock`` once nothing holds it, then the spare plist and
logs. It never touches another label.

The real agents are every installed ``pipecat.stt-server*`` plist under
``_smoke_lib.agents_home()`` (the account's home from the password database, not
``$HOME``, so an overridden ``HOME`` cannot empty the before/after snapshot) plus
every loaded ``pipecat.stt-server*`` job ``launchctl list`` reports, plist or not;
the spare is excluded. A loaded job without a plist has its pid and endpoint
read from one ``launchctl print`` (its ``pid =`` line and ``arguments`` block).
The test-only ``PIPECAT_STT_SMOKE_TEST_AGENTS_HOME`` moves the plist home only
while ``launchctl`` is a stub (announced on stderr); set with the real
``launchctl`` it is refused (see ``_smoke_lib``). It cannot hide a loaded job,
which ``launchctl list`` still reports. A listener the harness cannot read never
compares as unchanged, so one in the baseline snapshot refuses the run before
the first install.

The spare port is held for the whole run: if the collision check cannot bind it,
or a later check finds a listener on it that is not the spare job, the run FAILs
there (the remaining checks are not run) and teardown still runs.
Listeners (``lsof``) and a labelled job's (loaded, pid) state (``launchctl
print``) are read through ``squat_check.py``'s helpers (one definition each).
Two reads live here: the job list (``launchctl list``) and a plistless job's
``launchctl print`` (its arguments block, pid parsed with
``squat_check.parse_launchd_pid``). A ``launchctl`` or ``lsof`` answer the
harness cannot read refuses or FAILs, never reads as "not loaded" or "free".

Exit 0 unless a requested step FAILed. The external tools (``launchctl``,
``lsof``, ``id``, ``bash``, ``python3``) are called by bare name through PATH.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import os
import plistlib
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
INSTALLER = SCRIPTS / "install_stt_agent.sh"
READ_PLIST = SCRIPTS / "read_stt_plist.py"
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

# Env names the installer reads. All are scrubbed from the child env so nothing
# inherited steers an install; the harness sets what it needs per call.
_SCRUB_PREFIXES = ("PIPECAT_STT_", "KODA_STT_")
_KEEP = ("PIPECAT_STT_LOG_DIR", "KODA_STT_LOG_DIR")
_WARN_RE = re.compile(
    r"leaving|still loaded|uninstall incomplete|may still hold|could not remove", re.IGNORECASE
)
_IN_USE_RE = re.compile(r"address already in use|errno 48", re.IGNORECASE)

PASS, FAIL, SKIPPED = "PASS", "FAIL", "SKIPPED"


class GuardRefused(Exception):
    """The harness refused to run: the label, port or environment is not safe."""


class CheckFailed(Exception):
    """A check's assertion did not hold."""


class PortLost(CheckFailed):
    """Something other than the spare job holds (or may hold) the spare port."""


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
    the installed plist; the installer's naming rule (the label with '.' replaced
    by '-') only for a key the plist lacks, or when there is no plist yet."""
    base = label.replace(".", "-")
    paths = {
        "StandardErrorPath": _log_dir() / f"{base}.err",
        "StandardOutPath": _log_dir() / f"{base}.log",
    }
    try:
        with open(_plist_path(label), "rb") as fh:
            data = plistlib.load(fh)
        for key in paths:
            val = data.get(key)
            if isinstance(val, str) and val:
                paths[key] = Path(val)
    except (OSError, ValueError, plistlib.InvalidFileException):
        pass
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


def current_uid(run=subprocess.run) -> str:
    res = _run(["id", "-u"], run)
    uid = (res.stdout or "").strip()
    if res.returncode != 0 or not uid.isdigit():
        raise RuntimeError("cannot read the uid with `id -u`")
    return uid


def require_launchd_state(label: str, uid: str, run=subprocess.run) -> tuple[bool, str | None]:
    """(loaded, pid) from read-only ``launchctl print`` (squat_check's reader).

    Raises RuntimeError when launchctl's answer is not "loaded" or launchctl's
    own "could not find service": an unreadable state never reads as unloaded."""
    state = squat_check.launchd_state(label, run, uid=uid)
    if state is None:
        raise RuntimeError(f"cannot read the launchd state of {label} (launchctl print failed)")
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


def print_job(label: str, uid: str, run=subprocess.run) -> tuple[str | None, dict[str, str]]:
    """(launchd pid, serve flags) from ONE read-only ``launchctl print`` of a
    loaded job: its ``pid =`` line (``squat_check.parse_launchd_pid``) and the
    flags (``--port``, ``--socket-path``, ...) in its ``arguments`` block.
    Raises RuntimeError when unreadable."""
    try:
        res = _run(["launchctl", "print", f"gui/{uid}/{label}"], run)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"cannot read {label} with launchctl print: {exc}") from exc
    if res.returncode != 0:
        raise RuntimeError(f"cannot read {label} with launchctl print (exit {res.returncode})")
    m = re.search(r"^\s*arguments = \{\s*\n(.*?)^\s*\}", res.stdout or "", re.MULTILINE | re.DOTALL)
    args = [ln.strip() for ln in m.group(1).splitlines() if ln.strip()] if m else []
    pid = squat_check.parse_launchd_pid(res.stdout)
    return pid, read_stt_plist.read_flags({"ProgramArguments": args})


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


def read_plist_field(label: str, field: str, run=subprocess.run) -> str:
    return read_plist_field_path(_plist_path(label), field, run)


def read_plist_field_path(plist: Path, field: str, run=subprocess.run, *, strict=False) -> str:
    """One field from PLIST; ``strict`` makes a malformed plist an error, not ''."""
    mode = ["--strict"] if strict else []
    res = _run([sys.executable, "-I", str(READ_PLIST), *mode, "--field", field, str(plist)], run)
    if res.returncode != 0:
        raise CheckFailed(f"cannot read {field} from {plist}: {(res.stderr or '').strip()}")
    return res.stdout or ""


# --- guard ------------------------------------------------------------------


def preconditions(env=os.environ, platform=sys.platform) -> str | None:
    """A reason to skip (not fail) on this machine, or None."""
    return _smoke_lib.launchd_skip_reason(env, platform)


def run_lock_path() -> Path:
    """The run lock. Under the agents home (the account home; the test override
    in stubbed runs), not ``$HOME``: the spare label lives in the per-uid launchd
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
            res = _run(["bash", str(INSTALLER), "port", backend], run)
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


def plistless_jobs(
    uid: str, run=subprocess.run, home: Path | None = None
) -> dict[str, tuple[str | None, dict[str, str]]]:
    """label -> (pid, serve flags) for each loaded real job with no installed
    plist, one ``launchctl print`` each."""
    have = {p.name[: -len(".plist")] for p in real_agent_plists(home)}
    return {label: print_job(label, uid, run) for label in sorted(loaded_agent_labels(run) - have)}


def plistless_agents(
    uid: str, run=subprocess.run, home: Path | None = None
) -> dict[str, dict[str, str]]:
    """label -> serve flags for each loaded real job with no installed plist."""
    return {label: flags for label, (_pid, flags) in plistless_jobs(uid, run, home).items()}


def agent_ports(
    home: Path | None = None, run=subprocess.run, *, plistless: dict | None = None
) -> dict[int, str]:
    """port -> label for each real agent's TCP port: recorded in its installed
    plist, or (PLISTLESS, from ``plistless_agents``) in a loaded job's arguments."""
    ports: dict[int, str] = {}
    for label, flags in (plistless or {}).items():
        port = flags.get("--port", "")
        if port.isdigit():
            ports[int(port)] = label
    for plist in real_agent_plists(home):
        try:
            port = read_plist_field_path(plist, "port", run, strict=True).strip()
        except (CheckFailed, OSError, subprocess.SubprocessError) as exc:
            raise GuardRefused(f"cannot read the port recorded in {plist}: {exc}") from exc
        if port.isdigit():
            ports[int(port)] = plist.name[: -len(".plist")]
    return ports


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


def _listener(port: str, sock: str, run) -> str | None:
    """Comma-joined listener (or ``.lock`` holder) pids, None when there is none,
    UNKNOWN when lsof cannot tell or there is no endpoint to watch."""
    if port:
        pids = tcp_listener_pids(port, run)
    elif sock:
        pids = lock_holders(Path(sock), run)
    else:
        return UNKNOWN  # no endpoint: nothing can prove it unchanged
    return UNKNOWN if pids is None else (",".join(pids) or None)


def snapshot_real_agents(
    run=subprocess.run, home: Path | None = None
) -> dict[str, tuple[str | None, str | None]]:
    """label -> (launchd pid, listener pid) for each real agent: every installed
    pipecat.stt-server* plist under ``agents_home()`` and every loaded
    pipecat.stt-server* job without one, the spare excluded. Read-only calls
    only; a launchd state it cannot read raises, a listener it cannot read (or a
    plist it cannot parse) is UNKNOWN, which never compares as unchanged."""
    uid = current_uid(run)
    snap: dict[str, tuple[str | None, str | None]] = {}
    for plist in real_agent_plists(home):
        label = plist.name[: -len(".plist")]
        _loaded, pid = require_launchd_state(label, uid, run)
        try:
            port = read_plist_field_path(plist, "port", run).strip()
            # The path exactly as recorded (read_stt_plist adds no newline).
            sock = read_plist_field_path(plist, "socket-path", run)
            listener = _listener(port, sock, run)
        except (CheckFailed, OSError, subprocess.SubprocessError):
            listener = UNKNOWN
        snap[label] = (pid, listener)
    for label, (pid, flags) in plistless_jobs(uid, run, home).items():
        listener = _listener(flags.get("--port", ""), flags.get("--socket-path", ""), run)
        snap[label] = (pid, listener)
    return snap


def snapshot_unknowns(snap) -> list[str]:
    """Labels whose listener the snapshot could not read (sorted)."""
    return sorted(label for label, (_pid, listener) in snap.items() if listener == UNKNOWN)


def compare_snapshots(before, after) -> list[str]:
    """Human-readable differences between two snapshots; empty means unchanged."""
    diffs = []
    for label in sorted(set(before) | set(after)):
        if label not in after:
            diffs.append(f"{label}: disappeared (was pid/listener {before[label]})")
        elif label not in before:
            diffs.append(f"{label}: appeared (pid/listener {after[label]})")
        else:
            b, a = before[label], after[label]
            if b[0] != a[0]:
                diffs.append(f"{label}: launchd pid {b[0]} -> {a[0]}")
            if UNKNOWN in (b[1], a[1]):
                diffs.append(
                    f"{label}: listener unreadable ({b[1]} -> {a[1]}); cannot prove it unchanged"
                )
            elif b[1] != a[1]:
                diffs.append(f"{label}: listener pid {b[1]} -> {a[1]}")
    return diffs


# --- start state ------------------------------------------------------------


def start_state_problems(label: str, uid: str, sock: Path, run=subprocess.run) -> list[str]:
    """Cleanup commands for each piece of spare state that already exists."""
    cmds = []
    loaded, _pid = require_launchd_state(label, uid, run)
    if loaded:
        cmds.append(f"launchctl bootout gui/{uid}/{label}")
    plist = _plist_path(label)
    if plist.exists() or plist.is_symlink():
        cmds.append(f"rm -f {plist}")
    # Teardown unlinks the spare's logs, so they must be the harness's own: logs
    # already at those paths (an earlier run, or a label sharing the basename)
    # refuse the run instead.
    for path in (sock, _lock_path(sock), *_log_paths(label)):
        if path.exists() or path.is_symlink():
            cmds.append(f"rm -f {path}")
    return cmds


# --- installer --------------------------------------------------------------


def install_env(label: str, port: int, *, socket_path=None, token_file=None, base=None) -> dict:
    env = {
        k: v
        for k, v in (base if base is not None else os.environ).items()
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


def run_installer(
    sub: str, env: dict, run=subprocess.run, *, new_session: bool = False
) -> subprocess.CompletedProcess:
    """``new_session`` puts the installer in its own session, so a terminal
    Ctrl-C (sent to the whole foreground group) cannot kill it mid-teardown."""
    res = run(
        ["bash", str(INSTALLER), sub],
        capture_output=True,
        text=True,
        timeout=INSTALL_TIMEOUT,
        env=env,
        cwd=REPO_ROOT,
        start_new_session=new_session,
    )
    return res


def install(env: dict, what: str) -> str:
    """Run the installer's install; a non-zero exit or a leave-behind warning FAILs."""
    res = run_installer("install", env)
    err = res.stderr or ""
    if res.returncode != 0:
        raise CheckFailed(f"install ({what}) exited {res.returncode}: {err.strip()[-300:]}")
    if _WARN_RE.search(err):
        raise CheckFailed(f"install ({what}) warned about a leftover: {err.strip()[-300:]}")
    return res.stdout or ""


# --- checks -----------------------------------------------------------------


class Harness:
    def __init__(self, label: str, port: int, inject: str | None):
        self.label = label
        self.port = port
        self.inject = inject
        self.uid = current_uid()
        self.sock = spare_socket_path()
        self.sock_dir_made = False
        self.token_dir: Path | None = None
        self.holder = None  # the collision check's port occupier
        self.injected = False

    # -- hooks, waits

    def maybe_inject(self) -> None:
        """Test hooks, fired once, right after the first install step."""
        if self.injected or not self.inject:
            return
        self.injected = True
        if self.inject == "fail":
            raise CheckFailed("injected failure (--inject fail)")
        if self.inject == "crash":
            raise LookupError("injected crash (--inject crash)")
        if self.inject == "hang":
            print("inject: hanging until interrupted", flush=True)
            while True:
                time.sleep(1)

    def wait_for(self, what: str, pred, timeout: float = UP_TIMEOUT, every: float = 1.0):
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

    def tcp_up(self, token: str | None = None) -> bool:
        return _smoke_lib.ws_handshake_status(host=HOST, port=self.port, token=token) == 101

    def sock_up(self) -> bool:
        return _smoke_lib.ws_handshake_status(unix_path=str(self.sock)) == 101

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
        self.maybe_inject()
        errlog = _err_log(self.label)  # now from the plist's StandardErrorPath
        offset = offsets.get(errlog, _size(errlog))
        deadline = time.monotonic() + COLLISION_TIMEOUT
        in_use = 0
        warned = False
        while time.monotonic() < deadline:
            text = _read_from(errlog, offset)
            in_use = _in_use_lines(text)
            if in_use >= 2:
                loaded, pid = require_launchd_state(self.label, self.uid)
                warned = self.squat_warns(loaded, pid)
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
        return f"{in_use} address-in-use lines, squat warning, agent up after free"

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

    def release_holder(self) -> None:
        if self.holder is not None:
            with contextlib.suppress(OSError):
                self.holder.close()
            self.holder = None

    def squat_warns(self, loaded: bool, pid: str | None) -> bool:
        res = _run(
            [
                "python3",
                "-I",
                str(SQUAT_CHECK),
                "--host",
                HOST,
                "--port",
                str(self.port),
                "--loaded",
                "1" if loaded else "0",
                "--pid",
                pid or "",
                "--label",
                self.label,
            ],
            timeout=CMD_TIMEOUT * 2,
        )
        return res.returncode == 3 and "squat-check: warning:" in (res.stderr or "")

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
        if "removing the old socket" not in out:
            raise CheckFailed("installer did not report removing the old socket")
        if sock.exists() or sock.is_symlink():
            raise CheckFailed(f"old socket {sock} still exists after the move back to TCP")
        if not lock.exists():
            raise CheckFailed(f"{lock} was removed; the installer must leave it")
        self.wait_for(f"{lock} to be unheld", lambda: lock_unheld(lock), timeout=10)
        return (
            "tcp -> socket -> tcp; old socket removed on the installer's "
            "unload-confirmed path, .lock left and unheld"
        )

    # -- check: token file

    def check_token(self) -> str:
        from stt_server.auth import load_token_file

        self.token_dir = Path(tempfile.mkdtemp(prefix="pipecat-smoke-token-", dir=_home()))
        os.chmod(self.token_dir, 0o700)
        token_file = self.token_dir / "token"
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_urlsafe(24) + "\n")
        # The server's own policy must accept it before launchd is asked to.
        try:
            resolved, token = load_token_file(str(token_file), uid=int(self.uid))
        except ValueError as exc:
            raise CheckFailed(f"token file policy refused the harness file: {exc}") from exc

        install(install_env(self.label, self.port, token_file=token_file), "token")
        if read_plist_field(self.label, "auth-token-file") != resolved:
            raise CheckFailed("plist does not record the resolved token file path")
        self.wait_for("the token agent to accept the token", lambda: self.tcp_up(token))
        try:
            _smoke_lib.assert_token_gate(HOST, self.port, str(token_file))
        except AssertionError as exc:
            raise CheckFailed(str(exc)) from exc
        return "no token -> 401, wrong token -> 401, token -> 101"

    # -- teardown

    def teardown(self) -> list[str]:
        """Remove everything the harness created. Touches only the spare label."""
        problems: list[str] = []
        label = self.label
        self.release_holder()
        logs = _log_paths(label)  # read StandardErrorPath before the plist goes
        try:
            socket_in_plist = read_plist_field(label, "socket-path")
        except (CheckFailed, OSError, subprocess.SubprocessError):
            socket_in_plist = ""
        socks = {str(self.sock)} | ({socket_in_plist} if socket_in_plist else set())

        env = install_env(label, self.port)
        try:
            res = run_installer("uninstall", env, new_session=True)
            if res.returncode != 0 or _WARN_RE.search(res.stderr or ""):
                problems.append(
                    f"uninstall: exit {res.returncode}: {(res.stderr or '').strip()[-200:]}"
                )
        except (OSError, subprocess.SubprocessError) as exc:
            problems.append(f"uninstall failed: {exc}")

        for sock in sorted(socks):
            lock = _lock_path(sock)
            if lock.exists() or lock.is_symlink():
                try:
                    self.wait_for(
                        f"{lock} to be unheld", lambda lock=lock: lock_unheld(lock), timeout=10
                    )
                except CheckFailed as exc:
                    problems.append(str(exc))
                    continue
                with contextlib.suppress(OSError):
                    lock.unlink()
            # The installer removes a recorded socket; sweep a leftover spare one.
            if sock == str(self.sock) and (Path(sock).exists() or Path(sock).is_symlink()):
                with contextlib.suppress(OSError):
                    Path(sock).unlink()
        if self.sock_dir_made:
            with contextlib.suppress(OSError):
                self.sock.parent.rmdir()
        if self.token_dir is not None:
            shutil.rmtree(self.token_dir, ignore_errors=True)
        plist = _plist_path(label)
        for path in [plist, *logs]:
            with contextlib.suppress(OSError):
                path.unlink()
        try:
            loaded, _pid = require_launchd_state(label, self.uid)
        except RuntimeError as exc:
            problems.append(f"{exc}; check by hand: launchctl print gui/{self.uid}/{label}")
        else:
            if loaded:
                problems.append(
                    f"{label} is still loaded; run: launchctl bootout gui/{self.uid}/{label}"
                )
        left = [p for p in [plist, *logs, self.sock, _lock_path(self.sock)] if p.exists()]
        if left:
            problems.append("left behind: " + ", ".join(str(p) for p in left))
        return problems


# --- main -------------------------------------------------------------------


def _print_table(results) -> None:
    width = max((len(n) for n, _s, _d in results), default=4)
    print()
    for name, status, detail in results:
        print(f"{status:<8} {name:<{width}}  {detail}".rstrip())


def run_checks(harness, checks) -> list[tuple[str, str, str]]:
    """Run CHECKS ((name, fn) pairs) in order, one row each. Any exception is that
    check's FAIL row and the next check still runs, except PortLost: the spare
    port is no longer the harness's, so the remaining checks are not run (they
    would install the spare onto a port someone else holds)."""
    rows: list[tuple[str, str, str]] = []
    for i, (name, fn) in enumerate(checks):
        try:
            if i:
                harness.ensure_port_ours()
            rows.append((name, PASS, fn()))
        except PortLost as exc:
            rows.append((name, FAIL, f"port lost: {exc}"))
            rows += [(n, SKIPPED, "not run: the spare port was lost") for n, _f in checks[i + 1 :]]
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Opt-in launchd verification on a spare label and port. "
        "PIPECAT_STT_LABEL and KODA_STT_LABEL must both be set explicitly to "
        f"{SPARE_LABEL}, e.g. PIPECAT_STT_LABEL={SPARE_LABEL} "
        f"KODA_STT_LABEL={SPARE_LABEL} .venv/bin/python scripts/smoke_launchd.py",
        epilog="--inject is a test hook for this harness's own tests, not for normal use.",
    )
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="spare TCP port (default 9998)")
    ap.add_argument("--label", default=SPARE_LABEL, help=f"must be {SPARE_LABEL} (guarded)")
    ap.add_argument(
        "--inject",
        choices=("fail", "crash", "hang"),
        help="TEST HOOK: 'fail' raises in the first check right after its install; "
        "'crash' raises an unexpected exception type there; "
        "'hang' sleeps there so a test can send SIGINT. Teardown must still run.",
    )
    args = ap.parse_args(argv)

    reason = preconditions()
    if reason:
        print(f"SKIPPED: {reason}")
        return 0

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

    # A stale export of the test-only override (launchctl is the real one) would
    # hide installed agents from the guard and snapshot: refuse before any call.
    notice = _smoke_lib.agents_home_notice()
    if _smoke_lib.ignored_agents_home_override() is not None:
        return refuse("guard", notice)
    home, agents_at = _home(), _smoke_lib.agents_home()
    if notice:
        print(notice, file=sys.stderr)
    elif home.resolve() != agents_at.resolve():
        print(
            f"warning: HOME={home} is not the account home {agents_at}; the spare goes "
            f"under $HOME, the real-agent snapshot and port guard read {agents_at}",
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
    try:
        uid = current_uid()
        plistless = plistless_agents(uid)
        ports = agent_ports(plistless=plistless)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return refuse("guard", f"cannot read the real agents: {exc}")
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

    try:
        before = snapshot_real_agents()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return refuse("snapshot", f"cannot snapshot the real agents: {exc}")
    # An unreadable baseline listener can never compare as unchanged, so the run
    # could not prove it left that agent alone: refuse before the first install.
    unknown = snapshot_unknowns(before)
    if unknown:
        return refuse(
            "snapshot",
            f"cannot read the listener of {', '.join(unknown)} (lsof failed, or no "
            "endpoint in its plist or arguments); nothing was installed",
        )

    state = {"tearing_down": False}
    old = {s: signal.signal(s, signal_handler(state)) for s in (signal.SIGINT, signal.SIGTERM)}
    harness = Harness(args.label, args.port, args.inject)
    interrupted = False
    try:
        results += run_checks(
            harness,
            (
                ("port collision", harness.check_collision),
                ("socket rollback", harness.check_rollback),
                ("token file", harness.check_token),
            ),
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
                or f"{len(before)} agents (plists under {agents_at}, plus loaded jobs), same pids",
            )
        )
        for s, h in old.items():
            signal.signal(s, h)

    _print_table(results)
    failed = interrupted or any(s == FAIL for _n, s, _d in results)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
