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
#                       (TCP mode). Raw PIPECAT_STT_AUTH_TOKEN is refused.
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
#   # TCP install removes a previously installed socket endpoint.
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
case "$BACKEND" in
mlx | whisper) CANONICAL_PORT=9565 ;;
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

# Print the --socket-path argument of an installed plist (empty if the plist is
# absent, unreadable, or a TCP agent). Used to decide socket cleanup from the
# transport actually installed, not from the caller's env.
installed_socket_path() {
    [[ -f "$PLIST_DST" ]] || return 0
    local json
    json="$(plutil -extract ProgramArguments json -o - "$PLIST_DST" 2>/dev/null)" || return 0
    # plutil's JSON escapes '/' as '\/'; unescape before extracting the value.
    printf '%s' "$json" | sed -e 's|\\/|/|g' -n -e 's/.*"--socket-path","\([^"]*\)".*/\1/p'
}

# Block until launchd no longer lists $LABEL (bounded; a stuck job only warns, the
# bootstrap that follows then reports the real error). PIPECAT_STT_UNLOAD_WAIT is
# the cap in seconds (default 30).
wait_unloaded() {
    local waited=0 cap="${PIPECAT_STT_UNLOAD_WAIT:-30}"
    while launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; do
        if [[ "$waited" -ge "$cap" ]]; then
            echo "warning: $LABEL still loaded ${cap}s after bootout" >&2
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
}

# Remove a socket and its .lock named by an INSTALLED plist. That plist is
# untrusted input (anything that can write the user's LaunchAgents dir can name
# any path in it), so this is deliberately conservative and NEVER aborts the
# caller (it runs after bootout, under `set -e`): every refusal or failure is a
# warning, and the function always returns 0. It acts only when:
#   * the path is absolute, has no '..' component, and lives under $HOME (the
#     same lexical rules as validate_socket_path, but warn-and-skip, not exit);
#   * the socket is a real socket (-S) and not a symlink; the .lock is a regular
#     file and not a symlink (so a plist naming a file, directory or symlink
#     never deletes it);
#   * the entry is owned by the current user.
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
    elif [[ -z "$PORT" ]]; then
        echo "error: backend '$BACKEND' has no canonical port; set PIPECAT_STT_PORT" >&2
        exit 1
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
    # Capture the previously installed transport BEFORE the plist is rewritten.
    old_socket="$(installed_socket_path)"
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
    if [[ -n "$old_socket" && -z "$SOCKET_PATH" ]]; then
        echo "note: endpoint changed from socket $old_socket to TCP $HOST:$PORT; removing the old socket"
        remove_socket_owned "$old_socket"
    fi
    # bootout returns before launchd finishes tearing the old job down (a loaded
    # ASR model takes seconds to exit). Bootstrapping into that window fails with
    # "Bootstrap failed: 5: Input/output error" and leaves the agent unloaded, so
    # wait until the label is gone, then retry the bootstrap once.
    wait_unloaded
    if ! launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"; then
        echo "warning: launchctl bootstrap failed for $LABEL; retrying once" >&2
        sleep 2
        wait_unloaded
        launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
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
    # Socket cleanup follows the transport recorded in the INSTALLED plist.
    old_socket="$(installed_socket_path)"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST_DST"
    if [[ -n "$old_socket" ]]; then
        remove_socket_owned "$old_socket"
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
