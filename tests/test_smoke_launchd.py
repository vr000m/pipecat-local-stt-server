"""Stubbed tests for ``scripts/smoke_launchd.py`` (plan Phase 2).

Nothing here touches real launchd. Every subprocess runs with ``PATH`` led by a
stub dir (``launchctl``, ``id``, ``lsof``) and a temp ``HOME``; the fixture
asserts the stub ``launchctl`` is the one resolved first. The real installer
runs against the stubs, as in ``tests/test_install_migration.py``.

The subprocess runs go through ``tests/_with_agents_home.py``, which patches
``_smoke_lib.agents_home`` to the temp home, so the real-agent plist scan never
reads the developer's ``~/Library/LaunchAgents`` (``HOME`` alone does not move
it, and production code has no override), and use a free ``--port``;
``test_stubbed_run_reads_no_real_library_path`` audits that.

Assumptions (contract is silent): the harness honours ``PIPECAT_STT_SOCKET``
from its environment for the spare socket path when set; the spare plist lives
at ``$HOME/Library/LaunchAgents/<label>.plist`` and logs under
``$HOME/Library/Logs/pipecat-stt`` with ``pipecat-stt-server-smoke`` in the name.
"""

from __future__ import annotations

import contextlib
import http.server
import importlib.util
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / "scripts" / "smoke_launchd.py"
LIB = REPO_ROOT / "scripts" / "_smoke_lib.py"
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
AUDIT_WRAPPER = Path(__file__).resolve().parent / "_audit_library_reads.py"

# The harness reads its uid from os.getuid(); the stub `id` (for the installer)
# answers the same value, so both name one gui/<uid> domain.
FAKE_UID = str(os.getuid())
SPARE = "pipecat.stt-server.smoke"
AGENTS_HOME_WRAPPER = Path(__file__).resolve().parent / "_with_agents_home.py"
CANONICAL = (9565, 9665, 9765, 9865)

# The one "could not read" sentinel (_smoke_lib.UNKNOWN), for the parametrize
# tables below; the harness's UNKNOWN is this same object.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
try:
    import _smoke_lib as _LIB
finally:
    sys.path.remove(str(REPO_ROOT / "scripts"))
UNKNOWN = _LIB.UNKNOWN

