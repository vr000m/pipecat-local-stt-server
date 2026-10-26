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

import importlib.util
import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

# Import the Nemotron default model from its single source of truth rather than
# re-hardcoding the literal: a model-id rename in the backend must fail this
# renderer regression instead of passing against a stale copy. nemotron.py
# imports mlx_audio only lazily, so this does not pull the optional package.
from stt_server.backends.nemotron import DEFAULT_NEMOTRON_MODEL as _DEFAULT_NEMOTRON_MODEL

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "render_stt_plist.py"
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_stt_agent.sh"
SNAPSHOT = Path(__file__).resolve().parent / "snapshots" / "pipecat-stt.plist"
SNAPSHOT_TCP = Path(__file__).resolve().parent / "snapshots" / "pipecat-stt-tcp.plist"

# TCP-mode baseline: SNAPSHOT_ENV minus SOCKET_PATH, plus PORT (HOST defaults
# to 127.0.0.1). Matches tests/snapshots/pipecat-stt-tcp.plist.
TCP_ENV: dict[str, str | None] = {"SOCKET_PATH": None, "PORT": "9565"}

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


def _run_render(env_overrides: dict[str, str | None], dst: Path) -> subprocess.CompletedProcess:
    """Run ``render_stt_plist.py`` in a clean env with ``PLIST_DST`` -> ``dst``.

    ``env`` is built from ``SNAPSHOT_ENV`` plus ``PLIST_DST`` and any
    overrides, so each test starts from the known-good baseline and only the
    variable under test changes. PATH is carried so the interpreter resolves.
    """
    env = dict(SNAPSHOT_ENV)
    env["PLIST_DST"] = str(dst)
    env.update({k: v for k, v in env_overrides.items() if v is not None})
    # A ``None`` override DROPS the key from the base env (absent != empty).
    for k, v in env_overrides.items():
        if v is None:
            env.pop(k, None)
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


@pytest.mark.parametrize("backend", ["echo", "mlx", "parakeet", "nemotron"])
def test_backend_allowlist_accepts_supported_backends(tmp_path: Path, backend: str):
    """``_BACKEND_RE`` must accept ``echo``, ``mlx``, ``parakeet``, and the new
    ``nemotron``."""
    dst = tmp_path / f"{backend}.plist"
    # parakeet/nemotron need a model id their regex accepts; supply one explicitly.
    overrides = {"BACKEND": backend}
    if backend == "parakeet":
        overrides["MODEL"] = _DEFAULT_PARAKEET_MODEL
    elif backend == "nemotron":
        overrides["MODEL"] = _DEFAULT_NEMOTRON_MODEL
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
        ("nemotron", _DEFAULT_NEMOTRON_MODEL),
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


@pytest.mark.parametrize(
    ("var", "base"),
    [
        ("SOCKET_PATH", {}),
        ("AUTH_TOKEN_FILE", TCP_ENV),
        ("PYTHON", {}),
        ("REPO_ROOT", {}),
        ("MODEL", {}),
        ("BACKEND", {}),
        ("PIPECAT_STT_LABEL", {}),
        ("PORT", TCP_ENV),
        ("HOST", TCP_ENV),
    ],
)
def test_trailing_newline_rejected_for_every_validated_value(
    tmp_path: Path, var: str, base: dict[str, str | None]
):
    """Regression: ``_require`` used ``pattern.match`` with ``$`` anchors, which
    accepts a trailing newline. Every validated value must reject it (exit 2,
    no plist written)."""
    valid = {
        "SOCKET_PATH": SNAPSHOT_ENV["SOCKET_PATH"],
        "AUTH_TOKEN_FILE": "/Users/test/.config/pipecat-stt/token",
        "PYTHON": SNAPSHOT_ENV["PYTHON"],
        "REPO_ROOT": SNAPSHOT_ENV["REPO_ROOT"],
        "MODEL": SNAPSHOT_ENV["MODEL"],
        "BACKEND": SNAPSHOT_ENV["BACKEND"],
        "PIPECAT_STT_LABEL": "pipecat.stt-server",
        "PORT": "9565",
        "HOST": "127.0.0.1",
    }[var]
    dst = tmp_path / "nl.plist"
    ok = _run_render({**base, var: valid}, dst)
    assert ok.returncode == 0, f"control render failed: {ok.stderr!r}"
    dst.unlink()
    r = _run_render({**base, var: valid + "\n"}, dst)
    assert r.returncode == 2, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert not dst.exists()


# ---------------------------------------------------------------------------
# TCP default / socket opt-in
# ---------------------------------------------------------------------------


