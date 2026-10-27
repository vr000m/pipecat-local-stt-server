"""Tests for ``scripts/squat_check.py`` (port-squat detection, issue #17).

Unit tests inject ``run`` (a fake ``subprocess.run`` that answers ``lsof`` /
``launchctl`` / ``id``) and a ``sleep`` that advances a scripted timeline, so the
1-second re-check is deterministic. A few CLI tests stub ``lsof`` on a temp
``PATH`` to pin the stream and exit-code contract (stdout always empty, warnings
on stderr prefixed ``squat-check: warning:``; exit 0 quiet, 3 warned, 2 cannot
tell).
"""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "squat_check.py"
PREFIX = "squat-check: warning:"
HOST = "127.0.0.1"
PORT = 9565


def _load():
    spec = importlib.util.spec_from_file_location("squat_check_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


squat_check = _load()


class World:
    """A scripted timeline of lsof listeners and launchd pids.

    ``snapshots`` is a list of listener lists, each row ``(pid, command, addr)``
    with ``addr`` like ``127.0.0.1`` or ``*``; the snapshot index advances each
    time the code under test calls ``sleep``. ``launchd_pids`` follows the same
    timeline for a ``launchctl print`` (None: no ``pid =`` line).
    """

    def __init__(
        self,
        snapshots,
        *,
        launchd_pids=(None,),
        launchd_loaded=(True,),
        launchctl_rc=None,
        lsof_rc=0,
        lsof_missing=False,
    ):
        self.snapshots = snapshots
        self.launchd_pids = launchd_pids
        self.launchd_loaded = launchd_loaded
        self.launchctl_rc = launchctl_rc
        self.lsof_rc = lsof_rc
        self.lsof_missing = lsof_missing
        self.step = 0
        self.calls: list[list[str]] = []

    def _at(self, seq):
        return seq[min(self.step, len(seq) - 1)]

    def sleep(self, _secs):
        self.step += 1

    def run(self, argv, *args, **kwargs):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        tool = os.path.basename(argv[0])
        if tool == "lsof":
            if self.lsof_missing:
                raise FileNotFoundError("lsof")
            if self.lsof_rc:
                return subprocess.CompletedProcess(argv, self.lsof_rc, "", "lsof: boom")
            fields = "".join(a[2:] for a in argv if a.startswith("-F"))
            out = []
            for pid, cmd, addr in self._at(self.snapshots):
                out.append(f"p{pid}")
                if "c" in fields:
                    out.append(f"c{cmd}")
                if "n" in fields:
                    out.append(f"n{addr}:{PORT}")
            return subprocess.CompletedProcess(argv, 0, "\n".join(out) + ("\n" if out else ""), "")
        if tool == "launchctl":
            if self.launchctl_rc is not None:
                return subprocess.CompletedProcess(argv, self.launchctl_rc, "", "launchctl: boom")
            if not self._at(self.launchd_loaded):
                msg = f'Could not find service "{argv[2].rsplit("/", 1)[-1]}" in domain\n'
                return subprocess.CompletedProcess(argv, 113, "", msg)
            pid = self._at(self.launchd_pids)
            body = "state = running\n" + (f"\tpid = {pid}\n" if pid is not None else "")
            return subprocess.CompletedProcess(argv, 0, body, "")
        if tool == "id":
            return subprocess.CompletedProcess(argv, 0, "501\n", "")
        raise AssertionError(f"unexpected command {argv!r}")

    def lsof_calls(self):
        return [c for c in self.calls if os.path.basename(c[0]) == "lsof"]


def _check(world, *, pid, loaded=1, label=None, host=HOST):
    return squat_check.check(host, PORT, pid, loaded, label=label, run=world.run, sleep=world.sleep)


def test_match_is_quiet(capsys):
    w = World([[(100, "python", HOST)]])
    assert _check(w, pid=100) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    assert w.lsof_calls(), "lsof must have been consulted"


def test_mismatch_warns_with_port_pids_and_command(capsys):
    w = World([[(200, "evilproxy", HOST)]])
    assert _check(w, pid=100) == 3
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.strip().splitlines()
    assert all(ln.startswith(PREFIX) for ln in out.err.strip().splitlines())
    assert str(PORT) in out.err and "200" in out.err and "100" in out.err
    assert "evilproxy" in out.err


def test_listener_absent_is_quiet(capsys):
    w = World([[]])
    assert _check(w, pid=100) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_loaded_without_pid_and_listener_warns_not_running(capsys):
    w = World([[(321, "nc", HOST)]])
    assert _check(w, pid=None) == 3
    err = capsys.readouterr().err
    assert err.startswith(PREFIX)
    assert f"port {PORT} held by pid 321" in err
    assert "not running" in err


def test_loaded_with_empty_string_pid_is_no_pid(capsys):
    w = World([[(321, "nc", HOST)]])
    assert _check(w, pid="") == 3
    assert "not running" in capsys.readouterr().err


def test_loaded_without_pid_and_no_listener_is_quiet(capsys):
    w = World([[]])
    assert _check(w, pid=None) == 0
    assert capsys.readouterr().err == ""


def test_job_not_loaded_is_quiet_even_with_a_listener(capsys):
    w = World([[(321, "nc", HOST)]])
    assert _check(w, pid=None, loaded=0) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_two_listeners_one_matching_still_warns(capsys):
    w = World([[(100, "python", HOST), (200, "nc", HOST)]])
    assert _check(w, pid=100) == 3
    err = capsys.readouterr().err
    assert "200" in err and PREFIX in err


@pytest.mark.parametrize("wildcard", ["*", "[::]"])
def test_wildcard_listener_with_different_pid_warns(capsys, wildcard):
    w = World([[(200, "nc", wildcard)]])
    assert _check(w, pid=100) == 3
    assert "200" in capsys.readouterr().err


def test_wildcard_listener_with_same_pid_is_quiet(capsys):
    w = World([[(100, "python", "*")]])
    assert _check(w, pid=100) == 0
    assert capsys.readouterr().err == ""


def test_listener_bound_to_another_host_is_ignored(capsys):
    w = World([[(200, "nc", "10.9.9.9")]])
    assert _check(w, pid=100) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_other_host_listener_does_not_mask_a_real_squatter(capsys):
    w = World([[(300, "nc", "10.9.9.9"), (200, "nc", HOST)]])
    assert _check(w, pid=100) == 3
    err = capsys.readouterr().err
    assert "200" in err and "300" not in err


def test_lsof_does_not_use_the_at_host_filter_form():
    w = World([[(200, "nc", "*")]])
    _check(w, pid=100)
    queries = [c for c in w.lsof_calls() if "-sTCP:LISTEN" in c]
    assert queries
    for argv in queries:
        assert not any(a.startswith("-iTCP@") for a in argv), argv
        assert any(a == f"-iTCP:{PORT}" for a in argv), argv


def test_lsof_failure_is_exit_2_and_no_warning(capsys):
    w = World([[(200, "nc", HOST)]], lsof_rc=1)
    assert _check(w, pid=100) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert PREFIX not in out.err
    assert len(out.err.strip().splitlines()) <= 1


def test_lsof_missing_is_exit_2_and_no_warning(capsys):
    w = World([[]], lsof_missing=True)
    assert _check(w, pid=100) == 2
    out = capsys.readouterr()
    assert out.out == "" and PREFIX not in out.err


def test_transient_mismatch_that_goes_away_is_quiet(capsys):
    # First read sees a squatter-looking listener; 1 s later the listener is gone.
    w = World([[(200, "python", HOST)], []])
    assert _check(w, pid=100) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    assert w.step == 1, "a would-be warning must be re-checked after exactly one sleep"


def test_transient_mismatch_that_now_matches_is_quiet(capsys):
    w = World([[(200, "python", HOST)], [(100, "python", HOST)]])
    assert _check(w, pid=100) == 0
    assert capsys.readouterr().err == ""


def test_persistent_mismatch_warns_after_recheck(capsys):
    w = World([[(200, "nc", HOST)], [(200, "nc", HOST)]])
    assert _check(w, pid=100) == 3
    assert w.step == 1
    assert PREFIX in capsys.readouterr().err


def test_quiet_first_read_does_not_sleep():
    w = World([[(100, "python", HOST)]])
    assert _check(w, pid=100) == 0
    assert w.step == 0


def test_recheck_rereads_pid_via_label_when_given(capsys):
    # The job restarted between the two reads: the caller's pid 100 is stale and
    # launchd now reports 200, which is the listener. No warning.
    w = World([[(200, "python", HOST)]], launchd_pids=(100, 200))
    assert _check(w, pid=100, label="pipecat.stt-server") == 0
    assert capsys.readouterr().err == ""
    ll = [c for c in w.calls if os.path.basename(c[0]) == "launchctl"]
    assert ll and ll[0][1] == "print" and ll[0][2].endswith("/pipecat.stt-server")
    assert not any(c[1] in ("bootout", "bootstrap", "kickstart", "kill") for c in ll)


def test_recheck_without_label_reuses_given_pid_and_skips_launchctl(capsys):
    w = World([[(200, "nc", HOST)]], launchd_pids=(999,))
    assert _check(w, pid=100) == 3
    assert not [c for c in w.calls if os.path.basename(c[0]) == "launchctl"]
    assert "100" in capsys.readouterr().err


def test_recheck_job_unloaded_within_the_second_is_quiet(capsys):
    # First read: loaded, no pid, a listener (case c). By the re-check launchd
    # says the job is gone, so case (a) applies: quiet.
    w = World([[(321, "nc", HOST)]], launchd_loaded=(True, False))
    assert _check(w, pid=None, label="pipecat.stt-server") == 0
    assert capsys.readouterr().err == ""


def test_recheck_launchctl_unreadable_keeps_callers_pid(capsys):
    # launchctl failing in the re-check is "can't tell", not "no pid": the
    # caller's pid 100 still matches the listener, so no false "not running".
    w = World([[(200, "nc", HOST)], [(100, "python", HOST)]], launchctl_rc=5)
    assert _check(w, pid=100, label="pipecat.stt-server") == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("rc", "stderr", "want"),
    [
        (0, "", (True, None)),
        (113, 'Could not find service "x" in domain for user gui: 501', (False, None)),
        (113, "", (False, None)),
        (1, "could not find service x", (False, None)),
        (1, "", None),
        (5, "Operation not permitted", None),
    ],
)
def test_launchd_state_not_loaded_only_on_launchctls_own_answer(rc, stderr, want):
    def run(argv, **_kw):
        return subprocess.CompletedProcess(argv, rc, "", stderr)

    assert squat_check.launchd_state("x", run, uid=501) == want