pytestmark = pytest.mark.skipif(
    not VENV_PYTHON.exists(), reason=f"{VENV_PYTHON} not found; run 'uv sync'"
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def smoke():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        return _load(HARNESS, "smoke_launchd_under_test")
    finally:
        sys.path.remove(str(REPO_ROOT / "scripts"))


@pytest.fixture(autouse=True)
def _stub_path_is_the_harness_path(smoke, monkeypatch):
    """The harness's PATH is fixed (``_smoke_lib.SAFE_PATH``); in-process
    tests run it with the test's own PATH (the stub launchctl / lsof / id
    first), exactly as ``tests/_with_agents_home.py`` does for a subprocess,
    so no test can reach the real launchctl through the fixed PATH."""
    monkeypatch.setattr(smoke._smoke_lib, "safe_path", lambda: os.environ.get("PATH", ""))


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Env:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.stub = tmp_path / "stubbin"
        self.stub.mkdir()
        self.log = tmp_path / "argv.log"
        self.home = tmp_path / "home"
        (self.home / "Library" / "LaunchAgents").mkdir(parents=True)
        self.agents = self.home / "Library" / "LaunchAgents"
        self.logs = self.home / "Library" / "Logs" / "pipecat-stt"
        self.loaded_marker = tmp_path / "loaded"
        self.print_fails = tmp_path / "print-fails"
        # "ok" (default), "noop" (exit 0, job stays loaded) or "fail" (exit 1).
        self.bootout_mode = tmp_path / "bootout-mode"
        # The spare job's launchd state is the loaded marker, and the stub moves
        # it the way launchd does: bootstrap of the spare plist loads it (a
        # second bootstrap while loaded fails with 5, as launchd's does) and
        # bootout of the spare label unloads it (fails with 3 when not loaded).
        # Not loaded is launchctl's own answer: exit 113, "Could not find service".
        _exe(
            self.stub / "launchctl",
            f"""#!/usr/bin/env bash
printf 'launchctl %s\\n' "$*" >> {self.log!s}
LOADED={self.loaded_marker!s}
if [[ "$1" == "print" ]]; then
    if [[ -e {self.print_fails!s} ]]; then echo "launchctl: boom" >&2; exit 5; fi
    if [[ -e "$LOADED" && "$2" == *"{SPARE}" ]]; then exit 0; fi
    echo "Could not find service \\"${{2##*/}}\\" in domain for user gui: {FAKE_UID}" >&2
    exit 113
fi
if [[ "$1" == "bootstrap" && "${{3:-}}" == *"/{SPARE}.plist" ]]; then
    if [[ -e "$LOADED" ]]; then echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; fi
    : > "$LOADED"
    exit 0
fi
if [[ "$1" == "bootout" && "$2" == *"/{SPARE}" ]]; then
    mode=$(cat {self.bootout_mode!s} 2>/dev/null || echo ok)
    if [[ "$mode" == fail ]]; then echo "Boot-out failed: 5: Input/output error" >&2; exit 1; fi
    if [[ ! -e "$LOADED" ]]; then echo "Boot-out failed: 3: No such process" >&2; exit 3; fi
    if [[ "$mode" == ok ]]; then rm -f "$LOADED"; fi
    exit 0
fi
exit 0
""",
        )
        _exe(
            self.stub / "id",
            f"""#!/usr/bin/env bash
printf 'id %s\\n' "$*" >> {self.log!s}
if [[ "$1" == "-u" ]]; then printf '{FAKE_UID}\\n'; exit 0; fi
exec /usr/bin/id "$@"
""",
        )
        # No listeners anywhere (lsof exits 1 with no output when nothing matches).
        _exe(
            self.stub / "lsof",
            f"#!/usr/bin/env bash\nprintf 'lsof %s\\n' \"$*\" >> {self.log!s}\nexit 1\n",
        )
        # Fixture invariant: the stubs are what the harness would resolve.
        found = shutil.which("launchctl", path=self.path)
        assert found == str(self.stub / "launchctl"), found

    @property
    def path(self) -> str:
        return f"{self.stub}:/usr/bin:/bin:/usr/sbin:/sbin"

    def env(self, **extra: str) -> dict[str, str]:
        e = {"HOME": str(self.home), "PATH": self.path}
        e.update(extra)
        e.pop("CI", None)
        return e

    def spare_env(self, **extra: str) -> dict[str, str]:
        return self.env(**{"PIPECAT_STT_LABEL": SPARE, "KODA_STT_LABEL": SPARE, **extra})

    def argv(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def run(
        self, args: list[str], env: dict[str, str], timeout: int = 180, audit=None, inject=None
    ):
        assert env["PATH"].split(":")[0] == str(self.stub)
        assert "CI" not in env
        return subprocess.run(
            self.cmd(*args, audit=audit, inject=inject),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=str(REPO_ROOT),
        )

    def cmd(self, *args: str, audit=None, inject=None) -> list[str]:
        """The harness command line, run through tests/_with_agents_home.py so
        the real-agent scan reads the temp home, never the account's
        ~/Library/LaunchAgents (optionally under the audit wrapper first).
        INJECT is the wrapper's test-only failure injection (fail, crash or
        hang right after the first install); the harness has no such flag."""
        pre = [str(AUDIT_WRAPPER), str(audit)] if audit is not None else []
        wrap = [str(AGENTS_HOME_WRAPPER), str(self.home)]
        if inject is not None:
            wrap += ["--inject", inject]
        return [str(VENV_PYTHON), *pre, *wrap, str(HARNESS), *args]

    def spare_residue(self) -> list[Path]:
        out = []
        plist = self.agents / f"{SPARE}.plist"
        if plist.exists():
            out.append(plist)
        if self.logs.exists():
            out += [p for p in self.logs.iterdir() if "smoke" in p.name]
        return out


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def _mutating(lines: list[str]) -> list[str]:
    return [
        ln for ln in lines if ln.startswith("launchctl") and ("bootout" in ln or "bootstrap" in ln)
    ]


# --------------------------------------------------------------------------
# check_guard
# --------------------------------------------------------------------------


def _good_env():
    return {"PIPECAT_STT_LABEL": SPARE, "KODA_STT_LABEL": SPARE}


@pytest.mark.parametrize(
    "label", ["pipecat.stt-server", "pipecat.stt-server.parakeet", "koda.stt-server", "", None]
)
def test_guard_refuses_other_labels(smoke, label):
    with pytest.raises(smoke.GuardRefused):
        smoke.check_guard(
            label,
            _free_port(),
            env=_good_env(),
            canonical_ports=set(CANONICAL),
            is_listening=lambda p: False,
        )


def test_guard_refuses_unset_env_label(smoke):
    with pytest.raises(smoke.GuardRefused):
        smoke.check_guard(
            SPARE,
            _free_port(),
            env={"KODA_STT_LABEL": SPARE},
            canonical_ports=set(CANONICAL),
            is_listening=lambda p: False,
        )


def test_guard_refuses_koda_label_override(smoke):
    e = {"PIPECAT_STT_LABEL": SPARE, "KODA_STT_LABEL": "koda.stt-server"}
    with pytest.raises(smoke.GuardRefused):
        smoke.check_guard(
            SPARE,
            _free_port(),
            env=e,
            canonical_ports=set(CANONICAL),
            is_listening=lambda p: False,
        )


@pytest.mark.parametrize("port", CANONICAL)
def test_guard_refuses_canonical_ports(smoke, port):
    with pytest.raises(smoke.GuardRefused):
        smoke.check_guard(
            SPARE,
            port,
            env=_good_env(),
            canonical_ports=set(CANONICAL),
            is_listening=lambda p: False,
        )


def test_guard_refuses_listening_port(smoke):
    with pytest.raises(smoke.GuardRefused):
        smoke.check_guard(
            SPARE,
            _free_port(),
            env=_good_env(),
            canonical_ports=set(CANONICAL),
            is_listening=lambda p: True,
        )


def test_guard_accepts_spare(smoke):
    assert (
        smoke.check_guard(
            SPARE,
            _free_port(),
            env=_good_env(),
            canonical_ports=set(CANONICAL),
            is_listening=lambda p: False,
        )
        is None
    )
    assert smoke.SPARE_LABEL == SPARE and smoke.DEFAULT_PORT == 9998


@pytest.mark.parametrize(
    "extra,label",
    [
        ([], "pipecat.stt-server"),  # env label mismatch is also exercised below
        (["--label", "pipecat.stt-server.parakeet"], "pipecat.stt-server.parakeet"),
        (["--label", "koda.stt-server"], "koda.stt-server"),
        (["--port", "9565"], SPARE),
        (["--port", "9865"], SPARE),
    ],
)
def test_cli_refusal_issues_no_launchctl_mutation(env, extra, label):
    e = env.spare_env()
    if not extra:
        e["PIPECAT_STT_LABEL"] = label
    r = env.run(extra, e)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert _mutating(env.argv()) == []
    assert not env.spare_residue()


def test_cli_refuses_unset_env_label(env):
    e = env.spare_env()
    e.pop("PIPECAT_STT_LABEL")
    r = env.run([], e)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert _mutating(env.argv()) == []


def test_cli_refuses_koda_label_override(env):
    e = env.spare_env(KODA_STT_LABEL="koda.stt-server")
    r = env.run([], e)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert _mutating(env.argv()) == []


# --------------------------------------------------------------------------
# preconditions
# --------------------------------------------------------------------------


def test_preconditions_refuses_ci(smoke):
    assert smoke.preconditions(env={"CI": "true"}, platform="darwin")


def test_preconditions_refuses_non_darwin(smoke):
    assert smoke.preconditions(env={}, platform="linux")


def test_preconditions_ok(smoke):
    assert smoke.preconditions(env={}, platform="darwin") is None


def test_cli_ci_skips_with_exit_zero(env):
    e = env.spare_env(CI="1")
    # Env.env() pops CI by default; the skip path is exercised explicitly here.
    e["CI"] = "1"
    r = subprocess.run(
        env.cmd(),
        capture_output=True,
        text=True,
        timeout=60,
        env=e,
        cwd=str(REPO_ROOT),
    )
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "SKIPPED:" in r.stdout
    assert _mutating(env.argv()) == []


# --------------------------------------------------------------------------
# snapshots
# --------------------------------------------------------------------------


def _st(smoke, pid="100", listener="100", **kw):
    """An AgentState for a running TCP job (overridable per field)."""
    base = dict(
        loaded=True,
        pid=pid,
        runs="1",
        endpoint=("tcp", "127.0.0.1", "9565"),
        plist="digest",
        listener=listener,
    )
    base.update(kw)
    return smoke.AgentState(**base)


def test_compare_snapshots_identical(smoke):
    s = {
        "pipecat.stt-server": _st(smoke),
        "pipecat.stt-server.parakeet": smoke.AgentState(False, None, None, None, "d", None),
    }
    assert smoke.compare_snapshots(s, dict(s)) == []


# one field changed (or unreadable) -> a difference naming that field. ``runs``
# moves on a restart that came back to the same pid and listener.
@pytest.mark.parametrize(
    ("field", "after", "same"),
    [
        ("pid", "200", False),
        ("listener", "999", False),
        ("listener", None, False),
        ("runs", "2", False),
        ("loaded", False, False),
        ("endpoint", ("tcp", "127.0.0.1", "9566"), False),
        ("endpoint", ("unix", "/s.sock"), False),
        ("plist", "other-digest", False),
        ("plist", None, False),
        ("plist", UNKNOWN, False),
        ("listener", UNKNOWN, False),
        ("runs", "1", True),
        ("pid", "100", True),
    ],
)
def test_compare_snapshots_field_state_space(smoke, field, after, same):
    import dataclasses

    before = {"pipecat.stt-server": _st(smoke)}
    changed = {
        "pipecat.stt-server": dataclasses.replace(before["pipecat.stt-server"], **{field: after})
    }
    diffs = smoke.compare_snapshots(before, changed)
    assert (diffs == []) is same
    if not same:
        assert any(field in d for d in diffs), diffs


def test_compare_snapshots_appear_disappear(smoke):
    a = {"pipecat.stt-server": _st(smoke)}
    b = {**a, "pipecat.stt-server.parakeet": _st(smoke, "5", "5")}
    assert smoke.compare_snapshots(a, b)
    assert smoke.compare_snapshots(b, a)


def test_snapshot_excludes_spare(smoke, env, monkeypatch):
    (env.agents / "pipecat.stt-server.plist").write_text("<plist><dict/></plist>")
    (env.agents / f"{SPARE}.plist").write_text("<plist><dict/></plist>")
    monkeypatch.setenv("HOME", str(env.home))
    monkeypatch.setenv("PATH", env.path)
    snap = smoke.snapshot_real_agents(home=env.home)
    assert SPARE not in snap
    assert "pipecat.stt-server" in snap


def test_snapshot_reads_account_home_not_env_home(smoke, env, monkeypatch, tmp_path):
    # An overridden HOME must not empty the snapshot (vacuous PASS).
    (env.agents / "pipecat.stt-server.plist").write_text("<plist><dict/></plist>")
    other = tmp_path / "other-home"
    (other / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(other))
    monkeypatch.setenv("PATH", env.path)
    monkeypatch.setattr(smoke._smoke_lib, "account_home", lambda: env.home)
    assert "pipecat.stt-server" in smoke.snapshot_real_agents()
    assert [p.name for p in smoke.real_agent_plists()] == ["pipecat.stt-server.plist"]


def _agent_plist(path: Path, port: str) -> None:
    import plistlib

    args = [sys.executable, "-m", "stt_server", "serve", "--host", "127.0.0.1", "--port", port]
    path.write_bytes(plistlib.dumps({"Label": path.stem, "ProgramArguments": args}))


def test_guard_refuses_port_recorded_in_installed_agent_plist(smoke, env):
    _agent_plist(env.agents / "pipecat.stt-server.custom.plist", "9123")
    _agent_plist(env.agents / f"{SPARE}.plist", "9124")  # the spare's own port is not real
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATH", env.path)
        snap = smoke.snapshot_real_agents(home=env.home)
    ports = smoke.snapshot_ports(snap)
    assert ports == {9123: "pipecat.stt-server.custom"}
    with pytest.raises(smoke.GuardRefused, match="pipecat.stt-server.custom"):
        smoke.check_guard(
            SPARE,
            9123,
            env=_good_env(),
            canonical_ports=set(CANONICAL),
            is_listening=lambda p: False,
            agent_ports=ports,
        )


def test_snapshot_ports_unreadable_plist_refuses(smoke, env):
    (env.agents / "pipecat.stt-server.broken.plist").write_text("not a plist")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATH", env.path)
        snap = smoke.snapshot_real_agents(home=env.home)
    assert snap["pipecat.stt-server.broken"].plist_port == UNKNOWN
    assert smoke.snapshot_unknowns(snap) == ["pipecat.stt-server.broken (plist_port)"]
    with pytest.raises(smoke.GuardRefused):
        smoke.snapshot_ports(snap)


# --------------------------------------------------------------------------
# launchctl / lsof answers the harness cannot read fail closed
# --------------------------------------------------------------------------


def _cp(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, out, err)


@pytest.mark.parametrize(
    ("rc", "err", "want"),
    [
        (0, "", (True, None)),
        (113, 'Could not find service "x" in domain', (False, None)),
        (1, "", "raise"),
        (5, "Operation not permitted", "raise"),
    ],
)
def test_launchd_state_unreadable_is_not_unloaded(smoke, rc, err, want):
    def run(argv, **_kw):
        return _cp(argv, rc, "", err)

    if want == "raise":
        with pytest.raises(RuntimeError):
            smoke.require_launchd_state(SPARE, FAKE_UID, run)
    else:
        assert smoke.require_launchd_state(SPARE, FAKE_UID, run) == want


def test_cli_unreadable_launchctl_refuses_start_state(env):
    env.print_fails.write_text("1")
    r = env.run(["--port", str(_free_port())], env.spare_env())
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "start state" in r.stdout and "launchd state" in r.stdout
    assert _mutating(env.argv()) == []
    assert not [ln for ln in env.argv() if "install" in ln]


def _lsof_like(holders):
    """A fake lsof that honours ``--``: every word after it is a file name."""

    def run(argv, **_kw):
        if "--" in argv:
            files = argv[argv.index("--") + 1 :]
            if any(f.startswith("-") for f in files):
                return _cp(argv, 1, "", f"lsof: status error on {files[-1]}\n")
        if holders is None:
            return _cp(argv, 1, "", "lsof: boom\n")
        if not holders:
            return _cp(argv, 1)
        return _cp(argv, 0, "".join(f"p{p}\nf3\nn*:1\n" for p in holders))

    return run


# (lsof answer) -> (lock_holders, lock_unheld): the whole holder/lock state space.
@pytest.mark.parametrize(
    ("holders", "want", "unheld"),
    [(["4242"], ["4242"], False), ([], [], True), (None, None, False)],
)
def test_lock_holders_state_space(smoke, holders, want, unheld):
    run = _lsof_like(holders)
    assert smoke.lock_holders(Path("/x/spare.sock.lock"), run) == want
    assert smoke.lock_unheld(Path("/x/spare.sock.lock"), run) is unheld


@pytest.mark.parametrize(("holders", "want"), [(["7"], True), ([], False)])
def test_is_listening_reads_lsof(smoke, holders, want):
    port = _free_port()
    assert smoke.is_listening(port, _lsof_like(holders)) is want


def test_is_listening_lsof_failure_refuses(smoke):
    with pytest.raises(smoke.GuardRefused):
        smoke.is_listening(_free_port(), _lsof_like(None))


# --------------------------------------------------------------------------
# logs: plist paths first, only this install's lines count
# --------------------------------------------------------------------------


def test_log_paths_prefer_the_plists_paths(smoke, env, monkeypatch, tmp_path):
    import plistlib

    monkeypatch.setenv("HOME", str(env.home))
    monkeypatch.delenv("PIPECAT_STT_LOG_DIR", raising=False)
    monkeypatch.delenv("KODA_STT_LOG_DIR", raising=False)
    rule = env.logs / "pipecat-stt-server-smoke.err"
    assert smoke._err_log(SPARE) == rule  # no plist: the installer's naming rule
    err, out = tmp_path / "elsewhere.err", tmp_path / "elsewhere.log"
    (env.agents / f"{SPARE}.plist").write_bytes(
        plistlib.dumps({"StandardErrorPath": str(err), "StandardOutPath": str(out)})
    )
    assert smoke._log_paths(SPARE) == [err, out]


def test_stale_address_in_use_lines_do_not_count(smoke, tmp_path):
    log = tmp_path / "x.err"
    line = "OSError: [Errno 48] error while attempting to bind: address already in use\n"
    log.write_text(line * 3)
    offset = smoke._size(log)
    assert smoke._in_use_lines(smoke._read_from(log, offset)) == 0
    with open(log, "a") as fh:
        fh.write(line)
    # One line naming both "Errno 48" and "address already in use" is ONE line.
    assert smoke._in_use_lines(smoke._read_from(log, offset)) == 1
    log.write_text("errno 48\n")  # truncated (rotated): read from the start
    assert smoke._in_use_lines(smoke._read_from(log, offset)) == 1


# --------------------------------------------------------------------------
# rollback: the spare socket directory, the installer's leave-behind warnings
# --------------------------------------------------------------------------


def test_prepare_private_dir_refuses_a_symlink_and_leaves_its_target(smoke, tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    link = tmp_path / "sockdir"
    link.symlink_to(target)
    with pytest.raises(smoke.CheckFailed, match="symlink"):
        smoke.prepare_private_dir(link)
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_prepare_private_dir_creates_or_tightens(smoke, tmp_path):
    new = tmp_path / "a" / "b"
    assert smoke.prepare_private_dir(new) is True
    assert stat.S_IMODE(new.stat().st_mode) == 0o700
    new.chmod(0o755)
    assert smoke.prepare_private_dir(new) is False
    assert stat.S_IMODE(new.stat().st_mode) == 0o700
    f = tmp_path / "file"
    f.write_text("")
    with pytest.raises(smoke.CheckFailed):
        smoke.prepare_private_dir(f)


# The installer's "warning:" lines, read from its source, each classified here by
# a fragment of its text: True = something was left behind (the harness must
# FAIL the install or the uninstall on it), False = benign. Every warning in the
# installer must match exactly one entry, so a new or reworded warning fails
# this test until it is classified, and the harness's rule must agree with each.
INSTALLER = REPO_ROOT / "scripts" / "install_stt_agent.sh"
_INSTALLER_WARNINGS = {
    "still loaded ${cap}s after bootout": True,
    "installed socket path '$sock' $reason; leaving it": True,
    "$sock.lock is held by a running server; leaving $sock": True,
    "$sock is not a socket; leaving it": True,
    "$sock not owned by current user, leaving it": True,
    "could not remove $sock": True,
    "could not tell whether $sock.lock is held; leaving $sock": True,
    "it looks unsafe, so no removal command is offered": True,
    "no later run will remove it": True,
    "socket mode (PIPECAT_STT_SOCKET set): ignoring": False,
    "a socket agent carries none": False,
    "launchctl bootstrap failed for $LABEL; retrying once": False,
    "old job may still hold $old_socket.lock; leaving $old_socket": True,
    "uninstalling $LABEL without socket cleanup": True,
    "$LABEL may still hold $old_socket.lock; leaving $old_socket": True,
    "the uninstall is incomplete": True,
}


def _installer_warnings() -> list[str]:
    import re

    return re.findall(r'echo "(warning: [^"]*)"', INSTALLER.read_text())


def _rendered(line: str) -> str:
    """A warning as the installer prints it: every expansion replaced by a value."""
    import re

    return re.sub(r"\$\{[^}]*\}|\$\([^)]*\)|\$[A-Za-z_][A-Za-z0-9_]*", "/h/x", line)


def test_every_installer_warning_is_classified_and_the_harness_agrees(smoke):
    found = _installer_warnings()
    assert len(found) >= len(_INSTALLER_WARNINGS)
    used = set()
    for line in found:
        keys = [k for k in _INSTALLER_WARNINGS if k in line]
        assert len(keys) == 1, f"classify this installer warning: {line!r} (matched {keys})"
        used.add(keys[0])
        printed = _rendered(line)
        assert (smoke.leave_behind_warnings(printed) != []) == _INSTALLER_WARNINGS[keys[0]], printed
    assert used == set(_INSTALLER_WARNINGS), "stale entries: " + repr(
        set(_INSTALLER_WARNINGS) - used
    )


def test_old_socket_note_and_log_naming_match_the_installer(smoke, monkeypatch, tmp_path):
    import re

    src = INSTALLER.read_text()
    notes = re.findall(r'echo "(note: endpoint changed [^"]*)"', src)
    assert len(notes) == 2 and all(n.endswith(smoke.OLD_SOCKET_NOTE) for n in notes)
    # The log dir the harness falls back on before a plist exists is the
    # installer's (the basename rule is pinned to the renderer below).
    assert (
        'LOG_DIR="${PIPECAT_STT_LOG_DIR:-${KODA_STT_LOG_DIR:-$HOME/Library/Logs/pipecat-stt}}"'
        in src
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PIPECAT_STT_LOG_DIR", raising=False)
    monkeypatch.delenv("KODA_STT_LOG_DIR", raising=False)
    logs = tmp_path / "Library" / "Logs" / "pipecat-stt"
    assert smoke._log_paths(SPARE) == [
        logs / "pipecat-stt-server-smoke.err",
        logs / "pipecat-stt-server-smoke.log",
    ]
    monkeypatch.setenv("KODA_STT_LOG_DIR", str(tmp_path / "k"))
    assert smoke._log_dir() == tmp_path / "k"
    monkeypatch.setenv("PIPECAT_STT_LOG_DIR", str(tmp_path / "p"))
    assert smoke._log_dir() == tmp_path / "p"
    # The canonical backends are the installer's port table.
    table = re.findall(r"^\s*([a-z]+)\) echo (\d+) ;;", src, re.MULTILINE)
    assert tuple(b for b, _p in table) == smoke.CANONICAL_BACKENDS


# Leave-behind lines the harness must refuse even when worded unlike any above.
@pytest.mark.parametrize(
    ("stderr", "left"),
    [
        ("", False),
        (
            "note: endpoint changed from socket /a to TCP 127.0.0.1:9; removing the old socket",
            False,
        ),
        ("warning: socket mode (PIPECAT_STT_SOCKET set): ignoring PIPECAT_STT_PORT (x)", False),
        ("warning: something new the harness has never seen", True),
        (
            "warning: launchctl bootstrap failed for x; retrying once\nwarning: could not remove /a",
            True,
        ),
    ],
)
def test_leave_behind_warnings_state_space(smoke, stderr, left):
    assert (smoke.leave_behind_warnings(stderr) != []) == left


# --------------------------------------------------------------------------
# start state
# --------------------------------------------------------------------------


def _plant_plist(e: Env, sock: Path):
    (e.agents / f"{SPARE}.plist").write_text("<plist><dict/></plist>")


def _plant_loaded(e: Env, sock: Path):
    e.loaded_marker.write_text("1")


def _plant_socket(e: Env, sock: Path):
    sock.parent.mkdir(parents=True, exist_ok=True)
    sock.write_text("")


def _plant_lock(e: Env, sock: Path):
    sock.parent.mkdir(parents=True, exist_ok=True)
    Path(str(sock) + ".lock").write_text("")


def _plant_err_log(e: Env, sock: Path):
    e.logs.mkdir(parents=True, exist_ok=True)
    (e.logs / "pipecat-stt-server-smoke.err").write_text("someone else's log\n")


def _plant_out_log(e: Env, sock: Path):
    e.logs.mkdir(parents=True, exist_ok=True)
    (e.logs / "pipecat-stt-server-smoke.log").write_text("someone else's log\n")


@pytest.mark.parametrize(
    ("plant", "expect"),
    [
        (_plant_plist, f"rm -f {{home}}/Library/LaunchAgents/{SPARE}.plist"),
        (_plant_loaded, f"launchctl bootout gui/{FAKE_UID}/{SPARE}"),
        (_plant_socket, "rm -f {sock}"),
        (_plant_lock, "rm -f {sock}.lock"),
        (_plant_err_log, "rm -f {home}/Library/Logs/pipecat-stt/pipecat-stt-server-smoke.err"),
        (_plant_out_log, "rm -f {home}/Library/Logs/pipecat-stt/pipecat-stt-server-smoke.log"),
    ],
)
def test_start_state_refusal(env, plant, expect):
    # The harness fixes its spare socket under $HOME; it does not read
    # PIPECAT_STT_SOCKET, so a stray variable cannot redirect it.
    sock = env.home / "Library" / "Caches" / "pipecat-stt-smoke-launchd" / "spare.sock"
    plant(env, sock)
    r = env.run(["--port", str(_free_port())], env.spare_env())
    assert r.returncode == 1, (r.stdout, r.stderr)
    out = r.stdout + r.stderr
    assert expect.format(home=env.home, sock=sock) in out
    # Print-only: no bootout/bootstrap actually issued, no installer run.
    assert _mutating(env.argv()) == []
    # A refused run leaves the planted state alone (it is not the harness's).
    if plant in (_plant_err_log, _plant_out_log):
        assert any(env.logs.iterdir())


# --------------------------------------------------------------------------
# teardown
# --------------------------------------------------------------------------


def _assert_torn_down(env: Env):
    assert f"launchctl bootout gui/{FAKE_UID}/{SPARE}" in env.argv()
    # The stub's bootout really unloads: a teardown that only logged a bootout
    # (or one launchd ignored) would leave the spare loaded here.
    assert not env.loaded_marker.exists(), "the spare job is still loaded after teardown"
    assert not env.spare_residue()
    # Never touches any other label.
    for ln in env.argv():
        if "bootout" in ln or "bootstrap" in ln:
            assert SPARE in ln or "stt-server" not in ln, ln
            assert "koda.stt-server" not in ln


def test_inject_fail_still_tears_down(env):
    r = env.run(["--port", str(_free_port())], env.spare_env(), inject="fail")
    assert r.returncode == 1, (r.stdout, r.stderr)
    _assert_torn_down(env)


def test_unreadable_baseline_snapshot_refuses_before_any_install(env):
    # A real agent whose plist cannot be read is UNKNOWN in the baseline, so
    # "real agents unchanged" could never PASS: refuse before the first install.
    (env.agents / "pipecat.stt-server.custom.plist").mkdir()
    r = env.run(["--port", str(_free_port())], env.spare_env(), inject="fail")
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "snapshot" in r.stdout and "pipecat.stt-server.custom" in r.stdout
    assert _mutating(env.argv()) == []
    assert "port collision" not in r.stdout
    assert not env.spare_residue()


@pytest.mark.parametrize(
    ("snap", "unknown"),
    [
        ({}, []),
        ({"a": ("1", "1", "1", "d")}, []),
        ({"a": (None, None, None, None)}, []),
        ({"a": ("1", "1", UNKNOWN, "d")}, ["a (listener)"]),
        ({"a": (None, None, None, UNKNOWN)}, ["a (plist)"]),
        ({"a": ("1", UNKNOWN, "1", "d")}, ["a (runs)"]),
        ({"a": (None, None, UNKNOWN, "d"), "b": ("2", "1", "2", "d")}, ["a (listener)"]),
        (
            {"b": ("2", "1", UNKNOWN, "d"), "a": ("1", UNKNOWN, "1", UNKNOWN)},
            ["a (runs, plist)", "b (listener)"],
        ),
    ],
)
def test_snapshot_unknowns_state_space(smoke, snap, unknown):
    states = {
        k: smoke.AgentState(pid is not None, pid, runs, None, plist, listener)
        for k, (pid, runs, listener, plist) in snap.items()
    }
    assert smoke.snapshot_unknowns(states) == unknown


def _run_lock(env: Env) -> Path:
    return env.home / "Library" / "Caches" / "pipecat-stt-smoke-launchd.lock"


def test_concurrent_run_is_refused_and_mutates_nothing(env):
    import fcntl
    import os

    lock = _run_lock(env)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = env.run(["--port", str(_free_port())], env.spare_env(), inject="fail")
    finally:
        os.close(fd)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "another smoke_launchd.py run" in r.stdout
    assert _mutating(env.argv()) == []
    assert not env.spare_residue()
    assert "port collision" not in r.stdout


def test_run_lock_is_released_after_a_run(env):
    import fcntl
    import os

    r = env.run(["--port", str(_free_port())], env.spare_env(), inject="fail")
    assert r.returncode == 1, (r.stdout, r.stderr)
    fd = os.open(_run_lock(env), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if still held
    finally:
        os.close(fd)


# The lock file's state -> acquired or refused.
@pytest.mark.parametrize("state", ["absent", "free", "held", "symlink", "directory"])
def test_run_lock_state_space(smoke, tmp_path, state):
    import fcntl
    import os

    lock = tmp_path / "Library" / "Caches" / "x.lock"
    other = None
    if state != "absent":
        lock.parent.mkdir(parents=True)
    if state in ("free", "held"):
        lock.write_text("")
    if state == "held":
        other = os.open(lock, os.O_RDWR)
        fcntl.flock(other, fcntl.LOCK_EX)
    if state == "symlink":
        (tmp_path / "target").write_text("")
        lock.symlink_to(tmp_path / "target")
    if state == "directory":
        lock.mkdir()
    try:
        if state in ("absent", "free"):
            fd = smoke.acquire_run_lock(lock)
            os.close(fd)
        else:
            with pytest.raises(smoke.GuardRefused):
                smoke.acquire_run_lock(lock)
    finally:
        if other is not None:
            os.close(other)
    if state == "symlink":
        assert (tmp_path / "target").read_text() == ""


def test_stubbed_run_reads_no_real_library_path(env, tmp_path):
    # A whole stubbed run (guard, snapshot, checks, teardown, re-snapshot) under
    # an audit hook: no open, scandir, mkdir or unlink of a path under the real
    # account's ~/Library, and no subprocess handed one.
    audit = tmp_path / "audit.log"
    r = env.run(["--port", str(_free_port())], env.spare_env(), audit=audit, inject="fail")
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert audit.exists(), "the audit hook never ran"
    lines = audit.read_text().splitlines()
    assert lines[0] == "audit: on"
    assert lines[1:] == []


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_signal_during_hang_still_tears_down(env, sig):
    e = env.spare_env()
    p = subprocess.Popen(
        env.cmd("--port", str(_free_port()), inject="hang"),
        env=e,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 90
        plist = env.agents / f"{SPARE}.plist"
        while time.time() < deadline and not plist.exists() and p.poll() is None:
            time.sleep(0.2)
        assert plist.exists(), "harness never installed the spare plist"
        time.sleep(1)
        p.send_signal(sig)
        p.wait(timeout=120)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
    assert p.returncode == 1, p.stdout.read() if p.stdout else ""
    _assert_torn_down(env)


def test_unexpected_exception_is_a_fail_row_and_checks_continue(env):
    r = env.run(["--port", str(_free_port())], env.spare_env(), inject="crash")
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "Traceback" not in r.stderr
    rows = {
        ln.split()[1]: ln.split()[0] for ln in r.stdout.splitlines() if ln[:4] in ("PASS", "FAIL")
    }
    assert "LookupError" in r.stdout
    assert rows.get("port") == "FAIL"
    # The later checks still ran (their rows exist) and the table was printed.
    assert "socket rollback" in r.stdout and "token file" in r.stdout
    assert "real agents unchanged" in r.stdout
    _assert_torn_down(env)


def test_teardown_runs_the_uninstall_in_its_own_session(smoke, env, monkeypatch):
    monkeypatch.setenv("HOME", str(env.home))
    monkeypatch.setenv("PATH", env.path)
    seen = []

    def fake_installer(sub, e, *, teardown=False):
        seen.append((sub, teardown))
        return _cp(["bash"], 0)

    monkeypatch.setattr(smoke, "run_installer", fake_installer)
    h = smoke.Harness(SPARE, _free_port())
    assert h.teardown() == []
    assert seen == [("uninstall", True)]


# --------------------------------------------------------------------------
# assert_token_gate (scripts/_smoke_lib.py)
# --------------------------------------------------------------------------


class _Open(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


class _AlwaysDeny(_Open):
    def do_GET(self):  # noqa: N802
        self.send_response(401)
        self.end_headers()
        self.wfile.write(b"unauthorized\n")


def _serve(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.mark.parametrize("handler", [_Open, _AlwaysDeny])
def test_assert_token_gate_raises_on_broken_gate(tmp_path, handler):
    # Covered only negatively: the real server speaks a WebSocket handshake, so a
    # plain HTTP fake cannot model the accept path faithfully.
    lib = _load(LIB, "smoke_lib_under_test")
    tok = tmp_path / "token"
    tok.write_text("s3cret\n")
    tok.chmod(0o600)
    srv = _serve(handler)
    try:
        port = srv.server_address[1]
        with pytest.raises(AssertionError):
            lib.assert_token_gate(
                "127.0.0.1",
                port,
                str(tok),
                connect=lambda: socket.create_connection(("127.0.0.1", port)),
            )
    finally:
        srv.shutdown()
        srv.server_close()


# --------------------------------------------------------------------------
# real agents launchd knows without a plist; unreadable listeners (H3)
# --------------------------------------------------------------------------

_GHOST = "pipecat.stt-server.ghost"
_GHOST_PRINT = f"""gui/{FAKE_UID}/{_GHOST} = {{
\tstate = running
\tpid = 777
\truns = 2
\targuments = {{
\t\t/venv/bin/python
\t\t-m
\t\tstt_server
\t\tserve
\t\t--host
\t\t127.0.0.1
\t\t--port
\t\t9123
\t}}
}}
"""


def _fake_launchd(listed="", prints=None, listeners=(), list_rc=0, calls=None):
    """A ``run`` for the snapshot: ``launchctl list`` answers LISTED, ``print``
    answers PRINTS[label] (exit 0; a missing label is launchctl's own "could not
    find service"), ``lsof`` lists LISTENERS (None: lsof fails) for any target.
    Every argv is appended to CALLS."""
    prints = prints or {}

    def run(argv, **_kw):
        if calls is not None:
            calls.append([str(a) for a in argv])
        tool = Path(str(argv[0])).name
        if tool == "id":
            return _cp(argv, 0, f"{FAKE_UID}\n")
        if tool == "launchctl" and argv[1] == "list":
            return _cp(argv, list_rc, listed, "" if not list_rc else "launchctl: boom")
        if tool == "launchctl" and argv[1] == "print":
            label = argv[2].rsplit("/", 1)[-1]
            if label in prints:
                return _cp(argv, 0, prints[label])
            return _cp(argv, 113, "", f'Could not find service "{label}"')
        if tool == "lsof":
            if listeners is None:
                return _cp(argv, 1, "", "lsof: boom")
            body = "".join(f"p{p}\nn127.0.0.1:9123\n" for p in listeners)
            return _cp(argv, 0 if body else 1, body)
        raise AssertionError(f"unexpected {argv!r}")

    return run


_LIST = f"PID\tStatus\tLabel\n777\t0\t{_GHOST}\n-\t0\t{SPARE}\n12\t0\tcom.apple.x\n"


def _job_print(label, *, running=True, transport="tcp", port="9123", sock="/x/job.sock", runs="3"):
    """A ``launchctl print`` body for a loaded job serving PORT (tcp) or SOCK."""
    serve = (
        ["--host", "127.0.0.1", "--port", port] if transport == "tcp" else ["--socket-path", sock]
    )
    args = "".join(f"\t\t{a}\n" for a in ["/venv/bin/python", "-m", "stt_server", "serve", *serve])
    pid = "\tpid = 777\n" if running else ""
    return (
        f"gui/{FAKE_UID}/{label} = {{\n\tstate = {'running' if running else 'waiting'}\n"
        f"\targuments = {{\n{args}\t}}\n\truns = {runs}\n{pid}}}\n"
    )


def _plist_for(path: Path, *, transport="tcp", port="9123", sock="/x/plist.sock") -> None:
    import plistlib

    serve = (
        ["--host", "127.0.0.1", "--port", port] if transport == "tcp" else ["--socket-path", sock]
    )
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": path.stem,
                "ProgramArguments": [sys.executable, "-m", "stt_server", "serve", *serve],
            }
        )
    )


# The snapshot's full state space: job loaded/unloaded x running or not x TCP or
# socket x plist present/absent x the plist's endpoint equal to the job's or not.
# The listener is read ONLY from the job's endpoint (its port, or its socket's
# .lock), never from the plist; a job with no pid serves nothing (None, no lsof
# call, an absent socket is not UNKNOWN); the plist is recorded as a digest and
# its intended port still reaches the guard.
_LABEL = "pipecat.stt-server.cell"


@pytest.mark.parametrize("plist", ["absent", "same", "differ"])
@pytest.mark.parametrize("transport", ["tcp", "unix"])
@pytest.mark.parametrize("job", ["unloaded", "loaded-stopped", "running"])
def test_snapshot_state_space(smoke, env, tmp_path, job, transport, plist):
    job_sock = tmp_path / "job.sock"
    plist_sock = tmp_path / "plist.sock"
    if plist == "absent" and job == "unloaded":
        pytest.skip("no plist and no job: not an agent")
    if transport == "unix":
        (tmp_path / "job.sock.lock").write_text("")  # held by 777 per the fake lsof
    prints = {}
    if job != "unloaded":
        prints[_LABEL] = _job_print(
            _LABEL, running=job == "running", transport=transport, port="9123", sock=str(job_sock)
        )
    if plist != "absent":
        _plist_for(
            env.agents / f"{_LABEL}.plist",
            transport=transport,
            port="9123" if plist == "same" else "9124",
            sock=str(job_sock if plist == "same" else plist_sock),
        )
    listed = f"PID\tStatus\tLabel\n777\t0\t{_LABEL}\n" if job != "unloaded" else ""
    calls: list[list[str]] = []
    run = _fake_launchd(listed, prints, listeners=["777"], calls=calls)
    st = smoke.snapshot_real_agents(run, home=env.home)[_LABEL]
    lsof = [c for c in calls if Path(c[0]).name == "lsof"]

    assert st.loaded == (job != "unloaded")
    assert st.pid == ("777" if job == "running" else None)
    assert st.runs == (None if job == "unloaded" else "3")
    want_ep = None
    if job != "unloaded":
        want_ep = ("tcp", "127.0.0.1", "9123") if transport == "tcp" else ("unix", str(job_sock))
    assert st.endpoint == want_ep
    assert (st.plist is None) == (plist == "absent")
    if job == "running":
        assert st.listener == "777"
        assert len(lsof) == 1
        if transport == "tcp":
            assert "-iTCP:9123" in lsof[0] and "-iTCP:9124" not in lsof[0]
        else:
            assert lsof[0][-1] == f"{job_sock}.lock"  # the job's lock, never the plist's
    else:
        assert st.listener is None and lsof == []
    ports = smoke.snapshot_ports({_LABEL: st})
    want_ports = set()
    if transport == "tcp" and job != "unloaded":
        want_ports.add(9123)
    if transport == "tcp" and plist != "absent":
        want_ports.add(9123 if plist == "same" else 9124)
    assert set(ports) == want_ports


# A running job whose endpoint cannot be read is UNKNOWN (refuses the run); a
# running socket job whose .lock does not exist has no holder (None, no lsof).
@pytest.mark.parametrize(
    ("transport", "lock", "listeners", "want", "lsof_calls"),
    [
        ("tcp", False, None, UNKNOWN, 1),
        ("tcp", False, [], None, 1),
        ("unix", True, None, UNKNOWN, 1),
        ("unix", True, [], None, 1),
        ("unix", False, ["777"], None, 0),
        ("none", False, ["777"], UNKNOWN, 0),
    ],
)
def test_snapshot_running_listener_state_space(
    smoke, env, tmp_path, transport, lock, listeners, want, lsof_calls
):
    sock = tmp_path / "job.sock"
    if lock:
        (tmp_path / "job.sock.lock").write_text("")
    body = _job_print(_LABEL, transport="tcp" if transport == "none" else transport, sock=str(sock))
    if transport == "none":
        body = body.replace("\t\t--host\n\t\t127.0.0.1\n\t\t--port\n\t\t9123\n", "")
    calls: list[list[str]] = []
    listed = f"PID\tStatus\tLabel\n777\t0\t{_LABEL}\n"
    run = _fake_launchd(listed, {_LABEL: body}, listeners=listeners, calls=calls)
    snap = smoke.snapshot_real_agents(run, home=env.home)
    assert snap[_LABEL].listener == want
    assert len([c for c in calls if Path(c[0]).name == "lsof"]) == lsof_calls
    assert (smoke.snapshot_unknowns(snap) == [f"{_LABEL} (listener)"]) == (want == UNKNOWN)


# Transient mutation: a restart that came back to the same pid and listener moves
# launchd's runs counter; a job that is loaded and booted out again before the
# next boundary is caught there because the comparison runs before every check.
def test_snapshot_catches_a_same_pid_restart_and_a_transient_load(smoke, env):
    listed = f"PID\tStatus\tLabel\n777\t0\t{_LABEL}\n"
    before = smoke.snapshot_real_agents(
        _fake_launchd(listed, {_LABEL: _job_print(_LABEL, runs="3")}, ["777"]), home=env.home
    )
    restarted = smoke.snapshot_real_agents(
        _fake_launchd(listed, {_LABEL: _job_print(_LABEL, runs="4")}, ["777"]), home=env.home
    )
    assert [d for d in smoke.compare_snapshots(before, restarted) if "runs" in d]
    _plist_for(env.agents / "pipecat.stt-server.idle.plist")
    idle = smoke.snapshot_real_agents(_fake_launchd(), home=env.home)
    loaded = smoke.snapshot_real_agents(
        _fake_launchd(
            "PID\tStatus\tLabel\n9\t0\tpipecat.stt-server.idle\n",
            {"pipecat.stt-server.idle": _job_print("pipecat.stt-server.idle", runs="1")},
            ["777"],
        ),
        home=env.home,
    )
    assert smoke.compare_snapshots(idle, loaded)


def test_snapshot_includes_a_loaded_agent_with_no_plist(smoke, env):
    run = _fake_launchd(_LIST, {_GHOST: _GHOST_PRINT}, listeners=["777"])
    snap = smoke.snapshot_real_agents(run, home=env.home)
    assert list(snap) == [_GHOST]  # spare and non-pipecat labels excluded
    assert (snap[_GHOST].pid, snap[_GHOST].listener, snap[_GHOST].plist) == ("777", "777", None)


def test_plistless_agent_port_reaches_the_guard(smoke, env):
    run = _fake_launchd(_LIST, {_GHOST: _GHOST_PRINT})
    ports = smoke.snapshot_ports(smoke.snapshot_real_agents(run, home=env.home))
    assert ports == {9123: _GHOST}
    with pytest.raises(smoke.GuardRefused, match=_GHOST):
        smoke.check_guard(
            SPARE,
            9123,
            env=_good_env(),
            canonical_ports=set(CANONICAL),
            is_listening=lambda p: False,
            agent_ports=ports,
        )


def test_loaded_agent_with_a_plist_is_not_counted_twice(smoke, env):
    _agent_plist(env.agents / f"{_GHOST}.plist", "9123")
    run = _fake_launchd(_LIST, {_GHOST: _GHOST_PRINT}, listeners=["777"])
    snap = smoke.snapshot_real_agents(run, home=env.home)
    assert list(snap) == [_GHOST] and snap[_GHOST].plist is not None


def test_unreadable_launchctl_list_fails_closed(smoke, env):
    with pytest.raises(RuntimeError):
        smoke.snapshot_real_agents(_fake_launchd(list_rc=5), home=env.home)


def test_unreadable_job_print_fails_closed(smoke, env):
    _agent_plist(env.agents / f"{_GHOST}.plist", "9123")

    def run(argv, **kw):
        if Path(str(argv[0])).name == "launchctl" and argv[1] == "print":
            return _cp(argv, 5, "", "launchctl: Input/output error")
        return _fake_launchd()(argv, **kw)

    with pytest.raises(smoke.LaunchdUnreadable):
        smoke.snapshot_real_agents(run, home=env.home)


# --------------------------------------------------------------------------
# the spare port lost after the guard (H2)
# --------------------------------------------------------------------------


class _FakeHarness:
    """Gates before check i: the real-agent comparison (every check), then the
    port check (every check but the first). LOSE_BEFORE stops the gate before
    that check index: the agents gate for stop="agents", else the port gate."""

    def __init__(self, lose_before=None, stop="port"):
        self.lose_before = lose_before
        self.stop = stop
        self.index = -1
        self.compared = 0
        self.ensured = 0
        self.released = 0

    def ensure_real_agents_unchanged(self):
        self.index += 1
        self.compared += 1
        if self.stop == "agents" and self.lose_before == self.index:
            raise _stop_exc(self.stop)

    def ensure_port_ours(self):
        self.ensured += 1
        if self.stop != "agents" and self.lose_before == self.index:
            raise _stop_exc(self.stop)

    def release_holder(self):
        self.released += 1


_SMOKE = None


def _stop_exc(stop):
    if stop == "port":
        return _SMOKE.PortLost("port 1 is held by pid 9, not the spare job")
    if stop == "agents":
        return _SMOKE.RealAgentsChanged("pipecat.stt-server: runs 1 -> 2")
    return _SMOKE.LaunchdUnreadable("cannot read the launchd state of the spare")


_STOP_PREFIX = {
    "port": "port lost",
    "launchd": "launchd state unreadable",
    "agents": "real agents changed",
}


def _checks(ran, behaviours, stop="port"):
    def make(name, how):
        def fn():
            ran.append(name)
            if how == "lost":
                raise _stop_exc(stop)
            if how == "fail":
                raise _SMOKE.CheckFailed("nope")
            if how == "crash":
                raise LookupError("boom")
            if how == "interrupt":
                raise KeyboardInterrupt("signal 2")
            return "ok"

        return fn

    return tuple((n, make(n, h)) for n, h in behaviours)


_NAMES = ("port collision", "socket rollback", "token file")


# what stops the checks (the port lost, the spare's launchd state unreadable, a
# real agent changed) x where (inside check i, or in the gate before check i; the
# real-agent gate runs before every check, the port gate before all but the
# first) x what the other checks do -> rows: the stopping check FAILs, later
# ones are SKIPPED and never run.
@pytest.mark.parametrize("stop", ["port", "launchd", "agents"])
@pytest.mark.parametrize("other", ["ok", "fail", "crash"])
@pytest.mark.parametrize(
    "where",
    [("in", 0), ("in", 1), ("in", 2), ("before", 0), ("before", 1), ("before", 2), (None, None)],
)
def test_run_checks_stop_state_space(smoke, where, other, stop):
    global _SMOKE
    _SMOKE = smoke
    kind, at = where
    if where == ("before", 0) and stop != "agents":
        pytest.skip("no port gate runs before the first check")
    behaviours = [
        (n, "lost" if (kind == "in" and i == at) else other) for i, n in enumerate(_NAMES)
    ]
    h = _FakeHarness(lose_before=at if kind == "before" else None, stop=stop)
    ran: list[str] = []
    rows: list = []
    assert smoke.run_checks(h, _checks(ran, behaviours, stop), rows) is rows
    assert [r[0] for r in rows] == list(_NAMES)  # every check has a row
    lost_at = at if kind is not None else None
    for i, (name, status, detail) in enumerate(rows):
        if lost_at is not None and i == lost_at:
            assert status == smoke.FAIL
            assert _STOP_PREFIX[stop] in detail
        elif lost_at is not None and i > lost_at:
            assert status == smoke.SKIPPED and name not in ran
        else:
            assert status == (smoke.PASS if other == "ok" else smoke.FAIL)
    assert h.released == len(rows) - (len(_NAMES) - 1 - lost_at if lost_at is not None else 0)
    # The real-agent gate ran before every check that started.
    assert h.compared == (lost_at + 1 if lost_at is not None else len(_NAMES))


# an interrupt inside check i x what the earlier checks did -> the interrupt
# propagates and the earlier checks' rows are already in the caller's list.
@pytest.mark.parametrize("at", [0, 1, 2])
@pytest.mark.parametrize("other", ["ok", "fail"])
def test_run_checks_interrupt_keeps_finished_rows(smoke, at, other):
    global _SMOKE
    _SMOKE = smoke
    behaviours = [(n, "interrupt" if i == at else other) for i, n in enumerate(_NAMES)]
    rows: list = [("guard", smoke.PASS, "")]
    with pytest.raises(KeyboardInterrupt):
        smoke.run_checks(_FakeHarness(), _checks([], behaviours), rows)
    assert [r[0] for r in rows] == ["guard", *_NAMES[:at]]
    want = smoke.PASS if other == "ok" else smoke.FAIL
    assert all(r[1] == want for r in rows[1:])


def test_collision_check_fails_when_the_port_was_taken_after_the_guard(smoke, env, monkeypatch):
    monkeypatch.setenv("HOME", str(env.home))
    monkeypatch.setenv("PATH", env.path)
    installs = []
    monkeypatch.setattr(smoke, "install", lambda *a, **k: installs.append(a) or "")
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen(1)
        port = squatter.getsockname()[1]
        h = smoke.Harness(SPARE, port)
        with pytest.raises(smoke.PortLost):
            h.check_collision()
    assert installs == [] and h.holder is None


# lsof answer x launchd pid -> ensure_port_ours raises PortLost?
@pytest.mark.parametrize(
    ("listeners", "pid", "lost"),
    [
        ([], None, False),
        ([], "100", False),
        (["100"], "100", False),
        (["200"], "100", True),
        (["100", "200"], "100", True),
        (["200"], None, True),
        (None, "100", True),
    ],
)
def test_ensure_port_ours_state_space(smoke, env, monkeypatch, listeners, pid, lost):
    monkeypatch.setenv("HOME", str(env.home))
    monkeypatch.setenv("PATH", env.path)
    h = smoke.Harness(SPARE, _free_port())
    monkeypatch.setattr(smoke, "tcp_listener_pids", lambda port, run=None: listeners)
    monkeypatch.setattr(smoke, "require_launchd_state", lambda *a, **k: (True, pid))
    if lost:
        with pytest.raises(smoke.PortLost):
            h.ensure_port_ours()
    else:
        assert h.ensure_port_ours() is None


# --------------------------------------------------------------------------
# a second signal; a symlinked socket-dir ancestor (H9, H8)
# --------------------------------------------------------------------------


def test_second_signal_is_ignored_once_teardown_starts(smoke):
    state = {"tearing_down": False}
    handler = smoke.signal_handler(state)
    with pytest.raises(KeyboardInterrupt):
        handler(signal.SIGINT, None)
    assert state["tearing_down"] is True
    assert handler(signal.SIGTERM, None) is None  # ignored, does not raise


def test_prepare_private_dir_refuses_a_symlinked_ancestor_leaving_home(smoke, tmp_path):
    home = tmp_path / "home"
    (home / "Library").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "Library" / "Caches").symlink_to(outside)
    target = home / "Library" / "Caches" / "pipecat-stt-smoke-launchd"
    with pytest.raises(smoke.CheckFailed, match="outside"):
        smoke.prepare_private_dir(target, home)
    assert list(outside.iterdir()) == []


def test_prepare_private_dir_allows_a_symlink_that_stays_inside_home(smoke, tmp_path):
    home = tmp_path / "home"
    (home / "Library" / "RealCaches").mkdir(parents=True)
    (home / "Library" / "Caches").symlink_to(home / "Library" / "RealCaches")
    target = home / "Library" / "Caches" / "pipecat-stt-smoke-launchd"
    assert smoke.prepare_private_dir(target, home) is True
    assert (home / "Library" / "RealCaches" / "pipecat-stt-smoke-launchd").is_dir()


# --------------------------------------------------------------------------
# start state over both homes; quoted cleanup commands (D, J)
# --------------------------------------------------------------------------


# where a stale (unloaded) spare plist sits x whether $HOME is the agents home
# -> the start state names exactly the plists that exist, in either home.
@pytest.mark.parametrize("where", ["none", "home", "account", "both"])
@pytest.mark.parametrize("same_home", [True, False])
def test_start_state_sees_a_spare_plist_in_either_home(smoke, env, monkeypatch, where, same_home):
    account = env.home if same_home else env.tmp / "account"
    (account / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(env.home))
    monkeypatch.setenv("PATH", env.path)
    monkeypatch.setattr(smoke._smoke_lib, "agents_home", lambda: account)
    in_home = env.agents / f"{SPARE}.plist"
    in_account = account / "Library" / "LaunchAgents" / f"{SPARE}.plist"
    if where in ("home", "both"):
        in_home.write_text("<plist><dict/></plist>")
    if where in ("account", "both"):
        in_account.write_text("<plist><dict/></plist>")
    sock = env.home / "Library" / "Caches" / "pipecat-stt-smoke-launchd" / "spare.sock"
    cmds = smoke.start_state_problems(SPARE, FAKE_UID, sock)
    want = set()
    if where != "none":
        want = {f"rm -f {in_home}"} if (same_home or where in ("home", "both")) else set()
        if where in ("account", "both"):
            want.add(f"rm -f {in_account}")
    assert set(cmds) == want


# A cleanup command a user pastes must name exactly the path the harness saw,
# whatever the path holds: shlex.split(printed) == the argv it stands for.
@pytest.mark.parametrize(
    "name", ["plain", "with space", "quote'in", 'dq"x', "semi;rm -rf x", "$(touch y)", "tab\tx"]
)
def test_cleanup_commands_round_trip_through_the_shell(smoke, env, monkeypatch, name):
    import shlex

    home = env.tmp / name / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", env.path)
    monkeypatch.setattr(smoke._smoke_lib, "agents_home", lambda: home)
    plist = home / "Library" / "LaunchAgents" / f"{SPARE}.plist"
    plist.write_text("<plist><dict/></plist>")
    sock = home / "Library" / "Caches" / "pipecat-stt-smoke-launchd" / "spare.sock"
    sock.parent.mkdir(parents=True)
    sock.write_text("")
    cmds = smoke.start_state_problems(SPARE, FAKE_UID, sock)
    assert sorted(shlex.split(c) for c in cmds) == sorted(
        [["rm", "-f", str(plist)], ["rm", "-f", str(sock)]]
    )


# --------------------------------------------------------------------------
# the installer runs in its own process group (E)
# --------------------------------------------------------------------------

_FAKE_INSTALLER = """#!/usr/bin/env bash
marker="$PIPECAT_SMOKE_TEST_MARKER"
sleep 60 &
echo $! > "$marker.desc"
: > "$marker.ready"
if [[ "$PIPECAT_SMOKE_TEST_MODE" == quick ]]; then sleep 1; echo done; exit 0; fi
sleep 60
"""


def _gone(pid: int) -> bool:
    """Not running: no such process, or a zombie awaiting its new parent's reap."""
    res = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return res.stdout.strip() == "" or res.stdout.strip().startswith("Z")


# the trigger x install or teardown uninstall -> outcome; in every cell the
# installer's background descendant is no longer running when run_installer
# returns or raises, so teardown cannot race it.
@pytest.mark.parametrize(
    ("trigger", "teardown", "mode", "want"),
    [
        ("none", False, "quick", "result"),
        ("timeout", False, "slow", "timeout"),
        ("signal", False, "slow", "interrupt"),
        ("signal", True, "quick", "result"),  # teardown: a signal does not stop it
        ("none", True, "quick", "result"),
    ],
)
def test_installer_group_never_outlives_run_installer(
    smoke, monkeypatch, tmp_path, trigger, teardown, mode, want
):
    import os

    fake = tmp_path / "fake_installer.sh"
    _exe(fake, _FAKE_INSTALLER)
    marker = tmp_path / "m"
    monkeypatch.setattr(smoke, "INSTALLER", fake)
    monkeypatch.setattr(smoke, "INSTALL_TIMEOUT", 2 if trigger == "timeout" else 30)
    env = {
        "PATH": "/usr/bin:/bin",
        "PIPECAT_SMOKE_TEST_MARKER": str(marker),
        "PIPECAT_SMOKE_TEST_MODE": mode,
    }
    state = {"tearing_down": teardown}
    old = {
        s: signal.signal(s, smoke.signal_handler(state)) for s in (signal.SIGINT, signal.SIGTERM)
    }

    def fire():
        deadline = time.monotonic() + 30
        while not Path(f"{marker}.ready").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)

    t = threading.Thread(target=fire) if trigger == "signal" else None
    got = None
    try:
        if t:
            t.start()
        try:
            res = smoke.run_installer("install", env, teardown=teardown)
            got = "result"
        except subprocess.TimeoutExpired:
            got = "timeout"
        except KeyboardInterrupt:
            got = "interrupt"
        desc = int(Path(f"{marker}.desc").read_text())
        gone_at_return = _gone(desc)
    finally:
        if t:
            t.join()
        for s, h in old.items():
            signal.signal(s, h)
    assert got == want
    assert gone_at_return, f"installer descendant {desc} still running after run_installer"
    if want == "result":
        assert res.returncode == 0 and "done" in res.stdout
    if want == "interrupt":
        assert state["tearing_down"] is True  # the harness's own handler saw the signal


# --------------------------------------------------------------------------
# the launchctl stub's state transitions drive teardown's verdict (G)
# --------------------------------------------------------------------------


def test_launchctl_stub_models_bootstrap_and_bootout(env):
    lc = str(env.stub / "launchctl")
    plist = str(env.agents / f"{SPARE}.plist")
    target = f"gui/{FAKE_UID}/{SPARE}"

    def call(*a):
        return subprocess.run([lc, *a], capture_output=True, text=True).returncode

    assert call("print", target) == 113
    assert call("bootout", target) == 3  # not loaded
    assert call("bootstrap", f"gui/{FAKE_UID}", plist) == 0
    assert call("print", target) == 0
    assert call("bootstrap", f"gui/{FAKE_UID}", plist) == 5  # already loaded
    assert call("bootout", target) == 0
    assert call("print", target) == 113
    # Another label never changes the spare's state.
    assert call("bootstrap", f"gui/{FAKE_UID}", str(env.agents / "pipecat.stt-server.plist")) == 0
    assert call("print", target) == 113


# whether the spare is loaded when teardown starts x what bootout does ->
# teardown's problems. Teardown runs the real installer's uninstall against
# the stub; only a bootout that really unloads the job leaves no problem.
@pytest.mark.parametrize("loaded", [True, False])
@pytest.mark.parametrize("bootout", ["ok", "noop", "fail"])
def test_teardown_verdict_follows_the_launchd_state(smoke, env, monkeypatch, loaded, bootout):
    monkeypatch.setenv("HOME", str(env.home))
    monkeypatch.setenv("PATH", env.path)
    monkeypatch.setattr(smoke._smoke_lib, "agents_home", lambda: env.home)
    monkeypatch.setattr(smoke, "UNLOAD_WAIT", 2)
    env.bootout_mode.write_text(bootout)
    if loaded:
        env.loaded_marker.write_text("")
    h = smoke.Harness(SPARE, _free_port())
    problems = h.teardown()
    stays_loaded = loaded and bootout != "ok"
    assert env.loaded_marker.exists() == stays_loaded
    assert any("still loaded" in p for p in problems) == stays_loaded, problems
    if not stays_loaded:
        assert problems == []


# Every way a regression could touch the real ~/Library -> the audit wrapper
# logs it (stat-family reads raise no audit event; the wrapper covers them).
_PROBES = {
    "exists": "Path(P).exists()",
    "is_symlink": "Path(P).is_symlink()",
    "stat": "os.stat(P)",
    "lstat": "os.path.lexists(P)",
    "os.path.exists": "os.path.exists(P)",
    "open": "open(P)",
    "chmod": "os.chmod(P, 0o700)",
    "rename": "os.rename(P, P + '.x')",
    "rmtree": "shutil.rmtree(P, ignore_errors=True)",
    "unlink": "os.unlink(P)",
    "listdir": "os.listdir(P)",
}


@pytest.mark.parametrize("probe", list(_PROBES))
def test_audit_wrapper_sees_every_real_library_touch(tmp_path, probe):
    import os
    import pwd

    # A path that does not exist: every probe fails with ENOENT after the hook.
    real = Path(pwd.getpwuid(os.getuid()).pw_dir) / "Library" / f"pipecat-audit-{os.getpid()}-x"
    assert not os.path.lexists(real)
    script = tmp_path / "probe.py"
    script.write_text(
        "import contextlib, os, shutil\n"
        "from pathlib import Path\n"
        f"P = {str(real)!r}\n"
        f"with contextlib.suppress(OSError):\n    {_PROBES[probe]}\n"
    )
    log = tmp_path / "audit.log"
    res = subprocess.run(
        [sys.executable, str(AUDIT_WRAPPER), str(log), str(script)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stderr
    lines = log.read_text().splitlines()
    assert lines[0] == "audit: on"
    assert any(str(real) in ln for ln in lines[1:]), lines
    assert not os.path.lexists(real)


# The harness drives squat_check.py in the recipes' one form: the launchctl print
# exit status on argv, its output (which can carry a legacy token) on stdin. The
# listener cells prove the pid came from stdin: the agent's own pid is quiet.
@pytest.mark.parametrize(("listener", "warns"), [("777", True), ("4242", False)])
def test_harness_squat_check_uses_the_recipes_form_and_no_answer_on_argv(
    smoke, tmp_path, monkeypatch, listener, warns
):
    import re

    secret = "legacy-token-must-not-reach-argv"
    stub = tmp_path / "bin"
    stub.mkdir()
    record = tmp_path / "record.log"
    _exe(
        stub / "launchctl",
        f"""#!/usr/bin/env bash
printf 'launchctl %s\\n' "$*" >> {tmp_path / "launchctl.log"}
if [[ "$1" == "print" ]]; then
    printf 'state = running\\n\\tpid = 4242\\n\\tenvironment = {{\\n'
    printf '\\t\\tKODA_STT_AUTH_TOKEN => {secret}\\n\\t}}\\n'
    exit 0
fi
exit 0
""",
    )
    _exe(
        stub / "lsof",
        "#!/usr/bin/env bash\n"
        f'for a in "$@"; do [[ "$a" == -Fc ]] && {{ printf "p{listener}\\ncnc\\n"; exit 0; }}; done\n'
        f'printf "p{listener}\\nn127.0.0.1:9998\\n"\n',
    )
    real = shutil.which("python3")
    _exe(
        stub / "python3",
        f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> {record}\nexec {real} "$@"\n',
    )
    monkeypatch.setenv("PATH", f"{stub}:/usr/bin:/bin")
    h = object.__new__(smoke.Harness)
    h.label, h.port, h.uid = SPARE, 9998, FAKE_UID
    assert h.squat_warns() is warns
    # Quiet on the first pass needs the pid from stdin: one launchctl print (the
    # harness's own); a warning re-reads it once more in the confirm pass.
    prints = (tmp_path / "launchctl.log").read_text().splitlines()
    assert len(prints) == (3 if warns else 1), prints
    argv = record.read_text().splitlines()
    assert len(argv) == 1 and "squat_check.py" in argv[0] and secret not in argv[0]
    recipe = next(
        ln
        for ln in (REPO_ROOT / "justfile").read_text().splitlines()
        if ln.strip().startswith("exec python3") and "squat_check.py" in ln
    )
    flags = lambda text: re.findall(r"(?<!\S)--[a-z][a-z-]*", text)  # noqa: E731
    assert flags(argv[0]) == flags(recipe) == ["--host", "--port", "--label", "--launchctl-rc"]


# Teardown removes the spare socket and .lock only while it holds the .lock's
# flock itself: .lock state x socket state -> what is removed, what is reported.
@pytest.mark.parametrize("sock_state", ["absent", "socket", "file"])
@pytest.mark.parametrize("lock_state", ["absent", "free", "held", "symlink"])
def test_remove_spare_socket_state_space(smoke, lock_state, sock_state):
    import fcntl
    import os
    import tempfile

    d = Path(tempfile.mkdtemp(prefix="sk", dir="/tmp"))  # short: AF_UNIX path limit
    sock, lock = d / "s.sock", d / "s.sock.lock"
    holder = None
    try:
        if sock_state == "socket":
            with socket.socket(socket.AF_UNIX) as s:
                s.bind(str(sock))
        elif sock_state == "file":
            sock.write_text("")
        if lock_state in ("free", "held"):
            lock.write_text("")
        if lock_state == "held":  # another open file description holds the flock
            holder = os.open(lock, os.O_RDWR)
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if lock_state == "symlink":
            (d / "target").write_text("")
            lock.symlink_to(d / "target")
        h = object.__new__(smoke.Harness)
        h.sock = sock
        problems: list[str] = []
        h.remove_spare_socket(problems, wait=0.3)

        provable = lock_state == "free"
        sock_ours = sock_state == "socket"
        if sock_state == "absent" and lock_state == "absent":
            assert problems == []
        # The socket goes only when the harness held the lock and it is a socket.
        assert os.path.lexists(sock) == (sock_state != "absent" and not (provable and sock_ours))
        # The lock goes only when the harness held it (a free regular file).
        assert os.path.lexists(lock) == (lock_state != "absent" and not provable)
        if lock_state == "held":
            assert any("still held" in p for p in problems), problems
        if lock_state == "symlink":
            assert problems and (d / "target").exists()
        if lock_state == "absent" and sock_state != "absent":
            assert any("to prove it unserved" in p for p in problems), problems
        if provable and sock_state == "file":
            assert any("not a socket" in p for p in problems), problems
        if provable and sock_state != "file":
            assert problems == []
    finally:
        if holder is not None:
            os.close(holder)
        shutil.rmtree(d, ignore_errors=True)


# A cleanup that fails can never PASS: each failure below is a teardown problem,
# and the residue check uses lexists, so a dangling symlink is still residue.
@pytest.mark.parametrize("failure", ["none", "token-dir", "sock-dir", "dangling-socket"])
def test_teardown_reports_every_failed_cleanup(smoke, env, monkeypatch, failure):
    import os

    monkeypatch.setenv("HOME", str(env.home))
    monkeypatch.setenv("PATH", env.path)
    monkeypatch.setattr(smoke._smoke_lib, "agents_home", lambda: env.home)
    monkeypatch.setattr(smoke, "UNLOAD_WAIT", 2)
    h = smoke.Harness(SPARE, _free_port())
    h.sock_dir_made = smoke.prepare_private_dir(h.sock.parent, env.home)
    locked = None
    if failure == "token-dir":
        h.token_dir = env.home / "tok"
        h.token_dir.mkdir()
        (h.token_dir / "token").write_text("x")
        h.token_dir.chmod(0o500)  # its entry cannot be unlinked
        locked = h.token_dir
    elif failure == "sock-dir":
        (h.sock.parent / "someone-elses").write_text("")
    elif failure == "dangling-socket":
        h.sock.symlink_to(env.home / "nowhere")
    try:
        problems = h.teardown()
    finally:
        if locked is not None:
            locked.chmod(0o700)
    if failure == "none":
        assert problems == []
        assert not os.path.lexists(h.sock.parent)
    else:
        assert problems, failure
        assert any("left behind" in p for p in problems), problems
    if failure == "dangling-socket":
        assert os.path.lexists(h.sock) and str(h.sock) in " ".join(problems)


# The harness's fallback log names come from the code that writes the plist's
# StandardErrorPath / StandardOutPath: for each label, the harness's names equal
# the paths render_stt_plist.py really renders (the installer's shell copy is
# pinned to the renderer in tests/test_render_stt_plist.py).
@pytest.mark.parametrize(
    "label", [SPARE, "pipecat.stt-server", "koda.stt-server", "pipecat.stt-server.parakeet"]
)
def test_log_paths_match_what_the_renderer_writes(smoke, monkeypatch, tmp_path, label):
    import plistlib

    logs = tmp_path / "logs"
    dst = tmp_path / "out.plist"
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(REPO_ROOT),
        "PIPECAT_STT_LABEL": label,
        "PYTHON": sys.executable,
        "REPO_ROOT": str(REPO_ROOT),
        "BACKEND": "echo",
        "MODEL": "test/model",
        "HOME": str(tmp_path),
        "LOG_DIR": str(logs),
        "PLIST_DST": str(dst),
        "HOST": "127.0.0.1",
        "PORT": "9998",
    }
    res = subprocess.run(
        [str(VENV_PYTHON), str(REPO_ROOT / "scripts" / "render_stt_plist.py")],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert res.returncode == 0, res.stderr
    rendered = plistlib.loads(dst.read_bytes())
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))  # no plist at the label's path
    monkeypatch.setenv("PIPECAT_STT_LOG_DIR", str(logs))
    assert smoke._log_paths(label) == [
        Path(rendered["StandardErrorPath"]),
        Path(rendered["StandardOutPath"]),
    ]


# The runner's launchd row takes its FAIL detail from the harness's failure
# channel (LAUNCHD_FAIL_MARK lines), fed here the REAL harness output: the row
# names the check that failed, never just the table's last row.
@pytest.mark.parametrize(
    ("case", "want"),
    [
        ("inject-fail", "port collision: injected failure"),
        ("guard", "guard: KODA_STT_LABEL must be set"),
        ("crash", "Traceback-last-line"),
        ("channel-not-last", "token file: nope"),
    ],
)
def test_runner_reads_the_harness_failure_channel(smoke, env, monkeypatch, case, want):
    runner = _load(REPO_ROOT / "scripts" / "smoke.py", "smoke_runner_for_channel")
    if case == "channel-not-last":  # the channel line wins over the last line
        out = smoke._smoke_lib.LAUNCHD_FAIL_MARK + "token file: nope\nFAIL     trailer  x\n"
        r = subprocess.CompletedProcess([], 1, out, "")
    elif case == "crash":  # no channel line: the runner falls back to the last line
        r = subprocess.CompletedProcess(
            [], 1, "", "Traceback (most recent call last):\nTraceback-last-line\n"
        )
    else:
        e = env.spare_env() if case == "inject-fail" else env.env(PIPECAT_STT_LABEL=SPARE)
        r = env.run(["--port", str(_free_port())], e, inject="fail")
        assert r.returncode == 1, (r.stdout, r.stderr)
        lines = [
            ln for ln in r.stdout.splitlines() if ln.startswith(smoke._smoke_lib.LAUNCHD_FAIL_MARK)
        ]
        assert lines and any(want in ln for ln in lines), r.stdout
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(runner, "run_command", lambda *a, **k: r)
    row = runner.launchd_step(True)
    assert row.outcome == "FAIL" and want in row.detail, row
    if case == "inject-fail":
        assert "real agents unchanged" not in row.detail


# The uid comes from the kernel, never from a PATH-resolved `id`: os.getuid()
# x a hostile `id` on PATH -> current_uid().
@pytest.mark.parametrize("uid", [0, 501, 4242])
@pytest.mark.parametrize("id_answer", ["999\n", "", "not-a-number\n"])
def test_current_uid_state_space(smoke, monkeypatch, uid, id_answer):
    calls = []
    monkeypatch.setattr(smoke.os, "getuid", lambda: uid)
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda argv, **k: calls.append(argv) or _cp(argv, 0, id_answer),
    )
    assert smoke.current_uid() == str(uid)
    assert calls == []


# The failure injection lives in tests/_with_agents_home.py only: the production
# harness has no test hook, so `--inject <mode>` is an argparse usage error
# (exit 2) and nothing is installed or booted out.
@pytest.mark.parametrize("mode", ["fail", "crash", "hang"])
def test_production_harness_has_no_inject_hook(smoke, env, mode):
    assert not hasattr(smoke.Harness, "maybe_inject")
    r = env.run(["--inject", mode, "--port", str(_free_port())], env.spare_env(), timeout=60)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "unrecognized arguments: --inject" in r.stderr
    assert _mutating(env.argv()) == []
    assert not env.spare_residue()


# --------------------------------------------------------------------------
# real-agent snapshot fails closed, one source of truth (review round 6)
# --------------------------------------------------------------------------


# launchctl print of a real job: loaded? x pid line? x runs line? -> the runs
# field, and whether the baseline refuses the run. A loaded job with a pid and
# no `runs =` line is UNKNOWN (None == None would blind the restart check).
@pytest.mark.parametrize(
    ("job", "pid", "runs", "want_runs", "refused"),
    [
        ("unloaded", False, False, None, False),
        ("loaded", False, False, None, False),
        ("loaded", False, True, "3", False),
        ("loaded", True, True, "3", False),
        ("loaded", True, False, UNKNOWN, True),
    ],
)
def test_read_job_runs_state_space(smoke, env, job, pid, runs, want_runs, refused):
    prints = {}
    if job == "loaded":
        body = _job_print(_LABEL, running=pid, runs="3")
        if not runs:
            body = body.replace("\truns = 3\n", "")
        prints[_LABEL] = body
    listed = f"PID\tStatus\tLabel\n777\t0\t{_LABEL}\n" if job == "loaded" else ""
    _plist_for(env.agents / f"{_LABEL}.plist")
    snap = smoke.snapshot_real_agents(_fake_launchd(listed, prints, ["777"]), home=env.home)
    assert snap[_LABEL].runs == want_runs
    assert bool(smoke.snapshot_unknowns(snap)) is refused
    if refused:
        assert smoke.snapshot_unknowns(snap) == [f"{_LABEL} (runs)"]
        assert [d for d in smoke.compare_snapshots(snap, snap) if "runs unreadable" in d]


# Every AgentState field is compared (derived from dataclasses.fields, no
# hand-kept list): changing any one field is a difference naming it.
def test_compare_snapshots_covers_every_field(smoke):
    import dataclasses

    base = _st(smoke, plist_port="9565")
    for f in dataclasses.fields(smoke.AgentState):
        changed = dataclasses.replace(base, **{f.name: ("x", "changed")})
        diffs = smoke.compare_snapshots({"a": base}, {"a": changed})
        assert [d for d in diffs if f"a: {f.name} " in d], (f.name, diffs)


# The guard's port set comes from the baseline: a plist rewritten (or removed)
# after the baseline -> the guard still sees the port the baseline recorded.
@pytest.mark.parametrize("after", ["same", "rewritten", "removed", "malformed"])
def test_guard_ports_come_from_the_baseline(smoke, env, after):
    plist = env.agents / "pipecat.stt-server.custom.plist"
    _plist_for(plist, port="9123")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATH", env.path)
        before = smoke.snapshot_real_agents(home=env.home)
    if after == "rewritten":
        _plist_for(plist, port="9124")
    elif after == "removed":
        plist.unlink()
    elif after == "malformed":
        plist.write_text("not a plist")
    assert before["pipecat.stt-server.custom"].plist_port == "9123"
    assert smoke.snapshot_ports(before) == {9123: "pipecat.stt-server.custom"}


# job_endpoint reads the job's argv through read_stt_plist.read_argv_flags (no
# fake plist dict); the argv reader agrees with the plist reader on every case.
@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["/py", "-m", "stt_server", "serve", "--port", "9123"],
        ["/py", "serve", "--host", "::1", "--port=9124"],
        ["/py", "serve", "--socket-path", "/s.sock", "--port", "1"],
        ["/py", "serve", "--log-level", "--socket-path", "--port", "9"],
        ["/py", "serve", "--supervised", "--port", "9125"],
    ],
)
def test_read_argv_flags_matches_read_flags(smoke, monkeypatch, argv):
    rsp = smoke.read_stt_plist
    want = rsp.read_flags({"ProgramArguments": argv})
    assert rsp.read_argv_flags(argv) == want
    body = "\targuments = {\n" + "".join(f"\t\t{a}\n" for a in argv) + "\t}\n"

    def no_dict(_plist):
        raise AssertionError("job_endpoint must not build a fake plist dict")

    monkeypatch.setattr(rsp, "read_flags", no_dict)
    ep = smoke.job_endpoint(body)
    if want.get("--socket-path"):
        assert ep == ("unix", want["--socket-path"])
    elif want.get("--port"):
        assert ep == ("tcp", want.get("--host", ""), want["--port"])
    else:
        assert ep is None


