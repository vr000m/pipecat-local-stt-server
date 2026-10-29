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


def _print_body(pid, runs):
    """A ``launchctl print`` body with optional ``pid =`` and ``runs =`` lines."""
    body = "state = running\n" + (f"\tpid = {pid}\n" if pid is not None else "")
    return body + (f"\truns = {runs}\n" if runs is not None else "")


class World:
    """A scripted timeline of lsof listeners and launchd pids.

    ``snapshots`` is a list of listener lists, each row ``(pid, command, addr)``
    with ``addr`` like ``127.0.0.1`` or ``*``; the snapshot index advances each
    time the code under test calls ``sleep``. ``launchd_pids`` follows the same
    timeline for a ``launchctl print`` (None: no ``pid =`` line). A running job
    (a pid) prints ``runs = 1``, as launchctl does; a ``launchd_reads`` answer
    may name its own runs as a third item (None: no ``runs =`` line).
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
        launchd_reads=None,
    ):
        # launchd_reads: one (loaded, pid[, runs]) answer per launchctl call, in
        # call order (None: launchctl fails), overriding the sleep timeline.
        self.launchd_reads = list(launchd_reads) if launchd_reads is not None else None
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
            if self.launchd_reads is not None:
                answer = self.launchd_reads.pop(0) if self.launchd_reads else (True, None)
                if answer is None:
                    return subprocess.CompletedProcess(argv, 5, "", "launchctl: boom")
                loaded, pid = answer[:2]
                runs = answer[2] if len(answer) > 2 else ("1" if pid is not None else None)
                if not loaded:
                    return subprocess.CompletedProcess(argv, 113, "", "Could not find service")
                return subprocess.CompletedProcess(argv, 0, _print_body(pid, runs), "")
            if self.launchctl_rc is not None:
                return subprocess.CompletedProcess(argv, self.launchctl_rc, "", "launchctl: boom")
            if not self._at(self.launchd_loaded):
                msg = f'Could not find service "{argv[2].rsplit("/", 1)[-1]}" in domain\n'
                return subprocess.CompletedProcess(argv, 113, "", msg)
            pid = self._at(self.launchd_pids)
            runs = "1" if pid is not None else None
            return subprocess.CompletedProcess(argv, 0, _print_body(pid, runs), "")
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


def test_recheck_launchctl_unreadable_with_persistent_squatter_is_cannot_tell(capsys):
    # The re-read of the pid failed, so the caller's pid 100 may be stale (a
    # restart could have made the listener 200 the job's own): exit 2, no warning.
    w = World([[(200, "nc", HOST)], [(200, "nc", HOST)]], launchctl_rc=5)
    assert _check(w, pid=100, label="pipecat.stt-server") == 2
    err = capsys.readouterr().err
    assert PREFIX not in err and len(err.strip().splitlines()) == 1


# The re-check's launchd read x the second listener observation -> exit code.
# (re-read answer, second listener pid) for a caller pid of 100.
@pytest.mark.parametrize(
    ("launchctl_rc", "pids", "second", "want"),
    [
        (None, (100, 100), 100, 0),  # re-read pid matches the listener
        (None, (100, 100), 200, 3),  # re-read pid, persistent squatter
        (None, (100, 200), 200, 0),  # restarted: the new pid is the listener
        (5, (None,), 100, 0),  # unreadable, listener matches the caller's pid
        (5, (None,), 200, 2),  # unreadable, would warn on a stale pid
        (5, (None,), None, 0),  # unreadable, listener gone
    ],
)
def test_recheck_pid_state_space(capsys, launchctl_rc, pids, second, want):
    snap2 = [] if second is None else [(second, "nc", HOST)]
    w = World([[(200, "nc", HOST)], snap2], launchd_pids=pids, launchctl_rc=launchctl_rc)
    assert _check(w, pid=100, label="pipecat.stt-server") == want
    assert (PREFIX in capsys.readouterr().err) == (want == 3)


# The re-check brackets its second listener read with two launchd reads: the
# pid read before it and the pid read after it, x the second listener -> exit.
# A warning needs the same pid on both sides; a job that restarted during the
# listener read (its new pid holding the port) is "cannot tell", never a warning.
# Caller pid 100, first listener 200 (a would-be squat).
@pytest.mark.parametrize(
    ("before", "after", "second", "want"),
    [
        ((True, 100), (True, 100), 200, 3),  # stable pid, persistent squatter
        ((True, 100), (True, 200), 200, 2),  # restarted mid-read: new pid is the listener
        ((True, 100), (True, 300), 200, 2),  # restarted mid-read, pid changed
        ((True, None), (True, 300), 300, 2),  # came up mid-read: no pid, then the listener
        ((True, None), (True, None), 300, 3),  # stable no pid, listener: not running
        ((True, 100), (False, None), 200, 0),  # unloaded mid-read: quiet
        ((True, 100), None, 200, 2),  # the bracketing read failed
        ((True, 100), (True, 100), 100, 0),  # the listener now matches: quiet
        ((True, 200), (True, 200), 200, 0),  # restarted before the re-check: quiet
        ((True, 100), (True, 100), None, 0),  # listener gone: quiet
    ],
)
def test_recheck_brackets_the_listener_read_with_two_pid_reads(capsys, before, after, second, want):
    snap2 = [] if second is None else [(second, "nc", HOST)]
    w = World([[(200, "nc", HOST)], snap2], launchd_reads=[before, after])
    assert _check(w, pid=100, label="pipecat.stt-server") == want
    err = capsys.readouterr().err
    assert (PREFIX in err) == (want == 3)
    if want == 2:
        assert len(err.strip().splitlines()) == 1


# The bracketing reads compare the spawn counter too: (before, after) launchd
# answers (loaded, pid, runs) around the second listener read x that listener
# -> exit. A job that started, listened and exited between two no-pid reads
# moved `runs`, so its own (now gone) listener is "cannot tell", never "label
# not running"; a restart back to the same pid moved it too; a running job with
# no `runs =` line (UNKNOWN) never compares as unchanged. Caller pid 100, first
# listener 200.
@pytest.mark.parametrize(
    ("before", "after", "second", "want"),
    [
        ((True, None, "4"), (True, None, "4"), 300, 3),  # stable, not running: warn
        ((True, None, "4"), (True, None, "5"), 300, 2),  # ran and exited mid-read
        ((True, None, None), (True, None, None), 300, 3),  # never ran, no line: stable
        ((True, None, None), (True, None, "1"), 300, 2),  # first spawn mid-read
        ((True, 100, "4"), (True, 100, "4"), 200, 3),  # stable pid and runs: squatter
        ((True, 100, "4"), (True, 100, "5"), 200, 2),  # restarted back to pid 100
        ((True, 100, None), (True, 100, None), 200, 2),  # runs UNKNOWN on both sides
        ((True, 100, "4"), (True, 100, None), 200, 2),  # runs UNKNOWN after
        ((True, 100, "4"), (True, 100, "5"), 100, 0),  # listener matches: quiet first
        ((True, None, "4"), (False, None, None), 300, 0),  # unloaded mid-read: quiet
    ],
)
def test_recheck_brackets_the_listener_read_with_runs_too(capsys, before, after, second, want):
    snap2 = [] if second is None else [(second, "nc", HOST)]
    w = World([[(200, "nc", HOST)], snap2], launchd_reads=[before, after])
    assert _check(w, pid=100, label="pipecat.stt-server") == want
    err = capsys.readouterr().err
    assert (PREFIX in err) == (want == 3)
    if want == 2:
        assert len(err.strip().splitlines()) == 1 and "pid or runs" in err


def test_restart_between_the_pid_read_and_the_listener_read_does_not_warn(capsys):
    # The reported race: the re-check reads launchd pid 100, the job restarts,
    # and lsof then sees the new job (pid 200) on the port. Comparing 200 with
    # the stale 100 would warn; the bracketing read sees 200 and says cannot tell.
    w = World(
        [[(200, "python", HOST)], [(200, "python", HOST)]],
        launchd_reads=[(True, 100), (True, 200)],
    )
    assert _check(w, pid=100, label="pipecat.stt-server") == 2
    assert PREFIX not in capsys.readouterr().err


# lsof's answer (exit, stdout, stderr) -> lsof_output: the whole state space.
# Exit 1 with no stderr or only "lsof: WARNING:" lines is "nothing matched".
_WARN = "lsof: WARNING: can't stat() smbfs file system /Volumes/x\n"


@pytest.mark.parametrize(
    ("rc", "out", "err", "want"),
    [
        (0, "p1\n", "", "p1\n"),
        (0, "p1\n", _WARN, "p1\n"),
        (1, "", "", ""),
        (1, "", "  \n", ""),
        (1, "", _WARN, ""),
        (1, "", _WARN + "lsof: WARNING: Output information may be incomplete.\n", ""),
        (1, "", _WARN + "lsof: status error on /x.lock: No such file or directory\n", None),
        (1, "", "lsof: status error on /x.lock: No such file or directory\n", None),
        (1, "", "WARNING: not lsof's own line\n", None),
        (2, "", "", None),
        (2, "", _WARN, None),
        (127, "", "lsof: not found\n", None),
    ],
)
def test_lsof_output_state_space(rc, out, err, want):
    def run(argv, **_kw):
        return subprocess.CompletedProcess(argv, rc, out, err)

    assert squat_check.lsof_output(["-Fp"], run) == want


def test_warning_only_no_match_is_no_listener_not_cannot_tell(capsys):
    def run(argv, **_kw):
        return subprocess.CompletedProcess(argv, 1, "", _WARN)

    assert squat_check.listener_pids(PORT, HOST, run) == []
    assert squat_check.lsof_pids(["--", "/x.lock"], run) == []
    assert squat_check.check(HOST, PORT, "100", True, run=run, sleep=lambda _s: None) == 0
    assert capsys.readouterr().err == ""


def test_every_lsof_call_suppresses_warnings():
    w = World([[(200, "nc", HOST)], [(200, "nc", HOST)]])
    assert _check(w, pid=100) == 3  # listener reads plus the -Fc command read
    calls = w.lsof_calls()
    assert any("-Fc" in c for c in calls)
    for argv in calls:
        assert "-w" in argv, argv
    squat_check.lsof_pids(["--", "/x.lock"], w.run)
    assert "-w" in w.lsof_calls()[-1]


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


def _cli(path_dir: Path, *args: str, answer: str = "") -> subprocess.CompletedProcess:
    """Run the CLI with ANSWER (the caller's launchctl print output) on stdin."""
    env = {"PATH": str(path_dir), "HOME": str(path_dir)}
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        input=answer,
    )


