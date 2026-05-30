"""Render the koda.stt-server LaunchAgent plist safely.

Uses ``plistlib`` so XML escaping / quoting is handled by the stdlib instead
of ``sed`` string substitution (which would let hostile env values break out
of <string> and inject arbitrary ProgramArguments, a login-time RCE).

Inputs are read from env vars (see ``scripts/install_stt_agent.sh``) and
allowlist-validated before being placed in the plist. Unknown/invalid values
fail loudly rather than silently producing a broken or malicious plist.
"""

from __future__ import annotations

import os
import plistlib
import re
import sys
from pathlib import Path

DEFAULT_LABEL = "koda.stt-server"

_ABSPATH_RE = re.compile(r"^/[A-Za-z0-9._/+\- @]+$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")
_BACKEND_RE = re.compile(r"^(echo|mlx|parakeet)$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


def _env_first(*names: str) -> str | None:
    """Return the first set, non-empty env var among ``names``.

    Canonical-then-alias resolution: pass the canonical ``PIPECAT_STT_*`` name
    first, the deprecated ``KODA_STT_*`` alias second. Mirrors
    ``stt_server.env.env_first`` (not imported here — this script runs as a
    standalone subprocess from the launchd-render path).
    """
    for name in names:
        val = os.environ.get(name)
        if val is not None and val.strip() != "":
            return val
    return None


def _log_basename(label: str) -> str:
    """Derive a per-agent log-file basename from the launchd label.

    The legacy default label maps to the historical ``koda-stt`` basename so
    the default-env plist stays byte-identical to the pre-multi-instance
    render. Any other label gets a collision-free basename by replacing the
    ``.`` separators with ``-`` (e.g. ``koda.stt-server.parakeet`` ->
    ``koda-stt-server-parakeet``).
    """
    if label == DEFAULT_LABEL:
        return "koda-stt"
    return label.replace(".", "-")


def _require(name: str, value: str | None, pattern: re.Pattern[str], hint: str) -> str:
    if not value:
        print(f"error: {name} is required", file=sys.stderr)
        sys.exit(2)
    if not pattern.match(value):
        print(
            f"error: {name}={value!r} rejected by allowlist ({hint})",
            file=sys.stderr,
        )
        sys.exit(2)
    return value


def main() -> None:
    # The label is optional; an unset value keeps the legacy single-agent
    # label so the default-env render stays byte-identical to today's plist.
    # Canonical PIPECAT_STT_LABEL wins; KODA_STT_LABEL is a deprecated alias.
    label = _env_first("PIPECAT_STT_LABEL", "KODA_STT_LABEL") or DEFAULT_LABEL
    if not _LABEL_RE.match(label):
        print(
            f"error: PIPECAT_STT_LABEL={label!r} rejected by allowlist (alphanumerics / . _ -)",
            file=sys.stderr,
        )
        sys.exit(2)
    log_basename = _log_basename(label)

    python = _require("PYTHON", os.environ.get("PYTHON"), _ABSPATH_RE, "absolute path")
    cwd = _require("REPO_ROOT", os.environ.get("REPO_ROOT"), _ABSPATH_RE, "absolute path")
    socket_path = _require(
        "SOCKET_PATH", os.environ.get("SOCKET_PATH"), _ABSPATH_RE, "absolute path"
    )
    backend = _require("BACKEND", os.environ.get("BACKEND"), _BACKEND_RE, "echo|mlx|parakeet")
    model = _require(
        "MODEL",
        os.environ.get("MODEL"),
        _MODEL_RE,
        "alphanumerics / . _ / -",
    )
    home = _require("HOME", os.environ.get("HOME"), _ABSPATH_RE, "absolute path")
    log_dir = _require("LOG_DIR", os.environ.get("LOG_DIR"), _ABSPATH_RE, "absolute path")
    plist_dst = _require("PLIST_DST", os.environ.get("PLIST_DST"), _ABSPATH_RE, "absolute path")

    plist: dict = {
        "Label": label,
        "ProgramArguments": [
            python,
            "-m",
            "stt_server",
            "--socket-path",
            socket_path,
            "--backend",
            backend,
            "--model",
            model,
            "--log-level",
            "INFO",
        ],
        "WorkingDirectory": cwd,
        # Run at login and keep alive. ThrottleInterval guards against
        # restart storms from a fast-failing server (e.g. missing model).
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": home,
        },
        "StandardOutPath": str(Path(log_dir) / f"{log_basename}.log"),
        "StandardErrorPath": str(Path(log_dir) / f"{log_basename}.err"),
    }

    # Accept the canonical PIPECAT_STT_AUTH_TOKEN first, then the deprecated
    # KODA_STT_AUTH_TOKEN alias. The server reads PIPECAT_STT_AUTH_TOKEN-first,
    # so render the canonical name into the plist EnvironmentVariables block.
    auth_token = _env_first("PIPECAT_STT_AUTH_TOKEN", "KODA_STT_AUTH_TOKEN")
    if auth_token:
        # Prefer env over --auth-token so the token never lands in `ps`.
        plist["EnvironmentVariables"]["PIPECAT_STT_AUTH_TOKEN"] = auth_token

    out = Path(plist_dst)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Write under a restrictive umask so the plist containing
    # PIPECAT_STT_AUTH_TOKEN is 0o600 from the start (no race where another
    # local user could read it between create and chmod).
    prev_umask = os.umask(0o077)
    try:
        with out.open("wb") as f:
            plistlib.dump(plist, f)
    finally:
        os.umask(prev_umask)
    # Belt-and-braces: enforce 0o600 even if the file already existed.
    os.chmod(out, 0o600)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
