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
guard (exact spare label in both ``PIPECAT_STT_LABEL`` and ``KODA_STT_LABEL``, a
port that is neither canonical nor already listening) -> start state (no spare
plist, loaded job, socket or ``.lock``) -> snapshot of the real agents -> the
three checks inside a try/finally -> teardown -> re-snapshot and compare. A guard
or start-state refusal is a FAIL and happens before any ``launchctl`` bootstrap or
bootout and before any installer call. Teardown always runs after that point
(failure, exception, SIGINT, SIGTERM): the installer's ``uninstall`` for the spare
label, then the spare ``.lock`` once nothing holds it, then the spare plist and
logs. It never touches another label.

Exit 0 unless a requested step FAILed. The external tools (``launchctl``,
``lsof``, ``id``, ``bash``, ``python3``) are called by bare name through PATH.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import plistlib
import re
import secrets
import shutil
import signal
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

SPARE_LABEL = "pipecat.stt-server.smoke"
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
_WARN_RE = re.compile(r"leaving|still loaded|uninstall incomplete", re.IGNORECASE)
_IN_USE_RE = re.compile(r"address already in use|errno 48", re.IGNORECASE)

PASS, FAIL, SKIPPED = "PASS", "FAIL", "SKIPPED"


class GuardRefused(Exception):
    """The harness refused to run: the label, port or environment is not safe."""


class CheckFailed(Exception):
    """A check's assertion did not hold."""


# --- environment, paths ----------------------------------------------------


def _home() -> Path:
    return Path(os.environ.get("HOME") or str(Path.home()))


def _plist_path(label: str) -> Path:
    return _home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _log_dir() -> Path:
    custom = os.environ.get("PIPECAT_STT_LOG_DIR") or os.environ.get("KODA_STT_LOG_DIR")
    return Path(custom) if custom else _home() / "Library" / "Logs" / "pipecat-stt"


def _log_paths(label: str) -> list[Path]:
    """The spare's log files: from the plist when installed, else by the
    installer's naming rule (basename is the label with '.' replaced by '-')."""
    base = label.replace(".", "-")
    paths = [_log_dir() / f"{base}.err", _log_dir() / f"{base}.log"]
    try:
        with open(_plist_path(label), "rb") as fh:
            data = plistlib.load(fh)
        for key in ("StandardErrorPath", "StandardOutPath"):
            val = data.get(key)
            if isinstance(val, str) and val and Path(val) not in paths:
                paths.append(Path(val))
    except (OSError, ValueError, plistlib.InvalidFileException):
        pass
    return paths


def _err_log(label: str) -> Path:
    return _log_paths(label)[0]


def spare_socket_path() -> Path:
    """A spare-specific socket path, so it can never be a real agent's socket."""
    return _home() / "Library" / "Caches" / "pipecat-stt-smoke-launchd" / "spare.sock"


def _lock_path(sock: Path | str) -> Path:
    return Path(f"{sock}.lock")


# --- process helpers (all by bare name through PATH) ----------------------


def _run(cmd, run=subprocess.run, timeout=CMD_TIMEOUT, **kw):
    return run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def current_uid(run=subprocess.run) -> str:
    res = _run(["id", "-u"], run)
    uid = (res.stdout or "").strip()
    if res.returncode != 0 or not uid.isdigit():
        raise RuntimeError("cannot read the uid with `id -u`")
    return uid


def launchd_state(label: str, uid: str, run=subprocess.run) -> tuple[bool, str | None]:
    """(loaded, pid) from ``launchctl print``. Read-only."""
    res = _run(["launchctl", "print", f"gui/{uid}/{label}"], run)
    if res.returncode != 0:
        return False, None
    m = re.search(r"^\s*pid = (\d+)", res.stdout or "", re.MULTILINE)
    return True, (m.group(1) if m else None)


def _lsof_pids(args: list[str], run=subprocess.run) -> list[str]:
    """Pids from ``lsof -Fp`` for the given selection; [] when none or lsof fails."""
    try:
        res = _run(["lsof", "-nP", *args, "-Fp"], run)
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({ln[1:] for ln in (res.stdout or "").splitlines() if ln.startswith("p")})


def tcp_listener_pids(port: int | str, run=subprocess.run) -> list[str]:
    return _lsof_pids([f"-iTCP:{port}", "-sTCP:LISTEN"], run)


def lock_holders(lock: Path, run=subprocess.run) -> list[str]:
    return _lsof_pids(["--", str(lock)], run)


