"""Stubbed tests for ``scripts/smoke_launchd.py`` (plan Phase 2).

Nothing here touches real launchd. Every subprocess runs with ``PATH`` led by a
stub dir (``launchctl``, ``id``, ``lsof``) and a temp ``HOME``; the fixture
asserts the stub ``launchctl`` is the one resolved first. The real installer
runs against the stubs, as in ``tests/test_install_migration.py``.

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

FAKE_UID = "501"
SPARE = "pipecat.stt-server.smoke"
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
        _exe(
            self.stub / "launchctl",
            f"""#!/usr/bin/env bash
printf 'launchctl %s\\n' "$*" >> {self.log!s}
if [[ "$1" == "print" ]]; then
    if [[ -e {self.loaded_marker!s} && "$2" == *"{SPARE}" ]]; then exit 0; fi
    exit 1
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

    def run(self, args: list[str], env: dict[str, str], timeout: int = 180):
        assert env["PATH"].split(":")[0] == str(self.stub)
        assert "CI" not in env
        return subprocess.run(
            [str(VENV_PYTHON), str(HARNESS), *args],
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
    snap = smoke.snapshot_real_agents()
    assert SPARE not in snap
    assert "pipecat.stt-server" in snap


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


@pytest.mark.parametrize(
    ("plant", "expect"),
    [
        (_plant_plist, f"rm -f {{home}}/Library/LaunchAgents/{SPARE}.plist"),
        (_plant_loaded, f"launchctl bootout gui/{FAKE_UID}/{SPARE}"),
        (_plant_socket, "rm -f {sock}"),
        (_plant_lock, "rm -f {sock}.lock"),
    ],
)
def test_start_state_refusal(env, plant, expect):
    # The harness fixes its spare socket under $HOME; it does not read
    # PIPECAT_STT_SOCKET, so a stray variable cannot redirect it.
    sock = env.home / "Library" / "Caches" / "pipecat-stt-smoke-launchd" / "spare.sock"
    plant(env, sock)
    r = env.run([], env.spare_env())
    assert r.returncode == 1, (r.stdout, r.stderr)
    out = r.stdout + r.stderr
    assert expect.format(home=env.home, sock=sock) in out
    # Print-only: no bootout/bootstrap actually issued, no installer run.
    assert _mutating(env.argv()) == []


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
    _assert_torn_down(env)


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
