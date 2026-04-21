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

LABEL = "koda.stt-server"

_ABSPATH_RE = re.compile(r"^/[A-Za-z0-9._/+\- @]+$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")
_BACKEND_RE = re.compile(r"^(echo|mlx)$")


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
    python = _require("PYTHON", os.environ.get("PYTHON"), _ABSPATH_RE, "absolute path")
    cwd = _require("REPO_ROOT", os.environ.get("REPO_ROOT"), _ABSPATH_RE, "absolute path")
    socket_path = _require(
        "SOCKET_PATH", os.environ.get("SOCKET_PATH"), _ABSPATH_RE, "absolute path"
    )
    backend = _require("BACKEND", os.environ.get("BACKEND"), _BACKEND_RE, "echo|mlx")
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
        "Label": LABEL,
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
        "StandardOutPath": str(Path(log_dir) / "koda-stt.log"),
        "StandardErrorPath": str(Path(log_dir) / "koda-stt.err"),
    }

    auth_token = os.environ.get("KODA_STT_AUTH_TOKEN")
    if auth_token:
        # Prefer env over --auth-token so the token never lands in `ps`.
        plist["EnvironmentVariables"]["KODA_STT_AUTH_TOKEN"] = auth_token

    out = Path(plist_dst)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Write under a restrictive umask so the plist containing
    # KODA_STT_AUTH_TOKEN is 0o600 from the start (no race where another
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
