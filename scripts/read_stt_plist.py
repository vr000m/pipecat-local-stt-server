#!/usr/bin/env python3
"""Read the endpoint flags from an installed STT LaunchAgent plist.

Usage:
    read_stt_plist.py PLIST             # four lines: host, port, socket-path, auth-token-file
    read_stt_plist.py --has-auth PLIST  # exit 0 if the agent requires a token, else 1

An absent or unreadable plist, or a flag that is not there, gives an empty
line. The exit status is 0 either way, so callers can sweep many plists.

ProgramArguments is walked element by element and a flag's VALUE is consumed in
the step after the flag, so a value that looks like a flag (``--log-level
--socket-path``) is never read as one. The justfile (stt-list, stt-status,
stt-probe) and scripts/install_stt_agent.sh both call this, so they cannot
disagree about what an installed plist says. Stdlib only: it must run under the
system python3 when the project venv is absent.
"""

from __future__ import annotations

import plistlib
import sys

_FLAGS = ("--host", "--port", "--socket-path", "--auth-token-file")
_ENV_TOKENS = ("PIPECAT_STT_AUTH_TOKEN", "KODA_STT_AUTH_TOKEN")


def _load(path: str) -> dict:
    try:
        with open(path, "rb") as fh:
            data = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_flags(plist: dict) -> dict[str, str]:
    args = plist.get("ProgramArguments")
    found: dict[str, str] = {}
    if not isinstance(args, list):
        return found
    expect = ""
    for el in (str(a) for a in args):
        if expect:
            if expect in _FLAGS:
                found[expect] = el
            expect = ""
        elif el.startswith("--"):
            expect = el
    return found


def has_auth(plist: dict) -> bool:
    env = plist.get("EnvironmentVariables")
    if isinstance(env, dict) and any(env.get(k) for k in _ENV_TOKENS):
        return True
    return bool(read_flags(plist).get("--auth-token-file"))


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--has-auth":
        return 0 if has_auth(_load(argv[2])) else 1
    if len(argv) != 2 or argv[1].startswith("--"):
        print(__doc__, file=sys.stderr)
        return 2
    flags = read_flags(_load(argv[1]))
    sys.stdout.write("".join(f"{flags.get(f, '')}\n" for f in _FLAGS))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
