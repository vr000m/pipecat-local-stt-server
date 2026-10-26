"""Tests for the operator-convenience ``justfile`` recipes.

These run ``just`` as a subprocess against a temporary ``HOME`` with stub
``launchctl`` / ``id`` / ``uv`` executables prepended to ``PATH`` (same hermetic
pattern as ``tests/test_install_migration.py``), so the launchd-touching and
``uv run``-touching behaviour is asserted in CI without a real launchd domain,
LaunchAgents on disk, or a project venv.

Asserted contracts (dev plan Phases 1-3 + Acceptance Criteria):

- ``just --list`` exposes the public recipe names.
- Unknown backend fails fast (non-zero) and names every valid backend.
- ``_resolve`` prints label / host / port / backend-name (four lines) per backend,
  kyutai included.
- ``docs/operations.md`` "Per-backend port convention" table, ``_resolve`` and the
  installer's own default port (read from a really-rendered plist) agree.
- ``stt-status``: bare backend -> ``--host/--port``; a path -> ``--socket-path``;
  longer probe timeout; stale ``STT_WS_*`` env is ignored.
- ``stt-list`` reads host/port/socket/auth-token-file from RENDERED plists
  (``_plist_endpoint``), probes TCP/socket/token-file agents, always exits 0.
- ``stt-probe`` requires an explicit endpoint and forwards ``--json``.
- ``stt-disable`` boots out AND keeps the plist; idempotent when not loaded.
- ``stt-enable`` with no plist fails with an actionable message.
- ``stt-install`` / ``stt-uninstall`` delegate with the exact PIPECAT_STT_* env.
- ``_ensure-extra`` kyutai arm syncs into ``.venv-kyutai`` (never ``.venv``).
- The branch diff stays within the Koda-safe file set (no pin bump needed).
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
JUSTFILE = REPO_ROOT / "justfile"
# The canonical per-backend port table lives in docs/operations.md; the justfile
# map and the installer default port mirror it (checked below).
PER_ASR_DOC = REPO_ROOT / "docs" / "operations.md"
INSTALLER = REPO_ROOT / "scripts" / "install_stt_agent.sh"
RENDERER = REPO_ROOT / "scripts" / "render_stt_plist.py"
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

FAKE_UID = "501"
BACKENDS = ("whisper", "parakeet", "nemotron", "kyutai")
# backend -> (label, port, installer backend-name)
EXPECTED = {
    "whisper": ("pipecat.stt-server", 9565, "mlx"),
    "parakeet": ("pipecat.stt-server.parakeet", 9765, "parakeet"),
    "nemotron": ("pipecat.stt-server.nemotron", 9865, "nemotron"),
    "kyutai": ("pipecat.stt-server.kyutai", 9665, "kyutai"),
}

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
printf 'uvenv UV_PROJECT_ENVIRONMENT=%s\\n' "${{UV_PROJECT_ENVIRONMENT-<unset>}}" >> {argv_log!s}
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
        # stt-install / stt-enable call `_ensure-extra`, which would otherwise
        # shell out to a real `uv sync` for backends whose extra is absent from
        # the dev/CI venv. Skip that side effect by default; a dedicated test
        # overrides this via extra_env to exercise the skip-message path.
        "PIPECAT_STT_SKIP_DEP_SYNC": "1",
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


def _uv_calls(argv_log: Path) -> list[str]:
    return [ln for ln in _argv_lines(argv_log) if ln.startswith("uv ")]


def _render_plist(
    home: Path,
    label: str,
    *,
    backend: str = "mlx",
    host: str = "127.0.0.1",
    port: int | None = 9565,
    socket_path: Path | None = None,
    auth_token_file: Path | None = None,
) -> Path:
    """Render a REAL plist with ``scripts/render_stt_plist.py`` into ``home``.

    Fixtures for ``stt-list`` must carry the same ProgramArguments the installer
    writes (``--host/--port`` or ``--socket-path``, ``--auth-token-file``), not
    the empty ``<dict/>`` plists that carry no endpoint.
    """
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    dst = agents / f"{label}.plist"
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
        "HOST": "" if socket_path else host,
        "PORT": "" if socket_path or port is None else str(port),
        "AUTH_TOKEN_FILE": str(auth_token_file) if auth_token_file else "",
    }
    res = subprocess.run(
        [sys.executable, str(RENDERER)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert res.returncode == 0, f"renderer failed: {res.stderr!r}"
    assert dst.is_file(), f"renderer did not write {dst}"
    args = plistlib.loads(dst.read_bytes())["ProgramArguments"]
    # Fixture sanity: the rendered plist really carries the requested endpoint.
    assert ("--socket-path" in args) == bool(socket_path)
    assert ("--auth-token-file" in args) == bool(auth_token_file)
    return dst


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
        "stt-probe",
    ):
        assert recipe in res.stdout
    # Private (underscore) helpers are not advertised in --list.
    assert "_resolve" not in res.stdout
    assert "_ensure-extra" not in res.stdout


def _resolve(backend: str, tmp_path: Path) -> subprocess.CompletedProcess:
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    return _run_just(["_resolve", backend], home=home, stub_dir=stub_dir)


@pytest.mark.parametrize("backend", BACKENDS)
def test_resolve_prints_four_fields_per_backend(tmp_path, backend):
    label, port, bk = EXPECTED[backend]
    res = _resolve(backend, tmp_path)
    assert res.returncode == 0, res.stderr
    # label, host, port, backend-name: one field per line.
    assert res.stdout.splitlines() == [label, "127.0.0.1", str(port), bk]


def test_resolve_unknown_backend_exits_nonzero(tmp_path):
    res = _resolve("bogus", tmp_path)
    assert res.returncode != 0
    assert res.stdout.strip() == ""


def test_unknown_backend_fails_fast_and_lists_valid(tmp_path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    res = _run_just(["stt-status", "bogus"], home=home, stub_dir=stub_dir)
    assert res.returncode != 0
    for name in BACKENDS:
        assert name in res.stderr


# --------------------------------------------------------------------------- #
# Port table mirrors _resolve and the installer default (drift fails CI)
# --------------------------------------------------------------------------- #


def _cell(text: str) -> str:
    """Strip markdown decoration (backticks, bold) from a table cell."""
    return text.strip().strip("*").strip().strip("`").strip()


def _readme_map() -> dict[str, tuple[str, int]]:
    """Parse the "Per-backend port convention" table in docs/operations.md into
    {backend: (label, port)}.

    Tolerant of formatting (backticks, bold, spacing, extra columns after the
    three required ones); strict on content: the header must name the ASR,
    LaunchAgent label and port columns in that order, and each row's label and
    port must parse. A column reorder fails loudly here instead of silently
    mis-mapping label/port.
    """
    lines = PER_ASR_DOC.read_text().splitlines()
    header_idx = next(
        (
            i
            for i, ln in enumerate(lines)
            if ln.startswith("|") and "LaunchAgent label" in ln and "port" in ln.lower()
        ),
        None,
    )
    assert header_idx is not None, "per-backend port table header not found in docs/operations.md"
    header = [_cell(c) for c in lines[header_idx].split("|")[1:-1]]
    assert header[0] == "ASR" and header[1] == "LaunchAgent label", header
    assert header[2].lower() == "port", f"port table column order changed: {header}"
    out: dict[str, tuple[str, int]] = {}
    for ln in lines[header_idx + 2 :]:  # skip the header row + `|---|` separator
        if not ln.startswith("|"):
            break  # table ends at the first non-row line
        cells = [_cell(c) for c in ln.split("|")[1:-1]]
        m = re.match(r"(whisper|parakeet|nemotron|kyutai)\b", cells[0])
        if not m:
            continue
        assert len(cells) >= 3, f"unexpected column count in port row: {cells}"
        assert re.fullmatch(r"\d{4,5}", cells[2]), f"unparseable port cell: {cells}"
        assert m.group(1) not in out, f"duplicate row for {m.group(1)}"
        out[m.group(1)] = (cells[1], int(cells[2]))
    return out


def _installer_default_port(tmp_path: Path, installer_backend: str) -> tuple[str, str]:
    """Run the REAL installer in a temp HOME (stubbed launchctl/id) with no port
    override and return the ``--host``/``--port`` it rendered into the plist.

    This exercises the installer's own default-port logic. The justfile
    delegation stub is deliberately NOT used: it receives the port explicitly
    from ``_resolve`` and so cannot detect a drifted installer default.
    """
    inst = tmp_path / "installer"
    inst.mkdir()
    stub_dir, _ = _make_stub_dir(inst)
    home = tmp_path / "home_installer"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    fake_py = inst / "fakepy" / "python"
    fake_py.parent.mkdir()
    _write_executable(fake_py, f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
    env = {
        "HOME": str(home),
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "PIPECAT_STT_BACKEND": installer_backend,
        # Interpreter + model are not under test; pin them so every backend
        # (kyutai's default venv does not exist here) can install hermetically.
        "PIPECAT_STT_PYTHON": str(fake_py),
        "PIPECAT_STT_MODEL": "test/model",
    }
    res = subprocess.run(
        ["bash", str(INSTALLER), "install"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert res.returncode == 0, f"installer failed: stdout={res.stdout!r} stderr={res.stderr!r}"
    plists = list((home / "Library" / "LaunchAgents").glob("pipecat.stt-server*.plist"))
    assert len(plists) == 1, plists
    args = plistlib.loads(plists[0].read_bytes())["ProgramArguments"]
    assert "--socket-path" not in args, "default install must be TCP"
    return args[args.index("--host") + 1], args[args.index("--port") + 1]


def test_port_table_covers_exactly_the_backends():
    table = _readme_map()
    assert set(table) == set(BACKENDS), f"port table backends: {set(table)}"
    # Literal sanity pin on top of the cross-checks below, so all three copies
    # drifting together to a wrong value is still caught.
    for backend, (label, port) in table.items():
        assert (label, port) == EXPECTED[backend][:2], backend


@pytest.mark.parametrize("backend", BACKENDS)
def test_port_table_matches_resolve(tmp_path, backend):
    label, port = _readme_map()[backend]
    res = _resolve(backend, tmp_path)
    assert res.returncode == 0, res.stderr
    r_label, r_host, r_port, _bk = res.stdout.splitlines()
    assert r_label == label, f"{backend}: label drift (docs vs _resolve)"
    assert r_port == str(port), f"{backend}: port drift (docs vs _resolve)"
    assert r_host == "127.0.0.1"


@pytest.mark.skipif(not VENV_PYTHON.exists(), reason="installer needs the project venv")
@pytest.mark.parametrize("backend", BACKENDS)
def test_port_table_matches_installer_default(tmp_path, backend):
    _label, port = _readme_map()[backend]
    res = _resolve(backend, tmp_path)
    assert res.returncode == 0, res.stderr
    installer_backend = res.stdout.splitlines()[3]
    host, rendered_port = _installer_default_port(tmp_path, installer_backend)
    assert host == "127.0.0.1"
    assert rendered_port == str(port), f"{backend}: installer default port drifted from the docs"


def test_justfile_header_mentions_the_docs_port_table():
    # Light check only (comment wording is not pinned): the header comment must
    # point maintainers at the doc the map mirrors.
    header = "\n".join(JUSTFILE.read_text().splitlines()[:20])
    assert "docs/operations.md" in header


def test_gitignore_has_kyutai_venv():
    lines = {ln.strip() for ln in (REPO_ROOT / ".gitignore").read_text().splitlines()}
    assert ".venv-kyutai/" in lines


# --------------------------------------------------------------------------- #
# stt-status: target resolution + timeout
# --------------------------------------------------------------------------- #


def _status_call(argv_log: Path) -> str:
    calls = [ln for ln in _uv_calls(argv_log) if "stt_server status" in ln]
    assert len(calls) == 1, calls
    return calls[0]


def _timeout_of(call: str) -> float:
    m = re.search(r"--timeout[ =](\d+(?:\.\d+)?)", call)
    assert m, f"no --timeout in {call!r}"
    return float(m.group(1))


@pytest.mark.parametrize("backend", BACKENDS)
def test_stt_status_bare_backend_resolves_to_host_port(tmp_path, backend):
    _label, port, _bk = EXPECTED[backend]
    stub_dir, argv_log = _make_stub_dir(tmp_path, status_exit=0)
    home = _home_with_agents(tmp_path, [])
    res = _run_just(
        ["stt-status", backend],
        home=home,
        stub_dir=stub_dir,
        # Stale ambient client env must never decide which server is probed.
        extra_env={
            "STT_WS_SOCKET": "/tmp/bogus-should-be-ignored.sock",
            "STT_WS_HOST": "203.0.113.9",
            "STT_WS_PORT": "1",
        },
    )
    assert res.returncode == 0, res.stderr
    call = _status_call(argv_log)
    assert f"--host 127.0.0.1 --port {port}" in call, call
    assert "--socket-path" not in call
    assert "bogus-should-be-ignored" not in call and "203.0.113.9" not in call
    # Longer than the 3s CLI default: a model-backed agent may still be loading.
    assert _timeout_of(call) > 3


def test_stt_status_path_target_is_socket_path(tmp_path):
    stub_dir, argv_log = _make_stub_dir(tmp_path, status_exit=0)
    home = _home_with_agents(tmp_path, [])
    sock = home / "Library" / "Caches" / "pipecat-stt" / "nemotron.sock"
    res = _run_just(["stt-status", str(sock)], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    call = _status_call(argv_log)
    assert f"--socket-path {sock}" in call, call
    assert "--host" not in call and "--port" not in call
    assert _timeout_of(call) > 3


def test_stt_status_propagates_probe_exit(tmp_path):
    stub_dir, _ = _make_stub_dir(tmp_path, status_exit=1)
    home = _home_with_agents(tmp_path, [])
    res = _run_just(["stt-status", "whisper"], home=home, stub_dir=stub_dir)
    assert res.returncode == 1


# --------------------------------------------------------------------------- #
# stt-probe: explicit endpoint required, flags forwarded
# --------------------------------------------------------------------------- #


def test_stt_probe_requires_explicit_endpoint(tmp_path):
    stub_dir, argv_log = _make_stub_dir(tmp_path, status_exit=0)
    home = _home_with_agents(tmp_path, [])
    res = _run_just(
        ["stt-probe"],
        home=home,
        stub_dir=stub_dir,
        extra_env={"STT_WS_HOST": "127.0.0.1", "STT_WS_PORT": "9565"},
    )
    assert res.returncode == 2, (res.returncode, res.stderr)
    assert "endpoint" in res.stderr
    assert not _uv_calls(argv_log), "no probe may run without an explicit endpoint"


def test_stt_probe_json_alone_is_not_an_endpoint(tmp_path):
    stub_dir, argv_log = _make_stub_dir(tmp_path, status_exit=0)
    home = _home_with_agents(tmp_path, [])
    res = _run_just(["stt-probe", "--json"], home=home, stub_dir=stub_dir)
    assert res.returncode == 2, (res.returncode, res.stderr)
    assert not _uv_calls(argv_log)


@pytest.mark.parametrize(
    "endpoint",
    [
        ["--socket-path", "/some/where.sock"],
        ["--host", "127.0.0.1", "--port", "9865"],
        ["--uri", "ws://127.0.0.1:9865"],
    ],
)
def test_stt_probe_forwards_endpoint_and_json(tmp_path, endpoint):
    stub_dir, argv_log = _make_stub_dir(tmp_path, status_exit=0)
    home = _home_with_agents(tmp_path, [])
    res = _run_just(["stt-probe", *endpoint, "--json"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    call = _status_call(argv_log)
    assert " ".join(endpoint) in call, call
    assert "--json" in call.split()


# --------------------------------------------------------------------------- #
# stt-list: endpoints read from RENDERED plists; always exits 0
# --------------------------------------------------------------------------- #


def test_stt_list_surfaces_custom_labelled_agent(tmp_path):
    stub_dir, _ = _make_stub_dir(tmp_path, print_loaded=True, status_exit=0)
    home = _home_with_agents(tmp_path, [])
    _render_plist(home, "pipecat.stt-server", port=9565)
    _render_plist(home, "pipecat.stt-server.custom", port=9999)
    res = _run_just(["stt-list"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    # Custom-labelled agent enumerated by the prefix sweep...
    custom_line = next(ln for ln in res.stdout.splitlines() if "pipecat.stt-server.custom" in ln)
    assert custom_line.startswith("running")


def test_stt_list_probes_tcp_agent_from_its_plist(tmp_path):
    stub_dir, argv_log = _make_stub_dir(tmp_path, print_loaded=True, status_exit=0)
    home = _home_with_agents(tmp_path, [])
    # A CUSTOM label with a non-canonical port: the endpoint can only have come
    # from the plist, never from a label -> port map.
    _render_plist(home, "pipecat.stt-server.custom", backend="nemotron", port=9871)
    res = _run_just(["stt-list"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    assert "127.0.0.1:9871" in res.stdout
    call = _status_call(argv_log)
    assert "--host 127.0.0.1 --port 9871" in call, call
    assert "--socket-path" not in call and "--auth-token-file" not in call
    assert "live: nemotron" in res.stdout


def test_stt_list_prints_socket_in_tilde_form(tmp_path):
    # A socket-mode agent's plist carries --socket-path; the recipe prints it in
    # the ~-form onoats config.toml's `ws_socket` uses and probes it by socket.
    stub_dir, argv_log = _make_stub_dir(tmp_path, print_loaded=True, status_exit=0)
    home = _home_with_agents(tmp_path, [])
    sock = home / "Library" / "Caches" / "pipecat-stt" / "stt.sock"
    _render_plist(home, "pipecat.stt-server", socket_path=sock)
    res = _run_just(["stt-list"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    assert "socket: ~/Library/Caches/pipecat-stt/stt.sock" in res.stdout
    # No absolute $HOME path should leak — it must be rendered as ~.
    assert str(home) not in res.stdout
    call = _status_call(argv_log)
    assert f"--socket-path {sock}" in call, call
    assert "--host" not in call and "--port" not in call


def test_stt_list_forwards_auth_token_file(tmp_path):
    stub_dir, argv_log = _make_stub_dir(tmp_path, print_loaded=True, status_exit=0)
    home = _home_with_agents(tmp_path, [])
    token = home / ".config" / "pipecat-stt" / "token"
    _render_plist(
        home, "pipecat.stt-server.parakeet", backend="parakeet", port=9765, auth_token_file=token
    )
    res = _run_just(["stt-list"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    call = _status_call(argv_log)
    assert "--host 127.0.0.1 --port 9765" in call, call
    assert f"--auth-token-file {token}" in call, call
    # The token FILE path is fine to forward; its contents never exist here.
    assert "live: " in res.stdout


def test_stt_list_sweeps_tcp_socket_and_token_agents_together(tmp_path):
    stub_dir, argv_log = _make_stub_dir(tmp_path, print_loaded=True, status_exit=0)
    home = _home_with_agents(tmp_path, [])
    sock = home / "Library" / "Caches" / "pipecat-stt" / "parakeet.sock"
    token = home / "tok"
    _render_plist(home, "pipecat.stt-server", port=9565)
    _render_plist(home, "pipecat.stt-server.parakeet", backend="parakeet", socket_path=sock)
    _render_plist(
        home, "pipecat.stt-server.nemotron", backend="nemotron", port=9865, auth_token_file=token
    )
    res = _run_just(["stt-list"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    calls = [ln for ln in _uv_calls(argv_log) if "stt_server status" in ln]
    assert len(calls) == 3, calls
    assert any("--port 9565" in c and "--auth-token-file" not in c for c in calls)
    assert any(f"--socket-path {sock}" in c for c in calls)
    assert any("--port 9865" in c and f"--auth-token-file {token}" in c for c in calls)


def test_stt_list_tolerates_stopped_endpoint(tmp_path):
    # Agent loaded, but its status probe exits 1 (stopped/unreachable endpoint).
    stub_dir, _ = _make_stub_dir(tmp_path, print_loaded=True, status_exit=1)
    home = _home_with_agents(tmp_path, [])
    _render_plist(home, "pipecat.stt-server.nemotron", backend="nemotron", port=9865)
    res = _run_just(["stt-list"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    assert "stopped/unreachable" in res.stdout


def test_stt_list_tolerates_stopped_socket_agent(tmp_path):
    stub_dir, _ = _make_stub_dir(tmp_path, print_loaded=True, status_exit=1)
    home = _home_with_agents(tmp_path, [])
    sock = home / "Library" / "Caches" / "pipecat-stt" / "stt.sock"
    _render_plist(home, "pipecat.stt-server", socket_path=sock)
    res = _run_just(["stt-list"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    assert "stopped/unreachable" in res.stdout


def test_stt_list_unloaded_agent_still_exits_zero(tmp_path):
    stub_dir, _ = _make_stub_dir(tmp_path, print_loaded=False, status_exit=1)
    home = _home_with_agents(tmp_path, [])
    _render_plist(home, "pipecat.stt-server", port=9565)
    res = _run_just(["stt-list"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    assert any(ln.startswith("stopped") for ln in res.stdout.splitlines())


def test_stt_list_endpointless_plist_is_not_probed_via_label_map(tmp_path):
    # No socket fallback: a plist with no endpoint (here the legacy empty dict)
    # must not be probed at a label-derived socket, and must not break the sweep.
    stub_dir, argv_log = _make_stub_dir(tmp_path, print_loaded=True, status_exit=0)
    home = _home_with_agents(tmp_path, ["pipecat.stt-server.nemotron"])
    res = _run_just(["stt-list"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    assert "pipecat.stt-server.nemotron" in res.stdout
    assert not any("--socket-path" in c for c in _uv_calls(argv_log))


def test_stt_list_empty(tmp_path):
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    res = _run_just(["stt-list"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    assert "no pipecat.stt-server* agents found" in res.stdout
    # No plist -> no probe, in particular no canonical-socket fallback.
    assert not _uv_calls(argv_log)


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil (macOS) not available")
@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"port": 9871}, ["127.0.0.1", "9871", "", ""]),
        ({"socket": True}, ["", "", "SOCK", ""]),
        ({"port": 9765, "token": True}, ["127.0.0.1", "9765", "", "TOKEN"]),
    ],
)
def test_plist_endpoint_helper_four_lines(tmp_path, kwargs, expected):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    sock = home / "s.sock"
    token = home / "tok"
    plist = _render_plist(
        home,
        "pipecat.stt-server.x",
        port=kwargs.get("port"),
        socket_path=sock if kwargs.get("socket") else None,
        auth_token_file=token if kwargs.get("token") else None,
    )
    res = _run_just(["_plist_endpoint", str(plist)], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    lines += [""] * (4 - len(lines))
    want = [str(sock) if v == "SOCK" else str(token) if v == "TOKEN" else v for v in expected]
    assert lines[:4] == want


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


@pytest.mark.parametrize("backend", BACKENDS)
def test_stt_disable_resolves_label_for_every_backend(tmp_path, backend):
    label = EXPECTED[backend][0]
    stub_dir, argv_log = _make_stub_dir(tmp_path, print_loaded=True)
    home = _home_with_agents(tmp_path, [label])
    res = _run_just(["stt-disable", backend], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    boots = [ln for ln in _argv_lines(argv_log) if ln.startswith("launchctl bootout")]
    assert boots == [f"launchctl bootout gui/{FAKE_UID}/{label}"], boots
    assert (home / "Library" / "LaunchAgents" / f"{label}.plist").exists()


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


@pytest.mark.parametrize("backend", BACKENDS)
def test_stt_enable_resolves_label_for_every_backend(tmp_path, backend):
    label = EXPECTED[backend][0]
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [label])
    plist = home / "Library" / "LaunchAgents" / f"{label}.plist"
    res = _run_just(["stt-enable", backend], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    lines = _argv_lines(argv_log)
    assert f"launchctl bootstrap gui/{FAKE_UID} {plist}" in lines, lines
    assert f"launchctl kickstart gui/{FAKE_UID}/{label}" in lines, lines


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
printf 'CMD=%s LABEL=%s PIPECAT_STT_HOST=%s PIPECAT_STT_PORT=%s SOCKET=%s BACKEND=%s\\n' \
  "$1" "$PIPECAT_STT_LABEL" "${{PIPECAT_STT_HOST-}}" "${{PIPECAT_STT_PORT-}}" \
  "${{PIPECAT_STT_SOCKET-}}" "$PIPECAT_STT_BACKEND" \
  >> {env_log!s}
exit 0
""",
    )
    return stub, env_log


