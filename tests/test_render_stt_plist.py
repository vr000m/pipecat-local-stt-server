"""Tests for the multi-instance ``scripts/render_stt_plist.py`` renderer.

``render_stt_plist.py`` is parameterised so two (or more) LaunchAgents can
coexist — distinct label, socket path, and log files per ASR. These tests
exercise it as a subprocess (its real entry point) and assert:

* default env renders a plist **byte-for-byte** equal to the committed
  snapshot ``tests/snapshots/pipecat-stt.plist`` (default ``pipecat.stt-server``);
* a custom ``KODA_STT_LABEL`` yields a plist whose internal ``Label`` and the
  derived ``StandardOutPath`` / ``StandardErrorPath`` do **not** collide with
  the default agent's;
* ``BACKEND=parakeet`` passes the ``_BACKEND_RE`` allowlist; ``BACKEND=bogus``
  is rejected;
* the backend-aware ``MODEL`` default satisfies ``_MODEL_RE`` for both ``mlx``
  and ``parakeet`` backends.

The renderer writes the plist to ``PLIST_DST`` and prints ``wrote <path>``;
it does not emit the plist to stdout, so each test points ``PLIST_DST`` at a
``tmp_path`` file and reads it back.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "render_stt_plist.py"
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_stt_agent.sh"
SNAPSHOT = Path(__file__).resolve().parent / "snapshots" / "pipecat-stt.plist"

# The fixed env the committed snapshot was captured under. Keep these values
# in lockstep with tests/snapshots/pipecat-stt.plist — they are the
# byte-for-byte baseline for the default-label render.
SNAPSHOT_ENV = {
    "PYTHON": "/usr/bin/python3",
    "REPO_ROOT": "/Users/test/pipecat-local-stt-server",
    "SOCKET_PATH": "/Users/test/Library/Caches/pipecat-stt/stt.sock",
    "BACKEND": "mlx",
    "MODEL": "mlx-community/whisper-large-v3-turbo",
    "HOME": "/Users/test",
    "LOG_DIR": "/Users/test/Library/Logs/pipecat-stt",
}

_DEFAULT_PARAKEET_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"


def _run_render(env_overrides: dict[str, str], dst: Path) -> subprocess.CompletedProcess:
    """Run ``render_stt_plist.py`` in a clean env with ``PLIST_DST`` -> ``dst``.

    ``env`` is built from ``SNAPSHOT_ENV`` plus ``PLIST_DST`` and any
    overrides, so each test starts from the known-good baseline and only the
    variable under test changes. PATH is carried so the interpreter resolves.
    """
    env = dict(SNAPSHOT_ENV)
    env["PLIST_DST"] = str(dst)
    env.update(env_overrides)
    # PATH is needed for the subprocess to launch; nothing else leaks in.
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )


# ---------------------------------------------------------------------------
# Default env: byte-for-byte equal to the committed snapshot
# ---------------------------------------------------------------------------


def test_snapshot_fixture_exists():
    """The committed pre-change baseline must exist — every byte-for-byte
    regression assert below reads it as the expected value."""
    assert SNAPSHOT.is_file(), (
        f"missing baseline snapshot {SNAPSHOT}; capture it before parameterising "
        "render_stt_plist.py (Phase 3 plan: 'capture the plist snapshot fixture "
        "as the first commit of this phase')"
    )


def test_default_env_renders_plist_byte_for_byte_equal_to_snapshot(tmp_path: Path):
    """Default env (no ``KODA_STT_LABEL``) renders the default ``pipecat.stt-server``
    plist byte-for-byte identical to the committed snapshot."""
    dst = tmp_path / "rendered.plist"
    r = _run_render({}, dst)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert dst.is_file()
    assert dst.read_bytes() == SNAPSHOT.read_bytes(), (
        "default-env render drifted from the committed snapshot — the "
        "default pipecat.stt-server plist must match the committed bytes"
    )


def test_default_env_plist_has_default_label_and_log_paths(tmp_path: Path):
    """Default env uses the ``pipecat.stt-server`` label and ``pipecat-stt.log``
    / ``pipecat-stt.err`` filenames."""
    dst = tmp_path / "rendered.plist"
    r = _run_render({}, dst)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    plist = plistlib.loads(dst.read_bytes())
    assert plist["Label"] == "pipecat.stt-server"
    assert plist["StandardOutPath"].endswith("/pipecat-stt.log")
    assert plist["StandardErrorPath"].endswith("/pipecat-stt.err")


# ---------------------------------------------------------------------------
# Custom KODA_STT_LABEL: label + log paths must not collide with the default
# ---------------------------------------------------------------------------


def test_custom_label_sets_internal_label(tmp_path: Path):
    """A custom ``KODA_STT_LABEL`` flows into the plist's internal ``Label``."""
    dst = tmp_path / "parakeet.plist"
    r = _run_render({"KODA_STT_LABEL": "koda.stt-server.parakeet"}, dst)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    plist = plistlib.loads(dst.read_bytes())
    assert plist["Label"] == "koda.stt-server.parakeet"


