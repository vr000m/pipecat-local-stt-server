#!/usr/bin/env bash
# Install the pipecat.stt-server LaunchAgent so the transcription server
# runs at login and is auto-restarted by launchd on crash.
#
# Usage:
#   scripts/install_stt_agent.sh [install|uninstall|start|stop|restart|status|logs]
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
#                       file must be a regular file (not a symlink) owned by you,
#                       with no group/other permission bits and no ACL allow
#                       entry, in a directory chain (symlinks resolved) that only
#                       you or root own and nobody else can write to, by mode or
#                       by an ACL allow entry with a write right (group write only
#                       on a root-owned directory of group wheel or admin, or on a
#                       sticky one, 't' or 'T'). The plist records the
#                       resolved path, the one that was checked. Its content must be one token of
#                       visible ASCII (UTF-8, surrounding whitespace stripped; an
#                       empty file, internal whitespace or non-ASCII is refused,
#                       by the server's own stt_server.auth.read_token_file).
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
# Keys are exactly the renderer's backend names (_BACKEND_RE); the justfile maps
# its user-facing 'whisper' to 'mlx' before calling this script.
case "$BACKEND" in
mlx) CANONICAL_PORT=9565 ;;
kyutai) CANONICAL_PORT=9665 ;;
parakeet) CANONICAL_PORT=9765 ;;
nemotron) CANONICAL_PORT=9865 ;;
*) CANONICAL_PORT="" ;;
esac
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

# Print the value of FLAG (e.g. --socket-path) in the installed plist's
# ProgramArguments (empty if the plist is absent or has no such flag).
# scripts/read_stt_plist.py is the one reader of the installed plist (the justfile
# calls it too), run under the system python3 so an uninstall works with no venv.
# Used to decide socket cleanup from the transport actually installed, not from
# the caller's env. Callers check installed_auth_state first: a malformed plist
# also reads as empty here, which must never be taken as "no socket".
installed_arg() {
    local flag="$1" host port sock authfile
    [[ -e "$PLIST_DST" || -L "$PLIST_DST" ]] || return 0
    {
        IFS= read -r host
        IFS= read -r port
        IFS= read -r sock
        IFS= read -r authfile
    } < <(python3 -I "$READ_PLIST_PY" "$PLIST_DST")
    case "$flag" in
        --host) printf '%s' "$host" ;;
        --port) printf '%s' "$port" ;;
        --socket-path) printf '%s' "$sock" ;;
        --auth-token-file) printf '%s' "$authfile" ;;
    esac
}

# Classify the INSTALLED plist by its return status. The token value is never
# printed.
#   0  it requires a token: a non-empty env token in its EnvironmentVariables
#      (pre-0.5.0 plists) or an --auth-token-file argument;
#   1  there is no plist, or it was read and carries no token (the reader's
#      exit 3: never 1, which a Python traceback or a broken shim exits with);
#   2  a plist is there but could not be read (malformed, unreadable, wrong
#      shape), or the reader itself failed (any status but 0 or 3: python3
#      missing, a crash). This is never "no auth": install aborts, uninstall
#      skips socket cleanup.
installed_auth_state() {
    local rc=0
    [[ -e "$PLIST_DST" || -L "$PLIST_DST" ]] || return 1
    python3 -I "$READ_PLIST_PY" --has-auth "$PLIST_DST" || rc=$?
    case "$rc" in
    0) return 0 ;;
    3) return 1 ;;
    *) return 2 ;;
    esac
}

# ACL rights that let a principal change a directory's entries or its metadata
# (and so swap or re-permission the token file under it).
ACL_WRITE_RIGHTS="write append add_file add_subdirectory delete delete_child writeattr writeextattr writesecurity chown"