def test_tcp_snapshot_fixture_exists():
    assert SNAPSHOT_TCP.is_file()


def test_tcp_default_renders_byte_for_byte_equal_to_tcp_snapshot(tmp_path: Path):
    dst = tmp_path / "tcp.plist"
    r = _run_render(TCP_ENV, dst)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert dst.read_bytes() == SNAPSHOT_TCP.read_bytes()


def test_tcp_args_have_host_port_and_no_socket_path(tmp_path: Path):
    dst = tmp_path / "tcp.plist"
    r = _run_render({**TCP_ENV, "HOST": "localhost", "PORT": "9570"}, dst)
    assert r.returncode == 0, r.stderr
    args = plistlib.loads(dst.read_bytes())["ProgramArguments"]
    assert args[args.index("--host") + 1] == "localhost"
    assert args[args.index("--port") + 1] == "9570"
    assert "--socket-path" not in args
    assert "--auth-token-file" not in args


def test_socket_path_set_keeps_socket_plist_and_no_tcp_args(tmp_path: Path):
    dst = tmp_path / "sock.plist"
    r = _run_render({"PORT": None, "HOST": None}, dst)
    assert r.returncode == 0, r.stderr
    assert dst.read_bytes() == SNAPSHOT.read_bytes()
    args = plistlib.loads(dst.read_bytes())["ProgramArguments"]
    assert "--host" not in args and "--port" not in args


def test_empty_socket_path_means_tcp(tmp_path: Path):
    dst = tmp_path / "tcp.plist"
    r = _run_render({"SOCKET_PATH": "", "PORT": "9565"}, dst)
    assert r.returncode == 0, r.stderr
    assert dst.read_bytes() == SNAPSHOT_TCP.read_bytes()


def test_empty_host_defaults_to_loopback(tmp_path: Path):
    dst = tmp_path / "tcp.plist"
    r = _run_render({**TCP_ENV, "HOST": ""}, dst)
    assert r.returncode == 0, r.stderr
    assert dst.read_bytes() == SNAPSHOT_TCP.read_bytes()


@pytest.mark.parametrize("extra", [{"HOST": "127.0.0.1"}, {"PORT": "9565"}])
def test_socket_path_with_host_or_port_is_rejected(tmp_path: Path, extra: dict[str, str]):
    dst = tmp_path / "both.plist"
    r = _run_render(extra, dst)  # base env already has SOCKET_PATH
    assert r.returncode == 2, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert not dst.exists()


def test_socket_path_with_empty_host_and_port_is_socket_mode(tmp_path: Path):
    dst = tmp_path / "sock.plist"
    r = _run_render({"HOST": "", "PORT": ""}, dst)
    assert r.returncode == 0, r.stderr
    assert dst.read_bytes() == SNAPSHOT.read_bytes()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_accepted(tmp_path: Path, host: str):
    dst = tmp_path / "h.plist"
    r = _run_render({**TCP_ENV, "HOST": host}, dst)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    args = plistlib.loads(dst.read_bytes())["ProgramArguments"]
    assert args[args.index("--host") + 1] == host


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "127.0.0.2", "192.168.1.10", "127.0.0.1</string><x/>"],
)
def test_non_loopback_or_hostile_hosts_rejected(tmp_path: Path, host: str):
    dst = tmp_path / "h.plist"
    r = _run_render({**TCP_ENV, "HOST": host}, dst)
    assert r.returncode == 2, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert not dst.exists()


@pytest.mark.parametrize("port", ["0", "65536", "-1", "abc", "", "9565</string>", "95 65"])
def test_invalid_ports_rejected(tmp_path: Path, port: str):
    dst = tmp_path / "p.plist"
    r = _run_render({**TCP_ENV, "PORT": port}, dst)
    assert r.returncode == 2, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert not dst.exists()


def test_missing_port_in_tcp_mode_rejected(tmp_path: Path):
    dst = tmp_path / "p.plist"
    r = _run_render({"SOCKET_PATH": None, "PORT": None}, dst)
    assert r.returncode == 2, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert not dst.exists()


@pytest.mark.parametrize("port", ["1", "65535"])
def test_port_bounds_accepted(tmp_path: Path, port: str):
    dst = tmp_path / "p.plist"
    r = _run_render({**TCP_ENV, "PORT": port}, dst)
    assert r.returncode == 0, r.stderr
    args = plistlib.loads(dst.read_bytes())["ProgramArguments"]
    assert args[args.index("--port") + 1] == port


# ---------------------------------------------------------------------------
# kyutai backend
# ---------------------------------------------------------------------------