@pytest.mark.parametrize("recipe,cmd", [("stt-install", "install"), ("stt-uninstall", "uninstall")])
@pytest.mark.parametrize("backend", BACKENDS)
def test_install_uninstall_delegate_exact_env(tmp_path, recipe, cmd, backend):
    label, port, bk = EXPECTED[backend]
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    fake_script, env_log = _delegation_stub(tmp_path)
    # `script=` overrides the justfile variable so delegation hits our stub.
    res = _run_just(
        [f"script={fake_script}", recipe, backend],
        home=home,
        stub_dir=stub_dir,
    )
    assert res.returncode == 0, res.stderr
    logged = env_log.read_text().strip()
    assert logged.count("\n") == 0, "exactly one delegation expected"
    assert f"CMD={cmd}" in logged
    assert f"LABEL={label} " in logged
    assert "PIPECAT_STT_HOST=127.0.0.1 " in logged
    assert f"PIPECAT_STT_PORT={port} " in logged
    assert f"BACKEND={bk}" in logged
    # TCP is the default: no socket is passed unless the caller opts in.
    assert "SOCKET= " in logged


@pytest.mark.parametrize("recipe", ["stt-install", "stt-uninstall"])
def test_install_uninstall_socket_mode_only_when_caller_sets_it(tmp_path, recipe):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    fake_script, env_log = _delegation_stub(tmp_path)
    sock = home / "Library" / "Caches" / "pipecat-stt" / "parakeet.sock"
    res = _run_just(
        [f"script={fake_script}", recipe, "parakeet"],
        home=home,
        stub_dir=stub_dir,
        extra_env={"PIPECAT_STT_SOCKET": str(sock)},
    )
    assert res.returncode == 0, res.stderr
    logged = env_log.read_text().strip()
    assert f"SOCKET={sock} " in logged
    assert "LABEL=pipecat.stt-server.parakeet " in logged
    assert "BACKEND=parakeet" in logged


