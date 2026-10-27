#!/usr/bin/env bash
# Install the pipecat.stt-server LaunchAgent so the transcription server
# runs at login and is auto-restarted by launchd on crash.
#
# Usage:
#   scripts/install_stt_agent.sh [install|uninstall|start|stop|restart|status|logs]
#   scripts/install_stt_agent.sh port [BACKEND]   # print a backend's canonical port;
#                                                 # touches neither launchctl nor files
#
# Environment overrides. The canonical PIPECAT_STT_* names take precedence;
# the legacy KODA_STT_* names (shown in parentheses) are still honoured as
# deprecated aliases:
#   PIPECAT_STT_LABEL   (KODA_STT_LABEL)   launchd label / plist filename
#                       (default: pipecat.stt-server)
#   PIPECAT_STT_SOCKET  (KODA_STT_SOCKET)  path to a UDS socket. When non-empty the
#                       agent is a UDS agent (legacy transport); when unset/empty
#                       the agent listens on loopback TCP (the default).
#   PIPECAT_STT_HOST    loopback host for TCP mode (default: 127.0.0.1)
#   PIPECAT_STT_PORT    TCP port (default: backend canonical port — whisper/mlx
#                       9565, kyutai 9665, parakeet 9765, nemotron 9865; the echo
#                       backend has none and requires an explicit port)
#   PIPECAT_STT_AUTH_TOKEN_FILE  absolute path to a file holding the auth token
#                       (TCP mode). Raw PIPECAT_STT_AUTH_TOKEN is refused. The
#                       policy below is stt_server.auth.load_token_file, which
#                       `serve --auth-token-file` also runs at every start. The
#                       file must be a regular file (not a symlink) owned by you,
#                       with no group/other permission bits and no ACL allow
#                       entry, in a directory chain (symlinks resolved) that only
#                       you or root own and nobody else can write to, by mode or
#                       by an ACL allow entry with a write right (group write only
#                       on a root-owned directory of group wheel or admin; a sticky
#                       bit exempts nothing, so /tmp is refused). The plist records the
#                       resolved path, the one that was checked. Its content must be one token of
#                       visible ASCII (UTF-8, surrounding whitespace stripped; an
#                       empty file, internal whitespace or non-ASCII is refused).
#   PIPECAT_STT_UNLOAD_WAIT  seconds (integer 0..86400, read as decimal so "08"
#                       is 8; larger values are refused, not wrapped) to wait for
#                       launchd to finish tearing the old job down before
#                       bootstrap, and before uninstall removes a socket
#                       (default 30)
#   PIPECAT_STT_PYTHON  interpreter (default: .venv/bin/python, or
#                       .venv-kyutai/bin/python for the kyutai backend)
#   PIPECAT_STT_BACKEND (KODA_STT_BACKEND) backend name: echo|mlx|parakeet|nemotron|kyutai (default: mlx)
#   PIPECAT_STT_MODEL   (KODA_STT_MODEL)   model id (default: backend-aware — Whisper repo for
#                       mlx/echo, mlx-community/parakeet-tdt-0.6b-v3 for parakeet,
#                       mlx-community/nemotron-3.5-asr-streaming-0.6b for nemotron,
#                       kyutai/stt-1b-en_fr-mlx for kyutai)
#
# Two-agent install recipe (run Whisper and Parakeet ASR side by side). The
# default transport is loopback TCP on the backend's canonical port:
#
#   # 1. Whisper agent — default env: label pipecat.stt-server, whisper (mlx)
#   #    backend, TCP on 127.0.0.1:9565:
#   scripts/install_stt_agent.sh install
#
#   # 2. Parakeet agent — distinct label and backend; TCP on 127.0.0.1:9765
#   #    (its canonical port, picked from the backend):
#   #    Warm the ~1.5 GB Hugging Face model cache FIRST, otherwise the
#   #    first launch downloads it under KeepAlive + ThrottleInterval=10
#   #    and launchd may throttle-loop the agent before the download finishes.
#   .venv/bin/python -c 'import parakeet_mlx; parakeet_mlx.from_pretrained("mlx-community/parakeet-tdt-0.6b-v3")'
#   PIPECAT_STT_LABEL=pipecat.stt-server.parakeet \
#     PIPECAT_STT_BACKEND=parakeet \
#     scripts/install_stt_agent.sh install
#
#   # Socket opt-in (legacy transport) for either agent: set PIPECAT_STT_SOCKET
#   # to an absolute path under $HOME, e.g. for parakeet:
#   PIPECAT_STT_LABEL=pipecat.stt-server.parakeet \
#     PIPECAT_STT_SOCKET="$HOME/Library/Caches/pipecat-stt/parakeet.sock" \
#     PIPECAT_STT_BACKEND=parakeet \
#     scripts/install_stt_agent.sh install
#   # A socket install ignores PIPECAT_STT_HOST/PORT/AUTH_TOKEN_FILE (warned on
#   # stderr). Consumers must then point at the socket (e.g. STT_WS_SOCKET); a
#   # TCP install (or one onto a different socket path) removes a previously
#   # installed socket endpoint once the old job has unloaded.
#
# Auth token: pass a FILE (PIPECAT_STT_AUTH_TOKEN_FILE=/abs/path, TCP mode).
# A raw PIPECAT_STT_AUTH_TOKEN / KODA_STT_AUTH_TOKEN in the environment is
# REFUSED (the install fails) because it would be written into the plist.
# A reinstall over an agent whose installed plist requires a token (an env token,
# or --auth-token-file /x or --auth-token-file=/x: read with the server's own
# full-name-only grammar) fails unless PIPECAT_STT_AUTH_TOKEN_FILE is set.
#
# Operational constraint: this script manages exactly ONE agent per
# invocation, identified by PIPECAT_STT_LABEL. There is no registry or "all"
# mode — to manage the Parakeet agent with any subcommand
# (uninstall/start/stop/restart/status/logs) you MUST re-export its
# PIPECAT_STT_LABEL (and PIPECAT_STT_BACKEND when it is not the default), e.g.:
#   PIPECAT_STT_LABEL=pipecat.stt-server.parakeet \
#     PIPECAT_STT_BACKEND=parakeet \
#     scripts/install_stt_agent.sh status
# Uninstall removes the socket named in the INSTALLED plist, not the env.
# A default-env invocation always targets the default pipecat.stt-server agent.
set -euo pipefail