RUNNING_100 = "state = running\n\tpid = 100\n"
CLI = ("--host", HOST, "--port", str(PORT))


def test_cli_match_is_silent_exit_0(tmp_path):
    d = _stub_lsof(tmp_path, [(100, "python", f"{HOST}:{PORT}")])
    res = _cli(d, *CLI, "--launchctl-rc", "0", answer=RUNNING_100)
    assert (res.returncode, res.stdout, res.stderr) == (0, "", "")


def test_cli_squat_warns_on_stderr_exit_3_stdout_empty(tmp_path):
    d = _stub_lsof(tmp_path, [(200, "nc", f"*:{PORT}")])
    res = _cli(d, *CLI, "--launchctl-rc", "0", answer=RUNNING_100)
    assert res.returncode == 3
    assert res.stdout == ""
    lines = res.stderr.strip().splitlines()
    assert lines and all(ln.startswith(PREFIX) for ln in lines)
    assert "200" in res.stderr and "nc" in res.stderr


def test_cli_no_pid_line_means_no_pid(tmp_path):
    d = _stub_lsof(tmp_path, [(321, "nc", f"{HOST}:{PORT}")])
    res = _cli(d, *CLI, "--launchctl-rc", "0", answer="state = waiting\n")
    assert res.returncode == 3 and res.stdout == ""
    assert "not running" in res.stderr