def test_launchd_state_launchctl_missing_is_cannot_tell():
    def run(argv, **_kw):
        raise FileNotFoundError("launchctl")

    assert squat_check.launchd_state("x", run, uid=501) is None


# Host matching over the whole state space: plist host x listener address x job
# state. MATCH lists, per plist host, the listener addresses that block the
# agent's bind (wildcard on either side, same address, localhost = 127.0.0.1 or
# ::1). Written out by hand, not derived from the code under test.
_ADDRS = ("127.0.0.1", "*", "[::]", "0.0.0.0", "[::1]", "10.9.9.9")
_ALL = set(_ADDRS)
MATCH = {
    "127.0.0.1": {"127.0.0.1", "*", "[::]", "0.0.0.0"},
    "0.0.0.0": _ALL,
    "::": _ALL,
    "[::]": _ALL,
    "*": _ALL,
    "localhost": {"127.0.0.1", "*", "[::]", "0.0.0.0", "[::1]"},
    "::1": {"*", "[::]", "0.0.0.0", "[::1]"},
    "[::1]": {"*", "[::]", "0.0.0.0", "[::1]"},
}
# (loaded, launchd pid, listener pid) -> warns when the address matches?
_JOBS = {
    "not-loaded": (0, None, 200, False),
    "own-listener": (1, 100, 100, False),
    "squatter": (1, 100, 200, True),
    "loaded-no-pid": (1, None, 200, True),
}


