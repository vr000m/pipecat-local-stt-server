"""Bearer-token rules and the TCP bind-host allowlist (server, CLI, install scripts).

- ``LOOPBACK_HOSTS``: the only hosts a TCP listener may bind. Loopback TCP has
  no peer-uid check (any local process can connect); a bearer token is the
  optional guard there. The server's ``ServerConfig`` and the plist renderer
  (``scripts/render_stt_plist.py``) both use this one set.
- ``read_token_file`` / ``check_token_chars``: the bearer-token content rules.
  The serve path, the probe client and the installer's preflight
  (``scripts/install_stt_agent.sh``) all call these, so a token one accepts can
  never be one another refuses.
- ``load_token_file`` / ``check_token_file_policy``: the token FILE policy (where
  it lives, who owns it, who can read or replace it). ``serve --auth-token-file``
  (``read_token_file(strict=True)``) and the installer's preflight both run this
  one implementation, so an agent the installer accepts is one the server starts,
  and a file the server would refuse never reaches a plist.

This module itself uses only the stdlib (plus ``stt_server.errors``), but
``import stt_server.auth`` first runs the package ``__init__``, which imports
``stt_server.client`` and so ``websockets``: callers (the renderer, the
installer's preflight) need the project venv, not a bare system python3. It does
not load the server runtime or any backend implementation.
"""

from __future__ import annotations

import errno
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

from .errors import ConfigError

LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})

# ACL rights that let a principal change a directory's entries or its metadata
# (and so swap or re-permission the token file under it).
ACL_WRITE_RIGHTS: frozenset[str] = frozenset(
    {
        "write",
        "append",
        "add_file",
        "add_subdirectory",
        "delete",
        "delete_child",
        "writeattr",
        "writeextattr",
        "writesecurity",
        "chown",
    }
)
# Group write on a parent directory is allowed only when root owns it and its
# group is wheel (0) or admin (80): those members can already act as root.
_ROOT_GROUPS = frozenset({0, 80})
# macOS `ls -lde` prints each ACL entry on its own line after the listing, e.g.
# " 0: group:staff allow read" or " 1: user:bob inherited allow add_file,delete_child".
_ACL_ENTRY_RE = re.compile(r"^\s*[0-9]+:\s")
_ACL_ALLOW_RE = re.compile(r"\sallow\s+([A-Za-z_,]+)\s*$")
_LS = "/bin/ls"


def check_token_chars(token: str, what: str) -> str:
    """Refuse a token that can never match an ``Authorization`` header.

    ``token`` is already stripped and non-empty. Raises ``ConfigError`` naming
    ``what`` (the source, never the value) for internal whitespace, a control
    character or a non-ASCII character. Shared by the token file and env paths.
    """
    if any(not 0x21 <= ord(c) <= 0x7E for c in token):
        raise ConfigError(
            f"{what} must hold one token of visible ASCII characters "
            "(no internal whitespace, newlines or non-ASCII); anything else can never "
            "match an Authorization header"
        )
    return token


class _AclUnreadable(Exception):
    """``ls -lde`` failed, so the ACL is unknown (callers fail closed)."""


