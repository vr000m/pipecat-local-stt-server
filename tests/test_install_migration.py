"""Executable migration test for ``scripts/install_stt_agent.sh install``.

Phase 2 of the de-brand plan bakes a v0.1.x upgrade path into the install
entrypoint: when the resolved ``LABEL`` is the new default
``pipecat.stt-server``, the script must retire BOTH legacy LaunchAgents
(``koda.stt-server`` and ``koda.stt-server.parakeet``) — ``launchctl bootout``
each and ``rm`` its plist — *before* bootstrapping the renamed agent, so
launchd never double-runs the old and new defaults.

These tests run the real script as a subprocess against a temporary ``HOME``
with stub ``launchctl`` and ``id`` executables prepended to ``PATH``. The stubs
record every argv to a log file, so we can assert exactly which
``bootout``/``bootstrap`` invocations fired without touching the real launchd
domain or ``~/Library``.

Asserted contracts (plan Phase 2 + Acceptance Criteria):

1. Default-label install boots out ``gui/<uid>/koda.stt-server`` AND
   ``gui/<uid>/koda.stt-server.parakeet`` when their legacy plists are present.
2. Fresh machine (no legacy plist) -> migration is a silent no-op, exit 0.
3. The new label is never passed to ``bootout`` *before* ``bootstrap`` (the
   migration must not retire the agent it is about to install).
4. A non-default custom label (``PIPECAT_STT_LABEL=pipecat.stt-server.test``)
   boots out NEITHER legacy label.
5. ``KODA_STT_SOCKET`` / ``KODA_STT_LOG_DIR`` env aliases override the new
   ``pipecat-stt`` defaults (shell-only alias resolution).
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# Import the Nemotron default from its single source of truth: the installer's
# DEFAULT_MODEL nemotron arm must agree with this constant, and pinning the
# expected value to the literal here would let a backend rename pass both the
# script and this regression against a stale id. nemotron.py imports mlx_audio
# only lazily, so this top-level import does not pull the optional package.
from stt_server.backends.nemotron import DEFAULT_NEMOTRON_MODEL as NEMOTRON_MODEL

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "install_stt_agent.sh"
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

# The fake uid our ``id`` stub reports; used to build the expected
# ``gui/<uid>/<label>`` launchctl service targets.
FAKE_UID = "501"

LEGACY_LABELS = ("koda.stt-server", "koda.stt-server.parakeet")
NEW_LABEL = "pipecat.stt-server"


pytestmark = pytest.mark.skipif(
    not VENV_PYTHON.exists(),
    reason=(
        f"{VENV_PYTHON} not found — install_stt_agent.sh exits early without the "
        "project venv interpreter; run 'uv sync' to make the migration test runnable"
    ),
)


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _make_stub_dir(tmp_path: Path) -> tuple[Path, Path]:
    """Create a stub-bin dir with ``launchctl`` and ``id`` that log their argv.

    Returns ``(stub_dir, argv_log)``. Every invocation appends one line of the
    form ``<prog> <args...>`` to ``argv_log``. ``launchctl`` always exits 0 so
    the script's ``bootout ... || true`` and ``bootstrap`` both succeed without
    touching the real launchd domain. ``id -u`` echoes a fixed uid so the
    recorded service targets are deterministic.
    """
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir()
    argv_log = tmp_path / "argv.log"

    # launchctl: record argv, succeed. `print` must return non-zero so the
    # migration's presence check falls through to the plist-file test (mirrors a
    # machine where the legacy agent is not currently loaded but its plist
    # remains on disk).
    _write_executable(
        stub_dir / "launchctl",
        f"""#!/usr/bin/env bash
printf 'launchctl %s\\n' "$*" >> {argv_log!s}
if [[ "$1" == "print" ]]; then
    exit 1
fi
exit 0
""",
    )

    # id: record argv, emit the fixed uid for `id -u`, otherwise defer to the
    # real id so anything else still works.
    _write_executable(
        stub_dir / "id",
        f"""#!/usr/bin/env bash
printf 'id %s\\n' "$*" >> {argv_log!s}
if [[ "$1" == "-u" ]]; then
    printf '{FAKE_UID}\\n'
    exit 0
