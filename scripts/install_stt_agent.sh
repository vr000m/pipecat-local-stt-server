#!/usr/bin/env bash
# Install the koda.stt-server LaunchAgent so the transcription server
# runs at login and is auto-restarted by launchd on crash.
#
# Usage:
#   scripts/install_stt_agent.sh [install|uninstall|restart|status|logs]
#
# Environment overrides:
#   KODA_STT_SOCKET   path to the UDS socket (default: /tmp/koda-stt.sock)
#   KODA_STT_BACKEND  backend name (default: mlx)
#   KODA_STT_MODEL    MLX model (default: mlx-community/whisper-large-v3-turbo)
set -euo pipefail

LABEL="koda.stt-server"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_SRC="$REPO_ROOT/scripts/koda-stt.plist.template"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="${KODA_STT_LOG_DIR:-$HOME/Library/Logs/koda-stt}"
SOCKET_PATH="${KODA_STT_SOCKET:-/tmp/koda-stt.sock}"
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
    mkdir -p "$LOG_DIR" "$(dirname "$PLIST_DST")"
    sed \
        -e "s|@PYTHON@|$PYTHON|g" \
        -e "s|@CWD@|$REPO_ROOT|g" \
        -e "s|@SOCKET_PATH@|$SOCKET_PATH|g" \
        -e "s|@BACKEND@|$BACKEND|g" \
        -e "s|@MODEL@|$MODEL|g" \
        -e "s|@HOME@|$HOME|g" \
        -e "s|@LOG_DIR@|$LOG_DIR|g" \
        "$PLIST_SRC" > "$PLIST_DST"
    echo "wrote $PLIST_DST"
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
    rm -f "$SOCKET_PATH"
    echo "uninstalled: $LABEL"
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
    echo "usage: $0 [install|uninstall|restart|status|logs]" >&2
    exit 2
    ;;
esac