def test_custom_label_log_paths_do_not_collide_with_default(tmp_path: Path):
    """The parakeet agent's ``StandardOutPath`` / ``StandardErrorPath`` must be
    distinct from the default agent's so two agents never interleave logs."""
    default_dst = tmp_path / "default.plist"
    parakeet_dst = tmp_path / "parakeet.plist"

    r_default = _run_render({}, default_dst)
    assert r_default.returncode == 0, r_default.stderr
    r_parakeet = _run_render({"KODA_STT_LABEL": "koda.stt-server.parakeet"}, parakeet_dst)
    assert r_parakeet.returncode == 0, r_parakeet.stderr

    default = plistlib.loads(default_dst.read_bytes())
    parakeet = plistlib.loads(parakeet_dst.read_bytes())

    assert default["Label"] != parakeet["Label"]
    assert default["StandardOutPath"] != parakeet["StandardOutPath"], (
        "parakeet agent must not write to the whisper agent's stdout log"
    )
    assert default["StandardErrorPath"] != parakeet["StandardErrorPath"], (
        "parakeet agent must not write to the whisper agent's stderr log"
    )


def test_custom_label_log_paths_are_derived_from_the_label(tmp_path: Path):
    """The derived log filenames must carry the label so ``logs`` can tail the
    correct agent — not the hardcoded ``pipecat-stt.log`` / ``pipecat-stt.err``."""
    dst = tmp_path / "parakeet.plist"
    r = _run_render({"KODA_STT_LABEL": "pipecat.stt-server.parakeet"}, dst)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    plist = plistlib.loads(dst.read_bytes())
    # The label-derived filenames must not be the default hardcoded names.
    assert not plist["StandardOutPath"].endswith("/pipecat-stt.log")
    assert not plist["StandardErrorPath"].endswith("/pipecat-stt.err")
    # They must be distinguishable as the parakeet agent's logs.
    assert "parakeet" in Path(plist["StandardOutPath"]).name
    assert "parakeet" in Path(plist["StandardErrorPath"]).name
    # stdout and stderr remain two separate files.
    assert plist["StandardOutPath"] != plist["StandardErrorPath"]


@pytest.mark.parametrize(
    ("label", "expected_basename"),
    [
        (None, "pipecat-stt"),
        ("koda.stt-server", "koda-stt"),
        ("pipecat.stt-server.parakeet", "pipecat-stt-server-parakeet"),
    ],
)
def test_log_basename_mapping_is_pinned(tmp_path: Path, label: str | None, expected_basename: str):
    """Pin the exact label -> log-basename mapping.

    ``scripts/install_stt_agent.sh`` hardcodes the SAME mapping for its
    ``logs`` subcommand (which never calls the renderer). If these expected
    values change, update the installer's ``LOG_BASENAME`` derivation in
    lockstep — the two must agree or ``logs`` tails the wrong agent's file.
    """
    env = {} if label is None else {"KODA_STT_LABEL": label}
    dst = tmp_path / "agent.plist"
    r = _run_render(env, dst)
    assert r.returncode == 0, r.stderr
    plist = plistlib.loads(dst.read_bytes())
    assert Path(plist["StandardOutPath"]).name == f"{expected_basename}.log"
    assert Path(plist["StandardErrorPath"]).name == f"{expected_basename}.err"