fi
exec /usr/bin/id "$@"
""",
    )

    return stub_dir, argv_log


def _run_install(
    tmp_path: Path,
    stub_dir: Path,
    *,
    env_overrides: dict[str, str] | None = None,
    cmd: str = "install",
) -> subprocess.CompletedProcess:
    """Run ``install_stt_agent.sh <cmd>`` (default ``install``) in a hermetic temp ``HOME``.

    PATH is the stub dir prepended to the real PATH so ``launchctl``/``id``
    resolve to the stubs while ``bash``/``python`` still resolve normally.
    """
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)

    env = {
        "HOME": str(home),
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
    }
    if env_overrides:
        env.update(env_overrides)

    return subprocess.run(
        ["bash", str(SCRIPT), cmd],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(REPO_ROOT),
    )


def _create_legacy_plists(home: Path, labels=LEGACY_LABELS) -> None:
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    for label in labels:
        (agents / f"{label}.plist").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<plist><dict/></plist>\n'
        )


def _bootout_targets(argv_log: Path) -> list[str]:
    """Return the service targets passed to every ``launchctl bootout`` call."""
    if not argv_log.exists():
        return []
    targets: list[str] = []
    for line in argv_log.read_text().splitlines():
        parts = line.split()
        # lines look like: launchctl bootout gui/<uid>/<label>
        if len(parts) >= 3 and parts[0] == "launchctl" and parts[1] == "bootout":
            targets.append(parts[2])
    return targets


def _argv_lines(argv_log: Path) -> list[str]:
    return argv_log.read_text().splitlines() if argv_log.exists() else []


# ---------------------------------------------------------------------------
# (1) Default-label install retires BOTH legacy agents
# ---------------------------------------------------------------------------


def test_default_install_boots_out_both_legacy_agents(tmp_path: Path):
    """With both legacy plists present, a default-label install must bootout
    ``gui/<uid>/koda.stt-server`` AND ``gui/<uid>/koda.stt-server.parakeet``."""
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    _create_legacy_plists(tmp_path / "home")

    r = _run_install(tmp_path, stub_dir)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"

    targets = _bootout_targets(argv_log)
    for legacy in LEGACY_LABELS:
        assert f"gui/{FAKE_UID}/{legacy}" in targets, (
            f"default install must bootout legacy agent {legacy}; got bootout targets {targets}"
        )


def test_default_install_removes_legacy_plists(tmp_path: Path):
    """The migration must ``rm`` each legacy ``*.plist`` after booting it out."""
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    _create_legacy_plists(home)

    r = _run_install(tmp_path, stub_dir)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"

    agents = home / "Library" / "LaunchAgents"
    for legacy in LEGACY_LABELS:
        assert not (agents / f"{legacy}.plist").exists(), (
            f"legacy plist {legacy}.plist must be removed by the migration"
        )


def test_default_install_emits_migration_notice(tmp_path: Path):
    """A one-line notice must be emitted for each retired legacy agent."""
    stub_dir, _ = _make_stub_dir(tmp_path)
    _create_legacy_plists(tmp_path / "home")

    r = _run_install(tmp_path, stub_dir)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"

    out = r.stdout
    for legacy in LEGACY_LABELS:
        assert legacy in out, f"expected a migration notice mentioning {legacy}; stdout={out!r}"


# ---------------------------------------------------------------------------
# (2) Fresh machine: no legacy plist -> migration no-op, exit 0
# ---------------------------------------------------------------------------


def test_fresh_machine_install_is_noop_and_exits_zero(tmp_path: Path):
    """With no legacy plists present (and the stub ``launchctl print`` failing),
    the migration retires nothing and the install still exits 0.

    The script issues ``launchctl bootout ... 2>/dev/null || true`` for each
    legacy label unconditionally — that is a *harmless no-op* against launchd
    when nothing is loaded — so the observable "no-op" contract is: exit 0, no
    migration notice emitted, and no plist removed (there were none to remove).
    """
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    # Deliberately do NOT create any legacy plists.

    r = _run_install(tmp_path, stub_dir)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"

    # No migration notice should have been printed — the guard (print OR plist
    # exists) is false on a fresh machine, so nothing is reported as retired.
    assert "migrating" not in r.stdout, (
        f"fresh machine must not emit a migration notice; stdout={r.stdout!r}"
    )

    # No legacy plist exists or was created — the migration touched nothing.
    agents = home / "Library" / "LaunchAgents"
    for legacy in LEGACY_LABELS:
        assert not (agents / f"{legacy}.plist").exists(), (
            f"fresh machine must not have a {legacy}.plist"
        )


# ---------------------------------------------------------------------------
# (3) The new label is never booted out BEFORE bootstrap
# ---------------------------------------------------------------------------


def test_new_label_not_booted_out_before_bootstrap(tmp_path: Path):
    """The migration must not retire the new agent: the first time the new
    label appears as a ``bootout`` target it must be the install's own
    idempotency bootout that immediately precedes ``bootstrap`` — never the
    migration block (which runs before bootstrap)."""
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    _create_legacy_plists(tmp_path / "home")

    r = _run_install(tmp_path, stub_dir)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"

    # Consider only the launchctl calls; ``id -u`` lines are interleaved by the
    # shell's ``$(id -u)`` substitution and are irrelevant to ordering.
    lc = [ln for ln in _argv_lines(argv_log) if ln.startswith("launchctl ")]
    new_target = f"gui/{FAKE_UID}/{NEW_LABEL}"

    bootstrap_idx = next(
        (i for i, ln in enumerate(lc) if ln.startswith("launchctl bootstrap")),
        None,
    )
    assert bootstrap_idx is not None, f"expected a launchctl bootstrap call; lines={lc}"

    # The new label's only bootout must be the idempotency unload that
    # immediately precedes bootstrap — never the migration loop (which runs
    # before, and is separated from, the new agent's own bootout).
    new_bootout_indices = [i for i, ln in enumerate(lc) if ln == f"launchctl bootout {new_target}"]
    # Only `print` polls of the new label (wait for teardown) may sit between the
    # bootout and the bootstrap.
    expect_idx = bootstrap_idx - 1
    while expect_idx > 0 and lc[expect_idx] == f"launchctl print {new_target}":
        expect_idx -= 1
    assert new_bootout_indices == [expect_idx], (
        "the new label's only bootout must be the idempotency unload directly "
        f"before bootstrap; new-label bootout indices={new_bootout_indices}, "
        f"bootstrap_idx={bootstrap_idx}, launchctl lines={lc}"
    )

    # The migration's legacy bootouts must all precede the new agent's
    # pre-bootstrap unload, proving the new agent is never retired by migration.
    for i, ln in enumerate(lc):
        if any(ln == f"launchctl bootout gui/{FAKE_UID}/{legacy}" for legacy in LEGACY_LABELS):
            assert i < bootstrap_idx - 1, (
                f"legacy bootout at index {i} must come before the new agent's "
                f"pre-bootstrap unload at {bootstrap_idx - 1}; lines={lc}"
            )


# ---------------------------------------------------------------------------
# (4) Custom (non-default) label install retires NEITHER legacy agent
# ---------------------------------------------------------------------------


def test_custom_label_install_does_not_retire_legacy_agents(tmp_path: Path):
    """A non-default ``PIPECAT_STT_LABEL`` must not trigger the migration: even
    with both legacy plists present, neither legacy label is booted out and the
    legacy plists are left untouched."""
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    _create_legacy_plists(home)

    custom_label = "pipecat.stt-server.test"
    r = _run_install(
        tmp_path,
        stub_dir,
        env_overrides={
            "PIPECAT_STT_LABEL": custom_label,
            # Keep the socket distinct so nothing collides with the default agent.
            "PIPECAT_STT_SOCKET": str(home / "Library" / "Caches" / "pipecat-stt" / "test.sock"),
        },
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"

    targets = _bootout_targets(argv_log)
    for legacy in LEGACY_LABELS:
        assert f"gui/{FAKE_UID}/{legacy}" not in targets, (
            f"custom-label install must NOT retire legacy agent {legacy}; targets={targets}"
        )

    # The legacy plists must survive an unrelated custom-label install.
    agents = home / "Library" / "LaunchAgents"
    for legacy in LEGACY_LABELS:
        assert (agents / f"{legacy}.plist").exists(), (
            f"custom-label install must leave legacy plist {legacy}.plist in place"
        )

    # The custom agent itself is still bootstrapped under its own label.
    assert f"gui/{FAKE_UID}/{custom_label}" in targets or any(
        custom_label in ln for ln in _argv_lines(argv_log)
    ), f"custom label {custom_label} should still be managed; argv={_argv_lines(argv_log)}"


# ---------------------------------------------------------------------------
# (5) KODA_STT_SOCKET / KODA_STT_LOG_DIR override the new pipecat-stt defaults
# ---------------------------------------------------------------------------


def test_koda_socket_and_log_dir_env_override_new_defaults(tmp_path: Path):
    """The deprecated ``KODA_STT_SOCKET`` / ``KODA_STT_LOG_DIR`` aliases must
    still override the new ``pipecat-stt`` defaults.

    Proof: the install renders the plist into ``HOME`` via the real renderer
    and creates ``LOG_DIR`` / the socket's parent dir. With the koda-named
    overrides set, those directories must be created at the OVERRIDE paths and
    the rendered plist must reference the override socket — never the
    ``pipecat-stt`` defaults.
    """
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)

    override_log_dir = home / "custom-logs"
    override_socket = home / "custom-cache" / "stt.sock"

    r = _run_install(
        tmp_path,
        stub_dir,
        env_overrides={
            "KODA_STT_LOG_DIR": str(override_log_dir),
            "KODA_STT_SOCKET": str(override_socket),
        },
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"

    # The script creates LOG_DIR and the socket's parent dir at the override
    # paths — the default pipecat-stt dirs must NOT be created.
    assert override_log_dir.is_dir(), (
        f"KODA_STT_LOG_DIR override must be honoured; {override_log_dir} not created"
    )
    assert override_socket.parent.is_dir(), (
        f"KODA_STT_SOCKET override parent must be honoured; {override_socket.parent} not created"
    )
    assert not (home / "Library" / "Logs" / "pipecat-stt").exists(), (
        "default pipecat-stt log dir must not be created when KODA_STT_LOG_DIR is set"
    )
    assert not (home / "Library" / "Caches" / "pipecat-stt").exists(), (
        "default pipecat-stt cache dir must not be created when KODA_STT_SOCKET is set"
    )

    # The summary the script prints must echo the override paths.
    assert str(override_socket) in r.stdout, (
        f"install summary must report the override socket; stdout={r.stdout!r}"
    )
    assert str(override_log_dir) in r.stdout, (
        f"install summary must report the override log dir; stdout={r.stdout!r}"
    )

    # The rendered plist (default label) must reference the override socket.
    rendered = home / "Library" / "LaunchAgents" / f"{NEW_LABEL}.plist"
    assert rendered.is_file(), f"expected rendered plist at {rendered}"
    import plistlib

    plist = plistlib.loads(rendered.read_bytes())
    assert str(override_socket) in plist["ProgramArguments"], (
        f"rendered plist must use the KODA_STT_SOCKET override; args={plist['ProgramArguments']}"
    )


# ---------------------------------------------------------------------------
# (6) Backend-aware DEFAULT_MODEL: nemotron installs the Nemotron repo id,
#     never the Whisper default
# ---------------------------------------------------------------------------

WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"


def test_nemotron_backend_install_uses_nemotron_default_model(tmp_path: Path):
    """With ``PIPECAT_STT_BACKEND=nemotron`` and ``PIPECAT_STT_MODEL`` unset,
    the installer's backend-aware ``DEFAULT_MODEL`` must resolve to the Nemotron
    repo id — NOT the Whisper default that the ``else`` arm would yield.

    Proof: the install renders the plist into ``HOME`` via the real renderer;
    the rendered ``ProgramArguments`` must carry the Nemotron model after the
    ``--model`` flag.
    """
    import plistlib

    stub_dir, _ = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)

    r = _run_install(
        tmp_path,
        stub_dir,
        env_overrides={"PIPECAT_STT_BACKEND": "nemotron"},
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"

    rendered = home / "Library" / "LaunchAgents" / f"{NEW_LABEL}.plist"
    assert rendered.is_file(), f"expected rendered plist at {rendered}"
    args = plistlib.loads(rendered.read_bytes())["ProgramArguments"]

    assert "nemotron" in args, f"backend should be nemotron; args={args}"
    assert args[args.index("--model") + 1] == NEMOTRON_MODEL, (
        f"nemotron install must use the Nemotron default model, not Whisper; args={args}"
    )
    assert WHISPER_MODEL not in args, (
        f"nemotron install must NOT fall back to the Whisper default; args={args}"
    )


# ---------------------------------------------------------------------------
# (7) Socket-path validation runs BEFORE any filesystem mutation. A custom
#     PIPECAT_STT_SOCKET the server would reject (outside $HOME, or with a
#     symlinked parent) must fail the install cleanly with NO mkdir/chmod —
#     never tighten the operator's directory to 0700 and then crash on startup.
# ---------------------------------------------------------------------------


def test_install_refuses_socket_outside_home_without_mutating(tmp_path: Path):
    """A socket path outside $HOME is rejected before any filesystem change:
    exit 1, an actionable 'not under the trusted root' error, and the operator's
    pre-existing directory keeps its original (loose) permissions."""
    stub_dir, _ = _make_stub_dir(tmp_path)

    # An operator-owned dir OUTSIDE the hermetic HOME, deliberately 0755.
    outside = tmp_path / "outside"
    outside.mkdir()
    os.chmod(outside, 0o755)
    socket = outside / "stt.sock"

    r = _run_install(
        tmp_path,
        stub_dir,
        env_overrides={"PIPECAT_STT_SOCKET": str(socket)},
    )

    assert r.returncode == 1, f"expected exit 1; stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "not under the trusted root" in r.stderr, (
        f"expected an actionable under-$HOME error; stderr={r.stderr!r}"
    )
    # The load-bearing assertion: the install must NOT have chmod'd the
    # operator's directory before refusing.
    mode = stat.S_IMODE((tmp_path / "outside").stat().st_mode)
    assert mode == 0o755, f"install tightened an unsupported dir to {oct(mode)}; must not mutate"
    # The default log/cache dirs must not have been created either (validation
    # runs first).
    assert not (tmp_path / "home" / "Library" / "Logs" / "pipecat-stt").exists(), (
        "install must not create the log dir before validating the socket path"
    )


def test_install_refuses_symlinked_socket_parent_without_mutating(tmp_path: Path):
    """A socket whose parent is a symlink is rejected before any filesystem
    change: exit 1, a 'symlink' error, and the symlink's target keeps its
    original permissions (no chmod 700 fired through the link)."""
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)

    # A real dir under $HOME, deliberately 0755, and a symlink pointing at it.
    realdir = home / "realdir"
    realdir.mkdir()
    os.chmod(realdir, 0o755)
    link = home / "link"
    link.symlink_to(realdir)
    socket = link / "stt.sock"  # client-visible path traverses the symlink

    r = _run_install(
        tmp_path,
        stub_dir,
        env_overrides={"PIPECAT_STT_SOCKET": str(socket)},
    )

    assert r.returncode == 1, f"expected exit 1; stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "symlink" in r.stderr.lower(), (
        f"expected an actionable symlink-refusal error; stderr={r.stderr!r}"
    )
    # The symlink and its target must be untouched — the chmod 700 must not have
    # fired through the link onto the shared target.
    assert link.is_symlink(), "the socket-parent symlink must be left in place"
    mode = stat.S_IMODE(realdir.stat().st_mode)
    assert mode == 0o755, f"install chmod'd the symlink target to {oct(mode)}; must not mutate"


# ---------------------------------------------------------------------------
# (8) Loopback-TCP default transport (plan Phase 2). ``install`` with no
#     transport env yields a TCP agent on the backend's canonical port;
#     ``PIPECAT_STT_SOCKET`` opts back into the socket agent.
# ---------------------------------------------------------------------------

SOCKET_SNAPSHOT = REPO_ROOT / "tests" / "snapshots" / "pipecat-stt.plist"
KYUTAI_DEFAULT_PYTHON = REPO_ROOT / ".venv-kyutai" / "bin" / "python"

# backend -> canonical port (kyutai/echo need an explicit model: the model
# default is not under test here and the renderer requires a valid one).
CANONICAL_PORTS = {"mlx": 9565, "parakeet": 9765, "nemotron": 9865, "kyutai": 9665}


def _agents(home: Path) -> Path:
    return home / "Library" / "LaunchAgents"


def _rendered(home: Path, label: str = NEW_LABEL) -> dict:
    path = _agents(home) / f"{label}.plist"
    assert path.is_file(), f"expected rendered plist at {path}"
    return plistlib.loads(path.read_bytes())


def _flag(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def _stub_python(tmp_path: Path) -> Path:
    """A stand-in interpreter path that delegates to the real test interpreter.

    The installer runs ``"$PYTHON" scripts/render_stt_plist.py``, so the stub
    must actually execute; the rendered plist still records the stub path.
    """
    py = tmp_path / "fakepy" / "python"
    py.parent.mkdir(exist_ok=True)
    _write_executable(py, f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
    return py


def _backend_env(backend: str, tmp_path: Path) -> dict[str, str]:
    env = {"PIPECAT_STT_BACKEND": backend}
    if backend == "kyutai":
        env["PIPECAT_STT_PYTHON"] = str(_stub_python(tmp_path))
        env["PIPECAT_STT_MODEL"] = "kyutai/stt-test"
    return env


@pytest.mark.parametrize("backend", sorted(CANONICAL_PORTS))
def test_default_install_is_tcp_on_canonical_port(tmp_path: Path, backend: str):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    r = _run_install(tmp_path, stub_dir, env_overrides=_backend_env(backend, tmp_path))
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"

    args = _rendered(home)["ProgramArguments"]
    assert _flag(args, "--host") == "127.0.0.1"
    assert _flag(args, "--port") == str(CANONICAL_PORTS[backend])
    assert "--socket-path" not in args
    assert not (home / "Library" / "Caches" / "pipecat-stt").exists(), (
        "TCP install must not create the socket cache dir"
    )


def test_explicit_port_and_host_override_defaults(tmp_path: Path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    r = _run_install(
        tmp_path,
        stub_dir,
        env_overrides={"PIPECAT_STT_PORT": "9999", "PIPECAT_STT_HOST": "localhost"},
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    args = _rendered(tmp_path / "home")["ProgramArguments"]
    assert _flag(args, "--host") == "localhost"
    assert _flag(args, "--port") == "9999"


def test_non_loopback_host_fails_without_plist(tmp_path: Path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    r = _run_install(tmp_path, stub_dir, env_overrides={"PIPECAT_STT_HOST": "0.0.0.0"})
    assert r.returncode != 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert not (_agents(tmp_path / "home") / f"{NEW_LABEL}.plist").exists()


def test_echo_backend_requires_explicit_port(tmp_path: Path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    r = _run_install(tmp_path, stub_dir, env_overrides={"PIPECAT_STT_BACKEND": "echo"})
    assert r.returncode != 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "PIPECAT_STT_PORT" in (r.stderr + r.stdout)
    assert not (_agents(tmp_path / "home") / f"{NEW_LABEL}.plist").exists()


def test_echo_backend_with_explicit_port_works(tmp_path: Path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    r = _run_install(
        tmp_path,
        stub_dir,
        env_overrides={"PIPECAT_STT_BACKEND": "echo", "PIPECAT_STT_PORT": "9123"},
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    args = _rendered(tmp_path / "home")["ProgramArguments"]
    assert _flag(args, "--port") == "9123"
    assert _flag(args, "--backend") == "echo"


def _normalise(obj, home: Path):
    """Map real install paths onto the snapshot's fixed ``/Users/test`` paths."""
    text = plistlib.dumps(obj).decode()
    text = text.replace(str(VENV_PYTHON), "/usr/bin/python3")
    text = text.replace(str(REPO_ROOT), "/Users/test/pipecat-local-stt-server")
    text = text.replace(str(home), "/Users/test")
    return plistlib.loads(text.encode())


