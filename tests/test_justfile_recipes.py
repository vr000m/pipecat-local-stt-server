"""Tests for the operator-convenience ``justfile`` recipes.

These run ``just`` as a subprocess against a temporary ``HOME`` with stub
``launchctl`` / ``id`` / ``uv`` executables prepended to ``PATH`` (same hermetic
pattern as ``tests/test_install_migration.py``), so the launchd-touching and
``uv run``-touching behaviour is asserted in CI without a real launchd domain,
LaunchAgents on disk, or a project venv.

Asserted contracts (dev plan Phases 1-3 + Acceptance Criteria):

- ``just --list`` exposes the public recipe names.
- Unknown backend fails fast (non-zero) and names all three valid backends.
- The ``_resolve`` map resolves each canonical backend to label/socket/backend.
- The map mirrors the README per-ASR table exactly (drift fails CI).
- ``stt-status`` passes ``--socket-path`` and ignores a stale ``STT_WS_SOCKET``.
- ``stt-list`` prefix-sweeps custom-labelled agents and tolerates a stopped
  socket (exit 0, renders "stopped/unreachable").
- ``stt-disable`` boots out AND keeps the plist; idempotent when not loaded.
- ``stt-enable`` with no plist fails with an actionable message.
- ``stt-install`` / ``stt-uninstall`` delegate with the exact PIPECAT_STT_* env.
- The branch diff stays within the Koda-safe file set (no pin bump needed).
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
JUSTFILE = REPO_ROOT / "justfile"
README = REPO_ROOT / "README.md"

FAKE_UID = "501"
BACKENDS = ("whisper", "parakeet", "nemotron")

pytestmark = pytest.mark.skipif(
    shutil.which("just") is None,
    reason="`just` not on PATH — install it to run the justfile recipe tests",
)


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _make_stub_dir(
    tmp_path: Path,
    *,
    print_loaded: bool = False,
    status_exit: int = 0,
    fail_actions: tuple[str, ...] = (),
) -> tuple[Path, Path]:
    """Stub ``launchctl`` / ``id`` / ``uv`` that log argv to ``argv.log``.

    ``print_loaded`` controls ``launchctl print``'s exit code (0 == agent
    loaded). ``status_exit`` controls the exit code of the stubbed
    ``uv run python -m stt_server status`` probe (non-zero == stopped socket).
    ``fail_actions`` names ``launchctl`` subcommands (e.g. ``bootout``,
    ``bootstrap``, ``kickstart``) that should exit non-zero, so recipe
    error-handling on a failed state change can be exercised.
    """
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir()
    argv_log = tmp_path / "argv.log"

    print_rc = 0 if print_loaded else 1
    fail_set = " ".join(fail_actions)
    _write_executable(
        stub_dir / "launchctl",
        f"""#!/usr/bin/env bash
printf 'launchctl %s\\n' "$*" >> {argv_log!s}
if [[ "$1" == "print" ]]; then
    if [[ {print_rc} -eq 0 ]]; then
        printf 'state = running\\n\\tpid = 4242\\n'
    fi
    exit {print_rc}
fi
FAIL_ACTIONS="{fail_set}"
for a in $FAIL_ACTIONS; do
    [[ "$1" == "$a" ]] && exit 1
done
exit 0
""",
    )

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

    # uv stub: log argv; for `uv run python -m stt_server status ...` emit a
    # status-shaped line and exit `status_exit`. Dispatch is argv-position-aware
    # (run / -m stt_server / status), not a substring match on the whole arg
    # string, so it fails loud if the `uv run … status` invocation shape changes.
    _write_executable(
        stub_dir / "uv",
        f"""#!/usr/bin/env bash
printf 'uv %s\\n' "$*" >> {argv_log!s}
if [[ "$1" == "run" && "$4" == "stt_server" && "$5" == "status" ]]; then
    if [[ {status_exit} -eq 0 ]]; then
        printf 'stt_server: ok\\n  backend: nemotron (model: x)\\n'
    fi
    exit {status_exit}