# Canonical PIPECAT_STT_* names take precedence; the legacy KODA_STT_* names
# are still honoured as deprecated aliases.
LABEL="${PIPECAT_STT_LABEL:-${KODA_STT_LABEL:-pipecat.stt-server}}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RENDER_PY="$REPO_ROOT/scripts/render_stt_plist.py"
READ_PLIST_PY="$REPO_ROOT/scripts/read_stt_plist.py"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="${PIPECAT_STT_LOG_DIR:-${KODA_STT_LOG_DIR:-$HOME/Library/Logs/pipecat-stt}}"
# Transport: socket mode only when PIPECAT_STT_SOCKET (or the deprecated
# KODA_STT_SOCKET alias) is non-empty; otherwise loopback TCP. Empty string
# counts as unset. When using a socket, keep it under the user's Caches dir
# (not /tmp, which is world-writable and lets a local attacker pre-create the
# path to DoS the agent).
SOCKET_PATH="${PIPECAT_STT_SOCKET:-${KODA_STT_SOCKET:-}}"
BACKEND="${PIPECAT_STT_BACKEND:-${KODA_STT_BACKEND:-mlx}}"
# Backend-aware MODEL default. render_stt_plist.py validates MODEL and
# exits when unset, so supply a sensible default per backend here: the
# Whisper repo for mlx/echo, the Parakeet TDT model for parakeet, the
# Nemotron 3.5 ASR model for nemotron. The parakeet default must agree with
# DEFAULT_PARAKEET_MODEL in stt_server/backends/parakeet.py; the nemotron
# default must agree with DEFAULT_NEMOTRON_MODEL in
# stt_server/backends/nemotron.py. Without the nemotron arm, BACKEND=nemotron
# would fall to the else and silently install the Whisper repo id.
if [[ "$BACKEND" == "parakeet" ]]; then
    DEFAULT_MODEL="mlx-community/parakeet-tdt-0.6b-v3"
elif [[ "$BACKEND" == "nemotron" ]]; then
    DEFAULT_MODEL="mlx-community/nemotron-3.5-asr-streaming-0.6b"
elif [[ "$BACKEND" == "kyutai" ]]; then
    DEFAULT_MODEL="kyutai/stt-1b-en_fr-mlx"
else
    DEFAULT_MODEL="mlx-community/whisper-large-v3-turbo"
fi
MODEL="${PIPECAT_STT_MODEL:-${KODA_STT_MODEL:-$DEFAULT_MODEL}}"

# TCP transport settings (used only when SOCKET_PATH is empty). Canonical
# ports are kept in step with the menu bar app; echo has none.
HOST="${PIPECAT_STT_HOST:-127.0.0.1}"
AUTH_TOKEN_FILE="${PIPECAT_STT_AUTH_TOKEN_FILE:-}"
# The ONE backend -> canonical port map. Keys are exactly the renderer's backend
# names (_BACKEND_RE); the justfile maps its user-facing 'whisper' to 'mlx' and
# asks this map through the `port` subcommand below instead of keeping its own,
# and docs/operations.md's port table is checked against it
# (tests/test_justfile_recipes.py). Prints the port; returns 1 for a backend
# with none (echo, or an unknown name).
canonical_port() {
    case "$1" in
    mlx) echo 9565 ;;
    kyutai) echo 9665 ;;
    parakeet) echo 9765 ;;
    nemotron) echo 9865 ;;
    *) return 1 ;;
    esac
}
CANONICAL_PORT="$(canonical_port "$BACKEND")" || CANONICAL_PORT=""
PORT="${PIPECAT_STT_PORT:-$CANONICAL_PORT}"

# Derive a per-agent log basename. Two explicit literal branches keep this in
# lockstep with render_stt_plist.py's _log_basename(): the new default maps to
# pipecat-stt, the retained legacy label keeps the historical koda-stt names,
# and any other label replaces '.' with '-' so two agents never share a log.
# The branches match string literals, NOT the LABEL default, so the new default
# can never be silently remapped to the old basename.
if [[ "$LABEL" == "pipecat.stt-server" ]]; then
    LOG_BASENAME="pipecat-stt"
elif [[ "$LABEL" == "koda.stt-server" ]]; then  # legacy shim, retained
    LOG_BASENAME="koda-stt"  # legacy basename, retained
else
    LOG_BASENAME="${LABEL//./-}"
fi

cmd="${1:-install}"