def test_cli_not_loaded_is_quiet(tmp_path):
    d = _stub_lsof(tmp_path, [(321, "nc", f"{HOST}:{PORT}")])
    res = _cli(d, *CLI, "--launchctl-rc", "113", answer='Could not find service "x"')
    assert (res.returncode, res.stdout, res.stderr) == (0, "", "")


def test_cli_lsof_failing_is_exit_2_without_warning(tmp_path):
    d = _stub_lsof(tmp_path, [], rc=1)
    res = _cli(d, *CLI, "--launchctl-rc", "0", answer=RUNNING_100)
    assert res.returncode == 2 and res.stdout == ""
    assert PREFIX not in res.stderr


def test_cli_lsof_absent_is_exit_2_without_warning(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    res = _cli(empty, *CLI, "--launchctl-rc", "0", answer=RUNNING_100)
    assert res.returncode == 2 and res.stdout == ""
    assert PREFIX not in res.stderr


# The one CLI form (recipes and harness): the caller's launchctl print answer,
# exit status on argv and output on stdin, x a foreign listener -> exit.
# classify_print is the one rule (launchd_state's too).
@pytest.mark.parametrize(
    ("rc", "output", "want"),
    [
        (0, "state = running\n\tpid = 100\n", 3),  # running, a foreign pid listens
        (0, "state = waiting\n", 3),  # loaded, no pid, a listener: case (c)
        (113, 'Could not find service "x" in domain for user gui: 501', 0),
        (1, 'Could not find service "x" in domain', 0),
        (5, "Input/output error", 2),
        (1, "Operation not permitted", 2),
        (1, "", 2),
    ],
)
def test_cli_launchctl_answer_state_space(tmp_path, rc, output, want):
    d = _stub_lsof(tmp_path, [(200, "nc", f"{HOST}:{PORT}")])
    res = _cli(d, *CLI, "--launchctl-rc", str(rc), answer=output)
    assert res.returncode == want, res.stderr
    assert res.stdout == ""
    assert (PREFIX in res.stderr) == (want == 3)
    assert ("cannot read the launchd state" in res.stderr) == (want == 2)
    assert squat_check.classify_print(rc, output) == (
        None
        if want == 2
        else ((False, None) if want == 0 else (True, squat_check.parse_launchd_pid(output)))
    )


# The second (--loaded/--pid) form and the argv form of the answer are gone: the
# answer can carry a legacy plist's token, so it must never ride on argv.
@pytest.mark.parametrize(
    "extra",
    [
        ("--loaded", "1", "--pid", "100"),
        ("--launchctl-rc", "0", "--pid", "100"),
        ("--launchctl-rc", "0", f"--launchctl-output={RUNNING_100}"),
    ],
)
def test_cli_has_one_form_and_takes_no_answer_on_argv(tmp_path, extra):
    d = _stub_lsof(tmp_path, [(200, "nc", f"{HOST}:{PORT}")])
    res = _cli(d, *CLI, *extra, answer=RUNNING_100)
    assert res.returncode == 2 and PREFIX not in res.stderr
    assert "unrecognized arguments" in res.stderr or "required" in res.stderr


# endpoint_holders: TCP reads listeners on the port; a socket reads the holders of
# <socket>.lock (never the socket path); a missing .lock has no holder.
@pytest.mark.parametrize(
    ("socket", "lock", "lsof_rc", "want", "lsof_target"),
    [
        (False, False, 0, ["200"], f"-iTCP:{PORT}"),
        (False, False, 2, None, f"-iTCP:{PORT}"),
        (True, True, 0, ["200"], ".lock"),
        (True, True, 2, None, ".lock"),
        (True, False, 0, [], None),
    ],
)
def test_endpoint_holders_state_space(tmp_path, socket, lock, lsof_rc, want, lsof_target):
    sock = tmp_path / "a.sock"
    if lock:
        (tmp_path / "a.sock.lock").write_text("")
    calls = []

    def run(argv, **_kw):
        calls.append(argv)
        if lsof_rc:
            return subprocess.CompletedProcess(argv, lsof_rc, "", "lsof: boom")
        return subprocess.CompletedProcess(argv, 0, f"p200\nn{HOST}:{PORT}\n", "")

    got = squat_check.endpoint_holders(HOST, PORT, str(sock) if socket else "", run)
    assert got == want
    if lsof_target is None:
        assert calls == []
    else:
        assert len(calls) == 1 and any(lsof_target in a for a in calls[0]), calls
        if socket:
            assert calls[0][-1] == f"{sock}.lock" and str(sock) not in calls[0][:-1]


# cli_bound IS the CLI's worst case: the slowest path through check() (a
# squatter that persists, --label given, so both pid re-reads run, then one
# command lookup per listener) x listener count -> the sum of every timeout it
# hands lsof / launchctl plus every sleep == cli_bound(n). A new wait in
# check() that cli_bound does not count fails here, and so would a caller's
# timeout derived from it.
@pytest.mark.parametrize("n", [1, 2, 3])
def test_cli_bound_is_the_slowest_path(capsys, n):
    spent = []

    def run(cmd, **kw):
        spent.append(kw["timeout"])
        if cmd[0] == "launchctl":
            return subprocess.CompletedProcess(cmd, 0, "\tpid = 100\n\truns = 1\n", "")
        if "-Fc" in cmd:
            return subprocess.CompletedProcess(cmd, 0, f"p{cmd[cmd.index('-p') + 1]}\ncx\n", "")
        rows = "".join(f"p{200 + i}\nn127.0.0.1:{PORT}\n" for i in range(n))
        return subprocess.CompletedProcess(cmd, 0, rows, "")

    rc = squat_check.check(HOST, PORT, "100", True, label="x", run=run, sleep=spent.append)
    assert rc == 3  # the warning path: every wait ran
    assert sum(spent) == squat_check.cli_bound(n)
    assert len(spent) == 2 + 1 + 2 + n  # two lsof, the sleep, two prints, n lookups