def test_install_unknown_backend_does_not_delegate(tmp_path):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    fake_script, env_log = _delegation_stub(tmp_path)
    res = _run_just([f"script={fake_script}", "stt-install", "bogus"], home=home, stub_dir=stub_dir)
    assert res.returncode != 0
    assert not env_log.exists()


# --------------------------------------------------------------------------- #
# _ensure-extra: backend -> Python extra onboarding
# --------------------------------------------------------------------------- #


def test_ensure_extra_skips_under_env_flag(tmp_path):
    # With PIPECAT_STT_SKIP_DEP_SYNC set (the harness default), the helper must
    # short-circuit BEFORE probing or shelling out to `uv sync`.
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    for backend in BACKENDS:
        res = _run_just(["_ensure-extra", backend], home=home, stub_dir=stub_dir)
        assert res.returncode == 0, res.stderr
        assert "skipping" in res.stdout.lower()
    assert not _uv_calls(argv_log)


def test_ensure_extra_unknown_backend_errors_even_when_skipped(tmp_path):
    # Backend validation happens before the skip check, so an unknown backend
    # errors regardless of PIPECAT_STT_SKIP_DEP_SYNC.
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    res = _run_just(["_ensure-extra", "bogus"], home=home, stub_dir=stub_dir)
    assert res.returncode != 0
    assert "unknown backend" in res.stderr


