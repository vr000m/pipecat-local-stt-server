"""Stubbed tests for ``scripts/smoke.py`` (plan Phase 3, Decisions 7-9).

Every step goes through the ``smoke.run_command`` seam, which these tests
monkeypatch; no real smoke script, installer or ``launchctl`` is ever run.
Where a real process is needed (the ``--agents`` step) it is a stub executable
on a PATH led by a stub dir, with a temp ``HOME``; the fixture asserts the stub
``launchctl`` resolves first and pops ``CI`` unless a test sets it.

Assumptions (contract is silent): the table prints one line per step holding
the step name and its outcome word; ``--agents`` discovers plists under
``_smoke_lib.agents_home()``, which the fixtures patch to the temp home
(``HOME`` alone does not move it, and production code has no override, so no
test reads the developer's real ``~/Library/LaunchAgents``);
commands whose argv[0] is not a stubbed tool (launchctl/id/uv/say/afconvert/
lsof) are faked as success in the ``--agents`` test, so only the stubbed tools
ever run for real.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE = REPO_ROOT / "scripts" / "smoke.py"
RENDERER = REPO_ROOT / "scripts" / "render_stt_plist.py"
SPARE = "pipecat.stt-server.smoke"


def _load():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location("smoke_runner_under_test", SMOKE)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["smoke_runner_under_test"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(REPO_ROOT / "scripts"))


@pytest.fixture(scope="module")
def smoke():
    return _load()


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


def _row(out: str, name: str) -> str:
    for line in out.splitlines():
        if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", line):
            return line
    raise AssertionError(f"no table row for {name!r} in:\n{out}")


def _outcome(line: str) -> str:
    for word in ("PASS", "FAIL", "SKIPPED"):
        if word in line:
            return word
    raise AssertionError(f"no outcome in {line!r}")


@pytest.fixture
def seam(smoke, monkeypatch, tmp_path):
    """Hermetic main(): temp HOME, CI unset, run_command recorded + faked."""
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(smoke._smoke_lib, "agents_home", lambda: home)
    monkeypatch.delenv("CI", raising=False)
    calls: list[tuple[list[str], dict | None]] = []
    behaviour = {"default": _cp(0), "by_script": {}}

    def fake(cmd, timeout, env=None, **kw):
        calls.append((list(cmd), dict(env) if env is not None else None))
        joined = " ".join(str(c) for c in cmd)
        for needle, what in behaviour["by_script"].items():
            if needle in joined:
                if isinstance(what, BaseException):
                    raise what
                return what
        return behaviour["default"]

    monkeypatch.setattr(smoke, "run_command", fake)
    fake.calls = calls
    fake.behaviour = behaviour
    fake.home = home
    return fake


def _launchd_calls(seam):
    return [(c, e) for c, e in seam.calls if any("smoke_launchd" in str(a) for a in c)]


# --------------------------------------------------------------------------- #
# Renderer + exit code
# --------------------------------------------------------------------------- #


def test_import_has_no_side_effects(smoke):
    assert (smoke.PASS, smoke.FAIL, smoke.SKIPPED) == ("PASS", "FAIL", "SKIPPED")


def test_table_renders_every_step_with_reason(smoke):
    results = [
        smoke.StepResult("supervised", smoke.PASS),
        smoke.StepResult("peercred", smoke.FAIL, "exit 3"),
        smoke.StepResult("launchd", smoke.SKIPPED, "not requested"),
        smoke.StepResult("agents", smoke.SKIPPED, "no installed agents"),
    ]
    out = smoke.render_table(results)
    for r in results:
        line = _row(out, r.name)
        assert r.outcome in line
        if r.detail:
            assert r.detail in out
    assert "exit 3" in out and "not requested" in out and "no installed agents" in out


@pytest.mark.parametrize(
    "outcomes,want",
    [
        (["PASS", "PASS"], 0),
        (["PASS", "SKIPPED"], 0),
        (["SKIPPED", "SKIPPED"], 0),
        (["PASS", "FAIL"], 1),
        (["FAIL", "SKIPPED"], 1),
        ([], 0),
    ],
)
def test_exit_code_rule(smoke, outcomes, want):
    results = [smoke.StepResult(f"s{i}", o) for i, o in enumerate(outcomes)]
    assert smoke.exit_code(results) == want


# --------------------------------------------------------------------------- #
# run_step outcomes (Decision 8)
# --------------------------------------------------------------------------- #


def test_run_step_not_requested_is_skipped_and_never_runs(smoke, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("run_command must not be called")

    monkeypatch.setattr(smoke, "run_command", boom)
    r = smoke.run_step("launchd", ["x"], 5, requested=False)
    assert r.outcome == smoke.SKIPPED and "not requested" in r.detail
    assert smoke.exit_code([r]) == 0


def test_run_step_precondition_absent_is_skipped(smoke, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("run_command must not be called")

    monkeypatch.setattr(smoke, "run_command", boom)
    r = smoke.run_step("launchd", ["x"], 5, skip_reason="CI is set")
    assert r.outcome == smoke.SKIPPED and "CI is set" in r.detail
    assert smoke.exit_code([r]) == 0


def test_run_step_zero_exit_passes(smoke, monkeypatch):
    monkeypatch.setattr(smoke, "run_command", lambda *a, **k: _cp(0))
    r = smoke.run_step("peercred", ["x"], 5)
    assert r.outcome == smoke.PASS
    assert smoke.exit_code([r]) == 0


def test_run_step_nonzero_exit_fails(smoke, monkeypatch):
    # Also the "guard refused" case: the harness exits non-zero.
    monkeypatch.setattr(smoke, "run_command", lambda *a, **k: _cp(2, err="refused"))
    r = smoke.run_step("launchd", ["x"], 5)
    assert r.outcome == smoke.FAIL
    assert smoke.exit_code([r]) == 1


def test_run_step_crash_fails(smoke, monkeypatch):
    def crash(*a, **k):
        raise OSError("no such interpreter")

    monkeypatch.setattr(smoke, "run_command", crash)
    r = smoke.run_step("peercred", ["x"], 5)
    assert r.outcome == smoke.FAIL and r.detail
    assert smoke.exit_code([r]) == 1


def test_run_step_timeout_fails_and_says_so(smoke, monkeypatch):
    def slow(cmd, timeout, env=None, **k):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(smoke, "run_command", slow)
    r = smoke.run_step("supervised", ["x"], 5)
    assert r.outcome == smoke.FAIL
    assert "timeout" in r.detail.lower() or "timed out" in r.detail.lower()
    assert smoke.exit_code([r]) == 1


# --------------------------------------------------------------------------- #
# main(): step set, order, launchd gating
# --------------------------------------------------------------------------- #


def test_main_default_steps_pass_exit_zero_and_launchd_not_built(smoke, seam, capsys):
    rc = smoke.main([])
    out = capsys.readouterr().out
    assert rc == 0
    for name in ("supervised", "peercred", "token-file"):
        assert _outcome(_row(out, name)) == "PASS"
    assert _outcome(_row(out, "launchd")) == "SKIPPED"
    assert _outcome(_row(out, "agents")) == "SKIPPED"
    # Not requested: not even built (no command, no label env).
    assert _launchd_calls(seam) == []
    assert not any(e and "PIPECAT_STT_LABEL" in e for _, e in seam.calls)
    # Order.
    lines = [ln for ln in out.splitlines()]
    idx = [
        next(i for i, ln in enumerate(lines) if n in ln)
        for n in ("supervised", "peercred", "token-file")
    ]
    assert idx == sorted(idx)


def test_main_python_steps_use_sys_executable_never_uv_run(smoke, seam):
    smoke.main([])
    for cmd, _ in seam.calls:
        assert cmd[0] != "uv", cmd
        if "uv" in cmd:
            assert "--no-sync" in cmd, cmd


def test_main_launchd_targets_harness_with_spare_labels(smoke, seam, monkeypatch, capsys):
    monkeypatch.setattr(smoke.sys, "platform", "darwin")
    rc = smoke.main(["--launchd"])
    out = capsys.readouterr().out
    assert rc == 0
    calls = _launchd_calls(seam)
    assert len(calls) == 1
    cmd, env = calls[0]
    assert cmd[0] == sys.executable
    assert any(str(a).endswith("scripts/smoke_launchd.py") for a in cmd)
    assert env is not None
    assert env["PIPECAT_STT_LABEL"] == SPARE
    assert env["KODA_STT_LABEL"] == SPARE
    assert _outcome(_row(out, "launchd")) == "PASS"


def test_main_launchd_guard_refused_is_fail_exit_one(smoke, seam, monkeypatch, capsys):
    monkeypatch.setattr(smoke.sys, "platform", "darwin")
    seam.behaviour["by_script"]["smoke_launchd"] = _cp(2, err="guard: refused")
    rc = smoke.main(["--launchd"])
    out = capsys.readouterr().out
    assert rc == 1
    assert _outcome(_row(out, "launchd")) == "FAIL"


def test_main_launchd_crash_and_timeout_are_fail(smoke, seam, monkeypatch, capsys):
    monkeypatch.setattr(smoke.sys, "platform", "darwin")
    for exc in (OSError("boom"), subprocess.TimeoutExpired(["x"], 1)):
        seam.behaviour["by_script"]["smoke_launchd"] = exc
        rc = smoke.main(["--launchd"])
        out = capsys.readouterr().out
        assert rc == 1
        assert _outcome(_row(out, "launchd")) == "FAIL"


def test_main_launchd_skipped_off_darwin(smoke, seam, monkeypatch, capsys):
    monkeypatch.setattr(smoke.sys, "platform", "linux")
    rc = smoke.main(["--launchd"])
    out = capsys.readouterr().out
    assert rc == 0
    assert _outcome(_row(out, "launchd")) == "SKIPPED"
    assert _launchd_calls(seam) == []


def test_main_launchd_skipped_when_ci_set(smoke, seam, monkeypatch, capsys):
    monkeypatch.setattr(smoke.sys, "platform", "darwin")
    monkeypatch.setenv("CI", "true")
    rc = smoke.main(["--launchd"])
    out = capsys.readouterr().out
    assert rc == 0
    assert _outcome(_row(out, "launchd")) == "SKIPPED"
    assert _launchd_calls(seam) == []


@pytest.mark.parametrize("step", ["supervised", "peercred"])
def test_main_failing_step_fails_whole_run(smoke, seam, capsys, step):
    seam.behaviour["by_script"][f"smoke_{step}"] = _cp(1)
    rc = smoke.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert _outcome(_row(out, step)) == "FAIL"


# --------------------------------------------------------------------------- #
# --agents: read-only under stubbed launchctl / id / uv
# --------------------------------------------------------------------------- #

STUBBED_TOOLS = {"launchctl", "id", "uv", "say", "afconvert", "lsof"}
FORBIDDEN = ("bootout", "bootstrap", "kickstart", "install_stt_agent", "installer")


def _render_plist(home: Path, label: str, *, port=None, socket_path=None, backend="mlx"):
    dst = home / "Library" / "LaunchAgents" / f"{label}.plist"
    dst.parent.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(REPO_ROOT),
        "PIPECAT_STT_LABEL": label,
        "PYTHON": sys.executable,
        "REPO_ROOT": str(REPO_ROOT),
        "BACKEND": backend,
        "MODEL": "test/model",
        "HOME": str(home),
        "LOG_DIR": str(home / "Library" / "Logs"),
        "PLIST_DST": str(dst),
        "SOCKET_PATH": str(socket_path) if socket_path else "",
        "HOST": "" if socket_path else "127.0.0.1",
        "PORT": "" if socket_path else str(port),
        "AUTH_TOKEN_FILE": "",
    }
    res = subprocess.run(
        [sys.executable, str(RENDERER)], capture_output=True, text=True, timeout=60, env=env
    )
    assert res.returncode == 0, res.stderr
    return dst


def _stub_env(tmp_path: Path, monkeypatch, *, with_say: bool = True):
    stub = tmp_path / "stubbin"
    stub.mkdir()
    log = tmp_path / "argv.log"
    for name in ("launchctl", "id", "uv", "lsof") + (("say", "afconvert") if with_say else ()):
        _exe(
            stub / name,
            f"#!/usr/bin/env bash\nprintf '{name} %s\\n' \"$*\" >> {log}\n"
            + ('if [[ "$1" == "-u" ]]; then echo 501; fi\n' if name == "id" else "")
            + (
                'if [[ "$1" == "print" ]]; then printf "state = running\\n\\truns = 7\\n\\tpid = 4242\\n"; fi\n'
                if name == "launchctl"
                else ""
            )
            + (
                'out=""; prev=""; for a in "$@"; do [[ "$prev" == "-o" ]] && out="$a"; prev="$a"; last="$a"; done; '
                + ('[[ -n "$out" ]] && : > "$out"\n' if name == "say" else ': > "$last"\n')
                if name in ("say", "afconvert")
                else ""
            )
            + "exit 0\n",
        )
    # PATH is the stub dir only when say must be absent, else stub first.
    path = str(stub) if not with_say else f"{stub}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    monkeypatch.setenv("PATH", path)
    assert shutil.which("launchctl") == str(stub / "launchctl")
    assert shutil.which("uv") == str(stub / "uv")
    if not with_say:
        assert shutil.which("say") is None
    return log


@pytest.fixture
def agents_env(smoke, monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(smoke._smoke_lib, "agents_home", lambda: home)
    monkeypatch.delenv("CI", raising=False)
    log = _stub_env(tmp_path, monkeypatch)
    ran: list[list[str]] = []
    real = subprocess.run

    def seam_run(cmd, timeout, env=None, **kw):
        ran.append([str(c) for c in cmd])
        if Path(str(cmd[0])).name in STUBBED_TOOLS:
            return real(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return _cp(0)  # smoke scripts / interpreter decodes: faked

    monkeypatch.setattr(smoke, "run_command", seam_run)
    return home, log, ran, tmp_path


def _argv(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def test_agents_skipped_when_none_installed(smoke, agents_env, capsys):
    _, log, ran, _ = agents_env
    rc = smoke.main(["--agents"])
    out = capsys.readouterr().out
    assert rc == 0
    assert _outcome(_row(out, "agents")) == "SKIPPED"
    assert not any(ln.startswith("say ") for ln in _argv(log))


def test_agents_read_only_with_tcp_and_socket_plists(smoke, agents_env, monkeypatch, capsys):
    # The plists are read for real (read_stt_plist.load, in-process, once each);
    # only the decode child is faked (it verifies the connection itself). The
    # stubbed launchd reports pid 4242 and runs 7, so each plist gets exactly
    # one decode at the endpoint it records, carrying that identity, and the
    # row is PASS. The only launchctl calls are read-only prints: one before
    # and one after each decode; nothing mutating.
    home, log, _ran, tmp_path = agents_env
    _render_plist(home, "pipecat.stt-server", port=9565)
    _render_plist(home, "pipecat.stt-server.parakeet", port=9765, backend="parakeet")
    sock = tmp_path / "s.sock"
    _render_plist(home, "pipecat.stt-server.sock", socket_path=sock)
    ran: list[list[str]] = []
    decodes: list[list[str]] = []
    loads: list[str] = []
    real = subprocess.run
    real_load = smoke.read_stt_plist.load
    monkeypatch.setattr(smoke.read_stt_plist, "load", lambda p: loads.append(p) or real_load(p))

    def seam_run(cmd, timeout, env=None, **kw):
        cmd = [str(c) for c in cmd]
        ran.append(cmd)
        if Path(cmd[0]).name in STUBBED_TOOLS:
            return real(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        assert "--_decode" in cmd, cmd  # the only other child this step may start
        decodes.append(cmd)
        return _cp(0)

    monkeypatch.setattr(smoke, "run_command", seam_run)
    row = smoke.agents_step(True)
    capsys.readouterr()
    assert row.outcome == "PASS", row
    assert sorted(loads) == sorted(str(p) for p in smoke.agent_plists())  # one read each

    def flag(cmd, name):
        return cmd[cmd.index(name) + 1] if name in cmd else ""

    got = sorted((flag(d, "--socket-path"), flag(d, "--host"), flag(d, "--port")) for d in decodes)
    assert got == sorted(
        [("", "127.0.0.1", "9565"), ("", "127.0.0.1", "9765"), (str(sock), "", "")]
    )
    assert all((flag(d, "--pid"), flag(d, "--runs")) == ("4242", "7") for d in decodes)
    lines = _argv(log)
    assert any(ln.startswith("say ") for ln in lines), "the clip was never generated"
    assert any(ln.startswith("afconvert ") for ln in lines)
    launchctl = [ln for ln in lines if ln.split()[0] == "launchctl"]
    assert sorted(ln.split()[-1].rsplit("/", 1)[-1] for ln in launchctl) == sorted(
        ["pipecat.stt-server", "pipecat.stt-server.parakeet", "pipecat.stt-server.sock"] * 2
    )
    assert all(ln.split()[1] == "print" for ln in launchctl), launchctl
    assert not [ln for ln in lines if ln.split()[0] == "uv"], lines
    for cmd in ran:
        assert not any(w in " ".join(cmd) for w in FORBIDDEN), cmd
        assert Path(cmd[0]).name not in ("launchctl", "uv"), cmd


# --agents: the launchd identity before the decode x the decode child's answer
# (it verifies the server end of its own connection; DECODE_REFUSED = nothing
# sent) x a FRESH identity read after it -> decoded? and the row. Only a
# running job with a readable pid and runs is decoded; only an unchanged
# (loaded, pid, runs) after a passing decode is a PASS.
_IDENT = {
    "unreadable": None,
    "unloaded": (False, None, None),
    "no-pid": (True, None, "3"),
    "no-runs": (True, "4242", None),
    "running": (True, "4242", "7"),
}
_AFTER = {
    "same": (True, "4242", "7"),
    "restarted-same-pid": (True, "4242", "8"),
    "new-pid": (True, "5151", "8"),
    "unloaded": (False, None, None),
    "unreadable": None,
}
_CHILD = {"pass": 0, "refused": 3, "fail": 1}


@pytest.mark.parametrize("transport", ["tcp", "unix"])
@pytest.mark.parametrize("after", list(_AFTER))
@pytest.mark.parametrize("child", list(_CHILD))
@pytest.mark.parametrize("ident", list(_IDENT))
def test_agents_identity_gate_state_space(
    smoke, agents_env, monkeypatch, capsys, ident, child, after, transport
):
    home, _log, _ran, tmp_path = agents_env
    if transport == "tcp":
        _render_plist(home, "pipecat.stt-server", port=9565)
    else:
        _render_plist(home, "pipecat.stt-server", socket_path=tmp_path / "s.sock")
    reads = {"n": 0}

    def identity(label, *a, **k):
        reads["n"] += 1
        return _IDENT[ident] if reads["n"] == 1 else _AFTER[after]

    monkeypatch.setattr(smoke._smoke_lib, "launchd_identity", identity)
    decodes: list[list[str]] = []

    def seam_run(cmd, timeout, env=None, **kw):
        cmd = [str(c) for c in cmd]
        if "--_decode" in cmd:
            decodes.append(cmd)
            return _cp(_CHILD[child], err="refused: x" if child == "refused" else "")
        return _cp(0)

    monkeypatch.setattr(smoke, "run_command", seam_run)
    row = smoke.agents_step(True)
    capsys.readouterr()
    ours = ident == "running"
    assert (len(decodes) == 1) == ours, (row, decodes)
    if not ours:
        assert "no token sent" in row.detail and row.outcome == "FAIL", row
        assert reads["n"] == 1
        return
    d = decodes[0]
    assert (d[d.index("--pid") + 1], d[d.index("--runs") + 1]) == ("4242", "7")
    want = "PASS" if child == "pass" and after == "same" else "FAIL"
    assert row.outcome == want, row
    assert ("no token sent" in row.detail) == (child == "refused"), row
    assert reads["n"] == (2 if child == "pass" else 1)  # the PASS needs a fresh read


# The token is resolved from the plist the way the server resolves it
# (--auth-token-file, else PIPECAT_STT_AUTH_TOKEN, else KODA_STT_AUTH_TOKEN, a
# blank value counting as unset) and reaches the decode child only on stdin.
@pytest.mark.parametrize(
    ("file_token", "env", "want"),
    [
        (None, {}, None),
        ("from-file", {}, "from-file"),
        (None, {"PIPECAT_STT_AUTH_TOKEN": "pc"}, "pc"),
        (None, {"KODA_STT_AUTH_TOKEN": "koda"}, "koda"),
        (None, {"PIPECAT_STT_AUTH_TOKEN": "pc", "KODA_STT_AUTH_TOKEN": "koda"}, "pc"),
        (None, {"PIPECAT_STT_AUTH_TOKEN": "  ", "KODA_STT_AUTH_TOKEN": " koda "}, "koda"),
        ("from-file", {"PIPECAT_STT_AUTH_TOKEN": "pc"}, "from-file"),
    ],
)
def test_agents_token_resolution_and_stdin_only(
    smoke, agents_env, monkeypatch, capsys, file_token, env, want
):
    import plistlib

    home, _log, _ran, tmp_path = agents_env
    args = [sys.executable, "-m", "stt_server", "serve", "--host", "127.0.0.1", "--port", "9565"]
    if file_token is not None:
        tok_dir = tmp_path / "tokdir"
        tok_dir.mkdir(mode=0o700)
        tok = tok_dir / "tok"
        tok.write_text(file_token + "\n")
        tok.chmod(0o600)  # the server's token-file policy
        args += ["--auth-token-file", str(tok)]
    plist = {"Label": "pipecat.stt-server", "ProgramArguments": args}
    if env:
        plist["EnvironmentVariables"] = env
    (home / "Library" / "LaunchAgents" / "pipecat.stt-server.plist").write_bytes(
        plistlib.dumps(plist)
    )
    monkeypatch.setattr(smoke._smoke_lib, "launchd_identity", lambda *a, **k: (True, "1", "1"))
    seen = []

    def seam_run(cmd, timeout, env=None, **kw):
        if "--_decode" in [str(c) for c in cmd]:
            seen.append(([str(c) for c in cmd], kw.get("input"), env))
        return _cp(0)

    monkeypatch.setattr(smoke, "run_command", seam_run)
    row = smoke.agents_step(True)
    capsys.readouterr()
    assert row.outcome == "PASS", row
    (cmd, given, child_env) = seen[0]
    assert given == (want.encode() if want else None)
    for secret in ("from-file", "pc", "koda"):
        assert not any(secret in a for a in cmd), cmd
        assert child_env is None or secret not in " ".join(child_env.values())
    assert "--auth-token-file" not in cmd


def test_agents_skipped_when_say_absent(smoke, monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(smoke._smoke_lib, "agents_home", lambda: home)
    monkeypatch.delenv("CI", raising=False)
    _render_plist(home, "pipecat.stt-server", port=9565)
    log = _stub_env(tmp_path, monkeypatch, with_say=False)
    monkeypatch.setattr(smoke, "run_command", lambda cmd, timeout, env=None, **k: _cp(0))
    rc = smoke.main(["--agents"])
    out = capsys.readouterr().out
    assert rc == 0
    assert _outcome(_row(out, "agents")) == "SKIPPED"
    assert "say" in _row(out, "agents").lower()
    assert not any(w in ln for ln in _argv(log) for w in FORBIDDEN)


# --------------------------------------------------------------------------
# run_command: a timed-out or interrupted child gets to run its own teardown
# --------------------------------------------------------------------------

_TRAPS_TERM = """
import signal, sys, time
def bye(*_):
    open(sys.argv[1], "w").write("teardown ran")
    sys.exit(1)