def test_socket_opt_in_renders_socket_snapshot(tmp_path: Path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    sock = home / "Library" / "Caches" / "pipecat-stt" / "stt.sock"
    r = _run_install(tmp_path, stub_dir, env_overrides={"PIPECAT_STT_SOCKET": str(sock)})
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"

    plist = _rendered(home)
    args = plist["ProgramArguments"]
    assert "--host" not in args and "--port" not in args
    assert _normalise(plist, home) == plistlib.loads(SOCKET_SNAPSHOT.read_bytes())


def test_empty_socket_env_means_tcp(tmp_path: Path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    r = _run_install(tmp_path, stub_dir, env_overrides={"PIPECAT_STT_SOCKET": ""})
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    args = _rendered(home)["ProgramArguments"]
    assert _flag(args, "--port") == "9565"
    assert "--socket-path" not in args
    assert not (home / "Library" / "Caches" / "pipecat-stt").exists()


def _seed_plist(home: Path, extra_args: list[str]) -> None:
    agents = _agents(home)
    agents.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": NEW_LABEL,
        "ProgramArguments": [
            str(VENV_PYTHON),
            "-m",
            "stt_server",
            *extra_args,
            "--backend",
            "mlx",
        ],
    }
    (agents / f"{NEW_LABEL}.plist").write_bytes(plistlib.dumps(plist))


def _bind_socket(sock: Path) -> socket.socket:
    """Bind a real AF_UNIX socket at ``sock``.

    pytest tmp paths routinely exceed the ~104-byte ``sun_path`` limit, so bind
    by a relative name from inside the directory. The returned socket must stay
    referenced (closing it does not unlink the inode).
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    prev = os.getcwd()
    os.chdir(sock.parent)
    try:
        s.bind(sock.name)
    finally:
        os.chdir(prev)
    return s


@pytest.fixture
def held_sockets():
    held: list[socket.socket] = []
    yield held
    for s in held:
        s.close()


def _seed_socket_files(home: Path, held: list[socket.socket]) -> tuple[Path, Path]:
    cache = home / "Library" / "Caches" / "pipecat-stt"
    cache.mkdir(parents=True, mode=0o700, exist_ok=True)
    sock = cache / "stt.sock"
    lock = cache / "stt.sock.lock"
    held.append(_bind_socket(sock))
    lock.write_text("")
    return sock, lock


def test_socket_to_tcp_reinstall_notes_and_removes_old_socket(tmp_path: Path, held_sockets):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    sock, lock = _seed_socket_files(home, held_sockets)
    _seed_plist(home, ["--socket-path", str(sock)])

    r = _run_install(tmp_path, stub_dir)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    low = r.stdout.lower()
    assert "endpoint" in low and "chang" in low, f"expected endpoint-changed note; {r.stdout!r}"
    assert not sock.exists(), "old socket must be removed on socket->TCP"
    assert not lock.exists(), "old .lock must be removed on socket->TCP"
    assert "--port" in _rendered(home)["ProgramArguments"]


def test_tcp_to_tcp_reinstall_prints_no_note(tmp_path: Path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    _seed_plist(home, ["--host", "127.0.0.1", "--port", "9565"])

    r = _run_install(tmp_path, stub_dir)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "chang" not in r.stdout.lower(), f"unexpected endpoint-changed note: {r.stdout!r}"


def test_socket_to_socket_reinstall_keeps_socket_behaviour(tmp_path: Path, held_sockets):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    sock, lock = _seed_socket_files(home, held_sockets)
    _seed_plist(home, ["--socket-path", str(sock)])

    r = _run_install(tmp_path, stub_dir, env_overrides={"PIPECAT_STT_SOCKET": str(sock)})
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "chang" not in r.stdout.lower(), f"unexpected endpoint-changed note: {r.stdout!r}"
    args = _rendered(home)["ProgramArguments"]
    assert _flag(args, "--socket-path") == str(sock)
    assert "--port" not in args
    assert sock.exists() and lock.exists(), "socket->socket must not delete the endpoint files"


def test_uninstall_cleans_socket_from_installed_plist_not_env(tmp_path: Path, held_sockets):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    sock, lock = _seed_socket_files(home, held_sockets)
    _seed_plist(home, ["--socket-path", str(sock)])

    # Caller env carries NO PIPECAT_STT_SOCKET.
    r = _run_install(tmp_path, stub_dir, cmd="uninstall")
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert not (_agents(home) / f"{NEW_LABEL}.plist").exists()
    assert not sock.exists(), "uninstall must remove the socket named in the installed plist"
    assert not lock.exists(), "uninstall must remove the .lock named in the installed plist"


def test_uninstall_tcp_plist_creates_no_socket_dir(tmp_path: Path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    _seed_plist(home, ["--host", "127.0.0.1", "--port", "9565"])

    r = _run_install(tmp_path, stub_dir, cmd="uninstall")
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert not (_agents(home) / f"{NEW_LABEL}.plist").exists()
    assert not (home / "Library" / "Caches" / "pipecat-stt").exists()


def _cache_dir(home: Path) -> Path:
    cache = home / "Library" / "Caches" / "pipecat-stt"
    cache.mkdir(parents=True, mode=0o700, exist_ok=True)
    return cache


def _cleanup_cases(tmp_path: Path, held: list[socket.socket]) -> dict[str, tuple[str, list[Path]]]:
    """Build hostile ``--socket-path`` targets; each case maps to
    ``(path named in the plist, paths that must survive)``."""
    home = tmp_path / "home"
    cache = _cache_dir(home)
    cases: dict[str, tuple[str, list[Path]]] = {}

    reg = cache / "regular.sock"
    reg.write_text("precious")
    cases["regular-file"] = (str(reg), [reg])

    d = cache / "dir.sock"
    d.mkdir()
    (d / "inner").write_text("x")
    cases["directory"] = (str(d), [d, d / "inner"])

    sub = cache / "sub"
    sub.mkdir()
    real = cache / "dotdot.sock"
    held.append(_bind_socket(real))
    cases["dotdot"] = (f"{sub}/../dotdot.sock", [real])

    target = cache / "target.sock"
    held.append(_bind_socket(target))
    link = cache / "link.sock"
    link.symlink_to(target)
    cases["symlink"] = (str(link), [link, target])

    outside = tmp_path / "outside"
    outside.mkdir()
    out_sock = outside / "o.sock"
    held.append(_bind_socket(out_sock))
    cases["outside-home"] = (str(out_sock), [out_sock])

    lockdir_sock = cache / "ld.sock"
    held.append(_bind_socket(lockdir_sock))
    (cache / "ld.sock.lock").mkdir()
    cases["lock-is-directory"] = (str(lockdir_sock), [cache / "ld.sock.lock"])

    locklink_sock = cache / "ll.sock"
    held.append(_bind_socket(locklink_sock))
    lock_target = cache / "lock-target"
    lock_target.write_text("keep")
    (cache / "ll.sock.lock").symlink_to(lock_target)
    cases["lock-is-symlink"] = (str(locklink_sock), [cache / "ll.sock.lock", lock_target])

    # Lexically under $HOME, but a directory component is a symlink that leads
    # outside it: the socket and lock entries themselves are real and owned.
    far = tmp_path / "far"
    far.mkdir()
    far_sock = far / "f.sock"
    held.append(_bind_socket(far_sock))
    (far / "f.sock.lock").write_text("")
    (cache / "linkdir").symlink_to(far)
    cases["symlinked-parent"] = (str(cache / "linkdir" / "f.sock"), [far_sock, far / "f.sock.lock"])
    return cases


@pytest.mark.parametrize("cmd", ["uninstall", "install"])
@pytest.mark.parametrize(
    "case",
    [
        "regular-file",
        "directory",
        "dotdot",
        "symlink",
        "outside-home",
        "lock-is-directory",
        "lock-is-symlink",
        "symlinked-parent",
    ],
)
def test_socket_cleanup_never_deletes_non_socket_targets(
    tmp_path: Path, held_sockets, case: str, cmd: str
):
    """The installed plist is untrusted input: a ``--socket-path`` naming a
    regular file, a directory, a ``..`` path, a symlink, a path outside $HOME,
    or a socket whose ``.lock`` is a directory/symlink must NOT be deleted, and
    the command must still succeed (a refusal is a warning, never an abort)."""
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    named, must_survive = _cleanup_cases(tmp_path, held_sockets)[case]
    _seed_plist(home, ["--socket-path", named])

    r = _run_install(tmp_path, stub_dir, cmd=cmd)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    for path in must_survive:
        assert os.path.lexists(path), f"{case}: {path} was deleted"
    assert "warning" in r.stderr, f"{case}: expected a skip warning; {r.stderr!r}"
    if cmd == "uninstall":
        assert not (_agents(home) / f"{NEW_LABEL}.plist").exists()


def test_socket_cleanup_still_removes_real_socket_and_lock(tmp_path: Path, held_sockets):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    sock, lock = _seed_socket_files(home, held_sockets)
    _seed_plist(home, ["--socket-path", str(sock)])
    r = _run_install(tmp_path, stub_dir, cmd="uninstall")
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert not sock.exists() and not lock.exists()
    assert "warning" not in r.stderr


def test_kyutai_uses_pipecat_stt_python(tmp_path: Path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    py = _stub_python(tmp_path)
    r = _run_install(
        tmp_path,
        stub_dir,
        env_overrides={
            "PIPECAT_STT_BACKEND": "kyutai",
            "PIPECAT_STT_MODEL": "kyutai/stt-test",
            "PIPECAT_STT_PYTHON": str(py),
        },
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert _rendered(tmp_path / "home")["ProgramArguments"][0] == str(py)


@pytest.mark.skipif(KYUTAI_DEFAULT_PYTHON.exists(), reason="real .venv-kyutai present")
def test_kyutai_default_python_is_kyutai_venv(tmp_path: Path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    r = _run_install(
        tmp_path,
        stub_dir,
        env_overrides={"PIPECAT_STT_BACKEND": "kyutai", "PIPECAT_STT_MODEL": "kyutai/stt-test"},
    )
    assert r.returncode != 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert str(KYUTAI_DEFAULT_PYTHON) in r.stderr


@pytest.mark.skipif(KYUTAI_DEFAULT_PYTHON.exists(), reason="real .venv-kyutai present")
@pytest.mark.parametrize("cmd", ["uninstall", "status", "logs"])
def test_non_install_commands_do_not_need_interpreter(tmp_path: Path, cmd: str):
    stub_dir, _ = _make_stub_dir(tmp_path)
    # ``logs`` would block on ``tail -F``; stub it so the command returns.
    _write_executable(stub_dir / "tail", "#!/usr/bin/env bash\nexit 0\n")
    r = _run_install(
        tmp_path,
        stub_dir,
        cmd=cmd,
        env_overrides={
            "PIPECAT_STT_BACKEND": "kyutai",
            "PIPECAT_STT_PYTHON": str(tmp_path / "does-not-exist" / "python"),
        },
    )
    assert r.returncode == 0, f"{cmd}: stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "not found" not in r.stderr


SECRET = "s3cr3t-token-value-xyz"


def test_env_auth_token_never_reaches_plist(tmp_path: Path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    for name in ("PIPECAT_STT_AUTH_TOKEN", "KODA_STT_AUTH_TOKEN"):
        r = _run_install(tmp_path, stub_dir, env_overrides={name: SECRET})
        plist_path = _agents(tmp_path / "home") / f"{NEW_LABEL}.plist"
        if plist_path.exists():
            assert SECRET.encode() not in plist_path.read_bytes(), name
        else:
            assert r.returncode != 0, f"{name}: no plist yet exit 0"
        assert SECRET not in r.stdout, f"{name}: token leaked to stdout"
        assert SECRET not in r.stderr, f"{name}: token leaked to stderr"
        plist_path.unlink(missing_ok=True)


def test_ambient_socket_path_does_not_leak_into_tcp_install(tmp_path: Path):
    """A raw ``SOCKET_PATH`` in the caller's env (the renderer's own input
    name) must not turn a TCP install into a socket one."""
    stub_dir, _ = _make_stub_dir(tmp_path)
    ambient = tmp_path / "home" / "Library" / "Caches" / "pipecat-stt" / "ambient.sock"
    r = _run_install(tmp_path, stub_dir, env_overrides={"SOCKET_PATH": str(ambient)})
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    args = _rendered(tmp_path / "home")["ProgramArguments"]
    assert "--socket-path" not in args
    assert _flag(args, "--port") == "9565"
    assert str(ambient) not in plistlib.dumps(_rendered(tmp_path / "home")).decode()


def test_ambient_tcp_vars_do_not_leak_into_socket_install(tmp_path: Path):
    """Raw ``HOST``/``PORT``/``AUTH_TOKEN_FILE`` in the caller's env must not
    reach a socket plist; it carries only the selected transport."""
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    sock = home / "Library" / "Caches" / "pipecat-stt" / "stt.sock"
    r = _run_install(
        tmp_path,
        stub_dir,
        env_overrides={
            "PIPECAT_STT_SOCKET": str(sock),
            "HOST": "127.0.0.1",
            "PORT": "7777",
            "AUTH_TOKEN_FILE": str(tmp_path / "ambient-token"),
        },
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    args = _rendered(home)["ProgramArguments"]
    assert _flag(args, "--socket-path") == str(sock)
    for flag in ("--host", "--port", "--auth-token-file"):
        assert flag not in args, f"ambient var leaked as {flag}"
    assert "ambient-token" not in plistlib.dumps(_rendered(home)).decode()
    assert "ignoring" not in r.stderr, "ambient raw vars are not installer inputs; no warning"


def test_socket_mode_warns_about_ignored_tcp_settings(tmp_path: Path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    sock = home / "Library" / "Caches" / "pipecat-stt" / "stt.sock"
    tok = tmp_path / "token.txt"
    tok.write_text(SECRET)
    r = _run_install(
        tmp_path,
        stub_dir,
        env_overrides={
            "PIPECAT_STT_SOCKET": str(sock),
            "PIPECAT_STT_AUTH_TOKEN_FILE": str(tok),
            "PIPECAT_STT_HOST": "localhost",
            "PIPECAT_STT_PORT": "9999",
        },
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    for name in ("PIPECAT_STT_AUTH_TOKEN_FILE", "PIPECAT_STT_HOST", "PIPECAT_STT_PORT"):
        assert name in r.stderr, f"{name} not named in warning: {r.stderr!r}"
    assert SECRET not in r.stderr
    plist = _rendered(home)
    args = plist["ProgramArguments"]
    assert "--host" not in args and "--port" not in args and "--auth-token-file" not in args
    # Plist bytes are unchanged by the warning: same as the socket snapshot.
    assert _normalise(plist, home) == plistlib.loads(SOCKET_SNAPSHOT.read_bytes())


def test_socket_mode_without_tcp_settings_prints_no_warning(tmp_path: Path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    sock = tmp_path / "home" / "Library" / "Caches" / "pipecat-stt" / "stt.sock"
    r = _run_install(tmp_path, stub_dir, env_overrides={"PIPECAT_STT_SOCKET": str(sock)})
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "ignoring" not in r.stderr


def test_auth_token_file_forwarded_in_tcp_mode(tmp_path: Path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    tok = tmp_path / "token.txt"
    tok.write_text(SECRET)
    tok.chmod(0o600)
    r = _run_install(tmp_path, stub_dir, env_overrides={"PIPECAT_STT_AUTH_TOKEN_FILE": str(tok)})
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    args = _rendered(tmp_path / "home")["ProgramArguments"]
    assert _flag(args, "--auth-token-file") == str(tok)
    assert "--port" in args


def test_shutil_which_bash_available():
    """Sanity guard: the test harness needs a real ``bash`` to invoke the
    script — surface a clear failure rather than an opaque subprocess error."""
    assert shutil.which("bash") is not None, "bash not on PATH; cannot run install script"


# ---------------------------------------------------------------------------
# bootout is asynchronous: bootstrapping into the teardown window fails with
# "Bootstrap failed: 5: Input/output error" and leaves the agent unloaded.
# ---------------------------------------------------------------------------


def _slow_teardown_launchctl(
    stub_dir: Path, tmp_path: Path, *, still_loaded_polls: int, bootstrap_failures: int
):
    """Replace the stub launchctl with one that models async teardown.

    After a ``bootout`` the service stays visible to ``print`` for
    ``still_loaded_polls`` calls; the first ``bootstrap_failures`` bootstrap
    calls exit 5 like launchd does in that window.
    """
    argv_log = tmp_path / "argv.log"
    state = tmp_path / "lc_state"
    state.mkdir()
    (state / "polls").write_text("0")
    (state / "boot_fail").write_text(str(bootstrap_failures))
    _write_executable(
        stub_dir / "launchctl",
        f"""#!/usr/bin/env bash