fi
exit 0
""",
    )

    return stub_dir, argv_log


def _run_just(
    args: list[str],
    *,
    home: Path,
    stub_dir: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = {
        "HOME": str(home),
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["just", *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(REPO_ROOT),
    )


def _home_with_agents(tmp_path: Path, labels: list[str]) -> Path:
    home = tmp_path / "home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    for label in labels:
        (agents / f"{label}.plist").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<plist><dict/></plist>\n'
        )
    return home


def _argv_lines(argv_log: Path) -> list[str]:
    return argv_log.read_text().splitlines() if argv_log.exists() else []


# --------------------------------------------------------------------------- #
# Recipe presence + resolver
# --------------------------------------------------------------------------- #


def test_just_list_exposes_public_recipes(tmp_path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    res = _run_just(["--list"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    for recipe in (
        "stt-list",
        "stt-status",
        "stt-disable",
        "stt-enable",
        "stt-install",
        "stt-uninstall",
    ):
        assert recipe in res.stdout
    # `_resolve` is private (underscore) — not advertised.
    assert "_resolve" not in res.stdout


@pytest.mark.parametrize(
    "backend,label,sock_name,bk",
    [
        ("whisper", "pipecat.stt-server", "stt.sock", "mlx"),
        ("parakeet", "pipecat.stt-server.parakeet", "parakeet.sock", "parakeet"),
        ("nemotron", "pipecat.stt-server.nemotron", "nemotron.sock", "nemotron"),
    ],
)
def test_resolve_maps_each_backend(tmp_path, backend, label, sock_name, bk):
    # Drive resolution through the public `stt-install` recipe — its delegation
    # stub captures LABEL/SOCKET/BACKEND — rather than invoking the private
    # `_resolve` helper directly, so this survives a future `_resolve` refactor.
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    fake_script, env_log = _delegation_stub(tmp_path)
    res = _run_just(
        [f"script={fake_script}", "stt-install", backend],
        home=home,
        stub_dir=stub_dir,
    )
    assert res.returncode == 0, res.stderr
    logged = env_log.read_text()
    assert f"LABEL={label}" in logged
    assert f"SOCKET={home}/Library/Caches/pipecat-stt/{sock_name}" in logged
    assert f"BACKEND={bk}" in logged


def test_unknown_backend_fails_fast_and_lists_valid(tmp_path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    res = _run_just(["stt-status", "bogus"], home=home, stub_dir=stub_dir)
    assert res.returncode != 0
    for name in BACKENDS:
        assert name in res.stderr


# --------------------------------------------------------------------------- #
# Map mirrors README (closes the third-source-of-truth gap)
# --------------------------------------------------------------------------- #


def _readme_map() -> dict[str, tuple[str, str]]:
    """Parse the README per-ASR table into {backend: (label, socket)}.

    Anchors on the table header and asserts its column order before indexing
    cells by position, so a future column insertion/reorder in the README fails
    loudly here instead of silently mis-mapping label/socket.
    """
    lines = README.read_text().splitlines()
    header_idx = next(
        (
            i
            for i, ln in enumerate(lines)
            if ln.startswith("|") and "LaunchAgent label" in ln and "Socket" in ln
        ),
        None,
    )
    assert header_idx is not None, "README per-ASR table header not found"
    header = [c.strip() for c in lines[header_idx].split("|")[1:-1]]
    assert header[:3] == ["ASR", "LaunchAgent label", "Socket"], (
        f"README per-ASR table column order changed: {header}"
    )
    out: dict[str, tuple[str, str]] = {}
    for ln in lines[header_idx + 2 :]:  # skip the header row + `|---|` separator
        if not ln.startswith("|"):
            break  # table ends at the first non-row line
        cells = [c.strip() for c in ln.split("|")[1:-1]]
        m = re.match(r"(whisper|parakeet|nemotron)\b", cells[0])
        if not m:
            continue
        assert len(cells) == 4, f"unexpected column count in per-ASR row: {cells}"
        out[m.group(1)] = (cells[1].strip("`"), cells[2].strip("`"))
    return out


def test_justfile_map_mirrors_readme(tmp_path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    fake_script, env_log = _delegation_stub(tmp_path)
    readme = _readme_map()
    assert set(readme) == set(BACKENDS), f"README table backends: {set(readme)}"
    for backend, (label, socket) in readme.items():
        # Resolve via the public `stt-install` recipe (delegation stub), not the
        # private `_resolve` helper — same mirror invariant, public interface.
        res = _run_just(
            [f"script={fake_script}", "stt-install", backend],
            home=home,
            stub_dir=stub_dir,
        )
        assert res.returncode == 0, res.stderr
        logged = env_log.read_text()
        assert f"LABEL={label}" in logged, f"{backend}: label drift"
        # README uses ~ ; justfile uses $HOME — compare after expansion.
        expanded = socket.replace("~", str(home), 1)
        assert f"SOCKET={expanded}" in logged, f"{backend}: socket drift"


# --------------------------------------------------------------------------- #
# Probe correctness: explicit --socket-path, ignores stale env
# --------------------------------------------------------------------------- #


def test_stt_status_passes_socket_path_and_ignores_stale_env(tmp_path):
    stub_dir, argv_log = _make_stub_dir(tmp_path, status_exit=0)
    home = _home_with_agents(tmp_path, [])
    res = _run_just(
        ["stt-status", "nemotron"],
        home=home,
        stub_dir=stub_dir,
        extra_env={"STT_WS_SOCKET": "/tmp/bogus-should-be-ignored.sock"},
    )
    assert res.returncode == 0, res.stderr
    uv_calls = [ln for ln in _argv_lines(argv_log) if ln.startswith("uv ")]
    assert any("stt_server status" in ln for ln in uv_calls)
    mapped = f"--socket-path {home}/Library/Caches/pipecat-stt/nemotron.sock"
    assert any(mapped in ln for ln in uv_calls), uv_calls
    assert not any("bogus-should-be-ignored" in ln for ln in uv_calls)


# --------------------------------------------------------------------------- #
# stt-list: prefix sweep + stopped-socket tolerance
# --------------------------------------------------------------------------- #


def test_stt_list_surfaces_custom_labelled_agent(tmp_path):
    stub_dir, _ = _make_stub_dir(tmp_path, print_loaded=True, status_exit=0)
    home = _home_with_agents(tmp_path, ["pipecat.stt-server", "pipecat.stt-server.custom"])
    res = _run_just(["stt-list"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    # Custom-labelled agent enumerated by the prefix sweep...
    assert "pipecat.stt-server.custom" in res.stdout
    # ...but with no live line (its socket is not in the canonical map).
    custom_line = next(ln for ln in res.stdout.splitlines() if "pipecat.stt-server.custom" in ln)
    assert custom_line.startswith("running")


def test_stt_list_prints_socket_in_tilde_form(tmp_path):
    # The socket line must match the ~-form onoats config.toml's `ws_socket`
    # uses, so an operator can correlate a config line with an agent. Whisper's
    # socket is stt.sock (not whisper.sock) — the case the nomenclature trap hits.
    stub_dir, _ = _make_stub_dir(tmp_path, print_loaded=True, status_exit=0)
    home = _home_with_agents(tmp_path, ["pipecat.stt-server"])
    res = _run_just(["stt-list"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    assert "socket: ~/Library/Caches/pipecat-stt/stt.sock" in res.stdout
    # No absolute $HOME path should leak — it must be rendered as ~.
    assert str(home) not in res.stdout


def test_stt_list_custom_label_has_no_canonical_socket(tmp_path):
    stub_dir, _ = _make_stub_dir(tmp_path, print_loaded=True, status_exit=0)
    home = _home_with_agents(tmp_path, ["pipecat.stt-server.custom"])
    res = _run_just(["stt-list"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    assert "custom label" in res.stdout


def test_stt_list_tolerates_stopped_socket(tmp_path):
    # Agent loaded, but its status probe exits 1 (stopped/unreachable socket).
    stub_dir, _ = _make_stub_dir(tmp_path, print_loaded=True, status_exit=1)
    home = _home_with_agents(tmp_path, ["pipecat.stt-server.nemotron"])
    res = _run_just(["stt-list"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    assert "stopped/unreachable" in res.stdout


def test_stt_list_empty(tmp_path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    res = _run_just(["stt-list"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    assert "no pipecat.stt-server* agents found" in res.stdout


# --------------------------------------------------------------------------- #
# stt-disable: bootout, keep plist, idempotent
# --------------------------------------------------------------------------- #


def test_stt_disable_boots_out_but_keeps_plist(tmp_path):
    stub_dir, argv_log = _make_stub_dir(tmp_path, print_loaded=True)
    home = _home_with_agents(tmp_path, ["pipecat.stt-server"])
    plist = home / "Library" / "LaunchAgents" / "pipecat.stt-server.plist"
    res = _run_just(["stt-disable", "whisper"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    boots = [ln for ln in _argv_lines(argv_log) if ln.startswith("launchctl bootout")]
    assert any(f"gui/{FAKE_UID}/pipecat.stt-server" in ln for ln in boots), boots
    # The headline invariant: disable != uninstall — the plist must survive.
    assert plist.exists(), "stt-disable must NOT remove the plist"


def test_stt_disable_idempotent_when_not_loaded(tmp_path):
    stub_dir, argv_log = _make_stub_dir(tmp_path, print_loaded=False)
    home = _home_with_agents(tmp_path, ["pipecat.stt-server"])
    res = _run_just(["stt-disable", "whisper"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    assert "not loaded" in res.stdout
    assert not any(ln.startswith("launchctl bootout") for ln in _argv_lines(argv_log))


def test_stt_disable_fails_when_bootout_fails(tmp_path):
    # Agent is loaded but `launchctl bootout` returns non-zero. The recipe must
    # surface the failure (non-zero exit, no success line) rather than masking
    # it behind the success echo — set -uo pipefail alone does NOT abort here.
    stub_dir, _ = _make_stub_dir(tmp_path, print_loaded=True, fail_actions=("bootout",))
    home = _home_with_agents(tmp_path, ["pipecat.stt-server"])
    res = _run_just(["stt-disable", "whisper"], home=home, stub_dir=stub_dir)
    assert res.returncode != 0, "bootout failure must propagate"
    assert "booted out" not in res.stdout
    assert "bootout failed" in res.stderr


# --------------------------------------------------------------------------- #
# stt-enable: actionable error when plist is absent
# --------------------------------------------------------------------------- #


def test_stt_enable_missing_plist_errors(tmp_path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])  # no plists
    res = _run_just(["stt-enable", "whisper"], home=home, stub_dir=stub_dir)
    assert res.returncode != 0
    assert "stt-install" in res.stderr


def test_stt_enable_fails_when_bootstrap_fails(tmp_path):
    # plist present, but `launchctl bootstrap` fails — the recipe must exit
    # non-zero, skip kickstart, and not print the success line.
    stub_dir, argv_log = _make_stub_dir(tmp_path, fail_actions=("bootstrap",))
    home = _home_with_agents(tmp_path, ["pipecat.stt-server"])
    res = _run_just(["stt-enable", "whisper"], home=home, stub_dir=stub_dir)
    assert res.returncode != 0, "bootstrap failure must propagate"
    assert "bootstrapped + kickstarted" not in res.stdout
    assert "bootstrap failed" in res.stderr
    # kickstart must not run once bootstrap failed.
    assert not any(ln.startswith("launchctl kickstart") for ln in _argv_lines(argv_log))


def test_stt_enable_fails_when_kickstart_fails(tmp_path):
    # bootstrap succeeds, but `launchctl kickstart` fails — still non-zero and
    # no success line (the agent was loaded but never started).
    stub_dir, _ = _make_stub_dir(tmp_path, fail_actions=("kickstart",))
    home = _home_with_agents(tmp_path, ["pipecat.stt-server"])
    res = _run_just(["stt-enable", "whisper"], home=home, stub_dir=stub_dir)
    assert res.returncode != 0, "kickstart failure must propagate"
    assert "bootstrapped + kickstarted" not in res.stdout
    assert "kickstart failed" in res.stderr


# --------------------------------------------------------------------------- #
# install/uninstall delegate with the exact PIPECAT_STT_* env
# --------------------------------------------------------------------------- #


def _delegation_stub(tmp_path: Path) -> tuple[Path, Path]:
    """A stub install script that records the PIPECAT_STT_* env it received."""
    env_log = tmp_path / "deleg.log"
    stub = tmp_path / "fake_install.sh"
    _write_executable(
        stub,
        f"""#!/usr/bin/env bash
