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
  (``_plist_field``, one keyed field per read), probes TCP/socket/token-file
  agents, always exits 0.
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
    unload_polls: int = 0,
) -> tuple[Path, Path]:
    """Stub ``launchctl`` / ``id`` / ``uv`` that log argv to ``argv.log``.

    ``print_loaded`` controls ``launchctl print``'s exit code (0 == agent
    loaded). ``status_exit`` controls the exit code of the stubbed
    ``uv run --no-sync python -m stt_server status`` probe (non-zero == stopped socket).
    ``fail_actions`` names ``launchctl`` subcommands (e.g. ``bootout``,
    ``bootstrap``, ``kickstart``) that should exit non-zero, so recipe
    error-handling on a failed state change can be exercised.
    ``unload_polls`` models asynchronous teardown: after a successful
    ``bootout``, ``print`` still reports the job loaded for that many calls
    (``-1``: forever), then reports it gone; a ``bootstrap`` while it is still
    listed fails with exit 5, as launchd does in that window.
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
TEARDOWN={stub_dir!s}/teardown-polls
if [[ -e "$TEARDOWN" ]]; then
    n=$(cat "$TEARDOWN")
    if [[ "$1" == "print" ]]; then
        if [[ "$n" -eq 0 ]]; then exit 1; fi
        if [[ "$n" -gt 0 ]]; then printf '%s' $((n - 1)) > "$TEARDOWN"; fi
        exit 0
    fi
    if [[ "$1" == "bootstrap" && "$n" -ne 0 ]]; then
        echo "Bootstrap failed: 5: Input/output error" >&2
        exit 5
    fi
fi
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
if [[ "$1" == "bootout" ]]; then printf '%s' {unload_polls} > "$TEARDOWN"; fi
if [[ "$1" == "bootstrap" ]]; then rm -f "$TEARDOWN"; fi
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

    # uv stub: log argv; for `uv run --no-sync python -m stt_server status ...` emit a
    # status-shaped line and exit `status_exit`. Dispatch is argv-position-aware
    # (run / --no-sync / -m stt_server / status), not a substring match on the whole arg
    # string, so it fails loud if the `uv run … status` invocation shape changes.
    _write_executable(
        stub_dir / "uv",
        f"""#!/usr/bin/env bash
printf 'uv %s\\n' "$*" >> {argv_log!s}
printf 'uvenv UV_PROJECT_ENVIRONMENT=%s\\n' "${{UV_PROJECT_ENVIRONMENT-<unset>}}" >> {argv_log!s}
if [[ "$1" == "sync" ]]; then touch {stub_dir!s}/uv-synced; fi
if [[ "$1" == "run" && "$2" == "--no-sync" && "$5" == "stt_server" && "$6" == "status" ]]; then
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
        # Any named row counts (not a hard-coded backend list): a docs-only new
        # backend must show up in the table so the set-equality check catches it.
        m = re.match(r"([A-Za-z][\w-]*)", cells[0])
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


def _recipe_body(name: str) -> str:
    """The text of one justfile recipe (its header line through the next
    top-level, non-indented, non-comment line)."""
    lines = JUSTFILE.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if re.match(rf"{re.escape(name)}\b.*:\s*$", ln))
    body = [lines[start]]
    for ln in lines[start + 1 :]:
        if ln and not ln[0].isspace() and not ln.startswith("#"):
            break
        body.append(ln)
    return "\n".join(body)


def _case_arm_backends(recipe: str) -> set[str]:
    return set(
        re.findall(
            r"^\s+(\w+)\)\s+(?:printf|extra=|py=|label=)", _recipe_body(recipe), re.MULTILINE
        )
    )


def _valid_line_backends(recipe: str) -> set[str]:
    m = re.search(r"\(valid: ([^)]*)\)", _recipe_body(recipe))
    assert m, f"no 'valid:' error line in {recipe}"
    return {b.strip() for b in m.group(1).split(",")}


def test_backend_set_is_derived_from_resolve_and_mirrored_everywhere():
    """The authoritative backend set is `_resolve`'s case arms. Its `valid:` error
    line, `_ensure-extra`'s arms, the docs port table, the test's BACKENDS and
    EXPECTED must all equal it, so a backend added in only one place fails."""
    from_resolve = _case_arm_backends("_resolve")
    assert from_resolve, "could not parse _resolve case arms"
    assert _valid_line_backends("_resolve") == from_resolve
    assert _case_arm_backends("_ensure-extra") == from_resolve
    assert _valid_line_backends("_ensure-extra") == from_resolve
    assert _case_arm_backends("_agent-python") == from_resolve
    assert _valid_line_backends("_agent-python") == from_resolve
    assert set(_readme_map()) == from_resolve
    assert set(BACKENDS) == from_resolve
    assert set(EXPECTED) == from_resolve


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


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil (macOS) not available")
@pytest.mark.parametrize("backend", BACKENDS)
def test_port_table_matches_installer_default(tmp_path, backend):
    _label, port = _readme_map()[backend]
    res = _resolve(backend, tmp_path)
    assert res.returncode == 0, res.stderr
    installer_backend = res.stdout.splitlines()[3]
    host, rendered_port = _installer_default_port(tmp_path, installer_backend)
    assert host == "127.0.0.1"
    assert rendered_port == str(port), f"{backend}: installer default port drifted from the docs"


def _installer_port(tmp_path: Path, *args: str, env: dict[str, str] | None = None):
    """Run the REAL installer's ``port`` subcommand under stub launchctl/id/uv
    (which log every call) in an empty temp HOME and cwd; return the result,
    the stub log lines and every path that exists under HOME and cwd afterwards."""
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    home = tmp_path / "porthome"
    cwd = tmp_path / "portcwd"
    home.mkdir()
    cwd.mkdir()
    res = subprocess.run(
        ["bash", str(INSTALLER), "port", *args],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "HOME": str(home),
            "PATH": f"{stub_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            **(env or {}),
        },
        cwd=str(cwd),
    )
    left = sorted(str(p) for root in (home, cwd) for p in root.rglob("*"))
    return res, _argv_lines(argv_log), left


# One backend -> port map, in the installer. Invariant: the docs table, the
# installer's `port` subcommand, `_resolve` and a really-rendered default plist
# give the same port for every backend; `port` calls no launchctl (nor anything
# the stubs log) and leaves HOME and cwd untouched; a backend with no canonical
# port (echo, unknown) exits 1 with nothing on stdout. State space: backend
# {the docs rows} + {echo, bogus, empty} x form {argument, PIPECAT_STT_BACKEND}.
@pytest.mark.parametrize("form", ["arg", "env"])
@pytest.mark.parametrize("backend", BACKENDS)
def test_installer_port_subcommand_is_the_port_map_and_touches_nothing(tmp_path, backend, form):
    _label, port = _readme_map()[backend]
    bk = EXPECTED[backend][2]
    args, env = ([bk], {}) if form == "arg" else ([], {"PIPECAT_STT_BACKEND": bk})
    res, calls, left = _installer_port(tmp_path, *args, env=env)
    assert res.returncode == 0, res.stderr
    assert res.stdout == f"{port}\n", f"{backend}: installer port drifted from the docs table"
    assert not calls, f"port ran a stubbed command: {calls}"
    assert not left, f"port touched the filesystem: {left}"


@pytest.mark.parametrize("backend", ["echo", "bogus", "MLX", "whisper"])
def test_installer_port_subcommand_refuses_a_backend_without_one(tmp_path, backend):
    res, calls, left = _installer_port(tmp_path, backend)
    assert res.returncode == 1, (res.returncode, res.stdout)
    assert res.stdout == ""
    assert "no canonical port" in res.stderr
    assert not calls and not left


