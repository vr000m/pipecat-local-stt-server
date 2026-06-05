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
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

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
) -> subprocess.CompletedProcess:
    """Run ``install_stt_agent.sh install`` in a hermetic temp ``HOME``.

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
        ["bash", str(SCRIPT), "install"],
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
    assert new_bootout_indices == [bootstrap_idx - 1], (
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

NEMOTRON_MODEL = "mlx-community/nemotron-3.5-asr-streaming-0.6b"
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


def test_shutil_which_bash_available():
    """Sanity guard: the test harness needs a real ``bash`` to invoke the
    script — surface a clear failure rather than an opaque subprocess error."""
    assert shutil.which("bash") is not None, "bash not on PATH; cannot run install script"