# `port [BACKEND]`: print BACKEND's canonical port (default: PIPECAT_STT_BACKEND,
# else mlx) and exit, before anything below can call launchctl or touch the
# filesystem; everything above this point only computes variables. Exit 1, with
# nothing on stdout, for a backend that has no canonical port.
if [[ "$cmd" == "port" ]]; then
    if ! canonical_port "${2:-$BACKEND}"; then
        echo "error: backend '${2:-$BACKEND}' has no canonical port (backends with one: mlx|kyutai|parakeet|nemotron)" >&2
        exit 1
    fi
    exit 0
fi

# Validate the unload-wait cap up front, before any launchctl call: it is used in
# an arithmetic comparison, where a non-numeric value aborts under `set -u` after
# the job was already booted out, a negative one would skip the wait, and a value
# past bash's 64-bit range wraps negative under 10# (skipping the wait too). So the
# accepted range is bounded explicitly: digits only, 0..UNLOAD_WAIT_MAX (a day).
UNLOAD_WAIT_MAX=86400
UNLOAD_WAIT="${PIPECAT_STT_UNLOAD_WAIT:-30}"
if [[ "$cmd" == "install" || "$cmd" == "uninstall" ]]; then
    if [[ ! "$UNLOAD_WAIT" =~ ^[0-9]+$ ]]; then
        echo "error: PIPECAT_STT_UNLOAD_WAIT='$UNLOAD_WAIT' must be a non-negative integer (seconds)" >&2
        exit 1
    fi
    # Strip leading zeros BEFORE any arithmetic: the length check then bounds the
    # value without evaluating a possibly overflowing number, and "00000000000008"
    # stays a valid 8. Bash arithmetic reads a leading 0 as octal ("08" is a base
    # error, "010" is 8), so the trimmed digits are read with 10# (decimal).
    trimmed_wait="${UNLOAD_WAIT#"${UNLOAD_WAIT%%[!0]*}"}"
    if ((${#trimmed_wait} > ${#UNLOAD_WAIT_MAX})) || ((10#${trimmed_wait:-0} > UNLOAD_WAIT_MAX)); then
        echo "error: PIPECAT_STT_UNLOAD_WAIT='$UNLOAD_WAIT' must be at most $UNLOAD_WAIT_MAX seconds" >&2
        exit 1
    fi
    UNLOAD_WAIT=$((10#${trimmed_wait:-0}))
fi

# Resolve the python interpreter. Only the install subcommand needs it (to run
# the renderer), so uninstall/status/logs do not require e.g. .venv-kyutai.
if [[ "$BACKEND" == "kyutai" ]]; then
    DEFAULT_PYTHON="$REPO_ROOT/.venv-kyutai/bin/python"
else
    DEFAULT_PYTHON="$REPO_ROOT/.venv/bin/python"
fi
PYTHON="${PIPECAT_STT_PYTHON:-$DEFAULT_PYTHON}"
if [[ "$cmd" == "install" && ! -x "$PYTHON" ]]; then
    if [[ "$BACKEND" == "kyutai" ]]; then
        echo "error: $PYTHON not found — create the kyutai venv first ('just _ensure-extra kyutai' creates .venv-kyutai)" >&2
    else
        echo "error: $PYTHON not found — run 'uv sync' first" >&2
    fi
    exit 1
fi

# scripts/read_stt_plist.py is the one reader of the installed plist (the justfile
# calls it too). It and unlink_socket_under_lock below are stdlib only: install, which already
# requires $PYTHON, runs them there; every other subcommand (uninstall) runs them
# under the system python3, so it works with no venv.
if [[ "$cmd" == "install" ]]; then
    READER_PYTHON="$PYTHON"
else
    READER_PYTHON="python3"
fi

# Print the value of field NAME (e.g. socket-path; the reader's --field names) in
# the installed plist (empty if the plist is absent or does not carry it). Used
# to decide socket cleanup from the transport actually installed, not from the
# caller's env. One keyed read per field: nothing here depends on the order of
# the reader's output. Returns 2 (printing nothing; the reader's error is on
# stderr) when a plist is there but cannot be read, or the reader failed: the
# reader's --strict form, so a malformed plist never reads as "no socket",
# whatever the caller checked before. Call it as
# `if ! v="$(installed_field NAME)"; then ...`.
installed_field() {
    local name="$1" out rc=0
    [[ -e "$PLIST_DST" || -L "$PLIST_DST" ]] || return 0
    out="$("$READER_PYTHON" -I "$READ_PLIST_PY" --strict --field "$name" "$PLIST_DST")" || rc=$?
    [[ "$rc" -eq 0 ]] || return 2
    printf '%s' "$out"
}

# Classify the INSTALLED plist by its return status. The token value is never
# printed.
#   0  it requires a token: a non-empty env token in its EnvironmentVariables
#      (pre-0.5.0 plists) or an --auth-token-file argument;
#   1  there is no plist, or it was read and carries no token (the reader's
#      exit 3: never 1, which a Python traceback or a broken shim exits with);
#   2  a plist is there but could not be read (malformed, unreadable, wrong
#      shape), or the reader itself failed (any status but 0 or 3: the
#      interpreter missing, a crash). This is never "no auth": install aborts.
installed_auth_state() {
    local rc=0
    [[ -e "$PLIST_DST" || -L "$PLIST_DST" ]] || return 1
    "$READER_PYTHON" -I "$READ_PLIST_PY" --has-auth "$PLIST_DST" || rc=$?
    case "$rc" in
    0) return 0 ;;
    3) return 1 ;;
    *) return 2 ;;
    esac
}

# Refuse a token file the server would refuse or mis-handle, and set
# RESOLVED_TOKEN_FILE to the path the plist must carry. The whole policy, for the
# file (absolute path, not a symlink, a regular file you own with no group/other
# bits and no ACL allow entry), its resolved directory chain (owned by you or
# root, writable by nobody else by mode or by a write-class ACL allow entry;
# group write only on a root-owned wheel/admin directory; a sticky bit exempts
# nothing) and its content (one visible-ASCII token), is the server's own
# stt_server.auth.load_token_file: `serve --auth-token-file` runs the same code at
# every start, so an agent this accepts is one the server starts, and the two
# cannot drift. It opens the file O_NOFOLLOW and checks what it opened. The uid
# comes from `id -u` here, as for every other check in this script.
#
# Validated == opened: the policy resolves the parent chain (symlinks followed)
# and checks that symlink-free path, which is what the plist records. A
# symlinked directory in the path as given could be repointed after this check;
# the resolved path has no such component left to repoint.
validate_token_file() {
    local f="$1" me resolved
    me="$(id -u)"
    if ! resolved="$("$PYTHON" -I -c '
import sys
try:
    from stt_server.auth import load_token_file
except ImportError:
    print(
        "error: cannot import stt_server - run with the project venv interpreter "
        "(PIPECAT_STT_PYTHON, default .venv/bin/python after uv sync)",
        file=sys.stderr,
    )
    sys.exit(1)
try:
    resolved, _token = load_token_file(sys.argv[1], uid=int(sys.argv[2]))
except ValueError as exc:
    print(f"error: PIPECAT_STT_AUTH_TOKEN_FILE: {exc}", file=sys.stderr)
    sys.exit(1)
sys.stdout.write(resolved)
' "$f" "$me")"; then
        exit 1
    fi
    # The renderer enforces its plist allowlist after the directories exist; ask
    # it now (its own --check-abspath mode, one regex) so a refused path fails
    # before any mkdir.
    if ! "$PYTHON" -I "$RENDER_PY" --check-abspath PIPECAT_STT_AUTH_TOKEN_FILE "$resolved"; then
        exit 1
    fi
    if [[ "$resolved" != "$f" ]]; then
        echo "note: PIPECAT_STT_AUTH_TOKEN_FILE resolves to $resolved; the plist records that path"
    fi
    RESOLVED_TOKEN_FILE="$resolved"
}

# Block until launchd no longer lists $LABEL. Bounded by PIPECAT_STT_UNLOAD_WAIT
# seconds (default 30): returns 0 once the job is gone, 1 (with a warning) if it
# is still loaded at the cap. Callers that remove the old job's socket must only
# do so after a 0 return: the old server owns it until it exits.
wait_unloaded() {
    local waited=0 cap="$UNLOAD_WAIT"
    while launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; do
        if [[ "$waited" -ge "$cap" ]]; then
            echo "warning: $LABEL still loaded ${cap}s after bootout" >&2
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    return 0
}

# Unlink the stale socket $1 while holding a shared flock on "$1.lock" (the
# server's _lock_path rule), so the lock check and the unlink are one step: a
# starter takes that lock (exclusive) before it binds, so while this holds it no
# server can bind a fresh socket at the path, and the unlink can only hit a
# socket no live server owns. The checks on the socket (a real socket per lstat,
# so never a symlink; owned by uid $2) run under the same lock, right before the
# unlink. Exit status: 0 removed (or nothing there), 1 the lock is held by
# another process (a running server), 2 cannot tell (any error: fails closed),
# 3 not a socket, 4 not owned by uid $2, 5 the unlink failed.
#   * The lock is shared and held only for these few syscalls: it conflicts with
#     a server's exclusive lock, and acquire_socket_lock retries a contended lock
#     for 250 ms, so it never makes a real starter fail (as for
#     stt_server.server.socket_lock_held). The lock drops when this exits, after
#     the unlink.
#   * No regular file at "$1.lock": no lock is taken, so nothing excludes a
#     starter. A server that creates the .lock (acquire_socket_lock) after the
#     re-read below, flocks it, unlinks the stale socket and binds, can have its
#     fresh socket removed by the unlink here (a window of a few syscalls; this
#     is accepted). What is guaranteed: a regular .lock that exists at the
#     re-read is honoured (held means the socket stays); one created after the
#     re-read is not seen. The installer never creates the .lock (nor unlinks
#     it); the .lock entry is re-read right before the unlink and any change
#     since the first look is 2, so a .lock created before the re-read is seen
#     and wins. A 0.3.x server takes no lock at all, and a server that still
#     holds a .lock inode an older installer unlinked is invisible here too.
#   * Stricter than socket_lock_held, which answers "not held" for a lock another
#     user owns or one it cannot open: this flocks any regular file there, and
#     every error is 2.
# Stdlib only, run under $READER_PYTHON, so install needs no system python3 and
# uninstall no venv. The name rule and the lock conflict are pinned against the
# server's own _lock_path and acquire_socket_lock, and the lock being held at the
# unlink is checked with an audit hook, by tests/test_install_migration.py
# (test_socket_cleanup_lock_target_state_space).
unlink_socket_under_lock() {
    "$READER_PYTHON" -I -c '
import errno, fcntl, os, stat, sys
def entry(path):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode))
try:
    sock, uid = sys.argv[1], int(sys.argv[2])
    lock = sock + ".lock"
    if entry(sock) is None:
        sys.exit(0)
    before = entry(lock)
    fd = None
    if before is not None and stat.S_ISREG(before[2]):
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != before[:2]:
            sys.exit(2)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            sys.exit(1 if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK) else 2)
    try:
        st = os.lstat(sock)
    except FileNotFoundError:
        sys.exit(0)
    if not stat.S_ISSOCK(st.st_mode):
        sys.exit(3)
    if st.st_uid != uid:
        sys.exit(4)
    if entry(lock) != before:
        sys.exit(2)
    try:
        os.unlink(sock)
    except FileNotFoundError:
        pass
    except OSError:
        sys.exit(5)
    sys.exit(0)