@pytest.mark.parametrize("job", sorted(_JOBS))
@pytest.mark.parametrize("addr", _ADDRS)
@pytest.mark.parametrize("host", sorted(MATCH))
def test_host_listener_job_state_space(capsys, host, addr, job):
    loaded, pid, lpid, warns_on_match = _JOBS[job]
    w = World([[(lpid, "nc", addr)]])
    want = 3 if (warns_on_match and addr in MATCH[host]) else 0
    assert _check(w, pid=pid, loaded=loaded, host=host) == want
    err = capsys.readouterr().err
    assert (PREFIX in err) == (want == 3)


def test_listener_pids_any_host_keeps_every_listener():
    w = World([[(1, "a", "10.9.9.9"), (2, "b", "127.0.0.1"), (3, "c", "[::1]")]])
    assert squat_check.listener_pids(PORT, None, w.run) == ["1", "2", "3"]
    assert squat_check.listener_pids(PORT, "127.0.0.1", w.run) == ["2"]


def test_lsof_pids_puts_fields_before_the_file_separator():
    calls = []

    def run(argv, **_kw):
        calls.append(argv)
        files = argv[argv.index("--") + 1 :] if "--" in argv else []
        if any(f.startswith("-") for f in files):  # lsof reads these as files
            return subprocess.CompletedProcess(argv, 1, "", "lsof: status error on -Fp\n")
        return subprocess.CompletedProcess(argv, 0, "p42\nf3\n", "")

    assert squat_check.lsof_pids(["--", "/x.lock"], run) == ["42"]
    assert calls[0].index("-Fp") < calls[0].index("--")