def _acl_allows(path: str, want: frozenset[str] | None) -> bool:
    """True when ``path`` has an ACL allow entry granting a right in ``want`` (any
    right at all when ``want`` is None); raise ``_AclUnreadable`` when its ACL
    cannot be read. Deny entries only narrow access, so they never count. macOS
    only: elsewhere there is no ``ls -e`` ACL listing and the answer is False."""
    if sys.platform != "darwin":
        return False
    try:
        proc = subprocess.run(
            [_LS, "-lde", "--", path],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _AclUnreadable from exc
    if proc.returncode != 0:
        raise _AclUnreadable
    for line in proc.stdout.splitlines():
        if not _ACL_ENTRY_RE.match(line):
            continue
        m = _ACL_ALLOW_RE.search(line)
        if not m:
            continue
        if want is None or any(r in want for r in m.group(1).split(",")):
            return True
    return False


def _refuse(path: str, problem: str, fix: str) -> ConfigError:
    return ConfigError(f"auth token file '{path}' {problem}; fix: {fix}")


def check_token_file_policy(
    path: str | os.PathLike[str],
    *,
    uid: int | None = None,
    opened: os.stat_result | None = None,
) -> str:
    """Apply the token-file policy to ``path``; return its resolved path.

    ``opened`` is the ``fstat`` of the file as the caller opened it (with
    ``O_NOFOLLOW``); without it the path is ``lstat``-ed here. ``uid`` is who must
    own it (default: the effective uid). Raises ``ConfigError`` naming the path,
    the problem and the fix. The policy:

    * an absolute path whose last component is not a symlink, naming a regular
      file owned by ``uid`` with no group/other permission bits and (macOS) no
      ACL allow entry: a readable file leaks the bearer, someone else's file (or
      a symlink) lets them set it;
    * resolved == checked: the parent directory is resolved (symlinks followed)
      and the resolved name must be the same file; every directory from there to
      ``/`` must be owned by ``uid`` or root and writable by nobody else, by mode
      (group write only on a root-owned directory of group wheel or admin; a
      sticky bit exempts nothing, since it stops others deleting the file, not
      creating its name once it is gone, and the server re-opens the path at
      every start) or by an ACL allow entry with a write-class right.
    """
    p = os.fspath(path)
    me = os.geteuid() if uid is None else uid
    if not os.path.isabs(p):
        raise _refuse(p, "must be an absolute path", "pass the full path to the file")
    if opened is None:
        try:
            opened = os.lstat(p)
        except FileNotFoundError:
            raise _refuse(
                p, "is not an existing regular file", "create it (install -m 600 /dev/null PATH)"
            ) from None
        except OSError as exc:
            raise _refuse(p, f"cannot be inspected ({exc.strerror})", "check the path") from exc
        if stat.S_ISLNK(opened.st_mode):
            raise _refuse(p, "is a symlink", "point it at the real file")
    if not stat.S_ISREG(opened.st_mode):
        raise _refuse(p, "is not an existing regular file", "point it at a regular file")
    try:
        parent = os.path.realpath(os.path.dirname(p), strict=True)
    except OSError as exc:
        raise _refuse(p, "has an unreadable parent directory", "check its directories") from exc
    resolved = os.path.join(parent, os.path.basename(p))
    try:
        rst = os.lstat(resolved)
    except OSError:
        rst = None
    if (
        rst is None
        or not stat.S_ISREG(rst.st_mode)
        or (rst.st_dev, rst.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise _refuse(
            p,
            f"does not resolve to the same regular file ({resolved})",
            "point it at the real file",
        )
    if opened.st_uid != me:
        raise _refuse(
            p,
            f"is not owned by the current user (owner uid {opened.st_uid}, you are {me})",
            "recreate it as yourself (install -m 600 /dev/null PATH, then write the token)",
        )
    mode = stat.S_IMODE(opened.st_mode)
    if mode & 0o077:
        raise _refuse(
            p,
            f"has mode {mode & 0o777:o}; it must not be group/other accessible",
            f"chmod 600 '{p}'",
        )
    # The mode bits are not the whole story on macOS: an ACL can grant another
    # user read (the token leaks) or write. Any allow entry on the file is refused;
    # a deny entry only narrows access and is fine.
    try:
        file_acl = _acl_allows(resolved, None)
    except _AclUnreadable:
        file_acl = True
    if file_acl:
        raise _refuse(
            p,
            "has an ACL allow entry, or its ACL cannot be read (see 'ls -le')",
            f"chmod -N '{resolved}' removes the ACL",
        )
    d = parent
    while True:
        try:
            dst = os.stat(d)
        except OSError as exc:
            raise _refuse(
                p, f"has parent directory '{d}' that cannot be inspected", "check its directories"
            ) from exc
        if dst.st_uid not in (me, 0):
            raise _refuse(
                p,
                f"has parent directory '{d}' owned by another user (uid {dst.st_uid})",
                "move the token file into a directory you own, e.g. ~/.config/pipecat-stt",
            )
        dmode = stat.S_IMODE(dst.st_mode)
        group_ok = dst.st_uid == 0 and dst.st_gid in _ROOT_GROUPS
        if dmode & 0o002 or (dmode & 0o020 and not group_ok):
            raise _refuse(
                p,
                f"has parent directory '{d}' writable by others (mode {dmode & 0o777:o}, "
                f"gid {dst.st_gid})",
                "move the token file out of the shared directory into one only you can "
                "write, e.g. ~/.config/pipecat-stt (mode 700)",
            )
        try:
            dir_acl = _acl_allows(d, ACL_WRITE_RIGHTS)
        except _AclUnreadable:
            dir_acl = True
        if dir_acl:
            raise _refuse(
                p,
                f"has parent directory '{d}' with an ACL entry that lets someone else write "
                "to it, or whose ACL cannot be read (see 'ls -lde')",
                f"chmod -N '{d}' removes the ACL, or move the token file elsewhere",
            )
        if d == "/":
            break
        d = os.path.dirname(d)
    return resolved


def load_token_file(path: str | os.PathLike[str], *, uid: int | None = None) -> tuple[str, str]:
    """Open a token file under the policy and read its token: ``(resolved, token)``.

    The file is opened with ``O_NOFOLLOW`` (and ``O_NONBLOCK``, so a FIFO cannot
    hang startup), the policy is applied to the descriptor's own ``fstat`` and
    then to its resolved path (``check_token_file_policy``), and the token is read
    from that same descriptor: what was checked is what is read. Content rules
    are ``read_token_file``'s strict ones. Raises ``ConfigError``; messages never
    include the token.
    """
    p = os.fspath(path)
    if not os.path.isabs(p):
        raise _refuse(p, "must be an absolute path", "pass the full path to the file")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(p, flags)
    except FileNotFoundError:
        raise _refuse(
            p, "is not an existing regular file", "create it (install -m 600 /dev/null PATH)"
        ) from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _refuse(p, "is a symlink", "point it at the real file") from None
        if exc.errno == errno.EACCES:
            raise _refuse(p, "is unreadable", f"chmod 600 '{p}' (as its owner)") from None
        raise _refuse(p, f"cannot be opened ({exc.strerror})", "check the path") from exc
    try:
        resolved = check_token_file_policy(p, uid=uid, opened=os.fstat(fd))
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
    finally:
        os.close(fd)
    try:
        text = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"auth token file {p} is not valid UTF-8") from exc
    return resolved, _strict_token(text.strip(), p)


def _strict_token(token: str, path: str) -> str:
    if not token:
        raise ConfigError(f"auth token file {path} is empty; an empty token would disable auth")
    return check_token_chars(token, f"auth token file {path}")


def read_token_file(path: str | os.PathLike[str], *, strict: bool = False) -> str | None:
    """Read a bearer-token file: UTF-8, surrounding whitespace stripped.

    The server compares ``Authorization: Bearer <token>`` byte-for-byte, so the
    token must survive an HTTP header. ``strict`` (the serve path) first applies
    the token-file policy (``load_token_file``: absolute path, no symlink, a
    regular file you own with no group/other bits or ACL allow entry, in a
    directory chain nobody else can write) and then refuses what would otherwise
    silently misbehave: an empty/whitespace-only file (the token would be
    ``None`` and auth disabled), a token with internal whitespace or a non-ASCII
    / control character (it can never match a header), and a non-UTF-8 file.
    Non-strict (the probe client) reads any file and keeps the lenient ``None``
    for an empty one. Messages never include the token.
    """
    if strict:
        return load_token_file(path)[1]
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ConfigError(f"auth token file {path} is not valid UTF-8") from exc
    return token or None
