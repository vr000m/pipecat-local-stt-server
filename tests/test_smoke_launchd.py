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
        ("plist", "unknown", False),
        ("listener", "unknown", False),
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
    ports = smoke.snapshot_ports(snap, home=env.home)
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
    snap = {"pipecat.stt-server.broken": smoke.AgentState(False, None, None, None, "d", None)}
    with pytest.raises(smoke.GuardRefused):
        smoke.snapshot_ports(snap, home=env.home)


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
        ({"a": ("1", "1", "d")}, []),
        ({"a": (None, None, None)}, []),
        ({"a": ("1", "unknown", "d")}, ["a"]),
        ({"a": (None, None, "unknown")}, ["a"]),
        ({"a": (None, "unknown", "d"), "b": ("2", "2", "d")}, ["a"]),
        ({"b": ("2", "unknown", "d"), "a": ("1", "1", "unknown")}, ["a", "b"]),
    ],
)
def test_snapshot_unknowns_state_space(smoke, snap, unknown):
    states = {
        k: smoke.AgentState(pid is not None, pid, None, None, plist, listener)
        for k, (pid, listener, plist) in snap.items()
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
        with pytest.raises(AssertionError):
            lib.assert_token_gate("127.0.0.1", srv.server_address[1], str(tok))
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
    ports = smoke.snapshot_ports({_LABEL: st}, home=env.home)
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
        ("tcp", False, None, "unknown", 1),
        ("tcp", False, [], None, 1),
        ("unix", True, None, "unknown", 1),
        ("unix", True, [], None, 1),
        ("unix", False, ["777"], None, 0),
        ("none", False, ["777"], "unknown", 0),
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
    assert (smoke.snapshot_unknowns(snap) == [_LABEL]) == (want == "unknown")


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
    ports = smoke.snapshot_ports(smoke.snapshot_real_agents(run, home=env.home), home=env.home)
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
