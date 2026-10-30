"""Run a script under an audit hook that logs any touch of the real ~/Library.

    python _audit_library_reads.py LOG SCRIPT [ARGS...]

Used by ``tests/test_smoke_launchd.py`` to prove a stubbed harness run reads no
path under the account's real ``~/Library`` (only the temp ``HOME``). LOG gets
``audit: on`` and then one line per offending event.

CPython raises no audit event for ``os.stat`` or ``os.lstat`` (what
``Path.exists``, ``Path.is_symlink``, ``Path.stat`` and ``os.path.exists`` use),
so those two are wrapped here as well; the audit events cover open, directory
listing, mkdir/rmdir, remove/unlink, chmod, rename and ``shutil.rmtree``.
"""

import os
import pwd
import runpy
import sys

LOG = sys.argv[1]
REAL = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir) + "/Library/"
# Audit events whose first argument is a path; for the two-path ones the
# second argument is checked too.
_PATH_EVENTS = {
    "open",
    "os.scandir",
    "os.listdir",
    "os.mkdir",
    "os.remove",
    "os.rmdir",
    "os.chmod",
    "os.chown",
    "os.truncate",
    "os.utime",
    "shutil.rmtree",
}
_TWO_PATH_EVENTS = {"os.rename", "os.link", "os.symlink"}
_busy = False
_stat, _lstat = os.stat, os.lstat


def _bad(p) -> bool:
    if not isinstance(p, (str, bytes, os.PathLike)):
        return False
    try:
        p = os.fsdecode(p)
    except TypeError:
        return False
    return os.path.realpath(os.path.expanduser(p)).startswith(REAL) or p.startswith(REAL)


def _log(event, hits):
    with open(LOG, "a") as fh:
        fh.write(f"{event} {hits}\n")


def _hook(event, args):
    global _busy
    if _busy:
        return
    _busy = True  # realpath below stats paths too
    try:
        hits = []
        if event in _PATH_EVENTS and args and _bad(args[0]):
            hits.append(os.fsdecode(args[0]))
        elif event in _TWO_PATH_EVENTS and len(args) > 1:
            hits += [os.fsdecode(a) for a in args[:2] if _bad(a)]
        elif event == "subprocess.Popen" and len(args) > 1:
            hits += [str(a) for a in (args[1] or []) if isinstance(a, str) and _bad(a)]
        if hits:
            _log(event, hits)
    finally:
        _busy = False


def _watched(name, real):
    def wrapper(path, *a, **k):
        global _busy
        if not _busy and not isinstance(path, int):
            _busy = True
            try:
                if _bad(path):
                    _log(name, [os.fsdecode(path)])
            finally:
                _busy = False
        return real(path, *a, **k)

    return wrapper


with open(LOG, "w") as fh:
    fh.write("audit: on\n")
os.stat = _watched("os.stat", _stat)
os.lstat = _watched("os.lstat", _lstat)
sys.addaudithook(_hook)
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name="__main__")