def test_stt_install_invokes_ensure_extra_then_delegates(tmp_path):
    # stt-install must run the dep-ensure step AND still delegate to the install
    # script. Under the skip flag the ensure step is a no-op message, so we can
    # assert both the skip line (proving the wiring) and the delegation.
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    fake_script, env_log = _delegation_stub(tmp_path)
    res = _run_just(
        [f"script={fake_script}", "stt-install", "nemotron"],
        home=home,
        stub_dir=stub_dir,
    )
    assert res.returncode == 0, res.stderr
    assert "skipping" in res.stdout.lower()  # _ensure-extra ran
    assert "CMD=install" in env_log.read_text()  # delegation still happened


# The sync/probe tests below must NOT skip the dep-sync, and must not probe or
# sync the real venvs. The justfile exposes the venv locations as overridable
# `just` variables (like `script`); discover their names from `just --evaluate`
# rather than hardcoding, then point them at a stub interpreter in tmp_path.


def _venv_overrides(tmp_path: Path, stub_dir: Path, home: Path, *, probe_rc: int):
    """Return (args, logs, forced) overriding the venv locations with stubs whose
    ``python`` logs its argv and exits ``probe_rc``.

    ``forced`` is False when the shared ``.venv`` location is not an overridable
    just variable: then the shared-arm probe result depends on the real venv, and
    callers assert only what holds either way.
    """
    res = _run_just(["--evaluate"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    variables = dict(re.findall(r'^(\w+)\s*:=\s*"(.*)"\s*$', res.stdout, flags=re.MULTILINE))
    if not variables:  # fall back to the unquoted `name := value` form
        variables = dict(re.findall(r"^(\w+) := (.*)$", res.stdout, flags=re.MULTILINE))
    kyutai = [k for k, v in variables.items() if v.rstrip("/").endswith(".venv-kyutai")]
    main = [
        k
        for k, v in variables.items()
        if re.search(r"/\.venv(/bin/python)?$", v.rstrip("/")) and k not in kyutai
    ]
    assert len(kyutai) == 1, f"no overridable kyutai venv variable; got {variables}"
    targets = [(kyutai[0], "kyutai")] + ([(main[0], "main")] if main else [])
    args: list[str] = []
    logs: dict[str, Path] = {}
    venvs: dict[str, Path] = {}
    for name, key in targets:
        venv = tmp_path / f"stubvenv-{key}"
        (venv / "bin").mkdir(parents=True)
        log = tmp_path / f"py-{key}.log"
        _write_executable(
            venv / "bin" / "python",
            f"#!/usr/bin/env bash\nprintf 'python %s\\n' \"$*\" >> {log!s}\nexit {probe_rc}\n",
        )
        value = str(venv / "bin" / "python") if variables[name].endswith("python") else str(venv)
        args.append(f"{name}={value}")
        logs[key] = log
        venvs[key] = venv
    logs.setdefault("main", tmp_path / "py-main.log")  # never written => stays empty
    return args, logs, venvs, bool(main)


def _read(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def test_ensure_extra_kyutai_syncs_into_its_own_venv(tmp_path):
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    args, logs, venvs, _forced = _venv_overrides(tmp_path, stub_dir, home, probe_rc=1)
    res = _run_just(
        [*args, "_ensure-extra", "kyutai"],
        home=home,
        stub_dir=stub_dir,
        extra_env={"PIPECAT_STT_SKIP_DEP_SYNC": ""},
    )
    assert res.returncode == 0, res.stderr
    log = _argv_lines(argv_log)
    syncs = [ln for ln in log if ln.startswith("uv sync")]
    assert len(syncs) == 1, log
    assert "--python 3.12" in syncs[0] and "--extra kyutai" in syncs[0], syncs
    # The UV_PROJECT_ENVIRONMENT line logged right after the sync call.
    env_line = log[log.index(syncs[0]) + 1]
    assert env_line == f"uvenv UV_PROJECT_ENVIRONMENT={venvs['kyutai']}", env_line
    # ...and that variable's default location is `.venv-kyutai` in the repo.
    default = _run_just(["--evaluate"], home=home, stub_dir=stub_dir).stdout
    assert re.search(r"/\.venv-kyutai\"?$", default, flags=re.MULTILINE), default
    # Probed the kyutai interpreter with the moshi_mlx import; never the main venv.
    assert "import moshi_mlx" in _read(logs["kyutai"])
    assert _read(logs["main"]) == ""
    # `--inexact` is for the shared .venv arms only.
    assert not any(ln.startswith("uv sync") and "--inexact" in ln and "kyutai" in ln for ln in log)


def test_ensure_extra_kyutai_present_does_not_sync(tmp_path):
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    args, logs, venvs, _forced = _venv_overrides(tmp_path, stub_dir, home, probe_rc=0)
    res = _run_just(
        [*args, "_ensure-extra", "kyutai"],
        home=home,
        stub_dir=stub_dir,
        extra_env={"PIPECAT_STT_SKIP_DEP_SYNC": ""},
    )
    assert res.returncode == 0, res.stderr
    assert "import moshi_mlx" in _read(logs["kyutai"])
    assert not any(ln.startswith("uv sync") for ln in _argv_lines(argv_log))
    assert _read(logs["main"]) == ""


@pytest.mark.parametrize(
    "backend,extra,probe",
    [
        ("whisper", "mlx", "import mlx_whisper"),
        ("parakeet", "parakeet", "import parakeet_mlx"),
        ("nemotron", "nemotron", "from mlx_audio.stt import load"),
    ],
)
def test_ensure_extra_shared_venv_arms_never_touch_kyutai_venv(tmp_path, backend, extra, probe):
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    args, logs, venvs, forced = _venv_overrides(tmp_path, stub_dir, home, probe_rc=1)
    res = _run_just(
        [*args, "_ensure-extra", backend],
        home=home,
        stub_dir=stub_dir,
        extra_env={"PIPECAT_STT_SKIP_DEP_SYNC": ""},
    )
    assert res.returncode == 0, res.stderr
    log = _argv_lines(argv_log)
    syncs = [ln for ln in log if ln.startswith("uv sync")]
    # Never the kyutai venv, whatever the probe outcome.
    assert _read(logs["kyutai"]) == ""
    assert not any("kyutai" in ln for ln in log), log
    for sync in syncs:
        assert sync == f"uv sync --extra {extra} --inexact", sync
        assert log[log.index(sync) + 1] == "uvenv UV_PROJECT_ENVIRONMENT=<unset>"
    if forced:
        # Probe stub fails -> exactly one additive sync into the shared .venv.
        assert len(syncs) == 1, log
        assert probe in _read(logs["main"])


# --------------------------------------------------------------------------- #
# Koda-safety: the branch diff stays within the additive file set
# --------------------------------------------------------------------------- #


# The Koda-consumed *imported* surface: the modules Koda pins at a SHA and
# imports as a library. Per the cross-repo contract, a pin bump is required only
# when these change — the client entrypoints (``TranscriptionClient``,
# ``resolve_endpoint_from_env``, ``is_cleartext_remote``) and the wire-protocol
# module (``PROTOCOL_VERSION`` + event constants). The rest of ``stt_server/``
# (the server runtime, backends, ``__main__``) and ``scripts/install_stt_agent.sh``
# are the *runtime* Koda runs from a HEAD checkout — server-side changes there are
# coordinated at checkout-update time, NOT via the pinned import, so they are not
# gated here. A ``server.hello`` / ``server.status`` payload change is still
# caught because it must bump ``PROTOCOL_VERSION`` in ``protocol.py``.
KODA_IMPORT_SURFACE = frozenset({"stt_server/client.py", "stt_server/protocol.py"})


def test_branch_diff_does_not_touch_koda_surface():
    """No-pin-bump invariant (per the cross-repo contract): this work must not
    modify the Koda-consumed *imported* surface — ``stt_server/client.py`` or
    ``stt_server/protocol.py`` — without a coordinated client pin bump. Changes to
    the server runtime / install script (run from a HEAD checkout) and additive
    files (justfile, these tests, README, dev-plan docs) are fine; this asserts
    the *negative* contract rather than an exact file allowlist."""
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
    forbidden = sorted(c for c in changed if c in KODA_IMPORT_SURFACE)
    assert not forbidden, (
        f"branch modifies the Koda-consumed IMPORTED surface (would require a "
        f"coordinated client pin bump): {forbidden}"
    )