def is_listening(port: int, run=subprocess.run) -> bool:
    """True when something listens on PORT (lsof, then a loopback connect)."""
    if tcp_listener_pids(port, run):
        return True
    import socket

    try:
        socket.create_connection((HOST, port), timeout=1).close()
    except OSError:
        return False
    return True


def read_plist_field(label: str, field: str, run=subprocess.run) -> str:
    res = _run(
        [sys.executable, "-I", str(READ_PLIST), "--field", field, str(_plist_path(label))],
        run,
    )
    if res.returncode != 0:
        raise CheckFailed(f"cannot read plist field {field}: {(res.stderr or '').strip()}")
    return res.stdout or ""


# --- guard ------------------------------------------------------------------


def preconditions(env=os.environ, platform=sys.platform) -> str | None:
    """A reason to skip (not fail) on this machine, or None."""
    if platform != "darwin":
        return f"launchd is macOS only (platform is {platform})"
    if env.get("CI"):
        return "CI is set; the launchd checks are opt-in and never run in CI"
    return None


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


def check_guard(label, port, *, env=os.environ, canonical_ports, is_listening) -> None:
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
    if is_listening(port):
        raise GuardRefused(f"port {port} already has a listener")


# --- real-agent snapshot ----------------------------------------------------


def snapshot_real_agents(run=subprocess.run) -> dict[str, tuple[str | None, str | None]]:
    """label -> (launchd pid, listener pid) for each installed pipecat.stt-server*
    plist except the spare. Read-only calls only."""
    uid = current_uid(run)
    snap: dict[str, tuple[str | None, str | None]] = {}
    agents = _home() / "Library" / "LaunchAgents"
    for plist in sorted(agents.glob("pipecat.stt-server*.plist")):
        label = plist.name[: -len(".plist")]
        if label == SPARE_LABEL:
            continue
        _loaded, pid = launchd_state(label, uid, run)
        listener = None
        try:
            port = read_plist_field_path(plist, "port", run)
            sock = read_plist_field_path(plist, "socket-path", run)
            if port:
                pids = tcp_listener_pids(port, run)
            elif sock:
                pids = _lsof_pids(["--", sock], run)
            else:
                pids = []
            listener = ",".join(pids) or None
        except (CheckFailed, OSError, subprocess.SubprocessError):
            listener = None
        snap[label] = (pid, listener)
    return snap


def read_plist_field_path(plist: Path, field: str, run=subprocess.run) -> str:
    res = _run([sys.executable, "-I", str(READ_PLIST), "--field", field, str(plist)], run)
    if res.returncode != 0:
        raise CheckFailed(f"cannot read {field} from {plist}")
    return res.stdout or ""


def compare_snapshots(before, after) -> list[str]:
    """Human-readable differences between two snapshots; empty means unchanged."""
    diffs = []
    for label in sorted(set(before) | set(after)):
        if label not in after:
            diffs.append(f"{label}: disappeared (was pid/listener {before[label]})")
        elif label not in before:
            diffs.append(f"{label}: appeared (pid/listener {after[label]})")
        elif before[label] != after[label]:
            b, a = before[label], after[label]
            if b[0] != a[0]:
                diffs.append(f"{label}: launchd pid {b[0]} -> {a[0]}")
            if b[1] != a[1]:
                diffs.append(f"{label}: listener pid {b[1]} -> {a[1]}")
    return diffs


# --- start state ------------------------------------------------------------


def start_state_problems(label: str, uid: str, sock: Path, run=subprocess.run) -> list[str]:
    """Cleanup commands for each piece of spare state that already exists."""
    cmds = []
    loaded, _pid = launchd_state(label, uid, run)
    if loaded:
        cmds.append(f"launchctl bootout gui/{uid}/{label}")
    plist = _plist_path(label)
    if plist.exists() or plist.is_symlink():
        cmds.append(f"rm -f {plist}")
    for path in (sock, _lock_path(sock)):
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