# check_token sends its throwaway token only over connect_verified sockets for
# the spare job's (pid, runs) from one read_job, and PASSes only when a fresh
# read_job after the gate shows the same pair: job state before x the verified
# connection's answer x the job after -> verdict, and whether a token was sent.
@pytest.mark.parametrize("after", ["same", "restarted"])
@pytest.mark.parametrize("conn", ["verified", "refused"])
@pytest.mark.parametrize("job", ["running", "no-runs", "unknown-runs", "no-pid"])
def test_check_token_sends_only_over_verified_connections(
    smoke, env, monkeypatch, job, conn, after
):
    monkeypatch.setenv("HOME", str(env.home))
    h = smoke.Harness(SPARE, _free_port())
    jobs = {
        "running": (True, "777", "3", None),
        "no-runs": (True, "777", None, None),
        "unknown-runs": (True, "777", UNKNOWN, None),
        "no-pid": (True, None, "3", None),
    }
    reads = []

    def read_job(label, uid, run=None):
        reads.append(label)
        if len(reads) == 1:
            return jobs[job]
        return (True, "777", "3" if after == "same" else "4", None)

    monkeypatch.setattr(smoke, "read_job", read_job)
    monkeypatch.setattr(smoke, "install", lambda e, what: "")
    monkeypatch.setattr(
        smoke, "read_plist_field", lambda label, f: str(env.home.resolve()) if f else ""
    )
    monkeypatch.setattr(smoke.Harness, "wait_for", lambda self, what, pred, **k: True)
    monkeypatch.setattr(
        "stt_server.auth.load_token_file", lambda p, uid=None: (str(env.home.resolve()), "t")
    )
    connects, sent = [], []

    def connect_verified(endpoint, pid, runs, identity, **k):
        connects.append((endpoint, pid, runs))
        if conn == "refused":
            raise smoke._smoke_lib.EndpointRefused("held by pid 666")
        return object()

    def gate(host, port, token_file, *, connect):
        connect()
        sent.append(token_file)

    monkeypatch.setattr(smoke._smoke_lib, "connect_verified", connect_verified)
    monkeypatch.setattr(smoke._smoke_lib, "assert_token_gate", gate)
    try:
        if job != "running":
            with pytest.raises(smoke.CheckFailed, match="no token sent"):
                h.check_token()
            assert connects == [] and sent == []
            return
        if conn == "refused":
            with pytest.raises(smoke.CheckFailed, match="token not sent"):
                h.check_token()
            assert sent == []
        elif after == "restarted":
            with pytest.raises(smoke.CheckFailed, match="changed during the token check"):
                h.check_token()
        else:
            assert "verified" in h.check_token()
        assert connects == [(("tcp", "127.0.0.1", str(h.port)), "777", "3")]
    finally:
        if h.token_dir is not None:
            shutil.rmtree(h.token_dir, ignore_errors=True)