def test_module_docstring_pins_the_invariant_and_the_limit():
    doc = (squat_check.__doc__ or "").lower()
    # Invariant: the pid listening on the port must be the pid launchd started.
    assert "listener" in doc and "launchd" in doc and "pid" in doc
    # Limit: lsof only sees sockets the invoking user may inspect.
    assert "lsof" in doc
    assert "other user" in doc or "visib" in doc or "cannot see" in doc


# --------------------------------------------------------------------------- #
# CLI: stream and exit-code contract with lsof stubbed on PATH
# --------------------------------------------------------------------------- #


def _stub_lsof(tmp_path: Path, rows, rc: int = 0) -> Path:
    d = tmp_path / "bin"
    d.mkdir()
    exe = d / "lsof"
    exe.write_text(
        f"""#!{sys.executable}
import sys
if {rc}:
    sys.stderr.write("lsof: boom\\n")
    sys.exit({rc})
fields = "".join(a[2:] for a in sys.argv[1:] if a.startswith("-F"))
for pid, cmd, addr in {list(rows)!r}:
    out = ["p%s" % pid]
    if "c" in fields:
        out.append("c" + cmd)
    if "n" in fields:
        out.append("n" + addr)
    print("\\n".join(out))
"""
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return d


def _cli(path_dir: Path, *args: str) -> subprocess.CompletedProcess:
    env = {"PATH": str(path_dir), "HOME": str(path_dir)}
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def test_cli_match_is_silent_exit_0(tmp_path):
    d = _stub_lsof(tmp_path, [(100, "python", f"{HOST}:{PORT}")])
    res = _cli(d, "--host", HOST, "--port", str(PORT), "--loaded", "1", "--pid", "100")
    assert (res.returncode, res.stdout, res.stderr) == (0, "", "")


def test_cli_squat_warns_on_stderr_exit_3_stdout_empty(tmp_path):
    d = _stub_lsof(tmp_path, [(200, "nc", f"*:{PORT}")])
    res = _cli(d, "--host", HOST, "--port", str(PORT), "--loaded", "1", "--pid", "100")
    assert res.returncode == 3
    assert res.stdout == ""
    lines = res.stderr.strip().splitlines()
    assert lines and all(ln.startswith(PREFIX) for ln in lines)
    assert "200" in res.stderr and "nc" in res.stderr


def test_cli_no_pid_flag_means_no_pid(tmp_path):
    d = _stub_lsof(tmp_path, [(321, "nc", f"{HOST}:{PORT}")])
    res = _cli(d, "--host", HOST, "--port", str(PORT), "--loaded", "1")
    assert res.returncode == 3 and res.stdout == ""
    assert "not running" in res.stderr


def test_cli_not_loaded_is_quiet(tmp_path):
    d = _stub_lsof(tmp_path, [(321, "nc", f"{HOST}:{PORT}")])
    res = _cli(d, "--host", HOST, "--port", str(PORT), "--loaded", "0")
    assert (res.returncode, res.stdout, res.stderr) == (0, "", "")


def test_cli_lsof_failing_is_exit_2_without_warning(tmp_path):
    d = _stub_lsof(tmp_path, [], rc=1)
    res = _cli(d, "--host", HOST, "--port", str(PORT), "--loaded", "1", "--pid", "100")
    assert res.returncode == 2 and res.stdout == ""
    assert PREFIX not in res.stderr


def test_cli_lsof_absent_is_exit_2_without_warning(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    res = _cli(empty, "--host", HOST, "--port", str(PORT), "--loaded", "1", "--pid", "100")
    assert res.returncode == 2 and res.stdout == ""
    assert PREFIX not in res.stderr