printf 'launchctl %s\\n' "$*" >> {argv_log!s}
case "$1" in
bootout) printf '{still_loaded_polls}' > {state!s}/polls; exit 0 ;;
print)
    n=$(cat {state!s}/polls)
    if [[ "$n" -gt 0 ]]; then printf '%s' $((n - 1)) > {state!s}/polls; exit 0; fi
    exit 1 ;;
bootstrap)
    f=$(cat {state!s}/boot_fail)
    if [[ "$f" -gt 0 ]]; then
        printf '%s' $((f - 1)) > {state!s}/boot_fail
        echo "Bootstrap failed: 5: Input/output error" >&2
        exit 5
    fi
    exit 0 ;;
esac
exit 0
""",
    )
    return argv_log


def _lc_lines(argv_log: Path) -> list[str]:
    return [ln for ln in _argv_lines(argv_log) if ln.startswith("launchctl ")]


def test_install_waits_for_bootout_teardown_before_bootstrap(tmp_path: Path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    argv_log = _slow_teardown_launchctl(
        stub_dir, tmp_path, still_loaded_polls=3, bootstrap_failures=0
    )
    r = _run_install(
        tmp_path,
        stub_dir,
        env_overrides={
            "PIPECAT_STT_BACKEND": "nemotron",
            "PIPECAT_STT_LABEL": "pipecat.stt-server.nemotron",
        },
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    lc = _lc_lines(argv_log)
    target = f"gui/{FAKE_UID}/pipecat.stt-server.nemotron"
    out = lc.index(f"launchctl bootout {target}")
    boot = lc.index(
        f"launchctl bootstrap gui/{FAKE_UID} {_agents(tmp_path / 'home') / 'pipecat.stt-server.nemotron.plist'}"
    )
    prints = [i for i, ln in enumerate(lc) if ln == f"launchctl print {target}" and out < i < boot]
    # 3 polls saw it loaded, the 4th saw it gone: bootstrap came only after that.
    assert len(prints) == 4, lc


def test_install_retries_bootstrap_once_on_transient_failure(tmp_path: Path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    argv_log = _slow_teardown_launchctl(
        stub_dir, tmp_path, still_loaded_polls=0, bootstrap_failures=1
    )
    r = _run_install(
        tmp_path,
        stub_dir,
        env_overrides={
            "PIPECAT_STT_BACKEND": "nemotron",
            "PIPECAT_STT_LABEL": "pipecat.stt-server.nemotron",
        },
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "retrying" in r.stderr
    assert len([ln for ln in _lc_lines(argv_log) if ln.startswith("launchctl bootstrap ")]) == 2
    assert "installed and started" in r.stdout


def test_install_fails_when_bootstrap_keeps_failing(tmp_path: Path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    argv_log = _slow_teardown_launchctl(
        stub_dir, tmp_path, still_loaded_polls=0, bootstrap_failures=5
    )
    r = _run_install(
        tmp_path,
        stub_dir,
        env_overrides={
            "PIPECAT_STT_BACKEND": "nemotron",
            "PIPECAT_STT_LABEL": "pipecat.stt-server.nemotron",
        },
    )
    assert r.returncode != 0
    assert len([ln for ln in _lc_lines(argv_log) if ln.startswith("launchctl bootstrap ")]) == 2
    assert "installed and started" not in r.stdout


def test_wait_for_unload_is_bounded(tmp_path: Path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    _slow_teardown_launchctl(stub_dir, tmp_path, still_loaded_polls=10_000, bootstrap_failures=0)
    r = _run_install(
        tmp_path,
        stub_dir,
        env_overrides={
            "PIPECAT_STT_BACKEND": "nemotron",
            "PIPECAT_STT_LABEL": "pipecat.stt-server.nemotron",
            "PIPECAT_STT_UNLOAD_WAIT": "1",
        },
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "still loaded" in r.stderr


# ---------------------------------------------------------------------------
# PIPECAT_STT_UNLOAD_WAIT is validated before any launchctl call.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cmd", ["install", "uninstall"])
@pytest.mark.parametrize(
    "value",
    [
        "abc",
        "-1",
        "1.5",
        " 3",
        # Past the 86400 cap, including values that overflow bash's 64-bit
        # arithmetic (10#9223372036854775808 wraps negative and would skip the wait).
        "86401",
        "100000",
        "9223372036854775807",
        "9223372036854775808",
        "99999999999999999999",
        "18446744073709551617",
    ],
)
def test_invalid_unload_wait_fails_before_bootout(tmp_path: Path, cmd: str, value: str):
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    _seed_plist(home, ["--host", "127.0.0.1", "--port", "9565"])
    before = (_agents(home) / f"{NEW_LABEL}.plist").read_bytes()
    r = _run_install(tmp_path, stub_dir, cmd=cmd, env_overrides={"PIPECAT_STT_UNLOAD_WAIT": value})
    assert r.returncode != 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "PIPECAT_STT_UNLOAD_WAIT" in r.stderr
    assert not any(ln.startswith("launchctl ") for ln in _argv_lines(argv_log))
    assert (_agents(home) / f"{NEW_LABEL}.plist").read_bytes() == before


# Accepted values are read as DECIMAL: bash arithmetic treats a leading 0 as
# octal ("08" is a base error that left the cap never firing; "010" was 8), so
# the script normalises with 10#. Observable: after bootout the old job stays
# loaded, and the number of `launchctl print` polls grows by exactly the cap.
# `sleep` is stubbed to a no-op and the stub launchctl reports "unloaded" after
# 200 prints, so a broken cap shows up as a wrong count instead of a hang.
def _count_wait_polls(tmp_path: Path, value: str) -> int:
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    _write_executable(stub_dir / "sleep", "#!/usr/bin/env bash\nexit 0\n")
    state = tmp_path / "print_count"
    state.write_text("0")
    _write_executable(
        stub_dir / "launchctl",
        f"""#!/usr/bin/env bash