printf 'CMD=%s LABEL=%s SOCKET=%s BACKEND=%s\\n' \
  "$1" "$PIPECAT_STT_LABEL" "$PIPECAT_STT_SOCKET" "$PIPECAT_STT_BACKEND" \
  >> {env_log!s}
exit 0
""",
    )
    return stub, env_log


@pytest.mark.parametrize("recipe,cmd", [("stt-install", "install"), ("stt-uninstall", "uninstall")])
def test_install_uninstall_delegate_exact_env(tmp_path, recipe, cmd):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    fake_script, env_log = _delegation_stub(tmp_path)
    # `script=` overrides the justfile variable so delegation hits our stub.
    res = _run_just(
        [f"script={fake_script}", recipe, "parakeet"],
        home=home,
        stub_dir=stub_dir,
    )
    assert res.returncode == 0, res.stderr
    logged = env_log.read_text().strip()
    assert f"CMD={cmd}" in logged
    assert "LABEL=pipecat.stt-server.parakeet" in logged
    assert f"SOCKET={home}/Library/Caches/pipecat-stt/parakeet.sock" in logged
    assert "BACKEND=parakeet" in logged


# --------------------------------------------------------------------------- #
# Koda-safety: the branch diff stays within the additive file set
# --------------------------------------------------------------------------- #


def test_branch_diff_does_not_touch_koda_surface():
    """No-pin-bump invariant (per the cross-repo contract): this work must not
    modify the Koda-consumed surface — anything under ``stt_server/`` (the
    imported client + the wire protocol) or ``scripts/install_stt_agent.sh``.
    Additive files (justfile, these tests, README, dev-plan docs) are fine; an
    unrelated docs-only commit does NOT void Koda safety, so this asserts the
    *negative* contract rather than an exact file allowlist."""
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    merge_base = subprocess.run(
        ["git", "merge-base", "main", "HEAD"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    if merge_base.returncode != 0:
        pytest.skip("no `main` ref to diff against")
    base = merge_base.stdout.strip()
    diff = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    changed = {ln for ln in diff.stdout.splitlines() if ln.strip()}
    forbidden = sorted(
        c for c in changed if c.startswith("stt_server/") or c == "scripts/install_stt_agent.sh"
    )
    assert not forbidden, (
        f"branch modifies the Koda-consumed surface (would require a pin bump): {forbidden}"
    )