def test_kyutai_backend_accepted_with_model(tmp_path: Path):
    dst = tmp_path / "kyutai.plist"
    r = _run_render({**TCP_ENV, "BACKEND": "kyutai", "MODEL": "kyutai/stt-1b-en_fr-mlx"}, dst)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    args = plistlib.loads(dst.read_bytes())["ProgramArguments"]
    assert args[args.index("--backend") + 1] == "kyutai"
    assert args[args.index("--model") + 1] == "kyutai/stt-1b-en_fr-mlx"


def test_kyutai_log_basename_follows_dot_to_dash_rule(tmp_path: Path):
    dst = tmp_path / "kyutai.plist"
    r = _run_render(
        {
            **TCP_ENV,
            "BACKEND": "kyutai",
            "MODEL": "kyutai/stt-1b-en_fr-mlx",
            "PIPECAT_STT_LABEL": "pipecat.stt-server.kyutai",
        },
        dst,
    )
    assert r.returncode == 0, r.stderr
    plist = plistlib.loads(dst.read_bytes())
    assert Path(plist["StandardOutPath"]).name == "pipecat-stt-server-kyutai.log"
    assert Path(plist["StandardErrorPath"]).name == "pipecat-stt-server-kyutai.err"


# ---------------------------------------------------------------------------
# AUTH_TOKEN_FILE: path-only; token values in env are refused
# ---------------------------------------------------------------------------


def test_auth_token_file_rendered_as_flag(tmp_path: Path):
    dst = tmp_path / "auth.plist"
    r = _run_render({**TCP_ENV, "AUTH_TOKEN_FILE": "/Users/test/.config/pipecat-stt/token"}, dst)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    args = plistlib.loads(dst.read_bytes())["ProgramArguments"]
    assert args[args.index("--auth-token-file") + 1] == "/Users/test/.config/pipecat-stt/token"


def test_auth_token_file_omitted_by_default(tmp_path: Path):
    dst = tmp_path / "noauth.plist"
    r = _run_render(TCP_ENV, dst)
    assert r.returncode == 0, r.stderr
    assert "--auth-token-file" not in plistlib.loads(dst.read_bytes())["ProgramArguments"]


@pytest.mark.parametrize(
    "path",
    [
        "relative/token",
        "token",
        "/Users/test/tok;rm",
        "/Users/test/t</string><x/>",
        "/Users/test/$(id)",
    ],
)
def test_bad_auth_token_file_rejected(tmp_path: Path, path: str):
    dst = tmp_path / "auth.plist"
    r = _run_render({**TCP_ENV, "AUTH_TOKEN_FILE": path}, dst)
    assert r.returncode == 2, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert not dst.exists()


@pytest.mark.parametrize("var", ["PIPECAT_STT_AUTH_TOKEN", "KODA_STT_AUTH_TOKEN"])
@pytest.mark.parametrize("base", [TCP_ENV, {}])
def test_env_auth_token_refused(tmp_path: Path, var: str, base: dict[str, str | None]):
    """A token value in the renderer's env is refused (it would land in the
    world-readable plist); the message points at AUTH_TOKEN_FILE and the
    secret never appears in output or on disk."""
    secret = "s3cr3t-token-value-xyz"
    dst = tmp_path / "token.plist"
    r = _run_render({**base, var: secret}, dst)
    assert r.returncode == 2, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "AUTH_TOKEN_FILE" in r.stderr
    assert secret not in r.stdout and secret not in r.stderr
    assert not dst.exists()
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert secret.encode() not in f.read_bytes()


def test_empty_env_auth_token_is_ignored(tmp_path: Path):
    dst = tmp_path / "empty-token.plist"
    r = _run_render({**TCP_ENV, "PIPECAT_STT_AUTH_TOKEN": "", "KODA_STT_AUTH_TOKEN": ""}, dst)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert dst.read_bytes() == SNAPSHOT_TCP.read_bytes()


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


def test_renderer_loopback_hosts_match_the_servers():
    """The renderer keeps its own copy of the server's TCP bind allowlist (it must
    not import the server); this pins the two together."""
    from stt_server.errors import ConfigError
    from stt_server.server import ServerConfig

    spec = importlib.util.spec_from_file_location(
        "render_stt_plist_under_test",
        Path(__file__).resolve().parents[1] / "scripts" / "render_stt_plist.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for host in sorted(mod._LOOPBACK_HOSTS):
        ServerConfig(host=host, port=9999)
    for host in ("0.0.0.0", "::", "192.168.1.5", "example.com", "127.0.0.2"):
        assert host not in mod._LOOPBACK_HOSTS
        with pytest.raises(ConfigError):
            ServerConfig(host=host, port=9999)