printf 'launchctl %s\\n' "$*" >> {argv_log!s}
if [[ "$1" == "print" ]]; then
    n=$(( $(cat {state!s}) + 1 )); printf '%s' "$n" > {state!s}
    if [[ "$n" -le 200 ]]; then exit 0; fi
    exit 1
fi
exit 0
""",
    )
    r = _run_install(
        tmp_path,
        stub_dir,
        env_overrides={
            "PIPECAT_STT_BACKEND": "nemotron",
            "PIPECAT_STT_LABEL": "pipecat.stt-server.nemotron",
            "PIPECAT_STT_UNLOAD_WAIT": value,
        },
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "value too great for base" not in r.stderr
    return int(state.read_text())


@pytest.mark.parametrize(
    "value,seconds",
    [("0", 0), ("5", 5), ("08", 8), ("09", 9), ("010", 10), ("30", 30), ("", 30)],
)
def test_unload_wait_is_decimal(tmp_path: Path, value: str, seconds: int):
    (tmp_path / "base").mkdir()
    (tmp_path / "case").mkdir()
    base = _count_wait_polls(tmp_path / "base", "0")
    assert _count_wait_polls(tmp_path / "case", value) - base == seconds


@pytest.mark.parametrize(
    "value,seconds",
    [
        ("86400", None),  # the upper bound itself is accepted
        ("0000000000000000000000000000008", 8),  # overlong but small once zero-trimmed
        ("00000000000000000000", 0),
    ],
)
def test_unload_wait_bounds_accept_boundary_and_zero_padded(
    tmp_path: Path, value: str, seconds: int | None
):
    (tmp_path / "base").mkdir()
    (tmp_path / "case").mkdir()
    base = _count_wait_polls(tmp_path / "base", "0")
    polls = _count_wait_polls(tmp_path / "case", value) - base
    if seconds is not None:
        assert polls == seconds


# ---------------------------------------------------------------------------
# Socket cleanup contract: a <socket>.lock is unlinked only once the job that
# may hold it is confirmed gone from launchd, and only after a successful
# bootstrap on install. State space: cmd {install socket->TCP, install
# socket A->B, uninstall} x old job {gone, still loaded} x bootstrap {ok, fails}.
# ---------------------------------------------------------------------------


def _second_socket(home: Path) -> Path:
    return home / "Library" / "Caches" / "pipecat-stt" / "other.sock"


@pytest.mark.parametrize(
    "mode,still_loaded,bootstrap_failures,expect_removed,expect_rc0",
    [
        ("to-tcp", False, 0, True, True),
        ("to-tcp", True, 0, False, True),
        ("to-tcp", False, 5, False, False),
        ("to-other-socket", False, 0, True, True),
        ("to-other-socket", True, 0, False, True),
        ("to-other-socket", False, 5, False, False),
        ("uninstall", False, 0, True, True),
        ("uninstall", True, 0, False, True),
    ],
)
def test_socket_lock_unlinked_only_after_old_job_gone(
    tmp_path: Path,
    held_sockets,
    mode: str,
    still_loaded: bool,
    bootstrap_failures: int,
    expect_removed: bool,
    expect_rc0: bool,
):
    stub_dir, _ = _make_stub_dir(tmp_path)
    _slow_teardown_launchctl(
        stub_dir,
        tmp_path,
        still_loaded_polls=10_000 if still_loaded else 0,
        bootstrap_failures=bootstrap_failures,
    )
    home = tmp_path / "home"
    sock, lock = _seed_socket_files(home, held_sockets)
    _seed_plist(home, ["--socket-path", str(sock)])
    env = {"PIPECAT_STT_UNLOAD_WAIT": "0"}
    cmd = "install"
    if mode == "to-other-socket":
        env["PIPECAT_STT_SOCKET"] = str(_second_socket(home))
    elif mode == "uninstall":
        cmd = "uninstall"
    r = _run_install(tmp_path, stub_dir, cmd=cmd, env_overrides=env)
    assert (r.returncode == 0) == expect_rc0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    if expect_removed:
        assert not sock.exists() and not lock.exists(), f"{mode}: old endpoint left behind"
    else:
        assert sock.exists() and lock.exists(), f"{mode}: old socket/.lock removed too early"


def test_socket_a_to_socket_b_reinstall_notes_change(tmp_path: Path, held_sockets):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    sock, lock = _seed_socket_files(home, held_sockets)
    _seed_plist(home, ["--socket-path", str(sock)])
    other = _second_socket(home)
    r = _run_install(tmp_path, stub_dir, env_overrides={"PIPECAT_STT_SOCKET": str(other)})
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "endpoint changed" in r.stdout and str(other) in r.stdout
    assert not sock.exists() and not lock.exists()
    assert _flag(_rendered(home)["ProgramArguments"], "--socket-path") == str(other)


# ---------------------------------------------------------------------------
# A reinstall never silently drops auth from a secured agent.
# State space: installed auth {none, PIPECAT env token, KODA env token, empty env
# token, --auth-token-file} x new transport {TCP no token file, TCP token file,
# socket}. Refused iff installed auth present AND new is TCP without a file.
# ---------------------------------------------------------------------------


def _seed_plist_with_auth(home: Path, auth: str, token_file: Path) -> Path:
    agents = _agents(home)
    agents.mkdir(parents=True, exist_ok=True)
    args = [str(VENV_PYTHON), "-m", "stt_server", "--host", "127.0.0.1", "--port", "9565"]
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home)}
    if auth == "env-pipecat":
        env["PIPECAT_STT_AUTH_TOKEN"] = SECRET
    elif auth == "env-koda":
        env["KODA_STT_AUTH_TOKEN"] = SECRET
    elif auth == "env-empty":
        env["PIPECAT_STT_AUTH_TOKEN"] = ""
    elif auth == "token-file":
        args += ["--auth-token-file", str(token_file)]
    path = agents / f"{NEW_LABEL}.plist"
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": NEW_LABEL,
                "ProgramArguments": [*args, "--backend", "mlx"],
                "EnvironmentVariables": env,
            }
        )
    )
    return path


def _good_token_file(tmp_path: Path) -> Path:
    tok = tmp_path / "token.txt"
    tok.write_text(SECRET + "\n")
    tok.chmod(0o600)
    return tok


@pytest.mark.parametrize("new", ["tcp-no-file", "tcp-file", "socket"])
@pytest.mark.parametrize("auth", ["none", "env-pipecat", "env-koda", "env-empty", "token-file"])
def test_reinstall_never_silently_drops_auth(tmp_path: Path, auth: str, new: str):
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    tok = _good_token_file(tmp_path)
    plist_path = _seed_plist_with_auth(home, auth, tok)
    before = plist_path.read_bytes()
    env: dict[str, str] = {}
    if new == "tcp-file":
        env["PIPECAT_STT_AUTH_TOKEN_FILE"] = str(tok)
    elif new == "socket":
        env["PIPECAT_STT_SOCKET"] = str(home / "Library" / "Caches" / "pipecat-stt" / "stt.sock")
    r = _run_install(tmp_path, stub_dir, env_overrides=env)
    had_auth = auth in ("env-pipecat", "env-koda", "token-file")
    refused = had_auth and new == "tcp-no-file"
    assert SECRET not in r.stdout and SECRET not in r.stderr, "token value leaked"
    if refused:
        assert r.returncode != 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        assert "PIPECAT_STT_AUTH_TOKEN_FILE" in r.stderr
        assert plist_path.read_bytes() == before, "refused install must not rewrite the plist"
        assert not any(ln.startswith("launchctl ") for ln in _argv_lines(argv_log))
    else:
        assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        assert SECRET.encode() not in plist_path.read_bytes()
        if had_auth and new == "socket":
            assert "auth token" in r.stderr


def test_uninstall_then_install_is_the_opt_out(tmp_path: Path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    _seed_plist_with_auth(home, "env-pipecat", _good_token_file(tmp_path))
    assert _run_install(tmp_path, stub_dir, cmd="uninstall").returncode == 0
    r = _run_install(tmp_path, stub_dir)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "--auth-token-file" not in _rendered(home)["ProgramArguments"]


# ---------------------------------------------------------------------------
# Token-file validation (TCP mode): regular, not a symlink, owned by the user,
# no group/other bits, non-empty. Socket mode ignores the file (covered above).
# ---------------------------------------------------------------------------


def _token_case(tmp_path: Path, stub_dir: Path, case: str) -> Path:
    good = _good_token_file(tmp_path)
    if case == "ok":
        return good
    if case == "missing":
        return tmp_path / "nope.txt"
    if case == "directory":
        d = tmp_path / "tokdir"
        d.mkdir(mode=0o700)
        return d
    if case == "symlink":
        link = tmp_path / "toklink"
        link.symlink_to(good)
        return link
    if case in ("group-readable", "other-readable"):
        good.chmod(0o640 if case == "group-readable" else 0o604)
        return good
    if case in ("empty", "whitespace"):
        good.write_text("" if case == "empty" else "  \n\t\n")
        good.chmod(0o600)
        return good
    if case in ("internal-newline", "internal-space", "non-ascii", "non-utf8"):
        # Content the server's strict reader refuses: it can never match a header.
        payload = {
            "internal-newline": f"{SECRET}\nmore\n".encode(),
            "internal-space": f"{SECRET} more\n".encode(),
            "non-ascii": f"{SECRET}\u00f6\n".encode(),
            "non-utf8": SECRET.encode() + b"\xff\xfe\n",
        }[case]
        good.write_bytes(payload)
        good.chmod(0o600)
        return good
    if case in ("parent-group-writable", "parent-other-writable"):
        shared = tmp_path / "shared"
        shared.mkdir()
        shared.chmod(0o770 if case == "parent-group-writable" else 0o707)
        moved = shared / "token.txt"
        moved.write_text(SECRET + "\n")
        moved.chmod(0o600)
        return moved
    if case == "other-owner":
        # The file is owned by the real uid; make `id -u` report someone else.
        _write_executable(
            stub_dir / "id",
            '#!/usr/bin/env bash\nif [[ "$1" == "-u" ]]; then echo 99999; exit 0; fi\n'
            'exec /usr/bin/id "$@"\n',
        )
        return good
    raise AssertionError(case)


@pytest.mark.parametrize(
    "case",
    [
        "ok",
        "missing",
        "directory",
        "symlink",
        "group-readable",
        "other-readable",
        "empty",
        "whitespace",
        "internal-newline",
        "internal-space",
        "non-ascii",
        "non-utf8",
        "parent-group-writable",
        "parent-other-writable",
        "other-owner",
    ],
)
def test_token_file_validated_in_tcp_mode(tmp_path: Path, case: str):
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    tok = _token_case(tmp_path, stub_dir, case)
    r = _run_install(tmp_path, stub_dir, env_overrides={"PIPECAT_STT_AUTH_TOKEN_FILE": str(tok)})
    plist = _agents(tmp_path / "home") / f"{NEW_LABEL}.plist"
    assert SECRET not in r.stdout and SECRET not in r.stderr
    if case == "ok":
        assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        assert _flag(_rendered(tmp_path / "home")["ProgramArguments"], "--auth-token-file") == str(
            tok
        )
    else:
        assert r.returncode != 0, f"{case}: stdout={r.stdout!r} stderr={r.stderr!r}"
        assert "PIPECAT_STT_AUTH_TOKEN_FILE" in r.stderr
        assert not plist.exists(), f"{case}: plist written despite a bad token file"
        assert not any(ln.startswith("launchctl ") for ln in _argv_lines(argv_log))


def test_relative_token_file_is_refused_by_the_preflight(tmp_path: Path):
    """The renderer needs an absolute path; the preflight must say so under the
    installer's own variable name, before any directory is created."""
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    log_dir = tmp_path / "logs"
    tok = _good_token_file(tmp_path)
    r = _run_install(
        tmp_path,
        stub_dir,
        env_overrides={
            "PIPECAT_STT_AUTH_TOKEN_FILE": os.path.relpath(tok, REPO_ROOT),
            "PIPECAT_STT_LOG_DIR": str(log_dir),
        },
    )
    assert r.returncode == 1, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "PIPECAT_STT_AUTH_TOKEN_FILE" in r.stderr and "absolute" in r.stderr
    assert not log_dir.exists(), "LOG_DIR created before the token file was rejected"
    assert not (_agents(tmp_path / "home") / f"{NEW_LABEL}.plist").exists()
    assert not any(ln.startswith("launchctl ") for ln in _argv_lines(argv_log))


