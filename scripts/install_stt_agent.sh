#!/usr/bin/env bash
# Install the koda.stt-server LaunchAgent so the transcription server
# runs at login and is auto-restarted by launchd on crash.
#
# Usage:
#   scripts/install_stt_agent.sh [install|uninstall|start|stop|restart|status|logs]
#
# Environment overrides:
#   KODA_STT_SOCKET   path to the UDS socket
#                     (default: $HOME/Library/Caches/koda-stt/stt.sock)
#   KODA_STT_BACKEND  backend name (default: mlx)
#   KODA_STT_MODEL    MLX model (default: mlx-community/whisper-large-v3-turbo)
set -euo pipefail

LABEL="koda.stt-server"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RENDER_PY="$REPO_ROOT/scripts/render_stt_plist.py"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="${KODA_STT_LOG_DIR:-$HOME/Library/Logs/koda-stt}"
# Default socket lives under the user's Caches dir (not /tmp, which is
# world-writable and lets a local attacker pre-create the path to DoS
# the agent). Override via KODA_STT_SOCKET.
SOCKET_PATH="${KODA_STT_SOCKET:-$HOME/Library/Caches/koda-stt/stt.sock}"
BACKEND="${KODA_STT_BACKEND:-mlx}"
MODEL="${KODA_STT_MODEL:-mlx-community/whisper-large-v3-turbo}"

# Resolve the python interpreter from the project venv.
PYTHON="$REPO_ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    echo "error: $PYTHON not found — run 'uv sync' first" >&2
    exit 1
fi

cmd="${1:-install}"

render_plist() {
    mkdir -p "$LOG_DIR" "$(dirname "$PLIST_DST")" "$(dirname "$SOCKET_PATH")"
    # Lock the socket's parent directory so another local user can't
    # pre-create a socket at the same path under a permissive umask.
    chmod 700 "$(dirname "$SOCKET_PATH")"
    # Delegate to plistlib (via render_stt_plist.py) so XML escaping and
    # allowlist validation handle hostile values instead of sed string
    # substitution (which would allow <string> breakout + RCE).
    PYTHON="$PYTHON" REPO_ROOT="$REPO_ROOT" SOCKET_PATH="$SOCKET_PATH" \
        BACKEND="$BACKEND" MODEL="$MODEL" HOME="$HOME" LOG_DIR="$LOG_DIR" \
        PLIST_DST="$PLIST_DST" \
        "$PYTHON" "$RENDER_PY"
}

case "$cmd" in
install)
    render_plist
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
    tail -F "$LOG_DIR/koda-stt.log" "$LOG_DIR/koda-stt.err"
    ;;
*)
    echo "usage: $0 [install|uninstall|start|stop|restart|status|logs]" >&2
    exit 2
    ;;
esac
