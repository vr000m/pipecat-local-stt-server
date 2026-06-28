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
#   PIPECAT_STT_SOCKET  (KODA_STT_SOCKET)  path to the UDS socket
#                       (default: $HOME/Library/Caches/pipecat-stt/stt.sock)
#   PIPECAT_STT_BACKEND (KODA_STT_BACKEND) backend name: echo|mlx|parakeet|nemotron (default: mlx)
#   PIPECAT_STT_MODEL   (KODA_STT_MODEL)   model id (default: backend-aware — Whisper repo for
#                       mlx/echo, mlx-community/parakeet-tdt-0.6b-v3 for parakeet,
#                       mlx-community/nemotron-3.5-asr-streaming-0.6b for nemotron)
#
# Two-agent install recipe (run Whisper and Parakeet ASR side by side):
#
#   # 1. Whisper agent — default env, uses the default label + socket so the
#   #    bot's STT_WS_SOCKET default still resolves to it:
#   scripts/install_stt_agent.sh install
#
#   # 2. Parakeet agent — distinct label, socket and backend:
#   #    Warm the ~1.5 GB Hugging Face model cache FIRST, otherwise the
#   #    first launch downloads it under KeepAlive + ThrottleInterval=10
#   #    and launchd may throttle-loop the agent before the download finishes.
#   .venv/bin/python -c 'import parakeet_mlx; parakeet_mlx.from_pretrained("mlx-community/parakeet-tdt-0.6b-v3")'
#   PIPECAT_STT_LABEL=pipecat.stt-server.parakeet \
#     PIPECAT_STT_SOCKET="$HOME/Library/Caches/pipecat-stt/parakeet.sock" \
#     PIPECAT_STT_BACKEND=parakeet \
#     scripts/install_stt_agent.sh install
#
# Operational constraint: this script manages exactly ONE agent per
# invocation, identified by PIPECAT_STT_LABEL (+ its socket). There is no
# registry or "all" mode — to manage the Parakeet agent with any
# subcommand (uninstall/start/stop/restart/status/logs) you MUST re-export
# its PIPECAT_STT_LABEL (and PIPECAT_STT_SOCKET) env, e.g.:
#   PIPECAT_STT_LABEL=pipecat.stt-server.parakeet \
#     PIPECAT_STT_SOCKET="$HOME/Library/Caches/pipecat-stt/parakeet.sock" \
#     scripts/install_stt_agent.sh status
# A default-env invocation always targets the default pipecat.stt-server agent.
set -euo pipefail

# Canonical PIPECAT_STT_* names take precedence; the legacy KODA_STT_* names
# are still honoured as deprecated aliases.
LABEL="${PIPECAT_STT_LABEL:-${KODA_STT_LABEL:-pipecat.stt-server}}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RENDER_PY="$REPO_ROOT/scripts/render_stt_plist.py"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="${PIPECAT_STT_LOG_DIR:-${KODA_STT_LOG_DIR:-$HOME/Library/Logs/pipecat-stt}}"
# Default socket lives under the user's Caches dir (not /tmp, which is
# world-writable and lets a local attacker pre-create the path to DoS
# the agent). Override via PIPECAT_STT_SOCKET (or legacy KODA_STT_SOCKET).
SOCKET_PATH="${PIPECAT_STT_SOCKET:-${KODA_STT_SOCKET:-$HOME/Library/Caches/pipecat-stt/stt.sock}}"
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
else
    DEFAULT_MODEL="mlx-community/whisper-large-v3-turbo"
fi
MODEL="${PIPECAT_STT_MODEL:-${KODA_STT_MODEL:-$DEFAULT_MODEL}}"

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

# Resolve the python interpreter from the project venv.
PYTHON="$REPO_ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    echo "error: $PYTHON not found — run 'uv sync' first" >&2
    exit 1
fi

cmd="${1:-install}"

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
    # Validate (and refuse) BEFORE mutating any filesystem state — the socket
    # dir chmod below must never tighten a directory the server will then reject.
    validate_socket_path "$SOCKET_PATH"
    mkdir -p "$LOG_DIR" "$(dirname "$PLIST_DST")"
    # Create the socket's parent directory owner-only (0700) from birth so
    # there is no window under a permissive umask (commonly 0755) in which
    # another local user could pre-create a socket at the same path. The server
    # now *refuses to start* (see _enforce_socket_dir_secure in
    # stt_server/server.py) if any ancestor through the trusted root is
    # group/other-writable, so 0700 here is load-bearing, not just hygiene.
    #
    # Upgrade note: installs predating this change created the dir at the
    # install shell umask (commonly 0755). The trailing `chmod 700` repairs such
    # a pre-existing dir in place so upgrades do not trip the new startup check;
    # `mkdir -m 700` covers the fresh-install path with no race window.
    mkdir -m 700 -p "$(dirname "$SOCKET_PATH")"
    chmod 700 "$(dirname "$SOCKET_PATH")"
    # Delegate to plistlib (via render_stt_plist.py) so XML escaping and
    # allowlist validation handle hostile values instead of sed string
    # substitution (which would allow <string> breakout + RCE).
    #
    # Contract: $LABEL is already alias-resolved here, and we inject it under
    # the *canonical* PIPECAT_STT_LABEL key. render_stt_plist.py re-resolves
    # PIPECAT_STT_LABEL-first, so the canonical name wins over any stray
    # KODA_STT_LABEL in the environment. The double-resolution is safe only as
    # long as the resolved value keeps being passed under the canonical key —
    # do not switch this to the deprecated alias key.
    #
    # The env-prefix assignments below intentionally re-export the same-named
    # shell vars into the renderer subprocess; the command word "$PYTHON" uses
    # the parent shell's value (identical), so SC2097/SC2098 are false positives
    # for this env-passing idiom.
    # shellcheck disable=SC2097,SC2098
    PYTHON="$PYTHON" REPO_ROOT="$REPO_ROOT" SOCKET_PATH="$SOCKET_PATH" \
        BACKEND="$BACKEND" MODEL="$MODEL" HOME="$HOME" LOG_DIR="$LOG_DIR" \
        PLIST_DST="$PLIST_DST" PIPECAT_STT_LABEL="$LABEL" \
        "$PYTHON" "$RENDER_PY"
}

case "$cmd" in
install)
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
    launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
    launchctl enable "gui/$(id -u)/$LABEL"
    launchctl kickstart -k "gui/$(id -u)/$LABEL"
    echo "installed and started: $LABEL"
    echo "  socket: $SOCKET_PATH"
    echo "  logs:   $LOG_DIR"
    ;;
uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST_DST"
    # Only remove the socket if it's owned by the current user — avoids
    # nuking an unrelated file someone pre-created at a world-writable path.
    if [[ -e "$SOCKET_PATH" ]]; then
        owner=$(stat -f "%u" "$SOCKET_PATH" 2>/dev/null || echo "")
        if [[ "$owner" == "$(id -u)" ]]; then
            rm -f "$SOCKET_PATH"
        else
            echo "warning: socket at $SOCKET_PATH not owned by current user, leaving it" >&2
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