def test_token_path_the_renderer_would_refuse_is_refused_by_the_preflight(tmp_path: Path):
    """A path outside the renderer's allowlist (here, parentheses) must fail in the
    preflight, before any directory is created, not later in the renderer."""
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    log_dir = tmp_path / "logs"
    odd = tmp_path / "tok(1)"
    odd.mkdir()
    tok = odd / "token.txt"
    tok.write_text(SECRET + "\n")
    tok.chmod(0o600)
    r = _run_install(
        tmp_path,
        stub_dir,
        env_overrides={
            "PIPECAT_STT_AUTH_TOKEN_FILE": str(tok),
            "PIPECAT_STT_LOG_DIR": str(log_dir),
        },
    )
    assert r.returncode == 1, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "allowlist" in r.stderr and "PIPECAT_STT_AUTH_TOKEN_FILE" in r.stderr
    assert not log_dir.exists()
    assert not any(ln.startswith("launchctl ") for ln in _argv_lines(argv_log))


# ---------------------------------------------------------------------------
# Installer <-> renderer backend contract: the installer accepts exactly the
# renderer's _BACKEND_RE names and rejects anything else before any mkdir.
# ---------------------------------------------------------------------------


def _renderer_backends() -> set[str]:
    src = (REPO_ROOT / "scripts" / "render_stt_plist.py").read_text()
    m = re.search(r'_BACKEND_RE = re\.compile\(r"\^\(([^)]*)\)\$"\)', src)
    assert m, "could not parse _BACKEND_RE"
    return set(m.group(1).split("|"))