# Exit 0 when path $1 has an ACL allow entry granting any right in the
# space-separated list $2 (or any right at all when $2 is "any"), 1 when it has
# none, 2 when its ACL cannot be read. macOS `ls -lde` prints each ACL entry on
# its own line after the listing, e.g. " 0: group:staff allow read" or
# " 1: user:bob inherited allow add_file,delete_child"; a path with no ACL prints
# no such line, so nothing changes there. Deny entries only narrow access.
acl_allows() {
    local path="$1" want="$2" out line r
    local -a rights
    out="$(ls -lde -- "$path" 2>/dev/null)" || return 2
    while IFS= read -r line; do
        [[ "$line" =~ ^[[:space:]]*[0-9]+:[[:space:]] ]] || continue
        [[ "$line" =~ [[:space:]]allow[[:space:]]+([A-Za-z_,]+)[[:space:]]*$ ]] || continue
        [[ "$want" == any ]] && return 0
        IFS=, read -ra rights <<<"${BASH_REMATCH[1]}"
        for r in "${rights[@]}"; do
            [[ " $want " == *" $r "* ]] && return 0
        done
    done <<<"$out"
    return 1
}

# Refuse a token file the server would mis-handle, and set RESOLVED_TOKEN_FILE
# to the path the plist must carry. File-system policy lives here: a
# group/other-readable file leaks the bearer; a file owned by another user (or a
# symlink) lets someone else set it; so does a parent directory someone else can
# write to; a missing path crash-loops the agent under KeepAlive. Content policy
# (empty = auth disabled, internal whitespace or non-ASCII = never matches a
# header, non-UTF-8) is the server's own stt_server.auth.read_token_file, called
# below so the two cannot drift.
#
# Validated == opened: the parent chain is resolved (symlinks followed) and the
# checks run on that symlink-free path, which is what the plist records. A
# symlinked directory in the path as given could be repointed after this check;
# the resolved path has no such component left to repoint.
validate_token_file() {
    local f="$1" owner group mode dir me sticky resolved base
    me="$(id -u)"
    # The renderer requires an absolute path; say so here, before any mkdir, and
    # under this variable's own name rather than the renderer's AUTH_TOKEN_FILE.
    if [[ "$f" != /* ]]; then
        echo "error: PIPECAT_STT_AUTH_TOKEN_FILE '$f' must be an absolute path" >&2
        exit 1
    fi
    if [[ -L "$f" ]]; then
        echo "error: PIPECAT_STT_AUTH_TOKEN_FILE '$f' is a symlink; point it at the real file" >&2
        exit 1
    fi
    if [[ ! -f "$f" ]]; then
        echo "error: PIPECAT_STT_AUTH_TOKEN_FILE '$f' is not an existing regular file" >&2
        exit 1
    fi
    dir="$(cd -P "$(dirname "$f")" 2>/dev/null && pwd -P)" || dir=""
    if [[ -z "$dir" ]]; then
        echo "error: PIPECAT_STT_AUTH_TOKEN_FILE '$f' has an unreadable parent directory" >&2
        exit 1
    fi
    base="$(basename "$f")"
    if [[ "$dir" == "/" ]]; then resolved="/$base"; else resolved="$dir/$base"; fi
    # Same tests again on the resolved path: it names the same file, but the
    # plist (and the server) will open this one.
    if [[ -L "$resolved" || ! -f "$resolved" ]]; then
        echo "error: PIPECAT_STT_AUTH_TOKEN_FILE '$f' does not resolve to a regular file ($resolved)" >&2
        exit 1
    fi
    owner=$(stat -f "%u" "$resolved" 2>/dev/null || echo "")
    if [[ "$owner" != "$me" ]]; then
        echo "error: PIPECAT_STT_AUTH_TOKEN_FILE '$f' is not owned by the current user" >&2
        exit 1
    fi
    mode=$(stat -f "%Lp" "$resolved" 2>/dev/null || echo "777")
    if (((8#$mode & 8#077) != 0)); then
        echo "error: PIPECAT_STT_AUTH_TOKEN_FILE '$f' has mode $mode; it must not be group/other accessible (chmod 600 it)" >&2
        exit 1
    fi
    # The mode bits are not the whole story on macOS: an ACL can grant another
    # user read (the token leaks) or write. Any allow entry on the file is refused;
    # a deny entry only narrows access and is fine.
    local acl_rc=0
    acl_allows "$resolved" any || acl_rc=$?
    if [[ "$acl_rc" -ne 1 ]]; then
        echo "error: PIPECAT_STT_AUTH_TOKEN_FILE '$f' has an ACL allow entry, or its ACL cannot be read (see 'ls -le'; 'chmod -N' removes the ACL)" >&2
        exit 1
    fi
    # Walk the resolved parents: each must belong to this user or root and not be
    # writable by anyone else, else that party could swap the file. A sticky
    # directory (e.g. /tmp) only lets owners rename their own files. Group write
    # is allowed only on a root-owned directory whose group is wheel (0) or admin
    # (80): those members can already act as root. Any other writable group (say
    # staff, 20) could swap the file. The sticky bit is read from the numeric mode
    # (%p): the %Lp mode drops it, and the symbolic one shows 't' or, without
    # other-execute (mode 1770), 'T'. An ACL allow entry carrying a write-class
    # right is refused whatever the mode, sticky or not.
    while :; do
        owner=$(stat -f "%u" "$dir" 2>/dev/null || echo "")
        group=$(stat -f "%g" "$dir" 2>/dev/null || echo "")
        mode=$(stat -f "%Lp" "$dir" 2>/dev/null || echo "777")
        sticky=$(stat -f "%p" "$dir" 2>/dev/null || echo "0")
        if [[ ! "$sticky" =~ ^[0-7]+$ ]] || (((8#$sticky & 8#1000) == 0)); then sticky=0; else sticky=1; fi
        if [[ "$owner" != "$me" && "$owner" != 0 ]]; then
            echo "error: PIPECAT_STT_AUTH_TOKEN_FILE '$f': parent directory '$dir' is owned by another user" >&2
            exit 1
        fi
        if [[ "$sticky" != 1 ]] && { (((8#$mode & 8#002) != 0)) || { (((8#$mode & 8#020) != 0)) && ! [[ "$owner" == 0 && ("$group" == 0 || "$group" == 80) ]]; }; }; then
            echo "error: PIPECAT_STT_AUTH_TOKEN_FILE '$f': parent directory '$dir' is writable by others (mode $mode, gid $group)" >&2
            exit 1
        fi
        acl_rc=0
        acl_allows "$dir" "$ACL_WRITE_RIGHTS" || acl_rc=$?
        if [[ "$acl_rc" -ne 1 ]]; then
            echo "error: PIPECAT_STT_AUTH_TOKEN_FILE '$f': parent directory '$dir' has an ACL entry that lets someone else write to it, or its ACL cannot be read (see 'ls -lde'; 'chmod -N' removes the ACL)" >&2
            exit 1
        fi
        [[ "$dir" == "/" ]] && break
        dir="$(dirname "$dir")"
    done
    if [[ ! -r "$resolved" ]]; then
        echo "error: PIPECAT_STT_AUTH_TOKEN_FILE '$f' is unreadable" >&2
        exit 1
    fi
    # The renderer enforces its plist allowlist after the directories exist; ask
    # it now (its own --check-abspath mode, one regex) so a refused path fails
    # before any mkdir.
    if ! "$PYTHON" -I "$RENDER_PY" --check-abspath PIPECAT_STT_AUTH_TOKEN_FILE "$resolved"; then
        exit 1
    fi
    if ! "$PYTHON" -I -c '
import sys
try:
    from stt_server.auth import read_token_file
except ImportError:
    print(
        "error: cannot import stt_server - run with the project venv interpreter "
        "(PIPECAT_STT_PYTHON, default .venv/bin/python after uv sync)",
        file=sys.stderr,
    )
    sys.exit(1)
try:
    read_token_file(sys.argv[1], strict=True)
except ValueError as exc:
    print(f"error: PIPECAT_STT_AUTH_TOKEN_FILE: {exc}", file=sys.stderr)
    sys.exit(1)
' "$resolved"; then
        exit 1
    fi
    if [[ "$resolved" != "$f" ]]; then
        echo "note: PIPECAT_STT_AUTH_TOKEN_FILE resolves to $resolved; the plist records that path"
    fi
    RESOLVED_TOKEN_FILE="$resolved"
}

# Block until launchd no longer lists $LABEL. Bounded by PIPECAT_STT_UNLOAD_WAIT
# seconds (default 30): returns 0 once the job is gone, 1 (with a warning) if it
# is still loaded at the cap. Callers that unlink a socket's .lock must only do
# so after a 0 return: the old server holds that lock until it exits, and
# unlinking a held lock lets a second server lock a new inode on the same socket.
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

# Exit 0 when no process holds the flock on lock file $1 (or there is no plain
# file there, which no server can hold: it opens the lock O_NOFOLLOW and refuses
# anything but a regular file), 1 when one holds it, 2 when the probe cannot
# tell. Mirrors stt_server.server.socket_lock_held (a shared, non-blocking flock
# for an instant, so it conflicts with a server's exclusive lock but never makes
# a real starter fail, as that retries 250 ms), in stdlib python3 so an
# uninstall needs no venv.
lock_free() {
    python3 -I -c '
import errno, fcntl, os, stat, sys
try:
    try:
        st = os.lstat(sys.argv[1])
    except FileNotFoundError:
        sys.exit(0)
    if not stat.S_ISREG(st.st_mode):
        sys.exit(0)
    fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError as exc:
        sys.exit(1 if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK) else 2)
    sys.exit(0)
except Exception:
    sys.exit(2)
' "$1" 2>/dev/null
}

# Remove a socket and its .lock named by an INSTALLED plist. That plist is
# untrusted input (anything that can write the user's LaunchAgents dir can name
# any path in it), so this is deliberately conservative and NEVER aborts the
# caller (it runs after bootout, under `set -e`): every refusal or failure is a
# warning, and the function always returns 0. It acts only when:
#   * the path is absolute, has no '..' component, and lives under $HOME (the
#     same lexical rules as validate_socket_path, but warn-and-skip, not exit);
#   * no directory component from the socket's parent up to and including $HOME
#     is a symlink (the lexical check alone would follow one out of $HOME);
#   * the socket is a real socket (-S) and not a symlink; the .lock is a regular
#     file and not a symlink (so a plist naming a file, directory or symlink
#     never deletes it);
#   * the entry is owned by the current user;
#   * no process holds the .lock (lock_free above). launchd no longer listing the
#     old job is not enough: a server started by hand can own the recorded path,
#     and unlinking a held lock lets a second server lock a new inode on the same
#     socket. A held lock (or a probe that cannot tell) leaves BOTH in place. A
#     server that starts in the instant between the probe and the unlink is not
#     covered.
remove_socket_owned() {
    local sock="$1" dir home="${HOME%/}" f owner kind
    dir="$(dirname "$sock")"
    case "$sock" in
    /*) : ;;
    *)
        echo "warning: installed socket path '$sock' is not absolute; leaving it" >&2
        return 0
        ;;
    esac
    case "/$sock/" in
    */../*)
        echo "warning: installed socket path '$sock' contains '..' components; leaving it" >&2
        return 0
        ;;
    esac
    if [[ "$dir" != "$home" && "$dir" != "$home/"* ]]; then
        echo "warning: installed socket path '$sock' is not under \$HOME ($home); leaving it" >&2
        return 0
    fi
    local component="$dir" parent
    while :; do
        if [[ -L "$component" ]]; then
            echo "warning: installed socket path '$sock' has a symlinked directory component '$component'; leaving it" >&2
            return 0
        fi
        [[ "$component" == "$home" ]] && break
        parent="$(dirname "$component")"
        [[ "$parent" == "$component" ]] && break
        component="$parent"
    done
    local lock_rc=0
    lock_free "$sock.lock" || lock_rc=$?
    if [[ "$lock_rc" -eq 1 ]]; then
        echo "warning: $sock.lock is held by a running server; leaving $sock and its .lock" >&2
        return 0
    elif [[ "$lock_rc" -ne 0 ]]; then
        echo "warning: could not tell whether $sock.lock is held; leaving $sock and its .lock" >&2
        return 0
    fi
    for kind in sock lock; do
        if [[ "$kind" == sock ]]; then
            f="$sock"
            [[ -S "$f" && ! -L "$f" ]] || {
                [[ -e "$f" || -L "$f" ]] && echo "warning: $f is not a socket; leaving it" >&2
                continue
            }
        else
            f="$sock.lock"
            [[ -f "$f" && ! -L "$f" ]] || {
                [[ -e "$f" || -L "$f" ]] && echo "warning: $f is not a regular file; leaving it" >&2
                continue
            }
        fi
        owner=$(stat -f "%u" "$f" 2>/dev/null || echo "")
        if [[ "$owner" == "$(id -u)" ]]; then
            rm -f "$f" 2>/dev/null || echo "warning: could not remove $f" >&2
        else
            echo "warning: $f not owned by current user, leaving it" >&2
        fi
    done
    return 0
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
    # plist, which is world-readable). Fail clearly instead.
    if [[ -n "${PIPECAT_STT_AUTH_TOKEN:-}${KODA_STT_AUTH_TOKEN:-}" ]]; then
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
    if [[ "$auth_state" -eq 2 ]]; then
        echo "error: cannot read the installed plist $PLIST_DST (malformed or unreadable, or python3 is missing), so this install cannot tell whether $LABEL requires an auth token or owns a socket. Fix or remove that plist, or run 'uninstall' first" >&2
        exit 1
    fi
    # Capture the previously installed transport BEFORE the plist is rewritten.
    old_socket="$(installed_arg --socket-path)"
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
    # bootstrap exits above under set -e, leaving the old socket and .lock in
    # place) and only once the old job was seen gone, so its .lock is never
    # unlinked while the old server may still hold it. Any change of socket path
    # (socket->TCP or socket A->socket B) retires the old one.
    if [[ -n "$old_socket" && "$old_socket" != "$SOCKET_PATH" ]]; then
        if [[ -z "$SOCKET_PATH" ]]; then
            echo "note: endpoint changed from socket $old_socket to TCP $HOST:$PORT; removing the old socket"
        else
            echo "note: endpoint changed from socket $old_socket to socket $SOCKET_PATH; removing the old socket"
        fi
        if [[ "$old_gone" -eq 1 ]]; then
            remove_socket_owned "$old_socket"
        else
            echo "warning: old job may still hold $old_socket.lock; leaving $old_socket and its .lock" >&2
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
    auth_state=0
    installed_auth_state || auth_state=$?
    if [[ "$auth_state" -eq 2 ]]; then
        echo "warning: cannot read the installed plist $PLIST_DST; uninstalling $LABEL without socket cleanup (remove its socket and .lock by hand if it had one)" >&2
        old_socket=""
    else
        old_socket="$(installed_arg --socket-path)"
    fi
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST_DST"
    if [[ -n "$old_socket" ]]; then
        # bootout is asynchronous: the server holds <socket>.lock until it exits,
        # so remove the socket and lock only once launchd no longer lists the job.
        if wait_unloaded; then
            remove_socket_owned "$old_socket"
        else
            echo "warning: $LABEL may still hold $old_socket.lock; leaving $old_socket and its .lock" >&2
        fi
    fi
    echo "uninstalled: $LABEL"
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
    echo "usage: $0 [install|uninstall|start|stop|restart|status|logs]" >&2
    exit 2
    ;;
esac