# An installer group that cannot be shown stopped is never a result: in a
# check it is InstallerNotStopped (a STOP_CHECKS failure, so no later check
# installs beside it); in teardown it is a problem, so teardown cannot PASS.
@pytest.mark.parametrize("where", ["check", "teardown"])
def test_installer_group_not_stopped_is_a_failure(smoke, env, monkeypatch, where):
    def run_owned(*a, **k):
        raise smoke._smoke_lib.GroupNotStopped("cannot SIGKILL process group 1: EPERM")

    monkeypatch.setattr(smoke._smoke_lib, "run_owned", run_owned)
    if where == "check":
        with pytest.raises(smoke.InstallerNotStopped):
            smoke.run_installer("install", {})
        assert issubclass(smoke.InstallerNotStopped, smoke.STOP_CHECKS)
        rows = smoke.run_checks(
            type("H", (), {"ensure_real_agents_unchanged": lambda s: None,
                           "ensure_port_ours": lambda s: None,
                           "release_holder": lambda s: None})(),
            (("a", lambda: smoke.install({}, "a")), ("b", lambda: "ran")),
            [],
        )  # fmt: skip
        assert [r[1] for r in rows] == [smoke.FAIL, smoke.SKIPPED]
        assert rows[0][2].startswith("installer not stopped")
    else:
        monkeypatch.setenv("HOME", str(env.home))
        monkeypatch.setattr(smoke._smoke_lib, "agents_home", lambda: env.home)
        monkeypatch.setattr(smoke, "require_launchd_state", lambda *a, **k: (False, None))
        h = smoke.Harness(SPARE, _free_port())
        plist = env.agents / f"{SPARE}.plist"
        plist.write_text("<plist/>")
        problems = h.teardown()
        assert any("uninstall failed" in p and "EPERM" in p for p in problems), problems
        # The uninstall itself may still be running: nothing more is removed.
        assert "teardown changed nothing" in problems[0] and plist.exists()