@pytest.mark.parametrize("backend", sorted(_renderer_backends() | {"whisper", "bogus", "MLX"}))
def test_installer_backend_allowlist_equals_renderer(tmp_path: Path, backend: str):
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    log_dir = tmp_path / "logs"
    env = {
        "PIPECAT_STT_BACKEND": backend,
        "PIPECAT_STT_PYTHON": str(_stub_python(tmp_path)),
        "PIPECAT_STT_MODEL": "test/model",
        "PIPECAT_STT_PORT": "9123",
        "PIPECAT_STT_LOG_DIR": str(log_dir),
    }
    r = _run_install(tmp_path, stub_dir, env_overrides=env)
    if backend in _renderer_backends():
        assert r.returncode == 0, f"{backend}: stdout={r.stdout!r} stderr={r.stderr!r}"
        assert _flag(_rendered(tmp_path / "home")["ProgramArguments"], "--backend") == backend
    else:
        assert r.returncode != 0
        assert "unknown backend" in r.stderr, r.stderr
        assert not log_dir.exists(), f"{backend}: LOG_DIR created before the backend was rejected"
        assert not (_agents(tmp_path / "home") / f"{NEW_LABEL}.plist").exists()
        assert not any(ln.startswith("launchctl ") for ln in _argv_lines(argv_log))