def test_justfile_keeps_no_port_map():
    """The justfile asks the installer for every port: none of its code lines
    carries a canonical port number, and _resolve calls the real installer's
    `port` subcommand (never the overridable `script`). The installer holds the
    map exactly once."""
    ports = {str(p) for _label, p, _bk in EXPECTED.values()}
    code = [ln for ln in JUSTFILE.read_text().splitlines() if not ln.lstrip().startswith("#")]
    offenders = [ln for ln in code if any(re.search(rf"\b{p}\b", ln) for p in ports)]
    assert not offenders, offenders
    body = _recipe_body("_resolve")
    assert 'scripts/install_stt_agent.sh" port "$bk"' in body, body
    assert "{{script}}" not in body
    inst = [ln for ln in INSTALLER.read_text().splitlines() if not ln.lstrip().startswith("#")]
    for p in ports:
        assert sum(bool(re.search(rf"\b{p}\b", ln)) for ln in inst) == 1, p


def test_resolve_calls_no_launchctl(tmp_path):
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    for backend in BACKENDS:
        assert _run_just(["_resolve", backend], home=home, stub_dir=stub_dir).returncode == 0
    assert not [ln for ln in _argv_lines(argv_log) if ln.startswith("launchctl")]


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


def test_stt_status_no_argument_delegates_to_stt_list(tmp_path):
    stub_dir, argv_log = _make_stub_dir(tmp_path, print_loaded=True, status_exit=0)
    home = _home_with_agents(tmp_path, [])
    _render_plist(home, "pipecat.stt-server.custom", backend="nemotron", port=9871)
    res = _run_just(["stt-status"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    # stt-list output: the agent and its live endpoint.
    assert "pipecat.stt-server.custom" in res.stdout
    assert "127.0.0.1:9871" in res.stdout
    # Probed via stt-list's plist sweep, not a single canonical probe.
    call = _status_call(argv_log)
    assert "--host 127.0.0.1 --port 9871" in call, call


def test_stt_status_no_argument_forwards_la_dir_override(tmp_path):
    """`just la_dir=X stt-status` must sweep X, not the real LaunchAgents dir."""
    stub_dir, argv_log = _make_stub_dir(tmp_path, print_loaded=True, status_exit=0)
    home = _home_with_agents(tmp_path, [])
    other = tmp_path / "other-home"
    _render_plist(other, "pipecat.stt-server.elsewhere", backend="nemotron", port=9872)
    la_dir = other / "Library" / "LaunchAgents"
    res = _run_just([f"la_dir={la_dir}", "stt-status"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    assert "pipecat.stt-server.elsewhere" in res.stdout, res.stdout
    assert "no pipecat.stt-server* agents found" not in res.stdout


def test_stt_status_no_argument_with_no_agents_exits_zero(tmp_path):
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    res = _run_just(["stt-status"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    assert "no pipecat.stt-server* agents found" in res.stdout
    assert not _uv_calls(argv_log)


def test_stt_status_bare_name_with_socket_plist_probes_its_socket(tmp_path):
    """Regression: an agent still installed as a socket agent must be probed at
    its socket, not at the canonical TCP port (which nothing listens on)."""
    stub_dir, argv_log = _make_stub_dir(tmp_path, status_exit=0)
    home = _home_with_agents(tmp_path, [])
    sock = home / "Library" / "Caches" / "pipecat-stt" / "nemotron.sock"
    _render_plist(home, "pipecat.stt-server.nemotron", backend="nemotron", socket_path=sock)
    res = _run_just(["stt-status", "nemotron"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    call = _status_call(argv_log)
    assert f"--socket-path {sock}" in call, call
    assert "--host" not in call and "--port" not in call and "9865" not in call
    assert _timeout_of(call) > 3


def test_stt_status_bare_name_with_tcp_plist_probes_plist_port(tmp_path):
    stub_dir, argv_log = _make_stub_dir(tmp_path, status_exit=0)
    home = _home_with_agents(tmp_path, [])
    _render_plist(home, "pipecat.stt-server.nemotron", backend="nemotron", port=9871)
    res = _run_just(["stt-status", "nemotron"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    call = _status_call(argv_log)
    assert "--host 127.0.0.1 --port 9871" in call, call
    assert "9865" not in call and "--auth-token-file" not in call
    assert _timeout_of(call) > 3


def test_stt_status_bare_name_forwards_auth_token_file(tmp_path):
    stub_dir, argv_log = _make_stub_dir(tmp_path, status_exit=0)
    home = _home_with_agents(tmp_path, [])
    token = home / ".config" / "pipecat-stt" / "token"
    _render_plist(
        home, "pipecat.stt-server.parakeet", backend="parakeet", port=9765, auth_token_file=token
    )
    res = _run_just(["stt-status", "parakeet"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    call = _status_call(argv_log)
    assert "--host 127.0.0.1 --port 9765" in call, call
    assert f"--auth-token-file {token}" in call, call


def test_stt_status_bare_name_without_plist_falls_back_to_canonical(tmp_path):
    stub_dir, argv_log = _make_stub_dir(tmp_path, status_exit=0)
    home = _home_with_agents(tmp_path, [])
    # A plist for a DIFFERENT backend must not influence this one.
    _render_plist(home, "pipecat.stt-server.parakeet", backend="parakeet", port=9999)
    res = _run_just(["stt-status", "nemotron"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    call = _status_call(argv_log)
    assert "--host 127.0.0.1 --port 9865" in call, call
    assert "--auth-token-file" not in call


def test_stt_status_bare_name_with_endpointless_plist_errors(tmp_path):
    stub_dir, argv_log = _make_stub_dir(tmp_path, status_exit=0)
    home = _home_with_agents(tmp_path, ["pipecat.stt-server.nemotron"])
    res = _run_just(["stt-status", "nemotron"], home=home, stub_dir=stub_dir)
    assert res.returncode == 1
    assert "no serve endpoint" in res.stderr
    assert not _uv_calls(argv_log)


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


READ_PLIST = REPO_ROOT / "scripts" / "read_stt_plist.py"
# Every field the reader serves, by name (scripts/read_stt_plist.py FIELDS).
FIELDS = ("host", "port", "socket-path", "auth-token-file", "interpreter")


def _read_plist(plist: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(READ_PLIST), *extra, str(plist)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _read_field(plist: Path, name: str, *, strict: bool = False) -> subprocess.CompletedProcess:
    return _read_plist(plist, *(["--strict"] if strict else []), "--field", name)


def _read_fields(plist: Path, *, strict: bool = False) -> dict[str, subprocess.CompletedProcess]:
    return {name: _read_field(plist, name, strict=strict) for name in FIELDS}


def _fields(**kw: str) -> dict[str, str]:
    """An expected field map: every field empty unless given (``socket_path=``
    stands for ``socket-path``, and so on)."""
    want = dict.fromkeys(FIELDS, "")
    want.update({k.replace("_", "-"): v for k, v in kw.items()})
    return want


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil (macOS) not available")
@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"port": 9871}, {"host": "127.0.0.1", "port": "9871"}),
        ({"socket": True}, {"socket_path": "SOCK"}),
        (
            {"port": 9765, "token": True},
            {"host": "127.0.0.1", "port": "9765", "auth_token_file": "TOKEN"},
        ),
    ],
)
def test_plist_field_helper_is_keyed(tmp_path, kwargs, expected):
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
    subst = {"SOCK": str(sock), "TOKEN": str(token)}
    want = _fields(interpreter=sys.executable, **{k: subst.get(v, v) for k, v in expected.items()})
    got = {}
    for name in FIELDS:
        res = _run_just(["_plist_field", str(plist), name], home=home, stub_dir=stub_dir)
        assert res.returncode == 0, res.stderr
        got[name] = res.stdout
    assert got == want


def test_stt_list_keeps_a_recorded_token_path_ending_in_a_newline(tmp_path):
    """``$(...)`` drops trailing newlines: the probe was handed ``/h/tok`` for a plist
    recording ``/h/tok\\n``. The capture appends and strips a sentinel, so the path
    the status probe gets is the recorded one, newline included."""
    stub_dir, argv_log = _make_stub_dir(tmp_path, print_loaded=True, status_exit=0)
    home = _home_with_agents(tmp_path, [])
    token = home / "tok\n"
    plist = home / "Library" / "LaunchAgents" / "pipecat.stt-server.nl.plist"
    plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "pipecat.stt-server.nl",
                "ProgramArguments": [
                    "py",
                    "-m",
                    "stt_server",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "9871",
                    "--auth-token-file",
                    str(token),
                ],
            }
        )
    )
    res = _run_just(["stt-list"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    # The stub logs "$*" then a newline; ``token`` already ends in one, so the
    # recorded path leaves a blank line (a stripped path would not).
    log = argv_log.read_text()
    assert f"--auth-token-file {token}\n" in log, log


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil (macOS) not available")
def test_plist_endpoint_and_stt_list_keep_paths_with_spaces(tmp_path):
    """Regression: bare ``read -r`` trims leading/trailing IFS whitespace, so a
    socket / token-file path ending in a space was probed at the wrong path."""
    stub_dir, argv_log = _make_stub_dir(tmp_path, print_loaded=True, status_exit=0)
    home = _home_with_agents(tmp_path, [])
    sock = home / "Library" / "Caches" / "my pipecat" / "stt sock "
    token = home / "my config" / "token "
    plist = _render_plist(home, "pipecat.stt-server.spaced", port=9871, auth_token_file=token)
    res = _run_just(["_plist_field", str(plist), "auth-token-file"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    assert res.stdout == str(token)
    # stt-list: token-file forwarded verbatim (trailing space preserved).
    res = _run_just(["stt-list"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    call = _status_call(argv_log)
    assert call.endswith(f"--auth-token-file {token}"), repr(call)
    # Socket path with spaces, incl. a trailing one.
    argv_log.unlink()
    plist.unlink()
    _render_plist(home, "pipecat.stt-server.spaced", socket_path=sock)
    res = _run_just(["stt-list"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    call = _status_call(argv_log)
    assert call.endswith(f"--socket-path {sock}"), repr(call)


def _plist_shape(home: Path, case: str) -> tuple[Path, dict[str, str]]:
    """Render/write one plist shape; return it and its expected field map."""
    label = "pipecat.stt-server.x"
    plist = home / "Library" / "LaunchAgents" / f"{label}.plist"
    py = sys.executable
    if case == "tcp":
        _render_plist(home, label, port=9871)
        return plist, _fields(host="127.0.0.1", port="9871", interpreter=py)
    if case == "socket":
        _render_plist(home, label, socket_path=home / "s.sock")
        return plist, _fields(socket_path=str(home / "s.sock"), interpreter=py)
    if case == "token":
        _render_plist(home, label, port=9765, auth_token_file=home / "tok")
        return plist, _fields(
            host="127.0.0.1", port="9765", auth_token_file=str(home / "tok"), interpreter=py
        )
    if case == "spaces":
        sock = home / "my dir" / "s sock "
        _render_plist(home, label, socket_path=sock)
        return plist, _fields(socket_path=str(sock), interpreter=py)
    args = {
        "quote-in-path": ["py", "--socket-path", '/h/a"b/s.sock'],
        "flag-like-value": ["py", "--log-level", "--socket-path", "/h/x.sock"],
        # A hostile value holding a newline cannot become another field.
        "newline-in-value": ["/p y\n", "--host", "127.0.0.1\nsocket-path=/evil", "--port", "9"],
        "no-args": [],
    }.get(case)
    if args is not None:
        plist.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": args}))
    want = {
        "quote-in-path": _fields(socket_path='/h/a"b/s.sock', interpreter="py"),
        # --socket-path is the VALUE of --log-level here, so it is not a flag.
        "flag-like-value": _fields(interpreter="py"),
        "newline-in-value": _fields(
            host="127.0.0.1\nsocket-path=/evil", port="9", interpreter="/p y\n"
        ),
    }.get(case, _fields())
    return plist, want


_SHAPES = [
    "tcp",
    "socket",
    "token",
    "spaces",
    "quote-in-path",
    "flag-like-value",
    "newline-in-value",
    "absent",
    "no-args",
]


@pytest.mark.parametrize("case", _SHAPES)
def test_plist_reader_shapes(tmp_path, case):
    """The one reader of the renderer's ProgramArguments (justfile
    ``_plist_field`` and the installer both call it) gives every field, by name,
    for every plist shape, in both the sweep and the --strict form."""
    home = _home_with_agents(tmp_path, [])
    plist, want = _plist_shape(home, case)
    stub_dir, _ = _make_stub_dir(tmp_path)
    for strict in (False, True):
        got = _read_fields(plist, strict=strict)
        assert all(r.returncode == 0 for r in got.values()), {k: r.stderr for k, r in got.items()}
        assert {k: r.stdout for k, r in got.items()} == want, f"strict={strict}"
    # The justfile recipe is a pass-through to the same reader, field by field.
    for name in FIELDS:
        via_just = _run_just(["_plist_field", str(plist), name], home=home, stub_dir=stub_dir)
        assert via_just.returncode == 0, via_just.stderr
        assert via_just.stdout == want[name], name


def test_plist_reader_refuses_the_positional_forms(tmp_path):
    """The four-line positional protocol is gone: a call without ``--field NAME``
    (or with an unknown NAME) is a usage error, never a line-ordered dump."""
    plist, _want = _plist_shape(_home_with_agents(tmp_path, []), "quote-in-path")
    for extra in (
        [],
        ["--strict"],
        ["--field"],
        ["--field", "bogus"],
        ["--strict", "--field", "x"],
    ):
        res = _read_plist(plist, *extra)
        assert res.returncode == 2, (extra, res.stdout)
        assert res.stdout == "", extra


def test_consumers_read_the_plist_by_field_name():
    """No consumer may split the reader's output by position: every call site in
    the justfile and the installer names one field (``--field`` / ``_plist_field
    PLIST NAME``), and none reads several lines from one call."""
    installer = INSTALLER.read_text()
    calls = [
        ln for ln in installer.splitlines() if "$READ_PLIST_PY" in ln and "READ_PLIST_PY=" not in ln
    ]
    assert calls, "installer no longer calls the reader?"
    for ln in calls:
        assert "--field" in ln or "--has-auth" in ln, ln
    just_calls = [ln for ln in JUSTFILE.read_text().splitlines() if "_plist_field" in ln]
    for ln in just_calls:
        if ln.lstrip().startswith("#") or ln.startswith("_plist_field"):
            continue
        assert re.search(
            r'_plist_field "\$plist" (host|port|socket-path|auth-token-file|interpreter)\b', ln
        ), ln
    reader_line = next(
        ln for ln in JUSTFILE.read_text().splitlines() if "read_stt_plist.py" in ln and "exec" in ln
    )
    assert "--field" in reader_line, reader_line
    assert "_plist_endpoint" not in JUSTFILE.read_text() + installer


def test_plist_reader_garbage_plist_is_empty_not_an_error(tmp_path):
    bad = tmp_path / "bad.plist"
    bad.write_text("not a plist")
    for name, res in _read_fields(bad).items():
        assert res.returncode == 0, name
        assert res.stdout == "", name
    # Garbage is an error for the auth guard, never "no token" (exit 3).
    assert _read_plist(bad, "--has-auth").returncode == 2


# Reader contract (scripts/read_stt_plist.py): the --field form is a sweep and
# exits 0 for every state, an empty value for an absent or malformed plist; the
# --has-auth form exits 0 (token), 3 (absent, or readable without a token) or 2
# (malformed: it cannot tell). State space: absent, valid-no-auth,
# valid-env-token, valid-token-file, truncated XML, garbage, wrong top-level
# type, wrong ProgramArguments type, a non-string argument, wrong
# EnvironmentVariables type, directory; x every field.
_VALID = {"host": "127.0.0.1", "port": "9765", "interpreter": "py"}
_READER_STATES = {
    # state: (fields, --has-auth status)
    "absent": (_fields(), 3),
    "valid-no-auth": (_fields(**_VALID), 3),
    "valid-env-token": (_fields(**_VALID), 0),
    "valid-token-file": (_fields(**_VALID, auth_token_file="/h/tok"), 0),
    "truncated": (_fields(), 2),
    "garbage": (_fields(), 2),
    "wrong-type": (_fields(), 2),
    "wrong-type-args": (_fields(), 2),
    "wrong-type-arg-element": (_fields(), 2),
    "wrong-type-env": (_fields(), 2),
    "directory": (_fields(), 2),
}


def _reader_state_plist(path: Path, state: str) -> Path:
    args = ["py", "-m", "stt_server", "--host", "127.0.0.1", "--port", "9765"]
    env: dict = {"PATH": "/usr/bin"}
    full: dict = {"Label": "x", "ProgramArguments": args, "EnvironmentVariables": env}
    if state == "absent":
        return path
    if state == "valid-env-token":
        env["PIPECAT_STT_AUTH_TOKEN"] = "s3cret"
    elif state == "valid-token-file":
        args += ["--auth-token-file", "/h/tok"]
    elif state == "truncated":
        # A token-carrying plist cut off mid-write: plistlib raises ExpatError.
        env["PIPECAT_STT_AUTH_TOKEN"] = "s3cret"
        blob = plistlib.dumps(full)
        path.write_bytes(blob[: len(blob) // 2])
        return path
    elif state == "garbage":
        path.write_text("not a plist")
        return path
    elif state == "wrong-type":
        path.write_bytes(plistlib.dumps(["py", "--auth-token-file", "/h/tok"]))
        return path
    elif state == "wrong-type-args":
        full["ProgramArguments"] = "py --auth-token-file /h/tok"
    elif state == "wrong-type-arg-element":
        # launchd takes strings only; a dict has no interpreter path to report.
        full["ProgramArguments"] = [{"py": 1}, "--auth-token-file", "/h/tok"]
    elif state == "wrong-type-env":
        full["EnvironmentVariables"] = ["PIPECAT_STT_AUTH_TOKEN=s3cret"]
    elif state == "directory":
        path.mkdir()
        return path
    path.write_bytes(plistlib.dumps(full))
    return path


@pytest.mark.parametrize("state", sorted(_READER_STATES))
def test_plist_reader_state_space(tmp_path, state):
    want, want_auth = _READER_STATES[state]
    plist = _reader_state_plist(tmp_path / "a.plist", state)
    got = _read_fields(plist)
    for name, res in got.items():
        assert res.returncode == 0, (name, res.stderr)
        assert res.stdout == want[name], name
        assert "Traceback" not in res.stderr
        assert "s3cret" not in res.stderr, "token value leaked"
        if want_auth == 2:
            assert "cannot read plist" in res.stderr, name
    auth = _read_plist(plist, "--has-auth")
    assert auth.returncode == want_auth, auth.stderr
    assert "Traceback" not in auth.stderr
    assert "s3cret" not in auth.stderr, "token value leaked"
    if want_auth == 2:
        assert "cannot read plist" in auth.stderr


@pytest.mark.parametrize("state", sorted(_READER_STATES))
def test_plist_reader_strict_form_state_space(tmp_path, state):
    """``--strict`` (the installer's and stt-enable's form) gives the sweep's
    value for an absent or readable plist, and exits 2 with no output for a
    malformed one, for every field, so a malformed plist can never read as "no
    socket" (or "no interpreter")."""
    want, want_auth = _READER_STATES[state]
    plist = _reader_state_plist(tmp_path / "a.plist", state)
    for name, res in _read_fields(plist, strict=True).items():
        assert "Traceback" not in res.stderr and "s3cret" not in res.stderr
        if want_auth == 2:
            assert res.returncode == 2, (name, res.stderr)
            assert res.stdout == ""
            assert "cannot read plist" in res.stderr
        else:
            assert res.returncode == 0, (name, res.stderr)
            assert res.stdout == want[name], name


# Reader <-> server flag grammar. Invariant: for every serve argv the server's
# argparse parser accepts, the reader returns the same host, port, socket-path
# and auth-token-file the server would use, and --has-auth is 0 exactly when the
# server runs with a token file. An argv the parser rejects starts no listener,
# so only acceptance is pinned for those (abbreviations must be rejected: the
# reader matches full names only). State space: flag form {space, '=', empty
# '=', abbreviated (space/'='), after a no-value switch, repeated (mixed forms,
# last wins), value with '=', flag-like value} x the four endpoint flags.
_GRAMMAR_CASES = {
    # case: (serve argv tail, accepted by the serve parser)
    "space-form": (["--host", "127.0.0.1", "--port", "9565"], True),
    "eq-form": (["--host=::1", "--port=9765"], True),
    "eq-socket": (["--socket-path=/h/s.sock"], True),
    "token-space": (["--auth-token-file", "/h/tok"], True),
    "token-eq": (["--auth-token-file=/h/tok"], True),
    "token-eq-empty": (["--auth-token-file="], True),
    "token-value-has-eq": (["--auth-token-file", "/h/a=b"], True),
    "after-switch": (["--supervised", "--auth-token-file", "/h/tok"], True),
    "switch-after": (["--auth-token-file=/h/tok", "--supervised", "--port", "9"], True),
    "repeat-eq-last": (["--auth-token-file", "/h/a", "--auth-token-file=/h/b"], True),
    "repeat-space-last": (["--auth-token-file=/h/a", "--auth-token-file", "/h/b"], True),
    "among-others": (["--model", "m", "--auth-token-file", "/h/t", "--backend", "mlx"], True),
    "eq-value-flag-like": (["--log-level=--socket-path", "--port", "9"], True),
    "port-leading-zero": (["--port", "09565"], True),
    "abbrev-token-space": (["--auth-token", "/h/tok"], False),
    "abbrev-token-eq": (["--auth=/h/tok"], False),
    "abbrev-socket": (["--sock", "/h/s.sock"], False),
    "abbrev-port-eq": (["--po=9565"], False),
    "flag-like-value": (["--log-level", "--socket-path", "/h/x.sock"], False),
}


def _serve_subparser():
    import argparse

    from stt_server.__main__ import _build_parser

    sub = next(a for a in _build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    return sub.choices["serve"]


def _load_reader():
    import importlib.util

    spec = importlib.util.spec_from_file_location("read_stt_plist", READ_PLIST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("case", sorted(_GRAMMAR_CASES))
def test_plist_reader_matches_serve_parser(tmp_path, case):
    from stt_server.__main__ import _build_parser

    tail, accepted = _GRAMMAR_CASES[case]
    try:
        args = _build_parser().parse_args(["serve", *tail])
    except SystemExit:
        args = None
    assert (args is not None) == accepted, f"{case}: serve parser acceptance changed"
    if args is None:
        return
    plist = tmp_path / "a.plist"
    plist.write_bytes(
        plistlib.dumps({"Label": "x", "ProgramArguments": ["py", "-m", "stt_server", *tail]})
    )
    got = _read_fields(plist)
    assert all(r.returncode == 0 for r in got.values()), {k: r.stderr for k, r in got.items()}
    host, port, sock, token = (
        got[n].stdout for n in ("host", "port", "socket-path", "auth-token-file")
    )
    assert got["interpreter"].stdout == "py"
    assert host == (args.host or "")
    assert (int(port) if port else None) == args.port
    assert sock == (args.socket_path or "")
    assert token == (args.auth_token_file or "")
    has_auth = _read_plist(plist, "--has-auth").returncode
    assert has_auth == (0 if args.auth_token_file else 3), f"{case}: --has-auth disagrees"


def test_plist_reader_switches_match_serve_parser():
    """The reader's no-value switches are exactly the serve parser's, and the
    parser takes full flag names only (the reader does no prefix matching)."""
    serve = _serve_subparser()
    reader = _load_reader()
    assert serve.allow_abbrev is False
    switches = {o for a in serve._actions if a.nargs == 0 for o in a.option_strings}
    assert {o for o in switches if o.startswith("--")} == set(reader._SWITCHES)
    takes_value = {o for a in serve._actions if a.nargs != 0 for o in a.option_strings}
    assert set(reader._FLAGS) <= takes_value


@pytest.mark.parametrize("state", ["truncated", "garbage", "wrong-type", "valid-token-file"])
def test_stt_list_sweep_survives_malformed_plist(tmp_path, state):
    """stt-list exits 0 and shows the no-endpoint line for a malformed plist,
    with a one-line warning and no traceback; a readable one still shows its
    endpoint."""
    home = _home_with_agents(tmp_path, [])
    _reader_state_plist(home / "Library" / "LaunchAgents" / "pipecat.stt-server.x.plist", state)
    stub_dir, _ = _make_stub_dir(tmp_path)
    res = _run_just(["stt-list"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    assert "Traceback" not in res.stderr
    if state == "valid-token-file":
        assert "endpoint: 127.0.0.1:9765" in res.stdout
    else:
        assert "no serve endpoint found in plist" in res.stdout
        assert "cannot read plist" in res.stderr


@pytest.mark.parametrize(
    "env,args,want",
    [
        ({"PIPECAT_STT_AUTH_TOKEN": "x"}, ["py"], 0),
        ({"KODA_STT_AUTH_TOKEN": "x"}, ["py"], 0),
        ({"PIPECAT_STT_AUTH_TOKEN": ""}, ["py"], 3),
        ({}, ["py", "--auth-token-file", "/h/tok"], 0),
        ({}, ["py", "--log-level", "--auth-token-file"], 3),
        ({}, ["py", "--port", "9"], 3),
    ],
)
def test_plist_reader_has_auth(tmp_path, env, args, want):
    plist = tmp_path / "a.plist"
    plist.write_bytes(
        plistlib.dumps({"Label": "x", "ProgramArguments": args, "EnvironmentVariables": env})
    )
    assert _read_plist(plist, "--has-auth").returncode == want


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


# bootout is asynchronous: launchd lists the job until it has torn it down, and
# a bootstrap in that window fails with error 5. Invariant: stt-disable reports
# "booted out" only once launchd no longer lists the label, so an stt-enable
# right after it bootstraps cleanly; still listed at its 30 s cap, it warns and
# exits 0 without "booted out". State space: teardown {immediate, 3 polls,
# never}, then stt-enable.
@pytest.mark.parametrize("unload_polls", [0, 3, -1])
def test_stt_disable_waits_for_teardown_before_enable(tmp_path, unload_polls):
    stub_dir, argv_log = _make_stub_dir(tmp_path, print_loaded=True, unload_polls=unload_polls)
    _write_executable(stub_dir / "sleep", "#!/usr/bin/env bash\nexit 0\n")
    label = "pipecat.stt-server"
    home = _home_with_agents(tmp_path, [label])
    res = _run_just(["stt-disable", "whisper"], home=home, stub_dir=stub_dir)
    assert res.returncode == 0, res.stderr
    lines = _argv_lines(argv_log)
    out = lines.index(f"launchctl bootout gui/{FAKE_UID}/{label}")
    polls = [ln for ln in lines[out:] if ln == f"launchctl print gui/{FAKE_UID}/{label}"]
    if unload_polls < 0:
        assert "still loaded" in res.stderr, res.stderr
        assert "booted out" not in res.stdout
        assert len(polls) == 31, polls  # bounded: the 30 s cap, one poll a second
        return
    # Polled until launchd stopped listing it: N "loaded" answers plus the "gone" one.
    assert len(polls) == unload_polls + 1, polls
    assert "booted out" in res.stdout
    enable = _run_just(["stt-enable", "whisper"], home=home, stub_dir=stub_dir)
    assert enable.returncode == 0, f"stdout={enable.stdout!r} stderr={enable.stderr!r}"
    assert "Bootstrap failed" not in enable.stderr


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
printf 'CMD=%s LABEL=%s PIPECAT_STT_HOST=%s PIPECAT_STT_PORT=%s SOCKET=%s BACKEND=%s PYTHON=%s\\n' \
  "$1" "$PIPECAT_STT_LABEL" "${{PIPECAT_STT_HOST-}}" "${{PIPECAT_STT_PORT-}}" \
  "${{PIPECAT_STT_SOCKET-}}" "$PIPECAT_STT_BACKEND" "${{PIPECAT_STT_PYTHON-<unset>}}" \
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
    # The recipe passes no HOST/PORT: the installer's own canonical defaults apply
    # (pinned to the port table by the installer and mirror tests).
    assert "PIPECAT_STT_HOST= PIPECAT_STT_PORT= " in logged, logged
    assert f"BACKEND={bk}" in logged
    # TCP is the default: no socket is passed unless the caller opts in.
    assert "SOCKET= " in logged


@pytest.mark.parametrize("sock_var", ["PIPECAT_STT_SOCKET", "KODA_STT_SOCKET"])
@pytest.mark.parametrize("recipe", ["stt-install", "stt-uninstall"])
def test_install_uninstall_socket_mode_only_when_caller_sets_it(tmp_path, recipe, sock_var):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    fake_script, env_log = _delegation_stub(tmp_path)
    sock = home / "Library" / "Caches" / "pipecat-stt" / "parakeet.sock"
    res = _run_just(
        [f"script={fake_script}", recipe, "parakeet"],
        home=home,
        stub_dir=stub_dir,
        extra_env={sock_var: str(sock)},
    )
    assert res.returncode == 0, res.stderr
    logged = env_log.read_text().strip()
    if sock_var == "PIPECAT_STT_SOCKET":
        assert f"SOCKET={sock} " in logged
    assert "LABEL=pipecat.stt-server.parakeet " in logged
    assert "BACKEND=parakeet" in logged
    # Socket mode: no canonical HOST/PORT, so the installer has no TCP-only
    # settings to warn about ignoring.
    assert "PIPECAT_STT_HOST= " in logged and "PIPECAT_STT_PORT= " in logged, logged


# Contract: stt-install / stt-uninstall never decide the transport. The installer owns
# the socket-vs-TCP rule and its canonical host/port defaults; the recipes only strip an
# ambient PIPECAT_STT_HOST / PIPECAT_STT_PORT so the canonical values always apply.
@pytest.mark.parametrize(
    "sock_env", [{}, {"PIPECAT_STT_SOCKET": "/p.sock"}, {"KODA_STT_SOCKET": ""}]
)
@pytest.mark.parametrize("recipe", ["stt-install", "stt-uninstall"])
def test_recipes_strip_ambient_host_and_port(tmp_path, recipe, sock_env):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    fake_script, env_log = _delegation_stub(tmp_path)
    res = _run_just(
        [f"script={fake_script}", recipe, "whisper"],
        home=home,
        stub_dir=stub_dir,
        extra_env={"PIPECAT_STT_HOST": "localhost", "PIPECAT_STT_PORT": "9999", **sock_env},
    )
    assert res.returncode == 0, res.stderr
    logged = env_log.read_text().strip()
    assert "PIPECAT_STT_HOST= PIPECAT_STT_PORT= " in logged, logged
    assert "9999" not in logged and "localhost" not in logged, logged


def test_recipes_do_not_reimplement_the_installers_socket_rule():
    """The alias rule lives only in install_stt_agent.sh; a second copy in the
    justfile drifted three times in review."""
    code = [
        ln
        for ln in (REPO_ROOT / "justfile").read_text().splitlines()
        if not ln.lstrip().startswith("#")
    ]
    offenders = [ln for ln in code if "PIPECAT_STT_SOCKET" in ln or "KODA_STT_SOCKET" in ln]
    assert not offenders, offenders


@pytest.mark.parametrize("recipe", ["stt-install", "stt-uninstall"])
def test_transport_is_not_a_command_line_override(tmp_path, recipe):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    fake_script, env_log = _delegation_stub(tmp_path)
    res = _run_just(
        ["agent_transport=socket", f"script={fake_script}", recipe, "whisper"],
        home=home,
        stub_dir=stub_dir,
    )
    assert res.returncode != 0, "a transport override must not be accepted"
    assert not env_log.exists(), "a rejected override must not delegate"


def test_no_toplevel_variable_derives_from_transport_env():
    """Every top-level variable is command-line overridable, so a variable computed
    from the installer's transport env would be a second source of truth."""
    text = (REPO_ROOT / "justfile").read_text()
    offenders = re.findall(r"^([A-Za-z_]\w*)\s*:=.*(?:PIPECAT_STT_|KODA_STT_)", text, re.MULTILINE)
    assert not offenders, f"transport env must be read inside recipes, not variables: {offenders}"


@pytest.mark.skipif(not VENV_PYTHON.exists(), reason="installer needs .venv/bin/python")
def test_stt_install_socket_rollback_prints_no_ignored_settings_warning(tmp_path):
    """The documented rollback `PIPECAT_STT_SOCKET=<path> just stt-install <backend>`
    through the REAL installer must not warn about HOST/PORT the caller never set."""
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    sock = home / "Library" / "Caches" / "pipecat-stt" / "stt.sock"
    res = _run_just(
        ["stt-install", "whisper"],
        home=home,
        stub_dir=stub_dir,
        extra_env={"PIPECAT_STT_SOCKET": str(sock)},
    )
    assert res.returncode == 0, f"stdout={res.stdout!r} stderr={res.stderr!r}"
    assert "ignoring" not in res.stderr, res.stderr
    args = plistlib.loads(
        (home / "Library" / "LaunchAgents" / "pipecat.stt-server.plist").read_bytes()
    )["ProgramArguments"]
    assert args[args.index("--socket-path") + 1] == str(sock)


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


def _venv_overrides(
    tmp_path: Path, stub_dir: Path, home: Path, *, probe_rc: int, fixed_by_sync: bool = True
):
    """Return (args, logs, venvs) overriding the venv locations with stubs whose
    ``python`` logs its argv and exits ``probe_rc``; with ``fixed_by_sync`` (the
    default) it exits 0 once the stub ``uv sync`` has run, as a sync that
    installed the extra would leave it.

    Exactly one kyutai and one shared-venv variable must be overridable;
    otherwise the shared-arm probe would depend on the real ``.venv`` and the
    tests would silently weaken, so this fails instead.
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
    assert len(main) == 1, f"expected exactly one overridable main-venv variable; got {variables}"
    targets = [(kyutai[0], "kyutai"), (main[0], "main")]
    args: list[str] = []
    logs: dict[str, Path] = {}
    venvs: dict[str, Path] = {}
    for name, key in targets:
        venv = tmp_path / f"stubvenv-{key}"
        (venv / "bin").mkdir(parents=True)
        log = tmp_path / f"py-{key}.log"
        healed = (
            f"[[ -e {stub_dir / 'uv-synced'!s} ]] && exit 0\n" if fixed_by_sync and probe_rc else ""
        )
        _write_executable(
            venv / "bin" / "python",
            f"#!/usr/bin/env bash\nprintf 'python %s\\n' \"$*\" >> {log!s}\n{healed}exit {probe_rc}\n",
        )
        value = str(venv / "bin" / "python") if variables[name].endswith("python") else str(venv)
        args.append(f"{name}={value}")
        logs[key] = log
        venvs[key] = venv
    return args, logs, venvs


def _read(path: Path) -> str:
    return path.read_text() if path.exists() else ""


@pytest.mark.parametrize("recipe", ["stt-install", "stt-enable"])
@pytest.mark.parametrize("backend,key", [("whisper", "main"), ("kyutai", "kyutai")])
def test_nested_ensure_extra_inherits_venv_overrides(tmp_path, recipe, backend, key):
    """stt-install / stt-enable re-invoke `just _ensure-extra`; the caller's
    `venv=` / `kyutai_venv=` overrides must reach it, or the probe silently
    checks (and may sync) the real venvs."""
    stub_dir, _ = _make_stub_dir(tmp_path)
    label = EXPECTED[backend][0]
    home = _home_with_agents(tmp_path, [label])
    fake_script, _env_log = _delegation_stub(tmp_path)
    args, logs, venvs = _venv_overrides(tmp_path, stub_dir, home, probe_rc=0)
    # stt-enable probes the interpreter its installed plist names: an agent
    # installed under these overrides records the overridden venv's python.
    _write_agent_plist(home, label, venvs[key] / "bin" / "python")
    res = _run_just(
        [*args, f"script={fake_script}", recipe, backend],
        home=home,
        stub_dir=stub_dir,
        extra_env={"PIPECAT_STT_SKIP_DEP_SYNC": ""},
    )
    assert res.returncode == 0, f"stdout={res.stdout!r} stderr={res.stderr!r}"
    assert "already present" in res.stdout, res.stdout
    assert "-c" in _read(logs[key]), f"{recipe}: overridden {key} venv was not probed"


# The preflight checks the interpreter the agent will run. Invariant: with
# PIPECAT_STT_PYTHON set (the installer's override), `_ensure-extra` and
# `stt-install` probe that interpreter, never the default venv, and never run a
# sync that could not fix it; a failed probe fails the recipe; stt-install hands
# the installer exactly the probed interpreter. State space: recipe
# {_ensure-extra, stt-install} x backend {whisper (shared venv), kyutai} x custom
# probe {ok, fails}. stt-enable reads its installed plist instead (below).
@pytest.mark.parametrize("probe_ok", [True, False])
@pytest.mark.parametrize("backend", ["whisper", "kyutai"])
@pytest.mark.parametrize("recipe", ["_ensure-extra", "stt-install"])
def test_ensure_extra_probes_the_agent_interpreter_override(tmp_path, recipe, backend, probe_ok):
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    label = EXPECTED[backend][0]
    home = _home_with_agents(tmp_path, [label])
    fake_script, env_log = _delegation_stub(tmp_path)
    # The default venvs would pass the probe: only the override may decide.
    args, logs, _venvs = _venv_overrides(tmp_path, stub_dir, home, probe_rc=0)
    custom_log = tmp_path / "py-custom.log"
    custom = tmp_path / "custom" / "python"
    custom.parent.mkdir()
    _write_executable(
        custom,
        f"#!/usr/bin/env bash\nprintf 'python %s\\n' \"$*\" >> {custom_log!s}\n"
        f"exit {0 if probe_ok else 1}\n",
    )
    res = _run_just(
        [*args, f"script={fake_script}", recipe, backend],
        home=home,
        stub_dir=stub_dir,
        extra_env={"PIPECAT_STT_SKIP_DEP_SYNC": "", "PIPECAT_STT_PYTHON": str(custom)},
    )
    assert "-c" in _read(custom_log), f"{recipe}: the override interpreter was not probed"
    assert _read(logs["main"]) == "" and _read(logs["kyutai"]) == "", "default venv probed"
    assert not any(ln.startswith("uv sync") for ln in _argv_lines(argv_log))
    if probe_ok:
        assert res.returncode == 0, f"stdout={res.stdout!r} stderr={res.stderr!r}"
        if recipe == "stt-install":
            assert f"PYTHON={custom}" in _read(env_log), "installer got another interpreter"
    else:
        assert res.returncode != 0, f"stdout={res.stdout!r} stderr={res.stderr!r}"
        assert "PIPECAT_STT_PYTHON" in res.stderr
        assert not env_log.exists() or _read(env_log) == "", "installer ran after a failed probe"


def _write_agent_plist(home: Path, label: str, interpreter: Path | str | None) -> Path:
    """An installed plist whose ProgramArguments[0] is ``interpreter`` (no
    ProgramArguments at all for None)."""
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    plist = agents / f"{label}.plist"
    data: dict = {"Label": label}
    if interpreter is not None:
        data["ProgramArguments"] = [str(interpreter), "-m", "stt_server", "serve"]
    plist.write_bytes(plistlib.dumps(data))
    return plist


def _logging_python(path: Path, log: Path, rc: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_executable(
        path, f"#!/usr/bin/env bash\nprintf 'python %s\\n' \"$*\" >> {log!s}\nexit {rc}\n"
    )
    return path


# stt-enable re-loads an EXISTING plist, so the interpreter launchd will run is
# the plist's ProgramArguments[0], whatever the caller's env says. Invariant:
# stt-enable probes exactly that interpreter, never PIPECAT_STT_PYTHON and never
# a default venv the plist does not name; only when the plist names the venv
# python can a sync fix a failed probe, otherwise it fails with the remedy and
# never bootstraps. State space: plist interpreter {default venv, custom} x probe
# {ok, fails} x caller env PIPECAT_STT_PYTHON {unset, a decoy} x backend
# {whisper, kyutai}.
@pytest.mark.parametrize("backend,key", [("whisper", "main"), ("kyutai", "kyutai")])
@pytest.mark.parametrize("decoy", [False, True])
@pytest.mark.parametrize("probe_ok", [True, False])
@pytest.mark.parametrize("which", ["default", "custom"])
def test_stt_enable_probes_the_installed_plists_interpreter(
    tmp_path, which, probe_ok, decoy, backend, key
):
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    label = EXPECTED[backend][0]
    home = _home_with_agents(tmp_path, [])
    args, logs, venvs = _venv_overrides(
        tmp_path, stub_dir, home, probe_rc=0 if probe_ok else 1, fixed_by_sync=True
    )
    custom_log = tmp_path / "py-custom.log"
    custom = _logging_python(tmp_path / "custom" / "python", custom_log, 0 if probe_ok else 1)
    decoy_log = tmp_path / "py-decoy.log"
    decoy_py = _logging_python(tmp_path / "decoy" / "python", decoy_log, 0)
    interp = venvs[key] / "bin" / "python" if which == "default" else custom
    plist = _write_agent_plist(home, label, interp)
    env = {"PIPECAT_STT_SKIP_DEP_SYNC": ""}
    if decoy:
        env["PIPECAT_STT_PYTHON"] = str(decoy_py)
    res = _run_just([*args, "stt-enable", backend], home=home, stub_dir=stub_dir, extra_env=env)
    probed = _read(logs[key]) if which == "default" else _read(custom_log)
    assert "-c" in probed, "the plist's interpreter was not probed"
    assert _read(decoy_log) == "", "the caller's PIPECAT_STT_PYTHON was probed"
    others = [k for k in ("main", "kyutai") if which == "custom" or k != key]
    assert all(_read(logs[k]) == "" for k in others), "a venv the plist does not name was probed"
    syncs = [ln for ln in _argv_lines(argv_log) if ln.startswith("uv sync")]
    bootstrap = f"launchctl bootstrap gui/{FAKE_UID} {plist}"
    if probe_ok or which == "default":
        # ok, or the venv python the plist names healed by one sync.
        assert res.returncode == 0, f"stdout={res.stdout!r} stderr={res.stderr!r}"
        assert len(syncs) == (0 if probe_ok else 1), syncs
        assert bootstrap in _argv_lines(argv_log)
    else:
        assert res.returncode != 0, f"stdout={res.stdout!r} stderr={res.stderr!r}"
        assert "ProgramArguments[0]" in res.stderr and str(custom) in res.stderr, res.stderr
        assert not syncs, "a sync cannot fix an interpreter outside the venv"
        assert not any(ln.startswith("launchctl bootstrap") for ln in _argv_lines(argv_log))


@pytest.mark.parametrize("state", ["no-program-arguments", "malformed"])
def test_stt_enable_refuses_a_plist_with_no_readable_interpreter(tmp_path, state):
    """With no interpreter to probe (no ProgramArguments, or a plist the reader
    cannot parse) stt-enable fails before any bootstrap; it never falls back to
    the caller's PIPECAT_STT_PYTHON or a default venv."""
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    label = EXPECTED["whisper"][0]
    home = _home_with_agents(tmp_path, [])
    args, logs, _venvs = _venv_overrides(tmp_path, stub_dir, home, probe_rc=0)
    plist = _write_agent_plist(home, label, None)
    if state == "malformed":
        plist.write_text("not a plist")
    decoy_log = tmp_path / "py-decoy.log"
    decoy_py = _logging_python(tmp_path / "decoy" / "python", decoy_log, 0)
    res = _run_just(
        [*args, "stt-enable", "whisper"],
        home=home,
        stub_dir=stub_dir,
        extra_env={"PIPECAT_STT_SKIP_DEP_SYNC": "", "PIPECAT_STT_PYTHON": str(decoy_py)},
    )
    assert res.returncode != 0, f"stdout={res.stdout!r} stderr={res.stderr!r}"
    assert "stt-install" in res.stderr, res.stderr
    assert _read(decoy_log) == "" and _read(logs["main"]) == "" and _read(logs["kyutai"]) == ""
    assert not any(ln.startswith("launchctl bootstrap") for ln in _argv_lines(argv_log))


# stt-install: the probed interpreter is the installed one by construction.
# Invariant: the PIPECAT_STT_PYTHON the installer receives equals the interpreter
# _ensure-extra probed, for every way of choosing it. State space: backend
# {whisper, kyutai} x choice {default venv, `venv=`/`kyutai_venv=` override,
# caller PIPECAT_STT_PYTHON}.
@pytest.mark.parametrize("choice", ["default", "override", "env"])
@pytest.mark.parametrize("backend,key", [("whisper", "main"), ("kyutai", "kyutai")])
def test_stt_install_hands_the_installer_the_probed_interpreter(tmp_path, backend, key, choice):
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    fake_script, env_log = _delegation_stub(tmp_path)
    overrides: list[str] = []
    extra_env = {"PIPECAT_STT_SKIP_DEP_SYNC": "1"}
    venv_dir = ".venv-kyutai" if key == "kyutai" else ".venv"
    want = str(REPO_ROOT / venv_dir / "bin" / "python")
    if choice == "override":
        args, _logs, venvs = _venv_overrides(tmp_path, stub_dir, home, probe_rc=0)
        overrides = args
        want = str(venvs[key] / "bin" / "python")
    elif choice == "env":
        want = str(tmp_path / "elsewhere" / "python")
        extra_env["PIPECAT_STT_PYTHON"] = want
    probed = _run_just(
        [*overrides, "_agent-python", backend], home=home, stub_dir=stub_dir, extra_env=extra_env
    )
    assert probed.returncode == 0, probed.stderr
    assert probed.stdout.strip() == want
    res = _run_just(
        [*overrides, f"script={fake_script}", "stt-install", backend],
        home=home,
        stub_dir=stub_dir,
        extra_env=extra_env,
    )
    assert res.returncode == 0, f"stdout={res.stdout!r} stderr={res.stderr!r}"
    assert _read(env_log).strip().endswith(f"PYTHON={want}"), _read(env_log)


@pytest.mark.skipif(not VENV_PYTHON.exists(), reason="installer needs .venv/bin/python")
def test_stt_install_under_a_venv_override_records_the_probed_interpreter(tmp_path):
    """Through the REAL installer: with `venv=` pointing elsewhere, the plist's
    ProgramArguments[0] is the overridden venv's python, the one _ensure-extra
    probed, not the installer's own .venv default."""
    stub_dir, _ = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    venv = tmp_path / "othervenv"
    log = tmp_path / "py-other.log"
    (venv / "bin").mkdir(parents=True)
    _write_executable(
        venv / "bin" / "python",
        # The import probe (`-c`) passes; everything else (the installer's
        # renderer and plist reader) runs on the real venv python.
        f"#!/usr/bin/env bash\nprintf 'python %s\\n' \"$*\" >> {log!s}\n"
        f'[[ "$1" == -c ]] && exit 0\nexec {VENV_PYTHON!s} "$@"\n',
    )
    res = _run_just(
        [f"venv={venv}", "stt-install", "whisper"],
        home=home,
        stub_dir=stub_dir,
        extra_env={"PIPECAT_STT_SKIP_DEP_SYNC": "", "PIPECAT_STT_MODEL": "test/model"},
    )
    assert res.returncode == 0, f"stdout={res.stdout!r} stderr={res.stderr!r}"
    args = plistlib.loads(
        (home / "Library" / "LaunchAgents" / "pipecat.stt-server.plist").read_bytes()
    )["ProgramArguments"]
    assert args[0] == str(venv / "bin" / "python"), args[0]
    assert "-c" in _read(log), "the recorded interpreter was not the probed one"


def test_ensure_extra_kyutai_syncs_into_its_own_venv(tmp_path):
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    args, logs, venvs = _venv_overrides(tmp_path, stub_dir, home, probe_rc=1)
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
    # --frozen: never let the sync rewrite uv.lock.
    assert "--frozen" in syncs[0], syncs
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


# A sync that exits 0 is not "ready" by itself. Invariant: `_ensure-extra`
# reports ready only after the import probe passes; a probe that still fails
# after a successful sync fails the recipe (and stt-install never reaches the
# installer). State space: arm {shared .venv, kyutai} x probe after sync {passes,
# still fails}.
@pytest.mark.parametrize("fixed", [True, False])
@pytest.mark.parametrize("backend,key", [("whisper", "main"), ("kyutai", "kyutai")])
def test_ensure_extra_reprobes_after_sync(tmp_path, backend, key, fixed):
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [EXPECTED[backend][0]])
    fake_script, env_log = _delegation_stub(tmp_path)
    args, logs, _venvs = _venv_overrides(tmp_path, stub_dir, home, probe_rc=1, fixed_by_sync=fixed)
    for recipe in ("_ensure-extra", "stt-install"):
        (stub_dir / "uv-synced").unlink(missing_ok=True)  # each recipe starts unsynced
        res = _run_just(
            [*args, f"script={fake_script}", recipe, backend],
            home=home,
            stub_dir=stub_dir,
            extra_env={"PIPECAT_STT_SKIP_DEP_SYNC": ""},
        )
        assert any(ln.startswith("uv sync") for ln in _argv_lines(argv_log))
        probes = [ln for ln in _read(logs[key]).splitlines() if " -c " in f" {ln} "]
        if fixed:
            assert res.returncode == 0, f"stdout={res.stdout!r} stderr={res.stderr!r}"
            assert "extra ready" in res.stdout
        else:
            assert res.returncode != 0, f"{recipe}: stdout={res.stdout!r} stderr={res.stderr!r}"
            assert "still fails" in res.stderr, res.stderr
            assert "extra ready" not in res.stdout
            assert len(probes) >= 2, f"{recipe}: no probe after the sync; {probes}"
            if recipe == "stt-install":
                assert not env_log.exists() or _read(env_log) == "", "installer ran"


def test_ensure_extra_kyutai_present_does_not_sync(tmp_path):
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    args, logs, venvs = _venv_overrides(tmp_path, stub_dir, home, probe_rc=0)
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
    args, logs, venvs = _venv_overrides(tmp_path, stub_dir, home, probe_rc=1)
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
    # Probe stub fails -> exactly one additive sync into the shared .venv.
    assert len(syncs) == 1, log
    assert probe in _read(logs["main"])


@pytest.mark.parametrize("backend", ["whisper", "parakeet", "nemotron"])
def test_ensure_extra_shared_arms_ignore_ambient_uv_project_environment(tmp_path, backend):
    """Regression: an exported ``UV_PROJECT_ENVIRONMENT`` (e.g. left over from a
    kyutai session) must NOT be inherited by the shared-venv sync, or the extra
    lands in the wrong venv."""
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    args, logs, venvs = _venv_overrides(tmp_path, stub_dir, home, probe_rc=1)
    ambient = str(tmp_path / ".venv-kyutai")
    res = _run_just(
        [*args, "_ensure-extra", backend],
        home=home,
        stub_dir=stub_dir,
        extra_env={"PIPECAT_STT_SKIP_DEP_SYNC": "", "UV_PROJECT_ENVIRONMENT": ambient},
    )
    assert res.returncode == 0, res.stderr
    log = _argv_lines(argv_log)
    syncs = [ln for ln in log if ln.startswith("uv sync")]
    assert len(syncs) == 1, log
    env_line = log[log.index(syncs[0]) + 1]
    assert env_line == "uvenv UV_PROJECT_ENVIRONMENT=<unset>", env_line
    assert ambient not in "\n".join(log)


def test_ensure_extra_kyutai_overrides_ambient_uv_project_environment(tmp_path):
    """The kyutai arm still syncs only into its own venv, whatever is ambient."""
    stub_dir, argv_log = _make_stub_dir(tmp_path)
    home = _home_with_agents(tmp_path, [])
    args, logs, venvs = _venv_overrides(tmp_path, stub_dir, home, probe_rc=1)
    ambient = str(tmp_path / "some-other-venv")
    res = _run_just(
        [*args, "_ensure-extra", "kyutai"],
        home=home,
        stub_dir=stub_dir,
        extra_env={"PIPECAT_STT_SKIP_DEP_SYNC": "", "UV_PROJECT_ENVIRONMENT": ambient},
    )
    assert res.returncode == 0, res.stderr
    log = _argv_lines(argv_log)
    syncs = [ln for ln in log if ln.startswith("uv sync")]
    assert len(syncs) == 1, log
    assert log[log.index(syncs[0]) + 1] == f"uvenv UV_PROJECT_ENVIRONMENT={venvs['kyutai']}"
    assert _read(logs["main"]) == ""


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


# ---------------------------------------------------------------------------
# Structural guard: a nested `just` does not inherit command-line overrides
# (`just la_dir=X recipe`), so each nested call must forward every top-level
# variable its callee reads, or the callee silently uses the real path.
# ---------------------------------------------------------------------------


def _justfile_recipes() -> tuple[set[str], dict[str, list[str]]]:
    text = (REPO_ROOT / "justfile").read_text()
    # Forwardable variables are the command-line knobs. A variable derived from the
    # installer's transport env would be a second source of truth, not a knob (and is
    # forbidden outright by test_no_toplevel_variable_derives_from_transport_env).
    variables = set(
        re.findall(r"^([A-Za-z_]\w*)\s*:=(?!.*(?:PIPECAT_STT_|KODA_STT_))", text, re.MULTILINE)
    )
    recipes: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        header = re.match(r"^([A-Za-z_][\w-]*)\b[^:\n]*:(?!=)", line)
        if header:
            current = recipes.setdefault(header.group(1), [])
        elif line and not line[0].isspace():
            current = None
        elif current is not None and not line.lstrip().startswith("#"):
            current.append(line)
    return variables, recipes


def test_nested_just_calls_forward_the_variables_their_callee_reads():
    variables, recipes = _justfile_recipes()

    def reads(recipe: str) -> set[str]:
        body = "\n".join(recipes[recipe])
        return {v for v in variables if re.search(rf"{{{{[^}}]*\b{v}\b[^}}]*}}}}", body)}

    nested = 0
    callers_seen: set[str] = set()
    for caller, body in recipes.items():
        for line in body:
            if re.match(r"\s*(echo|printf)\b", line):
                continue  # a message that names a recipe, not a call
            for m in re.finditer(r"\bjust((?:\s+\w+=(?:\S+))*)\s+([A-Za-z_][\w-]*)", line):
                callee = m.group(2)
                if callee not in recipes:
                    continue
                nested += 1
                callers_seen.add(caller)
                forwarded = set(re.findall(r"(\w+)=", m.group(1)))
                missing = reads(callee) - forwarded
                assert not missing, (
                    f"{caller}: nested `just {callee}` does not forward {sorted(missing)}; "
                    "an override given to the caller would be lost"
                )
    # A staleness floor, not a pin: it only proves the scan still finds nested calls
    # at all. Known callers are asserted by name so dropping one is a deliberate edit.
    assert nested >= 1, "scan found no nested just calls; the pattern is stale"
    assert {"stt-status", "stt-enable", "stt-install"} <= callers_seen, (
        f"expected nested just calls from stt-status, stt-enable and stt-install; "
        f"scan saw {sorted(callers_seen)}"
    )


def test_no_plain_uv_run_python_in_smoke_surfaces():
    """Plain `uv run python` re-syncs and strips the optional backend extras
    (crash-looping LaunchAgents). The justfile and every scripts/smoke_*.py
    must run `.venv/bin/python` (or `uv run --no-sync python`) instead. The
    README and docs/operations.md foreground examples are tracked separately."""
    files = [JUSTFILE, *sorted((REPO_ROOT / "scripts").glob("smoke_*.py"))]
    plain = re.compile(r"\buv run python\b")
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{n}: {line.strip()}"
        for path in files
        if path.exists()
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if plain.search(line)
    ]
    assert not offenders, "plain `uv run python` (needs --no-sync):\n" + "\n".join(offenders)