# run_owned decides "group not stopped" BEFORE it redelivers a held signal:
# the group kill (stopped / not stopped) x the held signal (none, a handler
# that raises, a handler that returns) -> the outcome. A raising handler's
# exception carries the GroupNotStopped (group_not_stopped, and as __cause__);
# otherwise GroupNotStopped is raised. The handler always gets the signal.
@pytest.mark.parametrize("held", ["none", "raises", "returns"])
@pytest.mark.parametrize("stopped", [True, False])
def test_run_owned_group_not_stopped_survives_a_held_signal(smoke, monkeypatch, stopped, held):
    import errno

    lib = smoke._smoke_lib
    real_killpg, real_group = os.killpg, lib.OwnedGroup
    got: list[int] = []

    class Group(real_group):
        def __init__(self, *a, **k):
            if held != "none":
                os.kill(os.getpid(), signal.SIGINT)  # arrives while held
            super().__init__(*a, **k)

    def killpg(pgid, sig):
        if stopped:
            return real_killpg(pgid, sig)
        raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))

    def handler(signum, _frame):
        got.append(signum)
        if held == "raises":
            raise KeyboardInterrupt(f"signal {signum}")

    monkeypatch.setattr(lib, "OwnedGroup", Group)
    monkeypatch.setattr(lib.os, "killpg", killpg)
    old = signal.signal(signal.SIGINT, handler)
    try:
        with (
            pytest.raises(BaseException)
            if (held == "raises" or not stopped)
            else (contextlib.nullcontext()) as caught
        ):
            res, _ = lib.run_owned([sys.executable, "-c", "pass"], 30, interrupt_signal=None)
    finally:
        signal.signal(signal.SIGINT, old)
    assert got == ([] if held == "none" else [signal.SIGINT])
    if held == "raises":
        exc = caught.value
        assert isinstance(exc, KeyboardInterrupt)
        gns = lib.group_not_stopped(exc)
        if stopped:
            assert gns is None
        else:
            assert isinstance(gns, lib.GroupNotStopped) and exc.__cause__ is gns
    elif not stopped:
        assert isinstance(caught.value, lib.GroupNotStopped)
    else:
        assert res.returncode is not None  # a result (a held signal stops it: -9)