def run_installer(sub: str, env: dict, run=subprocess.run) -> subprocess.CompletedProcess:
    res = run(
        ["bash", str(INSTALLER), sub],
        capture_output=True,
        text=True,
        timeout=INSTALL_TIMEOUT,
        env=env,
        cwd=REPO_ROOT,
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
        self.holder.bind((HOST, self.port))
        self.holder.listen(1)
        install(install_env(self.label, self.port), "collision")
        self.maybe_inject()
        errlog = _err_log(self.label)
        deadline = time.monotonic() + COLLISION_TIMEOUT
        in_use = 0
        warned = False
        while time.monotonic() < deadline:
            try:
                text = errlog.read_text(errors="replace")
            except OSError:
                text = ""
            in_use = len(_IN_USE_RE.findall(text))
            if in_use >= 2:
                loaded, pid = launchd_state(self.label, self.uid)
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
        if not sock.parent.exists():
            sock.parent.mkdir(parents=True, mode=0o700)
            self.sock_dir_made = True
        os.chmod(sock.parent, 0o700)

        install(install_env(self.label, self.port), "rollback: TCP")
        self.expect_transport("tcp")
        self.wait_for("the TCP agent to answer", self.tcp_up)

        install(install_env(self.label, self.port, socket_path=sock), "rollback: socket")
        self.expect_transport("socket")
        self.wait_for("the socket agent to answer", self.sock_up)
        if not lock.exists():
            raise CheckFailed(f"{lock} was not created by the socket server")

        _loaded, old_pid = launchd_state(self.label, self.uid)
        out = install(install_env(self.label, self.port), "rollback: back to TCP")
        self.expect_transport("tcp")
        self.wait_for("the TCP agent to answer again", self.tcp_up)
        if sock.exists() or sock.is_symlink():
            raise CheckFailed(f"old socket {sock} still exists after the move back to TCP")
        if old_pid and _pid_alive(old_pid):
            raise CheckFailed(f"old socket removed while the old job (pid {old_pid}) still ran")
        if "removing the old socket" not in out:
            raise CheckFailed("installer did not report removing the old socket")
        if not lock.exists():
            raise CheckFailed(f"{lock} was removed; the installer must leave it")
        self.wait_for(f"{lock} to be unheld", lambda: not lock_holders(lock), timeout=10)
        return "tcp -> socket -> tcp; old socket gone after unload, .lock left and unheld"

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
        if self.holder is not None:
            with contextlib.suppress(OSError):
                self.holder.close()
            self.holder = None
        logs = _log_paths(label)  # read StandardErrorPath before the plist goes
        try:
            socket_in_plist = read_plist_field(label, "socket-path")
        except (CheckFailed, OSError, subprocess.SubprocessError):
            socket_in_plist = ""
        socks = {str(self.sock)} | ({socket_in_plist} if socket_in_plist else set())

        env = install_env(label, self.port)
        try:
            res = run_installer("uninstall", env)
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
                        f"{lock} to be unheld", lambda lock=lock: not lock_holders(lock), timeout=10
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
        loaded, _pid = launchd_state(label, self.uid)
        if loaded:
            problems.append(
                f"{label} is still loaded; run: launchctl bootout gui/{self.uid}/{label}"
            )
        left = [p for p in [plist, *logs, self.sock, _lock_path(self.sock)] if p.exists()]
        if left:
            problems.append("left behind: " + ", ".join(str(p) for p in left))
        return problems


def _pid_alive(pid: str) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError):
        return True
    return True


# --- main -------------------------------------------------------------------


def _print_table(results) -> None:
    width = max((len(n) for n, _s, _d in results), default=4)
    print()
    for name, status, detail in results:
        print(f"{status:<8} {name:<{width}}  {detail}".rstrip())


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
        choices=("fail", "hang"),
        help="TEST HOOK: 'fail' raises in the first check right after its install; "
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

    try:
        check_guard(
            args.label,
            args.port,
            env=env,
            canonical_ports=canonical_ports(),
            is_listening=is_listening,
        )
    except GuardRefused as exc:
        return refuse("guard", str(exc))

    try:
        uid = current_uid()
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

    state = {"tearing_down": False}

    def on_signal(signum, _frame):
        if not state["tearing_down"]:
            raise KeyboardInterrupt(f"signal {signum}")

    old = {s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM)}
    harness = Harness(args.label, args.port, args.inject)
    interrupted = False
    try:
        for name, fn in (
            ("port collision", harness.check_collision),
            ("socket rollback", harness.check_rollback),
            ("token file", harness.check_token),
        ):
            try:
                results.append((name, PASS, fn()))
            except (
                CheckFailed,
                AssertionError,
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as exc:
                results.append((name, FAIL, str(exc) or type(exc).__name__))
            finally:
                harness.release_holder()
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
                "; ".join(diffs) or f"{len(before)} agents, same pids",
            )
        )
        for s, h in old.items():
            signal.signal(s, h)

    _print_table(results)
    failed = interrupted or any(s == FAIL for _n, s, _d in results)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
