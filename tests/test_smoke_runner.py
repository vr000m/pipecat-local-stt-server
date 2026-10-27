"""Stubbed tests for ``scripts/smoke.py`` (plan Phase 3, Decisions 7-9).

Every step goes through the ``smoke.run_command`` seam, which these tests
monkeypatch; no real smoke script, installer or ``launchctl`` is ever run.
Where a real process is needed (the ``--agents`` step) it is a stub executable
on a PATH led by a stub dir, with a temp ``HOME``; the fixture asserts the stub
``launchctl`` resolves first and pops ``CI`` unless a test sets it.

Assumptions (contract is silent): the table prints one line per step holding
the step name and its outcome word; ``--agents`` discovers plists under
``$HOME/Library/LaunchAgents`` (``Path.home()``/``expanduser`` honour ``HOME``);
commands whose argv[0] is not a stubbed tool (launchctl/id/uv/say/afconvert/
lsof) are faked as success in the ``--agents`` test, so only the stubbed tools
ever run for real.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
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
                'if [[ "$1" == "print" ]]; then printf "state = running\\n\\tpid = 4242\\n"; fi\n'
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


def test_agents_read_only_with_tcp_and_socket_plists(smoke, agents_env, capsys):
    home, log, ran, tmp_path = agents_env
    _render_plist(home, "pipecat.stt-server", port=9565)
    _render_plist(home, "pipecat.stt-server.parakeet", port=9765, backend="parakeet")
    sock = tmp_path / "s.sock"
    _render_plist(home, "pipecat.stt-server.sock", socket_path=sock)
    smoke.main(["--agents"])
    capsys.readouterr()
    lines = _argv(log)
    assert lines, "agents step should have consulted launchctl/uv stubs"
    for ln in lines:
        tool, _, rest = ln.partition(" ")
        if tool == "launchctl":
            assert rest.split()[0] in {"print", "list", "print-disabled"}, ln
        assert not any(w in ln for w in FORBIDDEN), ln
        if tool == "uv" and rest.startswith("run"):
            assert "--no-sync" in rest, ln
    for cmd in ran:
        joined = " ".join(cmd)
        assert not any(w in joined for w in FORBIDDEN), cmd
        if Path(cmd[0]).name == "uv" and "run" in cmd:
            assert "--no-sync" in cmd, cmd
        assert Path(cmd[0]).name != "launchctl" or cmd[1] in {"print", "list", "print-disabled"}


def test_agents_skipped_when_say_absent(smoke, monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
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