# After an installer that could not be shown stopped the teardown changes
# nothing: whether the installer group stopped x whether a signal was held
# during the install (the harness's own handler raises KeyboardInterrupt) ->
# the run's rows and the teardown path. Not stopped (with or without the
# signal): the install row is "installer not stopped", no uninstall runs, the
# spare's artifacts are untouched, and the teardown row FAILs with the
# shell-quoted cleanup commands. Stopped: the uninstall runs and removes them.
@pytest.mark.parametrize("signalled", [False, True])
@pytest.mark.parametrize("stopped", [True, False])
def test_teardown_changes_nothing_after_an_installer_not_stopped(
    smoke, env, monkeypatch, stopped, signalled
):
    import errno
    import shlex

    monkeypatch.setenv("HOME", str(env.home))
    monkeypatch.setenv("PATH", env.path)
    lib = smoke._smoke_lib
    real_killpg = os.killpg
    fake = env.tmp / "fake installer.sh"
    _exe(
        fake, "#!/bin/bash\n" + ('kill -INT "$PPID"\nsleep 0.3\n' if signalled else "") + "exit 0\n"
    )
    monkeypatch.setattr(smoke, "INSTALLER", fake)
    subs = []
    real_run_owned = lib.run_owned

    def run_owned(cmd, *a, **k):
        subs.append(cmd[-1])
        return real_run_owned(cmd, *a, **k)

    def killpg(pgid, sig):
        if stopped or subs[-1] == "uninstall":
            return real_killpg(pgid, sig)
        raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))

    monkeypatch.setattr(lib, "run_owned", run_owned)
    monkeypatch.setattr(lib.os, "killpg", killpg)
    monkeypatch.setattr(smoke, "require_launchd_state", lambda *a, **k: (False, None))
    monkeypatch.setattr(smoke, "snapshot_real_agents", lambda *a, **k: {})

    class H(smoke.Harness):
        def ensure_real_agents_unchanged(self):
            pass

        def ensure_port_ours(self):
            pass

    h = H(SPARE, _free_port())
    plist = env.agents / f"{SPARE}.plist"
    plist.write_text("<plist/>")  # read_stt_plist cannot parse it: logs by the naming rule
    logs = smoke._log_paths(SPARE)
    for p in logs:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("log\n")
    checks = (
        ("a", lambda: smoke.install(smoke.install_env(SPARE, h.port), "a")),
        ("b", lambda: "ran"),
    )
    results: list = []
    rc = smoke.checks_then_teardown(h, checks, results, {}, env.home)
    rows = {n: (s, d) for n, s, d in results}
    assert rc == 1 or stopped
    teardown_status, teardown_detail = rows["teardown"]
    if stopped:
        assert subs == ["install", "uninstall"]
        if signalled:
            assert rows["interrupted"][0] == smoke.FAIL
        else:
            assert rows["a"][0] == smoke.PASS and rows["b"] == (smoke.PASS, "ran")
        assert not plist.exists() and not any(p.exists() for p in logs)
        return
    assert subs == ["install"]  # no uninstall beside a possibly running installer
    assert rows["a"][0] == smoke.FAIL and rows["a"][1].startswith("installer not stopped")
    assert rows["b"][0] == smoke.SKIPPED
    if signalled:
        assert "KeyboardInterrupt" in rows["a"][1]
    assert plist.exists() and all(p.exists() for p in logs)  # nothing removed
    assert teardown_status == smoke.FAIL
    assert "teardown changed nothing" in teardown_detail
    assert str(plist) in teardown_detail
    bootout = f"cleanup: launchctl bootout gui/{h.uid}/{SPARE}"
    assert bootout in teardown_detail
    rm = next(p for p in teardown_detail.split("; ") if p.startswith("cleanup: rm -f "))
    assert shlex.split(rm[len("cleanup: ") :])[2:] == [
        str(plist),
        *map(str, logs),
        str(h.sock),
        str(smoke._lock_path(h.sock)),
    ]
    assert shlex.quote(str(fake)) in teardown_detail  # the pgrep check, quoted


# Every --port syntax the server's parser accepts (argparse type=int over the
# serve grammar: "--port N" or "--port=N", last one wins) x where it is
# recorded (an unloaded agent's plist, a loaded job's arguments block) -> the
# guard's port set holds 9998, or the snapshot is UNKNOWN and the run refuses.
# An unparseable value is never "no port".
_PORT_FORMS = [
    (["--port", "9998"], 9998),
    (["--port=9998"], 9998),
    (["--port", "+9998"], 9998),
    (["--port=+9998"], 9998),
    (["--port", " 9998 "], 9998),
    (["--port", "09998"], 9998),
    (["--port", "9_998"], 9998),
    (["--port", "٩٩٩٨"], 9998),  # Arabic-Indic digits: int() takes them
    (["--port", "1", "--port", "+9998"], 9998),  # the last occurrence wins
    (["--port", "abc"], "refuse"),
    (["--port="], "refuse"),
    (["--port", "--host"], "refuse"),
    (["--port", "99.98"], "refuse"),
]


@pytest.mark.parametrize(("argv", "want"), _PORT_FORMS)
def test_port_guard_reads_every_server_port_syntax_from_a_plist(smoke, env, argv, want):
    import plistlib

    args = [sys.executable, "-m", "stt_server", "serve", "--host", "127.0.0.1", *argv]
    plist = env.agents / "pipecat.stt-server.custom.plist"
    plist.write_bytes(plistlib.dumps({"Label": plist.stem, "ProgramArguments": args}))
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATH", env.path)
        snap = smoke.snapshot_real_agents(home=env.home)
    if want == "refuse":
        assert smoke.snapshot_unknowns(snap) == ["pipecat.stt-server.custom (plist_port)"]
        with pytest.raises(smoke.GuardRefused):
            smoke.snapshot_ports(snap)
    else:
        assert smoke.snapshot_unknowns(snap) == []
        assert smoke.snapshot_ports(snap) == {want: "pipecat.stt-server.custom"}


@pytest.mark.parametrize(("argv", "want"), _PORT_FORMS)
def test_port_guard_reads_every_server_port_syntax_from_a_job(smoke, argv, want):
    body = "\n".join(
        ["\tpid = 4242", "\truns = 3", "\targuments = {", "\t\t/py", "\t\tserve",
         "\t\t--host", "\t\t127.0.0.1", *(f"\t\t{a}" for a in argv), "\t}", ""]
    )  # fmt: skip

    def run(cmd, **_k):
        if cmd[:2] == ["launchctl", "print"]:
            return _cp(cmd, 0, body)
        return _cp(cmd, 1)  # lsof: no listener

    loaded, pid, runs, endpoint = smoke.read_job("pipecat.stt-server.x", "501", run)
    st = smoke.AgentState(
        loaded, pid, runs, endpoint, None, smoke.endpoint_listener(loaded, pid, endpoint, run)
    )
    snap = {"pipecat.stt-server.x": st}
    if want == "refuse":
        assert endpoint == smoke.UNKNOWN
        assert smoke.snapshot_unknowns(snap) == ["pipecat.stt-server.x (endpoint, listener)"]
        with pytest.raises(smoke.GuardRefused):
            smoke.snapshot_ports(snap)
    else:
        assert endpoint == ("tcp", "127.0.0.1", str(want))
        assert smoke.snapshot_ports(snap) == {want: "pipecat.stt-server.x"}


# parse_port IS the server's parse: for each form above, the serve parser's
# own answer (or its refusal) equals read_stt_plist.parse_port's.
@pytest.mark.parametrize(("argv", "_want"), _PORT_FORMS)
def test_parse_port_matches_the_server_parser(smoke, argv, _want):
    sys.path.insert(0, str(REPO_ROOT))
    from stt_server.__main__ import _build_parser

    flags = smoke.read_stt_plist.read_argv_flags(argv)
    try:
        server = _build_parser().parse_args(["serve", "--backend", "echo", *argv]).port
    except SystemExit:
        server = None
    assert smoke.read_stt_plist.parse_port(flags["--port"]) == server


# The transport-recovery checks prove the SPARE answered, not any listener:
# transport x the spare job's state x the verified connection -> tcp_up /
# sock_up True only for (running, verified, 101); a lookalike listener is a
# CheckFailed (wait_for retries it); no plain socket is ever the proof.
@pytest.mark.parametrize("status", [101, 401])
@pytest.mark.parametrize("conn", ["verified", "refused"])
@pytest.mark.parametrize("job", ["running", "no-pid", "unknown-runs", "unloaded"])
@pytest.mark.parametrize("transport", ["tcp", "unix"])
def test_transport_recovery_requires_the_spare_jobs_server_end(
    smoke, env, monkeypatch, transport, job, conn, status
):
    monkeypatch.setenv("HOME", str(env.home))
    h = smoke.Harness(SPARE, _free_port())
    jobs = {
        "running": (True, "777", "3", None),
        "no-pid": (True, None, None, None),
        "unknown-runs": (True, "777", smoke.UNKNOWN, None),
        "unloaded": (False, None, None, None),
    }
    monkeypatch.setattr(smoke, "read_job", lambda label, uid, run=None: jobs[job])
    connects, handshakes = [], []

    def connect_verified(endpoint, pid, runs, identity, **k):
        connects.append((endpoint, pid, runs))
        if conn == "refused":
            raise smoke._smoke_lib.EndpointRefused("held by pid 666, not the launchd job")
        return "verified-sock"

    def ws_handshake_status(**kw):
        handshakes.append(kw)
        assert kw.get("sock") == "verified-sock", kw  # never a plain address
        return status

    monkeypatch.setattr(smoke._smoke_lib, "connect_verified", connect_verified)
    monkeypatch.setattr(smoke._smoke_lib, "ws_handshake_status", ws_handshake_status)
    up = h.tcp_up if transport == "tcp" else h.sock_up
    want_ep = ("tcp", "127.0.0.1", str(h.port)) if transport == "tcp" else ("unix", str(h.sock))
    if job != "running":
        with pytest.raises(smoke.CheckFailed, match="pid and runs"):
            up()
        assert connects == [] and handshakes == []
    elif conn == "refused":
        with pytest.raises(smoke.CheckFailed, match="not the spare job"):
            up()
        assert handshakes == []
    else:
        assert up() is (status == 101)
    if job == "running":
        assert connects == [(want_ep, "777", "3")]


# Decision 12's post-exchange identity check in transport recovery: the FRESH
# read after the exchange x transport -> tcp_up / sock_up True only when it
# still gives the same (pid, runs); a restart (pid or runs changed) or an
# unload during the exchange is CheckFailed, an unreadable read is
# LaunchdUnreadable (the harness's stop rule).
@pytest.mark.parametrize(
    ("after", "want"),
    [
        ((True, "777", "3", None), True),
        ((True, "778", "3", None), "changed"),
        ((True, "777", "4", None), "changed"),
        ((False, None, None, None), "changed"),
        ("unreadable", "unreadable"),
    ],
)
@pytest.mark.parametrize("transport", ["tcp", "unix"])
def test_transport_recovery_rereads_the_identity_after_the_exchange(
    smoke, env, monkeypatch, transport, after, want
):
    monkeypatch.setenv("HOME", str(env.home))
    h = smoke.Harness(SPARE, _free_port())
    reads = []

    def read_job(label, uid, run=None):
        reads.append(label)
        if len(reads) == 1:
            return (True, "777", "3", None)
        if after == "unreadable":
            raise smoke.LaunchdUnreadable("launchctl print exit 5")
        return after

    exchanged = []
    monkeypatch.setattr(smoke, "read_job", read_job)
    monkeypatch.setattr(smoke._smoke_lib, "connect_verified", lambda *a, **k: "verified-sock")
    monkeypatch.setattr(
        smoke._smoke_lib, "ws_handshake_status", lambda **kw: exchanged.append(kw) or 101
    )
    up = h.tcp_up if transport == "tcp" else h.sock_up
    if want is True:
        assert up() is True
    elif want == "changed":
        with pytest.raises(smoke.CheckFailed, match="changed during the exchange"):
            up()
    else:
        with pytest.raises(smoke.LaunchdUnreadable):
            up()
    assert len(exchanged) == 1 and len(reads) == 2  # the fresh read follows the exchange


# A hostile caller environment never reaches the installer or any harness
# subprocess: each name below (bash startup files, an exported launchctl
# function, Python startup and loader variables, a raw token, the caller's
# PATH) x the child (the installer through install_env, a harness subprocess
# under scrubbed_environ) -> absent from the child's environment, PATH is the
# fixed SAFE_PATH, and BASH_ENV's file was never sourced.
_HOSTILE = {
    "BASH_ENV": None,  # set to a file that leaves a marker when sourced
    "ENV": "/nonexistent/env",
    "BASH_FUNC_launchctl%%": "() {  echo hijacked; }",
    "PYTHONPATH": "/evil",
    "PYTHONSTARTUP": "/evil/s.py",
    "PYTHONHOME": "/evil",
    "DYLD_INSERT_LIBRARIES": "/evil.dylib",
    "LD_PRELOAD": "/evil.so",
    "PIPECAT_STT_AUTH_TOKEN": "raw-secret",
    "IFS": "x",
}