signal.signal(signal.SIGTERM, bye)
print("ready", flush=True)
time.sleep(60)
"""


def test_run_command_timeout_lets_child_tear_down(smoke, tmp_path):
    # subprocess.run(timeout=) SIGKILLs the child, so a harness's finally never ran.
    marker = tmp_path / "marker"
    with pytest.raises(subprocess.TimeoutExpired):
        smoke.run_command([sys.executable, "-c", _TRAPS_TERM, str(marker)], 2)
    assert marker.read_text() == "teardown ran"


def test_run_command_kills_child_that_ignores_sigterm(smoke, monkeypatch):
    monkeypatch.setattr(smoke, "TEARDOWN_GRACE", 1.0)
    body = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    start = __import__("time").monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        smoke.run_command([sys.executable, "-c", body], 1)
    assert __import__("time").monotonic() - start < 20


# One step lifecycle (``_smoke_lib.run_owned``) over its whole state space:
# leader behaviour x trigger x a grandchild holding the pipes in the group. In
# every cell the runner signals (``os.kill`` / ``os.killpg``) a step pid only
# while that step is still unreaped, so the signal cannot reach a reused pid or
# process group; the grandchild never outlives the step; and the outcome is the
# step's result, TimeoutExpired, or KeyboardInterrupt for a signal.
_LIFECYCLE_LEADER = """
import signal, subprocess, sys, time
marker, mode, gc = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
if gc:
    child = subprocess.Popen(["sleep", "60"])  # inherits the pipes and the group
    open(marker + ".gc", "w").write(str(child.pid))
