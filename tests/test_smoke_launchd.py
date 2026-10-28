"""Stubbed tests for ``scripts/smoke_launchd.py`` (plan Phase 2).

Nothing here touches real launchd. Every subprocess runs with ``PATH`` led by a
stub dir (``launchctl``, ``id``, ``lsof``) and a temp ``HOME``; the fixture
asserts the stub ``launchctl`` is the one resolved first. The real installer
runs against the stubs, as in ``tests/test_install_migration.py``.

The subprocess runs also set the test-only ``PIPECAT_STT_SMOKE_TEST_AGENTS_HOME``
to the temp home, so the real-agent plist scan never reads the developer's
``~/Library/LaunchAgents`` (``HOME`` alone does not move it), and use a free
``--port``; ``test_stubbed_run_reads_no_real_library_path`` audits that.

Assumptions (contract is silent): the harness honours ``PIPECAT_STT_SOCKET``
from its environment for the spare socket path when set; the spare plist lives
at ``$HOME/Library/LaunchAgents/<label>.plist`` and logs under
``$HOME/Library/Logs/pipecat-stt`` with ``pipecat-stt-server-smoke`` in the name.
"""

from __future__ import annotations

import http.server
import importlib.util
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

FAKE_UID = "501"
SPARE = "pipecat.stt-server.smoke"
AGENTS_HOME_ENV = "PIPECAT_STT_SMOKE_TEST_AGENTS_HOME"
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
        # Not loaded is launchctl's own answer: exit 113, "Could not find service".
        _exe(
            self.stub / "launchctl",
            f"""#!/usr/bin/env bash
printf 'launchctl %s\\n' "$*" >> {self.log!s}
if [[ "$1" == "print" ]]; then
    if [[ -e {self.print_fails!s} ]]; then echo "launchctl: boom" >&2; exit 5; fi
    if [[ -e {self.loaded_marker!s} && "$2" == *"{SPARE}" ]]; then exit 0; fi
    echo "Could not find service \\"${{2##*/}}\\" in domain for user gui: {FAKE_UID}" >&2
    exit 113
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
        e = {"HOME": str(self.home), "PATH": self.path, AGENTS_HOME_ENV: str(self.home)}
        e.update(extra)
        e.pop("CI", None)
        return e

    def spare_env(self, **extra: str) -> dict[str, str]:
        return self.env(**{"PIPECAT_STT_LABEL": SPARE, "KODA_STT_LABEL": SPARE, **extra})

    def argv(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def run(self, args: list[str], env: dict[str, str], timeout: int = 180, audit=None):
        assert env["PATH"].split(":")[0] == str(self.stub)
        assert "CI" not in env
        pre = [str(AUDIT_WRAPPER), str(audit)] if audit is not None else []
        return subprocess.run(
            [str(VENV_PYTHON), *pre, str(HARNESS), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=str(REPO_ROOT),
        )

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
        [str(VENV_PYTHON), str(HARNESS)],
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


def test_compare_snapshots_identical(smoke):
    s = {"pipecat.stt-server": ("100", "100"), "pipecat.stt-server.parakeet": (None, None)}
    assert smoke.compare_snapshots(s, dict(s)) == []


def test_compare_snapshots_changed_pid(smoke):
    before = {"pipecat.stt-server": ("100", "100")}
    assert smoke.compare_snapshots(before, {"pipecat.stt-server": ("200", "100")})


def test_compare_snapshots_changed_listener(smoke):
    before = {"pipecat.stt-server": ("100", "100")}
    assert smoke.compare_snapshots(before, {"pipecat.stt-server": ("100", "999")})
    assert smoke.compare_snapshots(before, {"pipecat.stt-server": ("100", None)})


def test_compare_snapshots_appear_disappear(smoke):
    a = {"pipecat.stt-server": ("100", "100")}
    b = {**a, "pipecat.stt-server.parakeet": ("5", "5")}
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
    ports = smoke.agent_ports(home=env.home)
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


def test_agent_ports_unreadable_plist_refuses(smoke, env):
    (env.agents / "pipecat.stt-server.broken.plist").write_text("not a plist")
    with pytest.raises(smoke.GuardRefused):
        smoke.agent_ports(home=env.home)


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


# Every installer warning that means the old socket (or job) was left behind;
# install() must FAIL on each, which is what makes the rollback's "removed on
# the unload-confirmed path" claim hold.
@pytest.mark.parametrize(
    "line",
    [
        "warning: pipecat.stt-server.smoke still loaded 30s after bootout",
        "warning: old job may still hold /h/spare.sock.lock; leaving /h/spare.sock",
        "warning: /h/spare.sock.lock is held by a running server; leaving /h/spare.sock",
        "warning: /h/spare.sock is not a socket; leaving it",
        "warning: /h/spare.sock not owned by current user, leaving it",
        "warning: could not remove /h/spare.sock",
        "warning: could not tell whether /h/spare.sock.lock is held; leaving /h/spare.sock",
        "warning: installed socket path '/h/spare.sock' is not absolute; leaving it",
    ],
)
def test_install_fails_on_every_leave_behind_warning(smoke, line):
    assert smoke._WARN_RE.search(line)


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
    assert not env.spare_residue()
    # Never touches any other label.
    for ln in env.argv():
        if "bootout" in ln or "bootstrap" in ln:
            assert SPARE in ln or "stt-server" not in ln, ln
            assert "koda.stt-server" not in ln


def test_inject_fail_still_tears_down(env):
    r = env.run(["--inject", "fail", "--port", str(_free_port())], env.spare_env())
    assert r.returncode == 1, (r.stdout, r.stderr)
    _assert_torn_down(env)


def test_stale_agents_home_override_is_refused_before_anything_runs(env, tmp_path):
    # Exported outside a stubbed run (launchctl is not a stub; here there is no
    # launchctl at all, so nothing real can be reached), the test-only override
    # would hide installed agents from the guard: the harness refuses up front.
    empty = tmp_path / "nopath"
    empty.mkdir()
    e = {
        "HOME": str(env.home),
        "PATH": str(empty),
        AGENTS_HOME_ENV: str(env.home),
        "PIPECAT_STT_LABEL": SPARE,
        "KODA_STT_LABEL": SPARE,
    }
    r = subprocess.run(
        [str(VENV_PYTHON), str(HARNESS), "--port", str(_free_port())],
        capture_output=True,
        text=True,
        timeout=60,
        env=e,
        cwd=str(REPO_ROOT),
    )
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "FAIL" in r.stdout and "guard" in r.stdout
    assert AGENTS_HOME_ENV in r.stdout and "ignored" in r.stdout
    assert env.argv() == []  # not even id or lsof ran


def _endpointless_plist(path: Path) -> None:
    import plistlib

    with open(path, "wb") as fh:
        plistlib.dump(
            {
                "Label": path.stem,
                "ProgramArguments": [sys.executable, "-m", "stt_server", "serve"],
            },
            fh,
        )


def test_unreadable_baseline_snapshot_refuses_before_any_install(env):
    # A real agent whose listener cannot be read is UNKNOWN in the baseline, so
    # "real agents unchanged" could never PASS: refuse before the first install.
    _endpointless_plist(env.agents / "pipecat.stt-server.custom.plist")
    r = env.run(["--inject", "fail", "--port", str(_free_port())], env.spare_env())
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "snapshot" in r.stdout and "pipecat.stt-server.custom" in r.stdout
    assert _mutating(env.argv()) == []
    assert "port collision" not in r.stdout
    assert not env.spare_residue()


@pytest.mark.parametrize(
    ("snap", "unknown"),
    [
        ({}, []),
        ({"a": ("1", "1")}, []),
        ({"a": (None, None)}, []),
        ({"a": ("1", "unknown")}, ["a"]),
        ({"a": (None, "unknown"), "b": ("2", "2")}, ["a"]),
        ({"b": ("2", "unknown"), "a": ("1", "unknown")}, ["a", "b"]),
    ],
)
def test_snapshot_unknowns_state_space(smoke, snap, unknown):
    assert smoke.snapshot_unknowns(snap) == unknown


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
        r = env.run(["--inject", "fail", "--port", str(_free_port())], env.spare_env())
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

    r = env.run(["--inject", "fail", "--port", str(_free_port())], env.spare_env())
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
    r = env.run(["--inject", "fail", "--port", str(_free_port())], env.spare_env(), audit=audit)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "test-only override" in r.stderr
    assert audit.exists(), "the audit hook never ran"
    lines = audit.read_text().splitlines()
    assert lines[0] == "audit: on"
    assert lines[1:] == []


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_signal_during_hang_still_tears_down(env, sig):
    e = env.spare_env()
    p = subprocess.Popen(
        [str(VENV_PYTHON), str(HARNESS), "--inject", "hang", "--port", str(_free_port())],
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
    r = env.run(["--inject", "crash", "--port", str(_free_port())], env.spare_env())
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

    def fake_installer(sub, e, run=None, *, new_session=False):
        seen.append((sub, new_session))
        return _cp(["bash"], 0)

    monkeypatch.setattr(smoke, "run_installer", fake_installer)
    h = smoke.Harness(SPARE, _free_port(), None)
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


def _fake_launchd(listed="", prints=None, listeners=(), list_rc=0):
    prints = prints or {}

    def run(argv, **_kw):
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


def test_snapshot_includes_a_loaded_agent_with_no_plist(smoke, env):
    run = _fake_launchd(_LIST, {_GHOST: _GHOST_PRINT}, listeners=["777"])
    snap = smoke.snapshot_real_agents(run, home=env.home)
    assert snap == {_GHOST: ("777", "777")}  # spare and non-pipecat labels excluded


def test_plistless_agent_port_reaches_the_guard(smoke, env):
    run = _fake_launchd(_LIST, {_GHOST: _GHOST_PRINT})
    plistless = smoke.plistless_agents(FAKE_UID, run, env.home)
    assert plistless[_GHOST]["--port"] == "9123"
    ports = smoke.agent_ports(home=env.home, plistless=plistless)
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
    run = _fake_launchd(_LIST, {_GHOST: _GHOST_PRINT})
    assert smoke.plistless_agents(FAKE_UID, run, env.home) == {}


def test_unreadable_launchctl_list_fails_closed(smoke, env):
    with pytest.raises(RuntimeError):
        smoke.snapshot_real_agents(_fake_launchd(list_rc=5), home=env.home)


def test_unreadable_listener_is_unknown_in_the_snapshot(smoke, env):
    run = _fake_launchd(_LIST, {_GHOST: _GHOST_PRINT}, listeners=None)
    assert smoke.snapshot_real_agents(run, home=env.home) == {_GHOST: ("777", smoke.UNKNOWN)}


# (listener before, listener after) -> unchanged? UNKNOWN on either side never is.
@pytest.mark.parametrize(
    ("b", "a", "same"),
    [
        ("100", "100", True),
        (None, None, True),
        ("100", "200", False),
        ("100", None, False),
        ("unknown", "unknown", False),
        ("unknown", "100", False),
        ("100", "unknown", False),
        (None, "unknown", False),
    ],
)
def test_compare_snapshots_listener_state_space(smoke, b, a, same):
    diffs = smoke.compare_snapshots({"x": ("1", b)}, {"x": ("1", a)})
    assert (diffs == []) is same


# --------------------------------------------------------------------------
# the spare port lost after the guard (H2)
# --------------------------------------------------------------------------


class _FakeHarness:
    def __init__(self, lose_before=None):
        self.lose_before = lose_before
        self.ensured = 0
        self.released = 0

    def ensure_port_ours(self):
        self.ensured += 1
        if self.lose_before == self.ensured:
            raise _SMOKE.PortLost("port 1 is held by pid 9, not the spare job")

    def release_holder(self):
        self.released += 1


_SMOKE = None


def _checks(ran, behaviours):
    def make(name, how):
        def fn():
            ran.append(name)
            if how == "lost":
                raise _SMOKE.PortLost("cannot hold port 1 after the guard")
            if how == "fail":
                raise _SMOKE.CheckFailed("nope")
            if how == "crash":
                raise LookupError("boom")
            return "ok"

        return fn

    return tuple((n, make(n, h)) for n, h in behaviours)


_NAMES = ("port collision", "socket rollback", "token file")


# where the port is lost (inside check i, or before check i) x what the other
# checks do -> rows: the losing check FAILs, later ones are SKIPPED and never run.
@pytest.mark.parametrize("other", ["ok", "fail", "crash"])
@pytest.mark.parametrize(
    "where", [("in", 0), ("in", 1), ("in", 2), ("before", 1), ("before", 2), (None, None)]
)
def test_run_checks_port_loss_state_space(smoke, where, other):
    global _SMOKE
    _SMOKE = smoke
    kind, at = where
    behaviours = [
        (n, "lost" if (kind == "in" and i == at) else other) for i, n in enumerate(_NAMES)
    ]
    h = _FakeHarness(lose_before=at if kind == "before" else None)
    ran: list[str] = []
    rows = smoke.run_checks(h, _checks(ran, behaviours))
    assert [r[0] for r in rows] == list(_NAMES)  # every check has a row
    lost_at = at if kind is not None else None
    for i, (name, status, detail) in enumerate(rows):
        if lost_at is not None and i == lost_at:
            assert status == smoke.FAIL and "port lost" in detail
        elif lost_at is not None and i > lost_at:
            assert status == smoke.SKIPPED and name not in ran
        else:
            assert status == (smoke.PASS if other == "ok" else smoke.FAIL)
    assert h.released == len(rows) - (len(_NAMES) - 1 - lost_at if lost_at is not None else 0)


def test_collision_check_fails_when_the_port_was_taken_after_the_guard(smoke, env, monkeypatch):
    monkeypatch.setenv("HOME", str(env.home))
    monkeypatch.setenv("PATH", env.path)
    installs = []
    monkeypatch.setattr(smoke, "install", lambda *a, **k: installs.append(a) or "")
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen(1)
        port = squatter.getsockname()[1]
        h = smoke.Harness(SPARE, port, None)
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
    h = smoke.Harness(SPARE, _free_port(), None)
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