@pytest.mark.parametrize("child", ["installer", "harness-subprocess"])
def test_hostile_environment_never_reaches_a_harness_child(smoke, monkeypatch, tmp_path, child):
    # The production PATH, not the stub one: the children below call only
    # absolute paths, never launchctl.
    monkeypatch.setattr(smoke._smoke_lib, "safe_path", lambda: smoke._smoke_lib.SAFE_PATH)
    marker = tmp_path / "bash_env_sourced"
    startup = tmp_path / "startup.sh"
    startup.write_text(f": > {marker}\n")
    hostile = {**_HOSTILE, "BASH_ENV": str(startup)}
    base = {"HOME": str(tmp_path), "USER": "me", "TMPDIR": str(tmp_path), "PATH": "/evil/bin"}
    fake = tmp_path / "fake_installer.sh"
    _exe(fake, "#!/bin/bash\n/usr/bin/env\n")
    if child == "installer":
        monkeypatch.setattr(smoke, "INSTALLER", fake)
        out = smoke.run_installer(
            "install", smoke.install_env(SPARE, 9998, base={**base, **hostile})
        ).stdout
    else:
        for k, v in {**base, **hostile}.items():
            monkeypatch.setenv(k, v)
        with smoke.scrubbed_environ():
            out = subprocess.run([smoke.BASH, str(fake)], capture_output=True, text=True).stdout
        assert os.environ["PATH"] == "/evil/bin"  # restored afterwards
    names = {ln.split("=", 1)[0]: ln.split("=", 1)[1] for ln in out.splitlines() if "=" in ln}
    for name in hostile:
        assert name not in names, (name, out)
    assert "hijacked" not in out and "/evil" not in out
    assert names["PATH"] == smoke._smoke_lib.SAFE_PATH
    assert names["HOME"] == str(tmp_path)
    assert not marker.exists(), "BASH_ENV was sourced"
    if child == "installer":
        assert names["PIPECAT_STT_LABEL"] == SPARE


def test_the_harness_never_resolves_bash_through_path(smoke):
    seen = []
    smoke.canonical_ports(run=lambda cmd, **k: seen.append(cmd) or _cp(cmd, 0, "9565\n"))
    assert {c[0] for c in seen} == {"/bin/bash"}
    assert smoke._smoke_lib.SAFE_PATH == "/usr/bin:/bin:/usr/sbin:/sbin"


# Each bound in the harness's budget is the one the code really waits on:
# call site -> the timeout it uses == the named constant the budget sums.
def test_budget_bounds_are_the_ones_the_code_uses(smoke, monkeypatch):
    import inspect

    seen = {}

    def run_owned(cmd, timeout, **k):
        seen["install"] = timeout
        return _cp(cmd, 0), False

    monkeypatch.setattr(smoke._smoke_lib, "run_owned", run_owned)
    smoke.run_installer("install", {})
    assert seen["install"] == smoke.INSTALL_TIMEOUT

    def run(cmd, **k):
        seen.setdefault(Path(cmd[0]).name, set()).add(k.get("timeout"))
        return _cp(cmd, 1)

    smoke.squat_check.lsof_output(["-Fp"], run)
    smoke.squat_check.launchd_state("x", run)
    smoke._run(["launchctl", "list"], run)
    assert seen["lsof"] == {smoke.squat_check.LSOF_TIMEOUT}
    assert seen["launchctl"] == {smoke.squat_check.LAUNCHCTL_TIMEOUT, smoke.CMD_TIMEOUT}
    sig = inspect.signature(smoke.Harness.remove_spare_socket)
    assert sig.parameters["wait"].default == smoke.SOCKET_LOCK_WAIT
    assert inspect.signature(smoke.Harness.wait_for).parameters["every"].default == smoke.WAIT_EVERY
    for name in ("connect_verified", "ws_handshake_status"):
        fn = getattr(smoke._smoke_lib, name)
        assert inspect.signature(fn).parameters["timeout"].default == (
            smoke._smoke_lib.HANDSHAKE_TIMEOUT
        )
    # More agents, more snapshot time, in both budgets.
    assert smoke.teardown_budget(4) - smoke.teardown_budget(0) == pytest.approx(
        4 * (smoke.squat_check.LAUNCHCTL_TIMEOUT + smoke.squat_check.LSOF_TIMEOUT)
    )
    assert smoke.run_budget(4) > smoke.run_budget(0) > smoke.teardown_budget(0)


# One "could not read" sentinel: the harness's UNKNOWN IS _smoke_lib's (the
# same object, not an equal copy), and connect_verified refuses it as a runs
# counter (identity runs x pid -> refused before any connect).
@pytest.mark.parametrize(
    ("pid", "runs", "refused"),
    [("777", "3", False), ("777", UNKNOWN, True), ("777", None, True), (None, "3", True)],
)
def test_unknown_is_one_definition_and_never_an_identity(smoke, pid, runs, refused):
    assert smoke.UNKNOWN is smoke._smoke_lib.UNKNOWN is smoke.squat_check.UNKNOWN
    # An identifier-like literal is interned, so `is` alone cannot see a copy:
    # the scripts spell the sentinel exactly once, at its definition (in
    # squat_check, which cannot import _smoke_lib).
    copies = [
        f"{p.name}:{n}"
        for p in sorted((REPO_ROOT / "scripts").glob("*.py"))
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if repr(smoke.UNKNOWN) in line or f'"{smoke.UNKNOWN}"' in line
    ]
    assert len(copies) == 1 and copies[0].startswith("squat_check.py:"), copies
    connected = []

    class _Sock:
        def close(self):
            pass

    def held_by(endpoint, want, still_same, owner, **k):
        connected.append(endpoint)
        return _Sock()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(smoke._smoke_lib, "_connect_held_by", held_by)
        call = lambda: smoke._smoke_lib.connect_verified(  # noqa: E731
            ("tcp", "127.0.0.1", "1"), pid, runs, lambda: (pid, runs)
        )
        if refused:
            with pytest.raises(smoke._smoke_lib.EndpointRefused, match="no launchd pid and runs"):
                call()
            assert connected == []
        else:
            call()
            assert connected == [("tcp", "127.0.0.1", "1")]


# One precondition for every verified connection to the spare (finding 6):
# the job's read_job answer x the caller (verified_status, the token check) ->
# both refuse exactly the same answers with the same message (the token check
# adds only "; no token sent"), UNKNOWN runs included, and nothing connects.
_JOBS = {
    "running": ((True, "777", "3", None), None),
    "unloaded": ((False, None, None, None), "loaded False, pid None, runs None"),
    "no-pid": ((True, None, "3", None), "loaded True, pid None, runs 3"),
    "no-runs": ((True, "777", None, None), "loaded True, pid 777, runs None"),
    "unknown-runs": ((True, "777", UNKNOWN, None), f"loaded True, pid 777, runs {UNKNOWN}"),
}


@pytest.mark.parametrize("caller", ["verified_status", "check_token", "helper"])
@pytest.mark.parametrize("job", list(_JOBS))
def test_running_identity_is_one_precondition(smoke, env, monkeypatch, job, caller):
    monkeypatch.setenv("HOME", str(env.home))
    h = smoke.Harness(SPARE, _free_port())
    answer, why = _JOBS[job]
    monkeypatch.setattr(smoke, "read_job", lambda label, uid, run=None: answer)
    connects = []
    monkeypatch.setattr(
        smoke._smoke_lib, "connect_verified", lambda *a, **k: connects.append(a) or "sock"
    )
    monkeypatch.setattr(smoke._smoke_lib, "ws_handshake_status", lambda **k: 101)
    monkeypatch.setattr(smoke._smoke_lib, "assert_token_gate", lambda *a, connect: connect())
    monkeypatch.setattr(smoke, "install", lambda e, what: "")
    monkeypatch.setattr(smoke, "read_plist_field", lambda label, f: str(env.home.resolve()))
    monkeypatch.setattr(smoke.Harness, "wait_for", lambda self, what, pred, **k: True)
    monkeypatch.setattr(
        "stt_server.auth.load_token_file", lambda p, uid=None: (str(env.home.resolve()), "t")
    )
    call = {
        "verified_status": lambda: h.verified_status(("tcp", smoke.HOST, str(h.port))),
        "check_token": h.check_token,
        "helper": h.running_identity,
    }[caller]
    suffix = "; no token sent" if caller == "check_token" else ""
    try:
        if why is None:
            got = call()
            assert connects if caller != "helper" else got == ("777", "3")
            return
        with pytest.raises(smoke.CheckFailed) as exc:
            call()
        assert str(exc.value) == (
            f"the spare job is not running with a readable pid and runs ({why}){suffix}"
        )
        assert connects == []
    finally:
        if h.token_dir is not None:
            shutil.rmtree(h.token_dir, ignore_errors=True)


# The renderer sys.exit()s when stt_server does not import; that must reach the
# start state as a FAIL row carrying the renderer's own message (and the
# failure-channel line smoke.py reads), never a bare exit 1. stt_server
# importable? x where the renderer is first imported -> outcome.
def _break_stt_server(mp):
    mp.setitem(sys.modules, "stt_server", None)  # `import stt_server.*` -> ImportError
    for name in ("stt_server.auth", "stt_server.env", "render_stt_plist"):
        mp.delitem(sys.modules, name, raising=False)


@pytest.mark.parametrize("importable", [True, False])
@pytest.mark.parametrize("where", ["run", "teardown"])
def test_unimportable_renderer_is_a_start_state_fail_row(
    smoke, env, monkeypatch, capsys, importable, where
):
    import argparse

    monkeypatch.setenv("HOME", str(env.home))
    monkeypatch.setenv("PATH", env.path)
    monkeypatch.setattr(smoke._smoke_lib, "agents_home", lambda: env.home)
    if not importable:
        _break_stt_server(monkeypatch)
    if where == "teardown":
        monkeypatch.setattr(smoke, "run_installer", lambda *a, **k: _cp(["bash"], 0))
        monkeypatch.setattr(smoke, "require_launchd_state", lambda *a, **k: (False, None))
        if importable:
            assert smoke.Harness(SPARE, _free_port()).teardown() == []
        else:
            with pytest.raises(smoke.RendererUnavailable, match="cannot import stt_server"):
                smoke.Harness(SPARE, _free_port()).teardown()
        return
    monkeypatch.setattr(smoke, "snapshot_real_agents", lambda *a, **k: {})
    monkeypatch.setattr(smoke, "canonical_ports", lambda *a, **k: set(CANONICAL))
    monkeypatch.setattr(smoke, "is_listening", lambda port, run=None: False)
    monkeypatch.setattr(smoke, "acquire_run_lock", lambda path: os.open(os.devnull, os.O_RDONLY))
    ran = []
    # Past the start state the run would install: stop it there instead.
    monkeypatch.setattr(smoke.subprocess, "run", lambda *a, **k: ran.append(a) or _cp(a, 1))
    monkeypatch.setenv("PIPECAT_STT_LABEL", SPARE)
    monkeypatch.setenv("KODA_STT_LABEL", SPARE)
    rc = smoke._scrubbed_main(argparse.Namespace(label=SPARE, port=_free_port()))
    out = capsys.readouterr().out
    assert rc == 1
    rows = [ln for ln in out.splitlines() if ln.startswith(smoke.FAIL)]
    fails = smoke._smoke_lib.launchd_failures(out)
    if importable:
        # The renderer imported; the run went on to the stt_server -I check.
        assert ran and "render_stt_plist.py" not in out
        assert fails == [
            "start state: stt_server does not import under python -I (editable install?)"
        ]
    else:
        assert ran == []  # refused before the import check, nothing run
        assert rows and "start state" in rows[0]
        assert len(fails) == 1 and fails[0].startswith("start state: cannot read the start state: ")
        assert "render_stt_plist.py does not import" in fails[0]
        assert "cannot import stt_server" in fails[0]  # the renderer's own message


# squat_warns: the CLI's answer -> verdict. Its timeout is squat_check's own
# worst case (cli_bound) plus start-up, never a hand-copied number; a CLI that
# outlasts it, or cannot run, is "cannot tell": CheckFailed, so the collision
# check's row is a FAIL verdict, never a crash row.
_SQUAT_ANSWERS = {
    "warned": (3, "squat-check: warning: port 1 is held by pid 9\n", True),
    "quiet": (0, "", False),
    "cannot-tell": (2, "squat-check: cannot run lsof; skipping port check\n", False),
    "timeout": (subprocess.TimeoutExpired, "", "cannot tell: no verdict within"),
    "no-python": (FileNotFoundError, "", "cannot tell: cannot run it"),
}


@pytest.mark.parametrize("answer", list(_SQUAT_ANSWERS))
def test_squat_warns_answer_state_space(smoke, monkeypatch, answer):
    rc, err, want = _SQUAT_ANSWERS[answer]
    timeouts = []

    def run(cmd, run=None, timeout=smoke.CMD_TIMEOUT, **kw):
        timeouts.append(timeout)
        if rc is subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired(cmd, timeout)
        if rc is FileNotFoundError:
            raise FileNotFoundError("python3")
        return _cp(cmd, rc, "", err)

    monkeypatch.setattr(smoke, "_run", run)
    monkeypatch.setattr(
        smoke.squat_check, "read_print", lambda label, run=None, uid=None: (0, "\tpid = 4242\n")
    )
    h = object.__new__(smoke.Harness)
    h.label, h.port, h.uid = SPARE, 9998, FAKE_UID
    if isinstance(want, str):
        with pytest.raises(smoke.CheckFailed, match=want):
            h.squat_warns()
        rows = smoke.run_checks(
            type("H", (), {"ensure_real_agents_unchanged": lambda s: None,
                           "ensure_port_ours": lambda s: None,
                           "release_holder": lambda s: None})(),
            (("port collision", h.squat_warns),),
            [],
        )  # fmt: skip
        assert rows[0][1] == smoke.FAIL and rows[0][2].startswith("squat_check.py cannot tell")
    else:
        assert h.squat_warns() is want
    assert timeouts and set(timeouts) == {smoke.squat_cli_timeout()}


# The squat timeout and the budget both derive from squat_check.cli_bound:
# the timeout covers the CLI's worst case for the listeners the collision
# check can see, and every second of it is in run_budget (so smoke.py's
# SIGTERM can never land inside a squat check the budget did not count).
def test_squat_timeout_and_budget_follow_cli_bound(smoke, monkeypatch):
    assert smoke.SQUAT_LISTENERS >= 1  # the harness's own holder socket
    assert smoke.squat_cli_timeout() == (
        smoke.squat_check.cli_bound(smoke.SQUAT_LISTENERS) + smoke.SQUAT_START
    )
    assert smoke._squat_warns_bound() == (
        smoke.squat_check.LAUNCHCTL_TIMEOUT + smoke.squat_cli_timeout()
    )
    before = {n: smoke.run_budget(n) for n in (0, 3)}
    # A slower CLI (one more listener's lookup) moves the timeout and the
    # budget by exactly that much, and the teardown budget not at all.
    tear = smoke.teardown_budget(0)
    monkeypatch.setattr(smoke, "SQUAT_LISTENERS", smoke.SQUAT_LISTENERS + 1)
    step = smoke.squat_check.LSOF_TIMEOUT
    for n, b in before.items():
        assert smoke.run_budget(n) - b == pytest.approx(step)
    assert smoke.teardown_budget(0) == tear


# No signal escapes the teardown path (finding 1). A real SIGINT/SIGTERM sent to
# this process at each boundary: inside a check; after run_checks returned but
# before the switch to "ignore further signals" (begin_teardown's first call);
# inside the except body before its switch (a check's SystemExit led there);
# a second signal in the except body after the first one; twice during
# teardown -> teardown ran exactly once, its verdict and the re-snapshot row
# printed, the old handlers are back.
_BOUNDARIES = ("in-check", "before-switch", "except-body", "second-in-except", "teardown")


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("boundary", _BOUNDARIES)
def test_signal_at_every_boundary_still_tears_down(smoke, monkeypatch, capsys, boundary, sig):
    def kill():
        os.kill(os.getpid(), sig)
        time.sleep(0.05)  # the handler runs here at the latest

    tore = []

    class H:
        def ensure_real_agents_unchanged(self):
            pass

        def ensure_port_ours(self):
            pass

        def release_holder(self):
            pass

        def teardown(self):
            tore.append(1)
            if boundary == "teardown":
                kill()
                kill()
            return []

    def check():
        if boundary in ("in-check", "second-in-except"):
            kill()
        if boundary == "except-body":
            raise SystemExit("a check exited")
        return "ok"

    real_begin, calls = smoke.begin_teardown, []

    def begin(state, old):
        calls.append(state["tearing_down"])
        if len(calls) == 1 and boundary in ("before-switch", "except-body", "second-in-except"):
            kill()
        real_begin(state, old)

    monkeypatch.setattr(smoke, "begin_teardown", begin)
    monkeypatch.setattr(smoke, "snapshot_real_agents", lambda *a, **k: {})
    old = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    results: list = []
    rc = smoke.checks_then_teardown(H(), (("port collision", check),), results, {}, Path("/x"))
    out = capsys.readouterr().out
    assert tore == [1]
    assert [r[0] for r in results][-2:] == ["teardown", "real agents unchanged"]
    assert any(ln.startswith("PASS") and "teardown" in ln for ln in out.splitlines()), out
    assert "real agents unchanged" in out
    interrupted = [r for r in results if r[0] == "interrupted"]
    if boundary == "teardown":
        assert rc == 0 and interrupted == []  # the checks had finished
    else:
        assert rc == 1 and len(interrupted) == 1
    assert {s: signal.getsignal(s) for s in old} == old


