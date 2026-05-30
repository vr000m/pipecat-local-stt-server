#!/usr/bin/env bash
# Install the koda.stt-server LaunchAgent so the transcription server
# runs at login and is auto-restarted by launchd on crash.
#
# Usage:
#   scripts/install_stt_agent.sh [install|uninstall|start|stop|restart|status|logs]
#
# Environment overrides. The canonical PIPECAT_STT_* names take precedence;
# the legacy KODA_STT_* names (shown in parentheses) are still honoured as
# deprecated aliases:
#   PIPECAT_STT_LABEL   (KODA_STT_LABEL)   launchd label / plist filename
#                       (default: koda.stt-server)
#   PIPECAT_STT_SOCKET  (KODA_STT_SOCKET)  path to the UDS socket
#                       (default: $HOME/Library/Caches/koda-stt/stt.sock)
#   PIPECAT_STT_BACKEND (KODA_STT_BACKEND) backend name: echo|mlx|parakeet (default: mlx)
#   PIPECAT_STT_MODEL   (KODA_STT_MODEL)   model id (default: backend-aware — Whisper repo for
#                       mlx/echo, mlx-community/parakeet-tdt-0.6b-v3 for parakeet)
#
# Two-agent install recipe (run Whisper and Parakeet ASR side by side):
#
#   # 1. Whisper agent — default env, keeps the legacy label + socket so the
#   #    bot's STT_WS_SOCKET default still resolves to it:
#   scripts/install_stt_agent.sh install
#
#   # 2. Parakeet agent — distinct label, socket and backend:
#   #    Warm the ~1.5 GB Hugging Face model cache FIRST, otherwise the
#   #    first launch downloads it under KeepAlive + ThrottleInterval=10
#   #    and launchd may throttle-loop the agent before the download finishes.
#   .venv/bin/python -c 'import parakeet_mlx; parakeet_mlx.from_pretrained("mlx-community/parakeet-tdt-0.6b-v3")'
#   PIPECAT_STT_LABEL=koda.stt-server.parakeet \
#     PIPECAT_STT_SOCKET="$HOME/Library/Caches/koda-stt/parakeet.sock" \
#     PIPECAT_STT_BACKEND=parakeet \
#     scripts/install_stt_agent.sh install
#
# Operational constraint: this script manages exactly ONE agent per
# invocation, identified by PIPECAT_STT_LABEL (+ its socket). There is no
# registry or "all" mode — to manage the Parakeet agent with any
# subcommand (uninstall/start/stop/restart/status/logs) you MUST re-export
# its PIPECAT_STT_LABEL (and PIPECAT_STT_SOCKET) env, e.g.:
#   PIPECAT_STT_LABEL=koda.stt-server.parakeet \
#     PIPECAT_STT_SOCKET="$HOME/Library/Caches/koda-stt/parakeet.sock" \
#     scripts/install_stt_agent.sh status
# A default-env invocation always targets the legacy koda.stt-server agent.
set -euo pipefail

# Canonical PIPECAT_STT_* names take precedence; the legacy KODA_STT_* names
# are still honoured as deprecated aliases.
LABEL="${PIPECAT_STT_LABEL:-${KODA_STT_LABEL:-koda.stt-server}}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RENDER_PY="$REPO_ROOT/scripts/render_stt_plist.py"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="${PIPECAT_STT_LOG_DIR:-${KODA_STT_LOG_DIR:-$HOME/Library/Logs/koda-stt}}"
# Default socket lives under the user's Caches dir (not /tmp, which is
# world-writable and lets a local attacker pre-create the path to DoS
# the agent). Override via PIPECAT_STT_SOCKET (or legacy KODA_STT_SOCKET).
SOCKET_PATH="${PIPECAT_STT_SOCKET:-${KODA_STT_SOCKET:-$HOME/Library/Caches/koda-stt/stt.sock}}"
BACKEND="${PIPECAT_STT_BACKEND:-${KODA_STT_BACKEND:-mlx}}"
# Backend-aware MODEL default. render_stt_plist.py validates MODEL and
# exits when unset, so supply a sensible default per backend here: the
# Whisper repo for mlx/echo, the Parakeet TDT model for parakeet. Must
# agree with DEFAULT_PARAKEET_MODEL in stt_server/backends/parakeet.py.
if [[ "$BACKEND" == "parakeet" ]]; then
    DEFAULT_MODEL="mlx-community/parakeet-tdt-0.6b-v3"
else
    DEFAULT_MODEL="mlx-community/whisper-large-v3-turbo"
fi
MODEL="${PIPECAT_STT_MODEL:-${KODA_STT_MODEL:-$DEFAULT_MODEL}}"

# Derive a per-agent log basename matching render_stt_plist.py's
# _log_basename(): the legacy label keeps the historical koda-stt names,
# any other label replaces '.' with '-' so two agents never share a log.
if [[ "$LABEL" == "koda.stt-server" ]]; then
    LOG_BASENAME="koda-stt"
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

render_plist() {
    mkdir -p "$LOG_DIR" "$(dirname "$PLIST_DST")" "$(dirname "$SOCKET_PATH")"
    # Lock the socket's parent directory so another local user can't
    # pre-create a socket at the same path under a permissive umask.
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
    PYTHON="$PYTHON" REPO_ROOT="$REPO_ROOT" SOCKET_PATH="$SOCKET_PATH" \
        BACKEND="$BACKEND" MODEL="$MODEL" HOME="$HOME" LOG_DIR="$LOG_DIR" \
        PLIST_DST="$PLIST_DST" PIPECAT_STT_LABEL="$LABEL" \
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
    tail -F "$LOG_DIR/$LOG_BASENAME.log" "$LOG_DIR/$LOG_BASENAME.err"
    ;;
*)
    echo "usage: $0 [install|uninstall|start|stop|restart|status|logs]" >&2
    exit 2
    ;;
esac