def bye(*_):
    open(marker, "w").write("teardown ran")
    sys.exit(1)
for s in (signal.SIGINT, signal.SIGTERM):
    signal.signal(s, bye if mode == "trap" else signal.SIG_IGN)
open(marker + ".ready", "w").write("")
if mode == "exit":
    sys.exit(0)
time.sleep(60)
"""


@pytest.mark.parametrize(
    ("mode", "trigger", "gc", "want"),
    [
        ("exit", "none", False, "result"),
        ("exit", "none", True, "result"),
        ("trap", "timeout", False, "timeout"),
        ("trap", "timeout", True, "timeout"),
        ("ignore", "timeout", True, "timeout"),
        ("trap", "sigint", True, "interrupt"),
        ("trap", "sigterm", False, "interrupt"),
        ("trap", "sigterm-default", False, "interrupt"),
        ("ignore", "sigint", True, "interrupt"),
        ("ignore", "sigint+sigint", False, "interrupt"),
        ("ignore", "timeout+sigint", False, "interrupt"),
    ],
)
def test_step_lifecycle_state_space(smoke, monkeypatch, tmp_path, mode, trigger, gc, want):
    lib = smoke._smoke_lib
    grace = 1.5
    monkeypatch.setattr(smoke, "TEARDOWN_GRACE", grace)
    spawned: dict[int, subprocess.Popen] = {}
    real_popen, real_kill, real_killpg = subprocess.Popen, os.kill, os.killpg
    violations: list[str] = []

    class Recorded(real_popen):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            spawned[self.pid] = self

    def owned(what, pid):
        proc = spawned.get(abs(pid))
        if proc is not None and proc.returncode is not None:
            violations.append(f"{what}({pid}) after the step was reaped")

    def kill(pid, sig):
        owned("kill", pid)
        return real_kill(pid, sig)

    def killpg(pgid, sig):
        owned("killpg", pgid)
        return real_killpg(pgid, sig)

    monkeypatch.setattr(lib.subprocess, "Popen", Recorded)
    monkeypatch.setattr(lib.os, "kill", kill)
    monkeypatch.setattr(lib.os, "killpg", killpg)
    marker = tmp_path / "marker"
    sigs = {
        "sigint": [signal.SIGINT],
        "sigterm": [signal.SIGTERM],
        "sigterm-default": [signal.SIGTERM],
        "sigint+sigint": [signal.SIGINT, signal.SIGINT],
        "timeout+sigint": [signal.SIGINT],
    }.get(trigger, [])
    delay = 1.8 if trigger == "timeout+sigint" else 0.3  # after the 1 s timeout fired

    def fire():
        _wait_for(Path(f"{marker}.ready"))
        time.sleep(delay)
        for sig in sigs:
            real_kill(os.getpid(), sig)
            time.sleep(0.3)

    timeout = 1.0 if trigger.startswith("timeout") else 30.0
    cmd = [sys.executable, "-c", _LIFECYCLE_LEADER, str(marker), mode, "1" if gc else "0"]
    t = threading.Thread(target=fire)
    ctx = smoke._term_as_interrupt() if trigger == "sigterm" else contextlib.nullcontext()
    start = time.monotonic()
    got = None
    with ctx:
        t.start()
        try:
            got = ("result", smoke.run_command(cmd, timeout))
        except subprocess.TimeoutExpired:
            got = ("timeout", None)
        except KeyboardInterrupt:
            got = ("interrupt", None)
        finally:
            t.join()
    elapsed = time.monotonic() - start
    assert got[0] == want, got
    assert violations == []
    assert all(p.returncode is not None for p in spawned.values()), "a step was left unreaped"
    if want == "result":
        assert got[1].returncode == 0
    if mode == "trap" and want != "result":
        assert marker.read_text() == "teardown ran"
    if mode == "ignore" and want != "result":
        assert elapsed >= grace, "a second signal or the timeout cut the grace short"
    if gc:
        gpid = int(Path(f"{marker}.gc").read_text())
        deadline = time.monotonic() + 10
        while True:
            try:
                real_kill(gpid, 0)
            except ProcessLookupError:
                break
            assert time.monotonic() < deadline, f"grandchild {gpid} outlived the step"
            time.sleep(0.1)
    assert elapsed < 20


def test_owned_group_refuses_to_signal_after_the_reap(smoke):
    group = smoke._smoke_lib.OwnedGroup([sys.executable, "-c", "pass"])
    while not group.exited():
        time.sleep(0.05)
    group.kill_group()  # leader exited but unreaped: still ours
    group.reap()
    with pytest.raises(RuntimeError):
        group.kill_group()
    with pytest.raises(RuntimeError):
        group.signal_leader(signal.SIGTERM)


_TRAPS_INT = """
import signal, sys, time
signal.signal(signal.SIGINT, signal.default_int_handler)
open(sys.argv[1] + ".ready", "w").write("")
try:
    time.sleep(60)