# ONE `launchctl print` reader (squat_check.read_print + parse_print) behind
# every wrapper: squat_check.launchd_state (and the CLI's classify_print),
# _smoke_lib.launchd_identity (smoke.py --agents) and smoke_launchd.read_job
# (the snapshot, the verified connections). One table of answers -> every
# wrapper's projection of the same JobPrint, so they cannot diverge again. A
# running job with no `runs =` line is UNKNOWN in every wrapper that reports
# runs; a call that cannot run, or an answer that is neither loaded nor
# launchctl's own "could not find service", is "cannot tell" everywhere.
_ARGS_TCP = (
    "\targuments = {\n\t\t/py\n\t\tserve\n\t\t--host\n\t\t127.0.0.1\n\t\t--port\n\t\t9123\n\t}\n"
)
_PRINTS = {
    # name: (rc or an exception class, stdout, stderr, want JobPrint or None)
    "running": (0, f"\tpid = 4242\n\truns = 7\n{_ARGS_TCP}", "", (True, "4242", "7", "tcp")),
    "running-no-runs": (0, f"\tpid = 4242\n{_ARGS_TCP}", "", (True, "4242", UNKNOWN, "tcp")),
    "loaded-no-pid": (0, "\tstate = waiting\n\truns = 3\n", "", (True, None, "3", None)),
    "loaded-no-pid-no-runs": (0, "\tstate = waiting\n", "", (True, None, None, None)),
    "loaded-empty": (0, "", "", (True, None, None, None)),
    "odd-format": (
        0,
        "gui/501/x = {\r\n    runs = 12\r\n        pid = 99\r\n" + _ARGS_TCP.replace("\n", "\r\n"),
        "",
        (True, "99", "12", "tcp"),
    ),
    "socket-job": (
        0,
        "\tpid = 5\n\truns = 1\n\targuments = {\n\t\t/py\n\t\t--socket-path\n\t\t/s.sock\n\t}\n",
        "",
        (True, "5", "1", "unix"),
    ),
    "not-loaded-113": (113, "", 'Could not find service "x" in domain', (False, None, None, None)),
    "not-loaded-message": (1, "", 'Could not find service "x"', (False, None, None, None)),
    "exit-5": (5, "", "Input/output error", None),
    "exit-1-silent": (1, "", "", None),
    "exit-1-other": (1, "\tpid = 4242\n\truns = 7\n", "Operation not permitted", None),
    "no-launchctl": (FileNotFoundError, "", "", None),
    "timeout": (subprocess.TimeoutExpired, "", "", None),
}


@pytest.mark.parametrize("name", list(_PRINTS))
def test_one_launchctl_print_reader_behind_every_wrapper(smoke, name):
    rc, out, err, want = _PRINTS[name]
    calls = []

    def run(argv, **kw):
        calls.append((list(argv), kw.get("timeout")))
        if rc is FileNotFoundError:
            raise FileNotFoundError("launchctl")
        if rc is subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
        return _cp(argv, rc, out, err)

    sq, lib = smoke.squat_check, smoke._smoke_lib
    state = sq.launchd_state("x", run, uid=501)
    ident = lib.launchd_identity("x", uid=501, run=run)
    if want is None:
        assert state is None and ident is None
        with pytest.raises(smoke.LaunchdUnreadable):
            smoke.read_job("x", "501", run)
        if isinstance(rc, int):
            assert sq.classify_print(rc, out + "\n" + err) is None
    else:
        loaded, pid, runs, kind = want
        assert state == (loaded, pid)
        assert ident == (loaded, pid, runs)
        job = smoke.read_job("x", "501", run)
        assert job[:3] == (loaded, pid, runs)
        assert (job[3][0] if job[3] else None) == kind
        assert sq.classify_print(rc, out + "\n" + err) == (loaded, pid)
    # Every wrapper made the same one call, with the one reader's bound.
    assert len(calls) == 3
    assert {(tuple(a), t) for a, t in calls} == {
        (("launchctl", "print", "gui/501/x"), sq.LAUNCHCTL_TIMEOUT)
    }


# No wrapper keeps a parser of its own: the pid, runs and arguments rules
# live only in squat_check (one regex each).
def test_no_second_launchctl_print_parser(smoke):
    import re

    scripts = REPO_ROOT / "scripts"
    for name, pattern in (
        ("pid", r"pid = \("),
        ("runs", r"runs = \("),
        ("arguments", r"arguments = \\\{"),
    ):
        hits = [p.name for p in sorted(scripts.glob("*.py")) if re.search(pattern, p.read_text())]
        assert hits == ["squat_check.py"], (name, hits)
    for p in ("_smoke_lib.py", "smoke_launchd.py", "smoke.py"):
        src = (scripts / p).read_text()
        assert '["launchctl", "print"' not in src, p  # an argv: a call of its own


# --------------------------------------------------------------------------
# the live checks' happy paths end to end under stubs (Cause C)
# --------------------------------------------------------------------------
#
# check_collision and check_rollback run in-process with the REAL read_job
# (squat_check's reader over a stub `launchctl` on PATH), the REAL
# connect_verified (a stub `lsof` names the server end of each TCP connection;
# LOCAL_PEERPID is the real one for a socket), the REAL ws_handshake_status
# against a fake agent served by this process over a real socket, and, in the
# collision check, the REAL squat_check.py CLI. Only the installer is replaced
# (by a function that does what the installer's install leaves behind). The
# agent records every byte it receives, so each refusal cell can prove nothing
# was sent on the refused connection.


class _FakeAgent:
    """A websocket upgrade endpoint served by this process: answers 101 to any
    request (or, with TOKEN, 401 unless it carries ``Bearer TOKEN``) and
    records every request's bytes. TCP binds are retried until the port is
    free (the collision check's holder releases it)."""

    def __init__(self, kind: str, where, token: str | None = None, on_bind=None):
        self.requests: list[bytes] = []
        self.accepted = 0
        self.token = token
        self.on_bind = on_bind
        self.stop = threading.Event()
        self.srv = None
        self.kind, self.where = kind, where
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _bind(self):
        while not self.stop.is_set():
            s = socket.socket(socket.AF_INET if self.kind == "tcp" else socket.AF_UNIX)
            if self.kind == "tcp":
                # Earlier agents' closed connections may linger in TIME_WAIT;
                # a live listener (the collision check's holder) still
                # refuses the bind.
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", self.where) if self.kind == "tcp" else str(self.where))
                s.listen(8)
                s.settimeout(0.05)
                return s
            except OSError:
                s.close()
                time.sleep(0.05)
        return None

    def _run(self):
        self.srv = self._bind()
        if self.srv is None:
            return
        if self.on_bind:
            self.on_bind()
        while not self.stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except OSError:
                continue
            self.accepted += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        with conn:
            conn.settimeout(5)
            buf = b""
            try:
                while b"\r\n\r\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
            except OSError:
                pass
            if not buf:
                return
            self.requests.append(buf)
            ok = self.token is None or f"Bearer {self.token}".encode() in buf
            with contextlib.suppress(OSError):
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n\r\n" if ok else
                             b"HTTP/1.1 401 Unauthorized\r\n\r\n")  # fmt: skip

    def close(self):
        self.stop.set()
        self.t.join(3)
        if self.srv is not None:
            self.srv.close()
        if self.kind == "unix":
            with contextlib.suppress(OSError):
                os.unlink(self.where)


class _LiveStubs:
    """A stub ``launchctl`` and ``lsof`` on PATH, driven by files in STATE:

    * ``job``: "PID RUNS" of the loaded spare job (absent: not loaded);
      ``flap`` present: RUNS moves on every print (a restart loop);
      ``print-fails`` present: every print exits 5 (unreadable).
    * ``owner``: the pid lsof names as holding the server end of any TCP
      connection to ``port``; ``holder``: the pid lsof names as LISTENing on
      ``port`` (the collision check's own holder socket)."""

    def __init__(self, tmp: Path, port: int):
        self.state = tmp / "state"
        self.state.mkdir()
        self.stub = tmp / "livebin"
        self.stub.mkdir()
        self.log = tmp / "live.log"
        self.write("port", str(port))
        st = self.state
        _exe(
            self.stub / "launchctl",
            f"""#!/bin/bash
printf 'launchctl %s\\n' "$*" >> {self.log}
if [[ "$1" == print ]]; then
    [[ -e {st}/print-fails ]] && {{ echo "launchctl: boom" >&2; exit 5; }}
    [[ -s {st}/job ]] || {{ echo 'Could not find service "x" in domain' >&2; exit 113; }}
    read -r pid runs < {st}/job
    if [[ -e {st}/flap ]]; then
        n=$(( $(cat {st}/flap 2>/dev/null || echo 0) + 1 )); echo $n > {st}/flap; runs=$((runs + n))
    fi
    printf 'gui/x = {{\\n\\tstate = running\\n\\tpid = %s\\n\\truns = %s\\n}}\\n' "$pid" "$runs"
    exit 0
fi
exit 0
""",
        )
        _exe(
            self.stub / "lsof",
            f"""#!/bin/bash
printf 'lsof %s\\n' "$*" >> {self.log}
port=$(cat {st}/port)
for a in "$@"; do
    case "$a" in
    -sTCP:LISTEN)
        [[ -s {st}/holder ]] || exit 1
        printf 'p%s\\nn127.0.0.1:%s\\n' "$(cat {st}/holder)" "$port"; exit 0;;
    -Fc) printf 'p1\\ncpython\\n'; exit 0;;
    -iTCP@*)
        printf 'p%s\\nn127.0.0.1:%s->%s\\n' "$(cat {st}/owner)" "$port" "${{a#-iTCP@}}"; exit 0;;
    esac
done
exit 1
""",
        )

    def write(self, name: str, value: str) -> None:
        (self.state / name).write_text(value)

    def remove(self, name: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            (self.state / name).unlink()

    @property
    def path(self) -> str:
        return f"{self.stub}:/usr/bin:/bin:/usr/sbin:/sbin"


@pytest.fixture
def short_home(monkeypatch):
    """A HOME short enough for an AF_UNIX path under it (104 bytes on macOS)."""
    import tempfile

    home = Path(tempfile.mkdtemp(prefix="sl.", dir="/tmp")).resolve()
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PIPECAT_STT_LOG_DIR", raising=False)
    monkeypatch.delenv("KODA_STT_LOG_DIR", raising=False)
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _fast_waits(smoke, monkeypatch, timeout=3.0):
    real = smoke.Harness.wait_for

    def wait_for(self, what, pred, timeout_=None, every=None, **k):
        return real(self, what, pred, timeout=timeout, every=0.1)

    monkeypatch.setattr(smoke.Harness, "wait_for", wait_for)


def _write_spare_plist(home: Path, port: int | None = None, sock: Path | None = None):
    import plistlib

    serve = ["--host", "127.0.0.1", "--port", str(port)] if sock is None else [
        "--socket-path", str(sock)]  # fmt: skip
    (home / "Library" / "LaunchAgents" / f"{SPARE}.plist").write_bytes(
        plistlib.dumps(
            {
                "Label": SPARE,
                "ProgramArguments": [
                    sys.executable,
                    "-m",
                    "stt_server",
                    "serve",
                    "--backend",
                    "echo",
                    *serve,
                ],
            }  # fmt: skip
        )
    )


_SECRET_MARK = b"Authorization"

# the collision check, end to end: what the spare job looks like once the port
# is freed (its server end x its launchd identity) -> PASS, or the refusal and
# nothing sent on the refused connection.
_COLLISION = {
    "ok": None,
    "foreign-listener": "not the spare job",
    "pid-runs-change": "not the spare job",
    "identity-unreadable": "unreadable",
}


@pytest.mark.parametrize("cell", list(_COLLISION))
def test_collision_check_end_to_end(smoke, monkeypatch, tmp_path, short_home, cell):
    port = _free_port()
    stubs = _LiveStubs(tmp_path, port)
    monkeypatch.setenv("PATH", stubs.path)
    _fast_waits(smoke, monkeypatch)
    stubs.write("holder", str(os.getpid()))  # the harness's own holder socket
    stubs.write("owner", "777" if cell == "foreign-listener" else "4242")
    agents = []

    def on_bind():
        stubs.remove("holder")  # the holder is gone: the agent listens now
        if cell == "identity-unreadable":
            stubs.write("print-fails", "1")
        if cell == "pid-runs-change":
            stubs.write("flap", "0")

    def install(env, what):
        assert what == "collision" and env["PIPECAT_STT_PORT"] == str(port)
        assert "PIPECAT_STT_AUTH_TOKEN_FILE" not in env
        log = smoke._err_log(SPARE)  # no plist: the installer's naming rule
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as fh:
            fh.write("OSError: [Errno 48] address already in use\n" * 2)
        stubs.write("job", "4242 3")
        agents.append(_FakeAgent("tcp", port, on_bind=on_bind))
        return ""

    monkeypatch.setattr(smoke, "install", install)
    h = smoke.Harness(SPARE, port)
    try:
        want = _COLLISION[cell]
        if want is None:
            detail = h.check_collision()
            assert "squat warning" in detail and "server end verified" in detail
            assert len(agents[0].requests) >= 1
        elif want == "unreadable":
            with pytest.raises(smoke.LaunchdUnreadable):
                h.check_collision()
        else:
            with pytest.raises(smoke.CheckFailed, match=want):
                h.check_collision()
            assert agents[0].requests == []  # refused: nothing written
        assert h.holder is None
        # The squat CLI warned: it saw the holder (not pid 4242) on the port.
        log = stubs.log.read_text()
        assert "-sTCP:LISTEN" in log
        for req in agents[0].requests:
            assert _SECRET_MARK not in req
    finally:
        h.release_holder()
        for a in agents:
            a.close()


# the rollback body, end to end: TCP -> socket -> TCP, each transport's answer
# over a verified connection. A fault (the server end not the job's, a
# restart loop, an unreadable launchd state) injected at install k -> the check
# stops there with a refusal, no later install runs, nothing is sent on the
# refused connection; no fault -> PASS, old socket gone, .lock left.
_ROLLBACK_FAULTS = ["foreign-listener", "pid-runs-change", "identity-unreadable"]
_ROLLBACK_CELLS = [("none", None)] + [(f, k) for f in _ROLLBACK_FAULTS for k in (0, 1, 2)]


@pytest.mark.skipif(sys.platform != "darwin", reason="LOCAL_PEERPID is macOS")
@pytest.mark.parametrize(("fault", "at"), _ROLLBACK_CELLS)
def test_rollback_check_end_to_end(smoke, monkeypatch, tmp_path, short_home, fault, at):
    port = _free_port()
    stubs = _LiveStubs(tmp_path, port)
    monkeypatch.setenv("PATH", stubs.path)
    _fast_waits(smoke, monkeypatch)
    me = str(os.getpid())  # a socket's LOCAL_PEERPID is this process: the "job"
    stubs.write("owner", me)
    h = smoke.Harness(SPARE, port)
    sock = h.sock
    agents: list[_FakeAgent] = []
    installs: list[str] = []

    def install(env, what):
        k = len(installs)
        installs.append(what)
        had_socket = os.path.lexists(sock)  # the old socket agent's, before it stops
        for a in agents:
            a.close()
        stubs.write("job", f"{me} {k + 1}")
        if fault != "none" and k == at:
            if fault == "foreign-listener":
                stubs.write("job", f"4242 {k + 1}")
            elif fault == "pid-runs-change":
                stubs.write("flap", "0")
            else:
                stubs.write("print-fails", "1")
        out = ""
        if env.get("PIPECAT_STT_SOCKET"):
            assert env["PIPECAT_STT_SOCKET"] == str(sock)
            _write_spare_plist(short_home, sock=sock)
            Path(f"{sock}.lock").write_text("")
            agents.append(_FakeAgent("unix", sock))
        else:
            if had_socket:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(sock)
                out = f"note: endpoint changed from socket {sock} to TCP; {smoke.OLD_SOCKET_NOTE}"
            _write_spare_plist(short_home, port=port)
            agents.append(_FakeAgent("tcp", port))
        return out

    monkeypatch.setattr(smoke, "install", install)
    seen = []
    try:
        if fault == "none":
            detail = h.check_rollback()
            assert "tcp -> socket -> tcp" in detail
            assert installs == ["rollback: TCP", "rollback: socket", "rollback: back to TCP"]
            assert not os.path.lexists(sock) and os.path.exists(f"{sock}.lock")
            assert all(len(a.requests) >= 1 for a in agents)
        else:
            exc = smoke.LaunchdUnreadable if fault == "identity-unreadable" else smoke.CheckFailed
            with pytest.raises(exc):
                h.check_rollback()
            assert len(installs) == at + 1  # stopped at the faulty transport
            assert agents[-1].requests == []  # refused: nothing written
        seen = [r for a in agents for r in a.requests]
        assert all(_SECRET_MARK not in r for r in seen)
    finally:
        for a in agents:
            a.close()
        with contextlib.suppress(OSError):
            os.unlink(f"{sock}.lock")