except Exception:
    sys.exit(2)
' "$1" "$2" 2>/dev/null
}

# Print why socket path $1 is unsafe to act on (and return 1), or nothing (and
# return 0). The same lexical rules as validate_socket_path (absolute, no '..'
# component, under $HOME), plus no symlinked directory component from the
# socket's parent up to and including $HOME (the lexical check alone would
# follow one out of $HOME). Shared by remove_socket_owned and
# manual_socket_cleanup, so the command printed for a manual cleanup is offered
# only for a path the installer itself would clean.
socket_path_unsafe() {
    local sock="$1" dir home="${HOME%/}" component parent
    dir="$(dirname "$sock")"
    case "$sock" in
    /*) : ;;
    *)
        echo "is not absolute"
        return 1
        ;;
    esac
    case "/$sock/" in
    */../*)
        echo "contains '..' components"
        return 1
        ;;
    esac
    if [[ "$dir" != "$home" && "$dir" != "$home/"* ]]; then
        echo "is not under \$HOME ($home)"
        return 1
    fi
    component="$dir"
    while :; do
        if [[ -L "$component" ]]; then
            echo "has a symlinked directory component '$component'"
            return 1
        fi
        [[ "$component" == "$home" ]] && break
        parent="$(dirname "$component")"
        [[ "$parent" == "$component" ]] && break
        component="$parent"
    done
    return 0
}

# Remove the stale socket named by an INSTALLED plist (never its .lock, see
# below). That plist is
# untrusted input (anything that can write the user's LaunchAgents dir can name
# any path in it), so this is deliberately conservative and NEVER aborts the
# caller (it runs after bootout, under `set -e`): every refusal or failure is a
# warning, and the function always returns 0. It acts only when:
#   * the path passes socket_path_unsafe above: absolute, no '..' component,
#     under $HOME (the same lexical rules as validate_socket_path, but
#     warn-and-skip, not exit), and no symlinked directory component up to and
#     including $HOME;
#   * and then, in one step under the .lock (unlink_socket_under_lock above):
#     no process holds the .lock, and the path is a real socket (lstat, so a
#     plist naming a file, directory or symlink never deletes it) owned by the
#     current user. launchd no longer listing the old job is not enough: a
#     server started by hand can own the recorded path. A held lock (or a check
#     that cannot tell) leaves the socket in place. The shared lock is held
#     through the unlink, so a server starting meanwhile waits for it (its lock
#     retry) and binds after the unlink: it never loses a fresh socket.
# The .lock itself is never removed, like the server never removes it
# (acquire_socket_lock in stt_server/server.py): every starter must flock the
# same inode. The lock drops when the check exits, so unlinking the .lock
# after it would let a starter that just locked the old inode run beside a
# later one that creates a new inode at the same path. A leftover .lock is
# harmless; the next server reuses it.
remove_socket_owned() {
    local sock="$1" reason rc=0
    if ! reason="$(socket_path_unsafe "$sock")"; then
        echo "warning: installed socket path '$sock' $reason; leaving it" >&2
        return 0
    fi
    unlink_socket_under_lock "$sock" "$(id -u)" || rc=$?
    case "$rc" in
    0) : ;;
    1) echo "warning: $sock.lock is held by a running server; leaving $sock" >&2 ;;
    3) echo "warning: $sock is not a socket; leaving it" >&2 ;;
    4) echo "warning: $sock not owned by current user, leaving it" >&2 ;;
    5) echo "warning: could not remove $sock" >&2 ;;
    *) echo "warning: could not tell whether $sock.lock is held; leaving $sock" >&2 ;;
    esac
    return 0
}

# Print, on stderr, how to remove a stale socket $1 by hand once its job is gone.
# Used when the wait for unload timed out: the plist that named the socket is
# then gone (uninstall) or replaced (install), so no later run can find it. The
# command is offered only for a path socket_path_unsafe accepts (the plist is
# untrusted input; remove_socket_owned refuses the others too). It removes the
# path only if it is still a socket, not a symlink (test -S follows one) and
# owned by the user running it, and never the .lock; the lsof condition stands
# in for the lock check (a process holding the .lock has it open).
manual_socket_cleanup() {
    local q reason
    printf -v q '%q' "$1"
    if ! reason="$(socket_path_unsafe "$1")"; then
        echo "warning: the recorded socket path $q $reason; it looks unsafe, so no removal command is offered (check it by hand)" >&2
        return 0
    fi
    echo "warning: no later run will remove it (no installed plist names it any more); once the old server has exited ('lsof -- $q.lock' lists no process), remove it with: test -S $q && ! test -L $q && test -O $q && rm -f -- $q" >&2
}

# Validate the literal socket path against the SAME rules the server enforces in
# _enforce_socket_dir_secure() (stt_server/server.py) BEFORE creating or
# chmod-ing anything. The server refuses to start unless the socket directory is
# an absolute, ``..``-free path under $HOME whose chain contains no symlink
# component. Mirroring that here means a custom PIPECAT_STT_SOCKET the server
# would reject fails the install cleanly — with NO filesystem mutation — instead
# of tightening the operator's directory to 0700 and only then crash-looping the
# agent on startup. Keep this in lockstep with the Python rules.
validate_socket_path() {
    local sock="$1"
    local dir
    dir="$(dirname "$sock")"
    local home="${HOME%/}"

    # Absolute path required (lexical containment below assumes it).
    case "$dir" in
    /*) : ;;
    *)
        echo "error: socket directory '$dir' must be an absolute path under \$HOME ($home)" >&2
        exit 1
        ;;
    esac

    # No ``..`` components: without resolving symlinks, a ``..`` segment could
    # escape the trusted root while still passing the lexical check below.
    case "/$dir/" in
    */../*)
        echo "error: socket directory '$dir' must not contain '..' components" >&2
        exit 1
        ;;
    esac

    # Must live lexically under the trusted root ($HOME), or be $HOME itself.
    if [[ "$dir" != "$home" && "$dir" != "$home/"* ]]; then
        echo "error: socket directory '$dir' is not under the trusted root \$HOME ($home); point PIPECAT_STT_SOCKET (or KODA_STT_SOCKET) at a path under \$HOME" >&2
        exit 1
    fi

    # Reject any symlink component from the socket directory up to AND INCLUDING
    # $HOME. Only existing components can be symlinks; not-yet-created ones are
    # made 0700 below and cannot be. Walking up means a symlink anywhere in the
    # chain (not just the immediate parent) is caught before any mkdir/chmod.
    local component="$dir"
    while :; do
        if [[ -L "$component" ]]; then
            echo "error: socket directory component '$component' is a symlink; refusing to install (a symlinked ancestor can be repointed by another user to hijack the socket path)" >&2
            exit 1
        fi
        [[ "$component" == "$home" ]] && break
        local parent
        parent="$(dirname "$component")"
        [[ "$parent" == "$component" ]] && break
        component="$parent"
    done
}

render_plist() {
    # Same names as the renderer's _BACKEND_RE; fail here, before any mkdir.
    case "$BACKEND" in
    echo | mlx | parakeet | nemotron | kyutai) : ;;
    *)
        echo "error: unknown backend '$BACKEND' (valid: echo|mlx|parakeet|nemotron|kyutai)" >&2
        exit 2
        ;;
    esac
    # Never forward a raw token: the renderer refuses it (it would land in the
    # plist, which is world-readable). Fail clearly instead. Only lengths are
    # expanded, never the value: under `bash -x` a test like [[ -n "$TOKEN" ]]
    # prints the token to stderr. ${VAR+x} guards `set -u` without expanding it
    # (bash 3.2 has no [[ -v ]]).
    local raw_token_len=0
    if [[ -n "${PIPECAT_STT_AUTH_TOKEN+x}" ]]; then raw_token_len=$((raw_token_len + ${#PIPECAT_STT_AUTH_TOKEN})); fi
    if [[ -n "${KODA_STT_AUTH_TOKEN+x}" ]]; then raw_token_len=$((raw_token_len + ${#KODA_STT_AUTH_TOKEN})); fi
    if ((raw_token_len > 0)); then
        echo "error: PIPECAT_STT_AUTH_TOKEN / KODA_STT_AUTH_TOKEN would be written into the plist; write the token to a file and set PIPECAT_STT_AUTH_TOKEN_FILE instead" >&2
        exit 1
    fi
    if [[ -n "$SOCKET_PATH" ]]; then
        # Validate (and refuse) BEFORE mutating any filesystem state — the socket
        # dir chmod below must never tighten a directory the server will then reject.
        validate_socket_path "$SOCKET_PATH"
        # TCP-only settings have no meaning for a socket agent; say so rather
        # than silently dropping them (the plist bytes are unaffected).
        local -a ignored=()
        [[ -n "${PIPECAT_STT_AUTH_TOKEN_FILE:-}" ]] && ignored+=(PIPECAT_STT_AUTH_TOKEN_FILE)
        [[ -n "${PIPECAT_STT_HOST:-}" ]] && ignored+=(PIPECAT_STT_HOST)
        [[ -n "${PIPECAT_STT_PORT:-}" ]] && ignored+=(PIPECAT_STT_PORT)
        if ((${#ignored[@]} > 0)); then
            echo "warning: socket mode (PIPECAT_STT_SOCKET set): ignoring ${ignored[*]} (TCP-only settings; unset PIPECAT_STT_SOCKET for a TCP agent)" >&2
        fi
    else
        if [[ -z "$PORT" ]]; then
            echo "error: backend '$BACKEND' has no canonical port; set PIPECAT_STT_PORT" >&2
            exit 1
        fi
        if [[ -n "$AUTH_TOKEN_FILE" ]]; then
            validate_token_file "$AUTH_TOKEN_FILE"
            AUTH_TOKEN_FILE="$RESOLVED_TOKEN_FILE"
        fi
    fi
    mkdir -p "$LOG_DIR" "$(dirname "$PLIST_DST")"
    if [[ -n "$SOCKET_PATH" ]]; then
        # Create the socket's parent directory owner-only (0700) from birth so
        # there is no window under a permissive umask (commonly 0755) in which
        # another local user could pre-create a socket at the same path. The server
        # *refuses to start* (see _enforce_socket_dir_secure in
        # stt_server/server.py) if any ancestor through the trusted root is
        # group/other-writable, so 0700 here is load-bearing, not just hygiene.
        # The trailing `chmod 700` repairs a pre-existing 0755 dir in place.
        # shellcheck disable=SC2174
        mkdir -m 700 -p "$(dirname "$SOCKET_PATH")"
        chmod 700 "$(dirname "$SOCKET_PATH")"
    fi
    # Delegate to plistlib (via render_stt_plist.py) so XML escaping and
    # allowlist validation handle hostile values instead of sed string
    # substitution (which would allow <string> breakout + RCE).
    #
    # Contract: $LABEL is already alias-resolved here, and we inject it under
    # the *canonical* PIPECAT_STT_LABEL key. render_stt_plist.py re-resolves
    # PIPECAT_STT_LABEL-first, so the canonical name wins over any stray
    # KODA_STT_LABEL in the environment. Do not switch this to the alias key.
    #
    # Transport vars are exported conditionally: the renderer treats SOCKET_PATH
    # together with HOST/PORT as an error, and ambient HOST/PORT/SOCKET_PATH in
    # the caller's env must not leak through, so clear what does not apply.
    local -a transport_env
    if [[ -n "$SOCKET_PATH" ]]; then
        transport_env=(SOCKET_PATH="$SOCKET_PATH" HOST= PORT= AUTH_TOKEN_FILE=)
    else
        transport_env=(SOCKET_PATH= HOST="$HOST" PORT="$PORT" AUTH_TOKEN_FILE="$AUTH_TOKEN_FILE")
    fi
    env "${transport_env[@]}" \
        PYTHON="$PYTHON" REPO_ROOT="$REPO_ROOT" \
        BACKEND="$BACKEND" MODEL="$MODEL" HOME="$HOME" LOG_DIR="$LOG_DIR" \
        PLIST_DST="$PLIST_DST" PIPECAT_STT_LABEL="$LABEL" \
        "$PYTHON" "$RENDER_PY"
}

case "$cmd" in
install)
    # Never replace a plist we cannot read: it may require a token (dropping it
    # would leave an unauthenticated listener) and may name a socket we would
    # then never clean up. Uninstalling first is the explicit way past it.
    auth_state=0
    installed_auth_state || auth_state=$?
    # Capture the previously installed transport BEFORE the plist is rewritten.
    # Each read fails on its own for a plist it cannot read, so neither depends
    # on the other having run first.
    old_socket=""
    if [[ "$auth_state" -eq 2 ]] || ! old_socket="$(installed_field socket-path)"; then
        echo "error: cannot read the installed plist $PLIST_DST (malformed or unreadable, or the plist reader failed under $READER_PYTHON), so this install cannot tell whether $LABEL requires an auth token or owns a socket. Fix or remove that plist, or run 'uninstall' first" >&2
        exit 1
    fi
    # Never silently downgrade a secured agent: an installed plist that carried a
    # token (a pre-0.5.0 env token, or --auth-token-file) must not be replaced by
    # an unauthenticated TCP listener just because the caller did not re-export
    # the token file. Uninstalling first is the explicit opt-out.
    if [[ "$auth_state" -eq 0 ]]; then
        if [[ -z "$SOCKET_PATH" && -z "$AUTH_TOKEN_FILE" ]]; then
            echo "error: the installed $LABEL agent requires an auth token, but PIPECAT_STT_AUTH_TOKEN_FILE is not set; this install would replace it with an unauthenticated TCP listener. Set PIPECAT_STT_AUTH_TOKEN_FILE (see docs/operations.md), or run 'uninstall' first to install without a token" >&2
            exit 1
        elif [[ -n "$SOCKET_PATH" ]]; then
            echo "warning: the installed $LABEL agent required an auth token; a socket agent carries none (the socket's peer-uid check still applies)" >&2
        fi
    fi
    render_plist
    # Migrate v0.1.x installs: retire the legacy koda.stt-server agents that a
    # previous default install left behind, so launchd does not double-run the
    # old and new defaults. Guarded to the renamed-default install only — a
    # custom-label install manages just its selected agent and must not retire
    # unrelated legacy agents. Runs BEFORE the new agent's own bootout/bootstrap
    # so the new agent is never the one retired. Idempotent: with no legacy
    # agent/plist present it is a silent no-op (exit 0).
    #
    # The legacy socket (~/Library/Caches/koda-stt/) and logs
    # (~/Library/Logs/koda-stt/) are deliberately left in place — harmless
    # orphans, since the new agent uses the pipecat-stt paths. Consumers still
    # pinned to the old socket must set STT_WS_SOCKET (see README upgrade note).
    if [[ "$LABEL" == "pipecat.stt-server" ]]; then
        for legacy in koda.stt-server koda.stt-server.parakeet; do
            legacy_plist="$HOME/Library/LaunchAgents/$legacy.plist"
            if launchctl print "gui/$(id -u)/$legacy" >/dev/null 2>&1 || [[ -e "$legacy_plist" ]]; then
                echo "migrating: retiring legacy agent $legacy"
            fi
            launchctl bootout "gui/$(id -u)/$legacy" 2>/dev/null || true
            rm -f "$legacy_plist"
        done
    fi
    # Bootstrap (idempotent: unload first if already loaded).
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    # bootout returns before launchd finishes tearing the old job down (a loaded
    # ASR model takes seconds to exit). Bootstrapping into that window fails with
    # "Bootstrap failed: 5: Input/output error" and leaves the agent unloaded, so
    # wait until the label is gone, then retry the bootstrap once.
    old_gone=0
    if wait_unloaded; then old_gone=1; fi
    if ! launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"; then
        echo "warning: launchctl bootstrap failed for $LABEL; retrying once" >&2
        sleep 2
        if wait_unloaded; then old_gone=1; fi
        launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
    fi
    # Old socket cleanup runs only after the new job bootstrapped (a failed
    # bootstrap exits above under set -e, leaving the old socket in place) and
    # only once the old job was seen gone, so a socket the old server still owns
    # is never removed. Any change of socket path (socket->TCP or socket
    # A->socket B) retires the old socket; its .lock stays (see
    # remove_socket_owned).
    if [[ -n "$old_socket" && "$old_socket" != "$SOCKET_PATH" ]]; then
        if [[ -z "$SOCKET_PATH" ]]; then
            echo "note: endpoint changed from socket $old_socket to TCP $HOST:$PORT; removing the old socket"
        else
            echo "note: endpoint changed from socket $old_socket to socket $SOCKET_PATH; removing the old socket"
        fi
        if [[ "$old_gone" -eq 1 ]]; then
            remove_socket_owned "$old_socket"
        else
            echo "warning: old job may still hold $old_socket.lock; leaving $old_socket" >&2
            manual_socket_cleanup "$old_socket"
        fi
    fi
    launchctl enable "gui/$(id -u)/$LABEL"
    launchctl kickstart -k "gui/$(id -u)/$LABEL"
    echo "installed and started: $LABEL"
    if [[ -n "$SOCKET_PATH" ]]; then
        echo "  socket: $SOCKET_PATH"
    else
        echo "  endpoint: $HOST:$PORT"
    fi
    echo "  logs:   $LOG_DIR"
    ;;
uninstall)
    # Socket cleanup follows the transport recorded in the INSTALLED plist. An
    # unreadable plist names no socket we can trust, so uninstall still removes
    # the agent but leaves any socket in place.
    old_socket=""
    if ! old_socket="$(installed_field socket-path)"; then
        echo "warning: cannot read the installed plist $PLIST_DST; uninstalling $LABEL without socket cleanup (remove its socket by hand if it had one, once no server runs)" >&2
        old_socket=""
    fi
    # bootout fails when the job is not loaded, which is fine; whether it worked
    # is judged by launchd no longer listing the job, below.
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST_DST"
    # bootout is asynchronous, for a TCP agent too: until the job is gone its
    # server can still own its port (or its socket). So every uninstall waits, and
    # the socket goes only once launchd no longer lists the job (its .lock stays).
    # Still loaded at the cap: say so instead of claiming a clean uninstall (the
    # exit status stays 0, as before).
    # The plist is already gone at that point, so a re-run of uninstall finds no
    # socket to clean: print the exact cleanup instead of advising a re-run.
    if wait_unloaded; then
        [[ -n "$old_socket" ]] && remove_socket_owned "$old_socket"
        echo "uninstalled: $LABEL"
    else
        if [[ -n "$old_socket" ]]; then
            echo "warning: $LABEL may still hold $old_socket.lock; leaving $old_socket" >&2
            manual_socket_cleanup "$old_socket"
        fi
        echo "warning: $LABEL is still loaded, so its endpoint may still be in use; its plist is removed (it will not load again at login) but the uninstall is incomplete. Check 'launchctl print gui/$(id -u)/$LABEL' until it reports no such service" >&2
        echo "uninstall incomplete (still loaded): $LABEL"
    fi
    ;;
start)
    # Ensure running. ``launchctl kickstart`` without ``-k`` is a no-op when
    # the service is already running — which is what "start" should mean.
    # Use "restart" for a forced kick.
    if ! launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
        echo "$LABEL: not loaded. Run 'install' first." >&2
        exit 1
    fi
    launchctl kickstart "gui/$(id -u)/$LABEL"
    echo "started (or already running): $LABEL"
    ;;
stop)
    if ! launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
        echo "$LABEL: not loaded." >&2
        exit 0
    fi
    launchctl kill SIGTERM "gui/$(id -u)/$LABEL"
    echo "sent SIGTERM: $LABEL (KeepAlive will restart it — use 'uninstall' to disable)"
    ;;
restart)
    launchctl kickstart -k "gui/$(id -u)/$LABEL"
    echo "restarted: $LABEL"
    ;;
status)
    launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E "state|last exit|pid" || \
        echo "$LABEL: not loaded"
    ;;
logs)
    tail -F "$LOG_DIR/$LOG_BASENAME.log" "$LOG_DIR/$LOG_BASENAME.err"
    ;;
*)
    echo "usage: $0 [install|uninstall|start|stop|restart|status|logs|port [BACKEND]]" >&2
    exit 2
    ;;
esac