except KeyboardInterrupt:
    open(sys.argv[1], "w").write("teardown ran")
    sys.exit(1)
"""


def _wait_for(path: Path, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.05)


def test_direct_sigterm_to_runner_reaches_the_active_step(smoke, monkeypatch, tmp_path):
    # A SIGTERM sent to the runner itself (not via the terminal's group) must
    # reach the step, which then runs its own teardown, before the runner exits.
    monkeypatch.setattr(smoke, "TEARDOWN_GRACE", 15.0)
    marker = tmp_path / "marker"

    def fire():
        _wait_for(Path(f"{marker}.ready"))
        os.kill(os.getpid(), signal.SIGTERM)

    t = threading.Thread(target=fire)
    with smoke._term_as_interrupt():
        t.start()
        with pytest.raises(KeyboardInterrupt):
            smoke.run_command([sys.executable, "-c", _TRAPS_INT, str(marker)], 60)
    t.join()
    assert marker.read_text() == "teardown ran"


def test_direct_sigint_to_runner_reaches_the_active_step(smoke, monkeypatch, tmp_path):
    # The step runs in its own session, so only a forwarded signal can reach it.
    monkeypatch.setattr(smoke, "TEARDOWN_GRACE", 15.0)
    marker = tmp_path / "marker"

    def fire():
        _wait_for(Path(f"{marker}.ready"))
        os.kill(os.getpid(), signal.SIGINT)

    t = threading.Thread(target=fire)
    t.start()
    with pytest.raises(KeyboardInterrupt):
        smoke.run_command([sys.executable, "-c", _TRAPS_INT, str(marker)], 60)
    t.join()
    assert marker.read_text() == "teardown ran"


_LEAVES_A_GRANDCHILD = """
import signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen(["sleep", "60"])
open(sys.argv[1], "w").write(str(child.pid))
time.sleep(60)
"""


def test_stopped_step_leaves_no_process_behind(smoke, monkeypatch, tmp_path):
    monkeypatch.setattr(smoke, "TEARDOWN_GRACE", 1.0)
    pidfile = tmp_path / "grandchild"
    with pytest.raises(subprocess.TimeoutExpired):
        smoke.run_command([sys.executable, "-c", _LEAVES_A_GRANDCHILD, str(pidfile)], 3)
    gpid = int(pidfile.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(gpid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise AssertionError(f"grandchild {gpid} outlived the stopped step")


def test_token_check_sigterm_removes_server_and_token_dir(smoke, monkeypatch, tmp_path):
    # The runner's timeout SIGTERMs the token-file child; the child must stop its
    # server and remove its token directory, not die with both left behind.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    real_popen = subprocess.Popen
    started = []

    def popen(argv, *a, **k):
        if "stt_server" in [str(x) for x in argv]:
            # A "server" that never listens, so the child sits in its wait loop.
            proc = real_popen(["sleep", "60"], stdin=subprocess.DEVNULL, stderr=subprocess.PIPE)
            started.append(proc)
            return proc
        return real_popen(argv, *a, **k)

    monkeypatch.setattr(smoke.subprocess, "Popen", popen)
    hit_default = []
    old = signal.signal(signal.SIGTERM, lambda *_: hit_default.append(1))

    def fire():
        deadline = time.monotonic() + 30
        while not started and time.monotonic() < deadline:
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)

    t = threading.Thread(target=fire)
    t.start()
    try:
        with pytest.raises(SystemExit):
            smoke._token_check()
    finally:
        t.join()
        signal.signal(signal.SIGTERM, old)
        for proc in started:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    assert not hit_default, "_token_check installed no SIGTERM handler"
    assert started and started[0].returncode is not None, "server left running"
    assert not list(home.glob("stt-smoke-token.*")), "token directory left behind"


# smoke_peercred.py's answer (exit, stdout) -> the peercred row: every cell.
@pytest.mark.parametrize(
    ("rc", "out", "want"),
    [
        (0, "=== summary ===\n  same-uid multi-connection: PASS\n", "PASS"),
        (0, "  skipped: needs a second local uid reachable via passwordless sudo\n", "SKIPPED"),
        (0, "skipped: peer-cred smoke needs macOS/Linux, not win32\n", "SKIPPED"),
        (1, "skipped: peer-cred smoke needs macOS/Linux, not win32\n", "FAIL"),
        (1, "  skipped: needs a second local uid reachable via passwordless sudo\n", "FAIL"),
        (1, "", "FAIL"),
    ],
)
def test_peercred_outcome_state_space(smoke, monkeypatch, rc, out, want):
    monkeypatch.setattr(smoke, "run_command", lambda *a, **k: _cp(rc, out=out))
    assert smoke.peercred_step().outcome == want


# CI value x platform -> launchd skip: CI present with ANY value (empty too) skips.
@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("ci", [None, "", "0", "false", "1", "true"])
def test_launchd_skip_state_space(smoke, platform, ci):
    import _smoke_lib

    env = {} if ci is None else {"CI": ci}
    skips = _smoke_lib.launchd_skip_reason(env, platform) is not None
    assert skips == (platform != "darwin" or ci is not None)


# The agents home is the account home, full stop: no environment variable, HOME
# included, moves it, whatever launchctl is on PATH. Tests move it only by
# patching _smoke_lib.agents_home (in-process) or through
# tests/_with_agents_home.py (a subprocess); production code has no override.
@pytest.mark.parametrize("path", ["stub", "system", "none"])
def test_agents_home_has_no_environment_override(smoke, monkeypatch, tmp_path, path):
    import _smoke_lib

    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    _exe(stub / "launchctl", "#!/bin/sh\nexit 0\n")
    empty = tmp_path / "empty"
    empty.mkdir(exist_ok=True)
    paths = {"stub": f"{stub}:/usr/bin:/bin", "system": "/usr/bin:/bin", "none": str(empty)}
    monkeypatch.setenv("PATH", paths[path])
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PIPECAT_STT_SMOKE_TEST_AGENTS_HOME", str(tmp_path))
    assert _smoke_lib.agents_home() == _smoke_lib.account_home() != tmp_path
    for script in ("_smoke_lib.py", "smoke.py", "smoke_launchd.py"):
        assert "SMOKE_TEST_AGENTS_HOME" not in (REPO_ROOT / "scripts" / script).read_text()


def test_launchd_env_sets_both_spare_labels(smoke):
    env = smoke.launchd_env({"PATH": "/usr/bin"})
    assert env["PIPECAT_STT_LABEL"] == env["KODA_STT_LABEL"] == SPARE


def test_runner_and_harness_share_spare_label_and_preconditions(smoke):
    import _smoke_lib

    assert smoke.SPARE_LABEL == _smoke_lib.SPARE_LABEL == SPARE
    assert _smoke_lib.launchd_skip_reason({}, "linux")
    assert _smoke_lib.launchd_skip_reason({"CI": "1"}, "darwin")
    assert _smoke_lib.launchd_skip_reason({}, "darwin") is None


def test_main_turns_sigterm_into_an_interrupt_while_steps_run(smoke, seam, monkeypatch, capsys):
    seen = []

    def fake_build(agents, launchd):
        seen.append(signal.getsignal(signal.SIGTERM))
        return [smoke.StepResult("supervised", smoke.PASS)]

    monkeypatch.setattr(smoke, "build_and_run", fake_build)
    before = signal.getsignal(signal.SIGTERM)
    assert smoke.main([]) == 0
    capsys.readouterr()
    assert seen and seen[0] not in (signal.SIG_DFL, signal.SIG_IGN, None, before)
    with pytest.raises(KeyboardInterrupt):
        seen[0](signal.SIGTERM, None)
    assert signal.getsignal(signal.SIGTERM) == before


# --------------------------------------------------------------------------
# Decision 8 for every step: a crash or an interrupt is a FAIL row, the table
# still prints, and an interrupt starts no further step.
# --------------------------------------------------------------------------

_STEP_FNS = {
    "supervised": "supervised_step",
    "peercred": "peercred_step",
    "token-file": "token_file_step",
    "agents": "agents_step",
    "launchd": "launchd_step",
}


def _stub_steps(smoke, monkeypatch, failing: str, exc: BaseException) -> list[str]:
    ran: list[str] = []
    for name, attr in _STEP_FNS.items():

        def step(*_a, _name=name):
            ran.append(_name)
            if _name == failing:
                raise exc
            return smoke.StepResult(_name, smoke.PASS)

        monkeypatch.setattr(smoke, attr, step)
    return ran


@pytest.mark.parametrize("failing", list(_STEP_FNS))
def test_step_crash_is_a_fail_row_and_the_run_continues(smoke, seam, monkeypatch, capsys, failing):
    ran = _stub_steps(smoke, monkeypatch, failing, OSError("boom"))
    rc = smoke.main(["--agents", "--launchd"])
    out = capsys.readouterr().out
    assert rc == 1
    assert ran == list(_STEP_FNS)
    for name in _STEP_FNS:
        want = "FAIL" if name == failing else "PASS"
        assert _outcome(_row(out, name)) == want
    assert "crashed: OSError: boom" in _row(out, failing)


def test_agents_step_crash_is_a_fail_row(smoke, seam, monkeypatch, capsys):
    # The reported path: agents_step itself raising (here: plist discovery).
    def boom():
        raise PermissionError("LaunchAgents unreadable")

    monkeypatch.setattr(smoke, "agent_plists", boom)
    rc = smoke.main(["--agents"])
    out = capsys.readouterr().out
    assert rc == 1
    assert _outcome(_row(out, "agents")) == "FAIL"
    assert "PermissionError" in _row(out, "agents")
    assert _outcome(_row(out, "launchd")) == "SKIPPED"


@pytest.mark.parametrize("failing", list(_STEP_FNS))
def test_interrupt_stops_the_run_and_still_prints_the_table(
    smoke, seam, monkeypatch, capsys, failing
):
    ran = _stub_steps(smoke, monkeypatch, failing, KeyboardInterrupt())
    rc = smoke.main(["--agents", "--launchd"])
    out = capsys.readouterr().out
    assert rc == 1
    names = list(_STEP_FNS)
    k = names.index(failing)
    assert ran == names[: k + 1], "no step may start after an interrupt"
    assert _outcome(_row(out, failing)) == "FAIL" and "interrupted" in _row(out, failing)
    for name in names[:k]:
        assert _outcome(_row(out, name)) == "PASS"
    for name in names[k + 1 :]:
        assert _outcome(_row(out, name)) == "SKIPPED"


# _signals_held(reraise) x the signal that arrives in the block -> raised?
@pytest.mark.parametrize("reraise", [False, True])
@pytest.mark.parametrize("sig", [None, signal.SIGINT, signal.SIGTERM])
def test_signals_held_state_space(smoke, reraise, sig):
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}

    def body():
        with smoke._signals_held(reraise=reraise):
            if sig is not None:
                os.kill(os.getpid(), sig)
                time.sleep(0.2)  # let the handler run inside the block

    if reraise and sig is not None:
        with pytest.raises(KeyboardInterrupt):
            body()
    else:
        body()
    assert {s: signal.getsignal(s) for s in before} == before


_IGNORES_TERM_THEN_EXITS = """
import signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open(sys.argv[1], "w").write("")
time.sleep(6)
"""


def test_signal_during_timeout_teardown_stops_the_run(smoke, monkeypatch, tmp_path):
    # The step times out; while _stop waits for its teardown, Ctrl-C hits the
    # runner. That signal must not be dropped: run_command raises
    # KeyboardInterrupt (not TimeoutExpired) once the step has stopped.
    monkeypatch.setattr(smoke, "TEARDOWN_GRACE", 30.0)
    ready = tmp_path / "ready"

    def fire():
        _wait_for(ready)
        time.sleep(3)  # the 1 s step timeout has fired; _stop is waiting
        os.kill(os.getpid(), signal.SIGINT)

    t = threading.Thread(target=fire)
    t.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            smoke.run_command([sys.executable, "-c", _IGNORES_TERM_THEN_EXITS, str(ready)], 1)
    finally:
        t.join()


_EXITS_ON_TERM_LEAVING_A_PIPE_HOLDER = """
import signal, subprocess, sys, time
child = subprocess.Popen(["sleep", "60"])  # inherits stdout/stderr: holds the pipes
open(sys.argv[1], "w").write(str(child.pid))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
time.sleep(60)
"""


def test_stop_returns_when_the_child_exits_though_a_grandchild_holds_the_pipe(
    smoke, monkeypatch, tmp_path
):
    # The step dies on SIGTERM at once, but a grandchild keeps its pipes open:
    # _stop must follow the child's exit, not wait the whole grace for EOF.
    monkeypatch.setattr(smoke, "TEARDOWN_GRACE", 30.0)
    pidfile = tmp_path / "grandchild"
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        smoke.run_command(
            [sys.executable, "-c", _EXITS_ON_TERM_LEAVING_A_PIPE_HOLDER, str(pidfile)], 2
        )
    assert time.monotonic() - start < 15
    gpid = int(pidfile.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(gpid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise AssertionError(f"grandchild {gpid} outlived the stopped step")


def test_agents_step_passes_plist_paths_byte_for_byte(smoke, agents_env, monkeypatch, capsys):
    import plistlib

    home, _log, _ran, tmp_path = agents_env
    sock = f"{tmp_path}/ odd dir /s.sock "
    with open(home / "Library" / "LaunchAgents" / "pipecat.stt-server.sp.plist", "wb") as fh:
        plistlib.dump(
            {
                "Label": "pipecat.stt-server.sp",
                "ProgramArguments": [
                    sys.executable,
                    "-m",
                    "stt_server",
                    "serve",
                    "--socket-path",
                    sock,
                ],
            },
            fh,
        )
    decodes: list[list[str]] = []

    def seam_run(cmd, timeout, env=None, **kw):
        cmd = [str(c) for c in cmd]
        if "--_decode" in cmd:
            decodes.append(cmd)
        return _cp(0)

    monkeypatch.setattr(smoke, "run_command", seam_run)
    result = smoke.agents_step(True)
    capsys.readouterr()
    assert result.outcome == "PASS", result
    assert len(decodes) == 1
    # The child verifies and connects to this exact path.
    assert decodes[0][decodes[0].index("--socket-path") + 1] == sock


def _peercred_module():
    path = REPO_ROOT / "scripts" / "smoke_peercred.py"
    spec = importlib.util.spec_from_file_location("smoke_peercred_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("case", ["platform", "no-second-uid"])
def test_peercred_skip_lines_round_trip_to_skipped(smoke, monkeypatch, capsys, case):
    # Both sides of the SKIP contract: what smoke_peercred.py actually prints on
    # each skip path, fed to the runner's mapping, must come back SKIPPED.
    import asyncio

    pc = _peercred_module()
    if case == "platform":
        monkeypatch.setattr(pc.sys, "platform", "win32")
        assert asyncio.run(pc._run(None)) == 0
    else:
        monkeypatch.setattr(pc, "_find_second_uid", lambda: None)
        assert asyncio.run(pc._run_cross_uid("/nonexistent.sock")) is False
    printed = capsys.readouterr().out
    monkeypatch.setattr(smoke, "run_command", lambda *a, **k: _cp(0, out=printed))
    assert smoke.peercred_step().outcome == "SKIPPED"


def test_peercred_skip_marks_are_shared_constants(smoke):
    import _smoke_lib

    assert smoke.PEERCRED_NO_UID_MARK is _smoke_lib.PEERCRED_NO_UID_MARK
    assert smoke.PEERCRED_PLATFORM_MARK is _smoke_lib.PEERCRED_PLATFORM_MARK
    text = (REPO_ROOT / "scripts" / "smoke_peercred.py").read_text()
    assert "PEERCRED_NO_UID_MARK" in text and "PEERCRED_PLATFORM_MARK" in text


# The harness's answer (exit, first stdout line) -> the launchd row. Its skip
# line is _smoke_lib.LAUNCHD_SKIP_MARK, printed with exit 0: SKIPPED, never PASS,
# even when the runner's own precondition check did not skip first.
@pytest.mark.parametrize(
    ("rc", "out", "want"),
    [
        (0, "SKIPPED: launchd checks not run: CI is set\n", "SKIPPED"),
        (0, "\nPASS     port collision  ok\n", "PASS"),
        (0, "", "PASS"),
        (1, "SKIPPED: launchd checks not run: CI is set\n", "FAIL"),
        (1, "FAIL     guard  refused\n", "FAIL"),
    ],
)
def test_launchd_outcome_state_space(smoke, monkeypatch, rc, out, want):
    monkeypatch.setattr(smoke, "launchd_skip_reason", lambda: None)
    monkeypatch.setattr(smoke, "run_command", lambda *a, **k: _cp(rc, out=out))
    row = smoke.launchd_step(True)
    assert row.outcome == want
    if want == "SKIPPED":
        assert row.detail == "CI is set"


def test_launchd_skip_line_round_trips_from_the_real_harness(smoke, monkeypatch, tmp_path):
    # The harness itself, with CI set: whatever it prints for a skip must map
    # to SKIPPED through the runner's own launchd_step.
    harness = REPO_ROOT / "scripts" / "smoke_launchd.py"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "CI": "1"}
    res = subprocess.run(
        [sys.executable, str(harness)], capture_output=True, text=True, timeout=60, env=env
    )
    assert res.returncode == 0, res.stderr
    monkeypatch.setattr(smoke, "launchd_skip_reason", lambda: None)
    monkeypatch.setattr(smoke, "run_command", lambda *a, **k: res)
    row = smoke.launchd_step(True)
    assert row.outcome == "SKIPPED" and "CI" in row.detail


def test_runner_and_harness_share_outcome_names(smoke):
    import _smoke_lib

    harness = importlib.util.spec_from_file_location(
        "smoke_launchd_names", REPO_ROOT / "scripts" / "smoke_launchd.py"
    )
    mod = importlib.util.module_from_spec(harness)
    sys.modules["smoke_launchd_names"] = mod  # dataclasses resolve their module here
    harness.loader.exec_module(mod)
    for name in ("PASS", "FAIL", "SKIPPED"):
        assert getattr(smoke, name) == getattr(mod, name) == getattr(_smoke_lib, name)


def test_token_fixture_passes_the_server_policy(smoke, monkeypatch, tmp_path):
    # One fixture for smoke.py's token-file child and the launchd harness: the
    # file it writes must pass stt_server.auth's own policy.
    sys.path.insert(0, str(REPO_ROOT))
    from stt_server.auth import load_token_file

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    directory, token_file = smoke._smoke_lib.write_policy_token_file(home, "t-")
    try:
        resolved, token = load_token_file(str(token_file), uid=os.getuid())
        assert token and Path(resolved).resolve() == token_file.resolve()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# run_owned re-delivers a held SIGINT/SIGTERM on every path: raise point
# (none, OwnedGroup construction, the exit wait) x signal -> the handler that
# was in place gets the signal exactly once, and a raised error still surfaces.
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("raise_at", ["none", "construct", "exited"])
def test_run_owned_redelivers_a_held_signal_on_every_path(smoke, monkeypatch, raise_at, sig):
    lib = smoke._smoke_lib
    got: list[int] = []
    real_group = lib.OwnedGroup

    class Group(real_group):
        def __init__(self, *a, **k):
            os.kill(os.getpid(), sig)  # arrives while held
            if raise_at == "construct":
                raise OSError("cannot start")
            super().__init__(*a, **k)

        def exited(self):
            if raise_at == "exited":
                raise RuntimeError("wait failed")
            return super().exited()

    monkeypatch.setattr(lib, "OwnedGroup", Group)
    old = {
        s: signal.signal(s, lambda n, _f: got.append(n)) for s in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        if raise_at == "none":
            res, timed_out = lib.run_owned([sys.executable, "-c", "pass"], 30, grace=1)
            assert res.returncode is not None and not timed_out
        else:
            want = OSError if raise_at == "construct" else RuntimeError
            with pytest.raises(want):
                lib.run_owned([sys.executable, "-c", "pass"], 30, grace=1)
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    assert got == [sig]


# INPUT reaches the child's stdin (a secret travels this way, never on argv);
# without it stdin is /dev/null.
@pytest.mark.parametrize(("payload", "want"), [(b"s3cret\n", "s3cret\n"), (None, "")])
def test_run_owned_input_reaches_stdin_only(smoke, payload, want):
    cmd = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]
    res, _ = smoke._smoke_lib.run_owned(cmd, 30, input=payload)
    assert res.stdout == want
    assert all("s3cret" not in a for a in cmd)


# The group kill's outcome x whether the leader is still running -> run_owned
# returns the result only when the group is provably stopped: the SIGKILL
# landed (or the group is gone: ESRCH; or macOS's EPERM for a zombie-only
# group, accepted only once the leader exited) AND the leader was reaped.
# Anything else is GroupNotStopped, never a result.
@pytest.mark.parametrize(
    ("kill", "leader", "want"),
    [
        ("real", "exits", "result"),
        ("esrch", "exits", "result"),
        ("eperm", "exits", "result"),
        ("einval", "exits", "not-stopped"),
        ("eperm", "runs", "not-stopped"),
        ("noop", "runs", "not-stopped"),  # killpg "succeeds" but the leader survives
        ("real", "runs", "result"),
    ],
)
def test_run_owned_reports_a_failed_group_kill(smoke, monkeypatch, kill, leader, want):
    import errno

    lib = smoke._smoke_lib
    real_killpg = os.killpg
    spawned: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    class Recorded(real_popen):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            spawned.append(self)

    def killpg(pgid, sig):
        if kill == "real":
            return real_killpg(pgid, sig)
        if kill == "noop":
            return None
        code = {"esrch": errno.ESRCH, "eperm": errno.EPERM, "einval": errno.EINVAL}[kill]
        raise OSError(code, os.strerror(code))  # OSError maps the errno to its subclass

    monkeypatch.setattr(lib.subprocess, "Popen", Recorded)
    monkeypatch.setattr(lib.os, "killpg", killpg)
    body = "pass" if leader == "exits" else "import time; time.sleep(30)"
    try:
        if want == "result":
            res, _ = lib.run_owned([sys.executable, "-c", body], 0.5, timeout_signal=None)
            assert res.returncode is not None
        else:
            with pytest.raises(lib.GroupNotStopped):
                lib.run_owned([sys.executable, "-c", body], 0.5, timeout_signal=None)
    finally:
        for p in spawned:
            if p.returncode is None:
                with contextlib.suppress(OSError):
                    real_killpg(p.pid, signal.SIGKILL)
                p.wait(10)


# --agents reads the plist port as the server parses it: recorded value ->
# the canonical port it decodes through, or that agent's error (no decode).
@pytest.mark.parametrize(
    ("value", "want"),
    [("9998", "9998"), ("+9998", "9998"), ("09998", "9998"), ("abc", None), ("", None)],
)
def test_read_endpoint_parses_the_port_the_server_way(smoke, tmp_path, value, want):
    import plistlib

    plist = tmp_path / "pipecat.stt-server.x.plist"
    args = ["/py", "-m", "stt_server", "serve", "--host", "127.0.0.1", "--port", value]
    plist.write_bytes(plistlib.dumps({"ProgramArguments": args}))
    endpoint, token, err = smoke.read_endpoint(plist)
    if want is None:
        assert endpoint is None and token is None and "--port" in err
    else:
        assert err == "" and endpoint["port"] == want


# --agents reads an agent's token file through the server's own policy
# (stt_server.auth.load_token_file), never with a plain open(): what is at
# --auth-token-file -> the token, or that agent's error (nothing sent). A FIFO
# is refused at once (O_NONBLOCK + fstat), never a blocked runner; so is a FIFO
# where the plist itself should be.
@pytest.mark.parametrize(
    ("kind", "want"),
    [
        ("good", "s3cret"),
        ("group-readable", None),
        ("symlink", None),
        ("fifo", None),
        ("empty", None),
        ("inner-space", None),
        ("missing", None),
        ("relative", None),
        ("plist-fifo", None),
    ],
)
def test_read_endpoint_token_file_uses_the_server_policy(smoke, tmp_path, kind, want):
    import plistlib

    d = tmp_path / "tokdir"
    d.mkdir(mode=0o700)
    tok = d / "tok"
    if kind in ("good", "group-readable", "symlink"):
        tok.write_text("s3cret\n")
        tok.chmod(0o640 if kind == "group-readable" else 0o600)
    elif kind == "empty":
        tok.write_text("\n")
        tok.chmod(0o600)
    elif kind == "inner-space":
        tok.write_text("s3 cret\n")
        tok.chmod(0o600)
    elif kind == "fifo":
        os.mkfifo(tok, 0o600)
    path = str(tok)
    if kind == "symlink":
        (d / "link").symlink_to(tok)
        path = str(d / "link")
    elif kind == "relative":
        path = "tokdir/tok"
    plist = tmp_path / "pipecat.stt-server.x.plist"
    args = ["/py", "serve", "--host", "127.0.0.1", "--port", "9565", "--auth-token-file", path]
    if kind == "plist-fifo":
        os.mkfifo(plist, 0o600)
    else:
        plist.write_bytes(plistlib.dumps({"ProgramArguments": args}))
    got: list = []
    t = threading.Thread(target=lambda: got.append(smoke.read_endpoint(plist)), daemon=True)
    t.start()
    t.join(10)
    assert got, "read_endpoint blocked"
    endpoint, token, err = got[0]
    if want is None:
        assert endpoint is None and token is None and err
        assert "s3" not in err
    else:
        assert (token, err) == (want, "")


# The launchd step's timeout and grace ARE the harness's own budget for the
# installed agents (no copied number): agent count -> (timeout, grace) handed
# to run_command, and the grace covers the harness's whole teardown.
@pytest.mark.parametrize("n_agents", [0, 1, 4])
def test_launchd_step_uses_the_harness_budget(smoke, seam, monkeypatch, n_agents):
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import smoke_launchd

    for i in range(n_agents):
        _render_plist(seam.home, f"pipecat.stt-server.a{i}", port=9100 + i)
    got = {}

    def fake(cmd, timeout, env=None, **kw):
        got.update(timeout=timeout, grace=kw.get("grace"))
        return _cp(0)

    monkeypatch.setattr(smoke, "run_command", fake)
    monkeypatch.setattr(smoke, "launchd_skip_reason", lambda: None)
    smoke.launchd_step(True)
    assert got == {
        "timeout": smoke_launchd.run_budget(n_agents),
        "grace": smoke_launchd.teardown_budget(n_agents),
    }
    assert got["grace"] >= (
        smoke._smoke_lib.GROUP_DRAIN
        + smoke_launchd.INSTALL_TIMEOUT
        + smoke_launchd.SOCKET_LOCK_WAIT
        + smoke_launchd.snapshot_bound(n_agents)
    )
    assert got["timeout"] > got["grace"]
