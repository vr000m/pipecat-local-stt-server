#!/usr/bin/env python3
"""Read one field from an installed STT LaunchAgent plist.

Usage:
    read_stt_plist.py --field NAME PLIST           # the value of NAME (sweep form)
    read_stt_plist.py --strict --field NAME PLIST  # the same; exit 2 (no output) if malformed
    read_stt_plist.py --has-auth PLIST             # exit 0: requires a token; 3: no token; else: error

NAME is one of ``host``, ``port``, ``socket-path``, ``auth-token-file`` (the serve
flags of the same name in ``ProgramArguments``) or ``interpreter``
(``ProgramArguments[0]``, the program launchd runs). The value is written to
stdout exactly, with no trailing newline, and is empty when the plist is absent
or does not carry it. One field per call, by name: no caller depends on the
order of anything in the output, and a value can hold any character (a newline
in a hostile plist cannot shift one field into another).

A plist is in one of three states:

* absent (no file at PLIST): no flags and no token;
* readable: a dict whose ``ProgramArguments`` (when present) is a list of
  strings and whose ``EnvironmentVariables`` (when present) is a dict;
* malformed: present but unreadable, not a plist (garbage or truncated XML), or
  of the wrong shape (a top-level array, a non-list ``ProgramArguments``, a
  non-string argument, ...).

The ``--field`` form is a sweep: it exits 0 in all three states, printing an
empty value for an absent or malformed plist (a malformed one also gets a
one-line warning on stderr, never a traceback), so callers can loop over many
plists. ``--strict`` is the same read for one plist whose answer matters (the
installer, stt-enable): a malformed plist exits 2 with an error and prints
nothing, so it can never read as "no socket".
``--has-auth`` must not guess: exit 0 means the agent requires a token, 3 means
the plist is absent or readable without a token, and 2 means it could not tell
(a malformed plist, a usage error, or an unexpected crash). "No token" is 3, not
1, because 1 is what a Python traceback or a broken interpreter shim exits with;
the installer treats every status but 0 and 3 (an interpreter missing is 127) as an
error, never as "no auth".

ProgramArguments is read with the server's own serve grammar (stt_server's
argparse parser, which has ``allow_abbrev=False``): a flag is its full name only,
its value is the next element or follows ``=`` in the same element
(``--auth-token-file=/x``), the last occurrence wins, and a serve switch that
takes no value (``--supervised``, ``--help``) never consumes the next element. A
VALUE is consumed in the step after its flag, so a value that looks like a flag
(``--log-level --socket-path``) is never read as one. tests/test_justfile_recipes.py
checks this against the real parser. The justfile (stt-list, stt-status,
stt-enable), scripts/install_stt_agent.sh and scripts/smoke.py all call this, and
scripts/smoke_launchd.py imports it (``load``, ``read_field``, ``read_flags``),
so they cannot disagree about what an installed plist says. Stdlib only: it must run under the
system python3 when the project venv is absent.
"""

from __future__ import annotations

import plistlib
import sys

_FLAGS = ("--host", "--port", "--socket-path", "--auth-token-file")
# --field NAME -> what it reads: a serve flag ("--NAME"), or the interpreter.
INTERPRETER = "interpreter"
FIELDS = (*(f[2:] for f in _FLAGS), INTERPRETER)
# The serve flags that take no value; every other ``--flag`` takes one. Pinned to
# the serve parser by tests/test_justfile_recipes.py.
_SWITCHES = frozenset({"--supervised", "--help"})
_ENV_TOKENS = ("PIPECAT_STT_AUTH_TOKEN", "KODA_STT_AUTH_TOKEN")
NO_AUTH = 3  # --has-auth exit status for "no token"; see the module docstring


class MalformedPlist(Exception):
    """The plist exists but cannot be read as an agent definition."""


def _load(path: str) -> dict:
    """Return the plist dict; ``{}`` when absent; raise ``MalformedPlist`` otherwise."""
    try:
        with open(path, "rb") as fh:
            data = plistlib.load(fh)
    except FileNotFoundError:
        return {}
    # Anything else is malformed: plistlib raises InvalidFileException, ValueError,
    # xml.parsers.expat.ExpatError (truncated XML), TypeError, ... and open() an
    # OSError for an unreadable file or a directory. The sweep must not trace back.
    # Only the exception's type is reported: a parse message can quote plist
    # content, and an old plist may carry a token in EnvironmentVariables.
    except Exception as exc:
        raise MalformedPlist(type(exc).__name__) from exc
    if not isinstance(data, dict):
        raise MalformedPlist(f"top level is {type(data).__name__}, not a dict")
    for key, kind in (("ProgramArguments", list), ("EnvironmentVariables", dict)):
        if key in data and not isinstance(data[key], kind):
            raise MalformedPlist(f"{key} is {type(data[key]).__name__}, not a {kind.__name__}")
    # launchd takes only strings here; anything else has no interpreter or flag
    # value to report (str() of a dict is not a path).
    bad = [type(a).__name__ for a in data.get("ProgramArguments", []) if not isinstance(a, str)]
    if bad:
        raise MalformedPlist(f"ProgramArguments holds a {bad[0]}, not only strings")
    return data


def load(path: str) -> dict:
    """The plist at PATH as a dict: ``{}`` when absent, ``MalformedPlist`` when
    malformed (the three states above). ``scripts/smoke_launchd.py`` reads every
    plist through this and ``read_field`` in-process."""
    return _load(path)


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
            name, eq, value = el.partition("=")
            if eq:
                if name in _FLAGS:
                    found[name] = value
            elif el not in _SWITCHES:
                expect = el
    return found


def read_field(plist: dict, name: str) -> str:
    """The value of field ``name`` (one of ``FIELDS``) in a loaded plist, or ``""``."""
    if name == INTERPRETER:
        args = plist.get("ProgramArguments")
        return str(args[0]) if isinstance(args, list) and args else ""
    return read_flags(plist).get(f"--{name}", "")


def has_auth(plist: dict) -> bool:
    env = plist.get("EnvironmentVariables")
    if isinstance(env, dict) and any(env.get(k) for k in _ENV_TOKENS):
        return True
    return bool(read_flags(plist).get("--auth-token-file"))


def main(argv: list[str]) -> int:
    args = argv[1:]
    if len(args) == 2 and args[0] == "--has-auth":
        try:
            return 0 if has_auth(_load(args[1])) else NO_AUTH
        except MalformedPlist as exc:
            print(f"error: cannot read plist {args[1]}: {exc}", file=sys.stderr)
            return 2
    strict = args[:1] == ["--strict"]
    if strict:
        args = args[1:]
    if len(args) != 3 or args[0] != "--field" or args[1] not in FIELDS:
        print(__doc__, file=sys.stderr)
        return 2
    name, path = args[1], args[2]
    try:
        value = read_field(_load(path), name)
    except MalformedPlist as exc:
        if strict:
            print(f"error: cannot read plist {path}: {exc}", file=sys.stderr)
            return 2
        print(f"warning: cannot read plist {path}: {exc}", file=sys.stderr)
        value = ""
    sys.stdout.write(value)
    return 0


if __name__ == "__main__":
    try:
        status = main(sys.argv)
    except Exception as exc:  # never a traceback, and never a "no auth" status
        print(f"error: read_stt_plist.py failed: {type(exc).__name__}", file=sys.stderr)
        status = 2
    sys.exit(status)