# ---------------------------------------------------------------------------
# Installed-plist reader <-> installer guard. Invariant: the installer reads an
# installed plist as "no auth" only when it is absent or parsed cleanly without
# a token; every other reader outcome aborts an install (plist untouched, no
# launchctl call) and makes an uninstall skip socket cleanup, with a warning.
# State space: plist {absent, valid-no-auth, valid-env-token, valid-token-file,
# truncated, garbage, wrong-type, reader-crash} x cmd {install, uninstall}. The
# stt-list/stt-status sweep is covered in tests/test_justfile_recipes.py.
# ---------------------------------------------------------------------------

_PLIST_MALFORMED = ("truncated", "garbage", "wrong-type", "reader-crash")


def _seed_plist_state(home: Path, stub_dir: Path, state: str, sock: Path, tok: Path) -> Path:
    path = _agents(home) / f"{NEW_LABEL}.plist"
    _agents(home).mkdir(parents=True, exist_ok=True)
    if state == "absent":
        return path
    if state in ("valid-no-auth", "valid-env-token", "valid-token-file", "reader-crash"):
        auth = {"valid-no-auth": "none", "valid-env-token": "env-pipecat"}.get(state, "token-file")
        _seed_plist_with_auth(home, auth, tok)
        if state == "reader-crash":
            # The reader dies the way a traceback (or a broken python3 shim) does:
            # exit 1, which must never read as "no token".
            _write_executable(stub_dir / "python3", "#!/usr/bin/env bash\nexit 1\n")
        return path
    full = plistlib.dumps(
        {
            "Label": NEW_LABEL,
            "ProgramArguments": [str(VENV_PYTHON), "-m", "stt_server", "--socket-path", str(sock)],
            "EnvironmentVariables": {"PIPECAT_STT_AUTH_TOKEN": SECRET},
        }
    )
    if state == "truncated":
        path.write_bytes(full[: len(full) // 2])
    elif state == "garbage":
        path.write_text("not a plist")
    elif state == "wrong-type":
        path.write_bytes(plistlib.dumps(["--auth-token-file", str(tok)]))
    else:
        raise AssertionError(state)
    return path


@pytest.mark.parametrize("cmd", ["install", "uninstall"])
@pytest.mark.parametrize(
    "state",
    [
        "absent",
        "valid-no-auth",
        "valid-env-token",
        "valid-token-file",
        "truncated",
        "garbage",
        "wrong-type",
        "reader-crash",
    ],
)
def test_installed_plist_state_space(tmp_path: Path, held_sockets, state: str, cmd: str):
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    home = tmp_path / "home"
    sock, lock = _seed_socket_files(home, held_sockets)
    plist = _seed_plist_state(home, stub_dir, state, sock, _good_token_file(tmp_path))
    before = plist.read_bytes() if plist.exists() else None
    r = _run_install(tmp_path, stub_dir, cmd=cmd)
    out = r.stdout + r.stderr
    assert SECRET not in out, "token value leaked"
    malformed = state in _PLIST_MALFORMED
    if cmd == "install":
        if malformed:
            assert r.returncode == 1, f"stdout={r.stdout!r} stderr={r.stderr!r}"
            assert "cannot read the installed plist" in r.stderr
            assert plist.read_bytes() == before, "an unreadable plist must not be replaced"
            assert not any(ln.startswith("launchctl ") for ln in _argv_lines(argv_log))
        elif state in ("valid-env-token", "valid-token-file"):
            assert r.returncode != 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
            assert "PIPECAT_STT_AUTH_TOKEN_FILE" in r.stderr
            assert plist.read_bytes() == before
        else:
            assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
            assert "--port" in _rendered(home)["ProgramArguments"]
    else:
        assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        assert not plist.exists(), "uninstall still removes the agent's plist"
        assert ("without socket cleanup" in r.stderr) == malformed, r.stderr
        if malformed:
            assert sock.exists() and lock.exists(), "no trusted socket path: leave files alone"
    if state != "reader-crash":
        assert "Traceback" not in out