def _installer_log_basename(label: str) -> str:
    """Drive the installer's ACTUAL ``LOG_BASENAME`` derivation for ``label``.

    Extracts the real ``if/elif/else`` block from ``install_stt_agent.sh`` and
    evals it under bash, rather than re-implementing it here — a hand-copied
    replica could itself drift from the script and silently pass. Returns the
    basename the installer's ``logs`` subcommand would tail.
    """
    src = INSTALL_SCRIPT.read_text()
    start = src.index('if [[ "$LABEL" == "pipecat.stt-server" ]]; then')
    end = src.index("\nfi\n", start) + len("\nfi\n")
    block = src[start:end]
    snippet = f"LABEL={shlex.quote(label)}\n{block}\nprintf '%s' \"$LOG_BASENAME\""
    r = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, check=True)
    return r.stdout


@pytest.mark.parametrize(
    "label",
    ["pipecat.stt-server", "koda.stt-server", "pipecat.stt-server.parakeet", "a.b.c.d"],
)
def test_installer_log_basename_matches_renderer(tmp_path: Path, label: str):
    """Mechanically enforce the Python<->shell ``_log_basename`` lockstep that
    ``test_log_basename_mapping_is_pinned`` only asks for in prose.

    The installer's ``logs`` subcommand hardcodes its own copy of the mapping
    (it never calls the renderer). If the two copies diverge, ``logs`` tails a
    file the agent never writes. This drives the installer's real derivation
    block and asserts it equals the basename the renderer writes into the
    plist's ``StandardErrorPath`` for the same label.
    """
    dst = tmp_path / "agent.plist"
    r = _run_render({"PIPECAT_STT_LABEL": label}, dst)
    assert r.returncode == 0, r.stderr
    renderer_basename = Path(plistlib.loads(dst.read_bytes())["StandardErrorPath"]).stem
    assert _installer_log_basename(label) == renderer_basename


def test_explicit_legacy_label_renders_legacy_label_and_log_paths(tmp_path: Path):
    """An explicit legacy ``KODA_STT_LABEL=koda.stt-server`` still renders the
    legacy ``Label`` and ``koda-stt.{log,err}`` basenames at the full-render
    level — the end-to-end analogue of the ``_log_basename`` parametrize unit.

    This proves two contracts at once: the retained legacy ``_log_basename``
    shim maps the explicit legacy label to the legacy basename, and
    ``KODA_STT_LABEL`` still overrides the new ``pipecat.stt-server`` default.
    """
    dst = tmp_path / "legacy.plist"
    r = _run_render({"KODA_STT_LABEL": "koda.stt-server"}, dst)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    plist = plistlib.loads(dst.read_bytes())
    assert plist["Label"] == "koda.stt-server"
    assert plist["StandardOutPath"].endswith("/koda-stt.log")
    assert plist["StandardErrorPath"].endswith("/koda-stt.err")


# ---------------------------------------------------------------------------
# BACKEND allowlist (_BACKEND_RE) — parakeet accepted, bogus rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["echo", "mlx", "parakeet"])
def test_backend_allowlist_accepts_supported_backends(tmp_path: Path, backend: str):
    """``_BACKEND_RE`` must accept ``echo``, ``mlx``, and the new ``parakeet``."""
    dst = tmp_path / f"{backend}.plist"
    # parakeet needs a model id its regex accepts; supply one explicitly.
    overrides = {"BACKEND": backend}
    if backend == "parakeet":
        overrides["MODEL"] = _DEFAULT_PARAKEET_MODEL
    r = _run_render(overrides, dst)
    assert r.returncode == 0, f"backend={backend}: stdout={r.stdout!r} stderr={r.stderr!r}"
    plist = plistlib.loads(dst.read_bytes())
    assert backend in plist["ProgramArguments"]


def test_backend_allowlist_rejects_bogus_backend(tmp_path: Path):
    """A backend outside ``_BACKEND_RE`` fails loudly with ``sys.exit(2)``."""
    dst = tmp_path / "bogus.plist"
    r = _run_render({"BACKEND": "bogus"}, dst)
    assert r.returncode == 2, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "BACKEND" in r.stderr
    assert not dst.exists(), "a rejected backend must not write a plist"


