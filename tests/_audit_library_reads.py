"""Run a script under an audit hook that logs any touch of the real ~/Library.

    python _audit_library_reads.py LOG SCRIPT [ARGS...]

Used by ``tests/test_smoke_launchd.py`` to prove a stubbed harness run reads no
path under the account's real ``~/Library`` (only the temp ``HOME``). LOG gets
``audit: on`` and then one line per offending event.
"""

import os
import pwd
import runpy
import sys

LOG = sys.argv[1]
REAL = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir) + "/Library/"
_PATH_EVENTS = {"open", "os.scandir", "os.listdir", "os.mkdir", "os.remove", "os.rmdir"}
_busy = False


def _bad(p) -> bool:
    try:
        p = os.fsdecode(p)
    except TypeError:
        return False
    return os.path.realpath(os.path.expanduser(p)).startswith(REAL) or p.startswith(REAL)


def _hook(event, args):
    global _busy
    if _busy:
        return
    hits = []
    if event in _PATH_EVENTS and args and isinstance(args[0], (str, bytes, os.PathLike)):
        if _bad(args[0]):
            hits.append(os.fsdecode(args[0]))
    elif event == "subprocess.Popen" and len(args) > 1:
        hits += [str(a) for a in (args[1] or []) if isinstance(a, str) and _bad(a)]
    if hits:
        _busy = True
        try:
            with open(LOG, "a") as fh:
                fh.write(f"{event} {hits}\n")
        finally:
            _busy = False


with open(LOG, "w") as fh:
    fh.write("audit: on\n")
sys.addaudithook(_hook)
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name="__main__")