# ---------------------------------------------------------------------------
# Backend-aware MODEL default satisfies _MODEL_RE for both backends
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("backend", "model"),
    [
        ("mlx", "mlx-community/whisper-large-v3-turbo"),
        ("parakeet", _DEFAULT_PARAKEET_MODEL),
    ],
)
def test_backend_aware_model_default_passes_model_regex(tmp_path: Path, backend: str, model: str):
    """The model id chosen as the backend-aware default must satisfy
    ``_MODEL_RE`` — otherwise a two-agent install with ``KODA_STT_MODEL``
    unset would trip the renderer's allowlist."""
    dst = tmp_path / f"{backend}-model.plist"
    r = _run_render({"BACKEND": backend, "MODEL": model}, dst)
    assert r.returncode == 0, (
        f"backend={backend} model={model!r} rejected: stdout={r.stdout!r} stderr={r.stderr!r}"
    )
    plist = plistlib.loads(dst.read_bytes())
    args = plist["ProgramArguments"]
    assert model in args
    # The model argument follows the --model flag.
    assert args[args.index("--model") + 1] == model


def test_model_rejected_when_invalid(tmp_path: Path):
    """A model id with characters outside ``_MODEL_RE`` is rejected — guards
    the allowlist that the backend-aware default must satisfy."""
    dst = tmp_path / "bad-model.plist"
    r = _run_render({"MODEL": "bad model;rm -rf /"}, dst)
    assert r.returncode == 2, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "MODEL" in r.stderr
    assert not dst.exists()


# ---------------------------------------------------------------------------
# Auth-token env: canonical PIPECAT_STT_AUTH_TOKEN is rendered into the plist;
# the legacy KODA_STT_AUTH_TOKEN input is still accepted. The server reads
# PIPECAT_STT_AUTH_TOKEN-first, so the plist must carry the canonical key.
# ---------------------------------------------------------------------------


def test_render_writes_pipecat_auth_token_from_canonical(tmp_path: Path):
    dst = tmp_path / "auth.plist"
    r = _run_render({"PIPECAT_STT_AUTH_TOKEN": "pipecat-secret"}, dst)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    env = plistlib.loads(dst.read_bytes())["EnvironmentVariables"]
    assert env.get("PIPECAT_STT_AUTH_TOKEN") == "pipecat-secret"
    assert "KODA_STT_AUTH_TOKEN" not in env


def test_render_accepts_legacy_koda_auth_token_input(tmp_path: Path):
    # Legacy KODA_ input is honoured, but the rendered key is canonical.
    dst = tmp_path / "auth-legacy.plist"
    r = _run_render({"KODA_STT_AUTH_TOKEN": "koda-secret"}, dst)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    env = plistlib.loads(dst.read_bytes())["EnvironmentVariables"]
    assert env.get("PIPECAT_STT_AUTH_TOKEN") == "koda-secret"
    assert "KODA_STT_AUTH_TOKEN" not in env


def test_render_auth_token_pipecat_wins_over_koda(tmp_path: Path):
    dst = tmp_path / "auth-both.plist"
    r = _run_render(
        {
            "PIPECAT_STT_AUTH_TOKEN": "pipecat-secret",
            "KODA_STT_AUTH_TOKEN": "koda-secret",
        },
        dst,
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    env = plistlib.loads(dst.read_bytes())["EnvironmentVariables"]
    assert env.get("PIPECAT_STT_AUTH_TOKEN") == "pipecat-secret"


def test_render_custom_label_via_pipecat_env(tmp_path: Path):
    dst = tmp_path / "pipecat-label.plist"
    r = _run_render({"PIPECAT_STT_LABEL": "koda.stt-server.parakeet"}, dst)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    plist = plistlib.loads(dst.read_bytes())
    assert plist["Label"] == "koda.stt-server.parakeet"


def test_render_label_pipecat_wins_over_koda(tmp_path: Path):
    dst = tmp_path / "both-label.plist"
    r = _run_render(
        {
            "PIPECAT_STT_LABEL": "koda.stt-server.parakeet",
            "KODA_STT_LABEL": "koda.stt-server.legacy",
        },
        dst,
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert plistlib.loads(dst.read_bytes())["Label"] == "koda.stt-server.parakeet"
