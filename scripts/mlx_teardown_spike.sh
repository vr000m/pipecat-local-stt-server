#!/usr/bin/env bash
# Tier B of the MLX multi-model teardown spike.
#
# Runs the operator-side portion of the spike that Tier A (pytest)
# cannot cover: real MLX model loading, real LaunchAgent SIGTERM/respawn
# cycles, RSS growth, and launchd-stderr Metal-assertion scans.
#
# Prereqs:
#   - Apple Silicon
#   - ``uv sync --extra stt-server-mlx`` has run
#   - ``scripts/install_stt_agent.sh install`` has been run at least once
#
# Usage:
#   scripts/mlx_teardown_spike.sh [--cycles N] [--audio-ms MS] [--clients N]
#
# Exit 0 iff every cycle meets every pass criterion documented in the
# dev plan (docs/dev_plans/20260420-design-whisper-websocket-server.md,
# "Preflight Follow-Ups — Tier 2").

set -euo pipefail

CYCLES=3
AUDIO_MS=500
CLIENTS=2
while [[ $# -gt 0 ]]; do
    case "$1" in
        --cycles)   CYCLES="$2";   shift 2 ;;
        --audio-ms) AUDIO_MS="$2"; shift 2 ;;
        --clients)  CLIENTS="$2";  shift 2 ;;
        *) echo "usage: $0 [--cycles N] [--audio-ms MS] [--clients N]" >&2; exit 2 ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# `python -m stt_server` resolves the module via sys.path[0] = CWD. If
# the harness is invoked from outside the repo root, `_wait_for_respond`
# shells out with just `"$PYTHON" -m stt_server ...` and gets
# "No module named stt_server" — every cycle would log as a timeout
# even when the agent is healthy. Anchor here so the harness is
# location-independent.
cd "$REPO_ROOT"
LABEL="koda.stt-server"
SOCKET_PATH="${KODA_STT_SOCKET:-$HOME/Library/Caches/koda-stt/stt.sock}"
LOG_DIR="${KODA_STT_LOG_DIR:-$HOME/Library/Logs/koda-stt}"
PYTHON="$REPO_ROOT/.venv/bin/python"
OUT_DIR="${MLX_SPIKE_OUT:-/tmp/mlx-spike-$(date +%s)}"
SPIKE_LOG="$OUT_DIR/spike-log.txt"
SUMMARY="$OUT_DIR/summary.txt"

mkdir -p "$OUT_DIR"
exec > >(tee -a "$SPIKE_LOG") 2>&1

echo "=== MLX teardown spike ==="
echo "cycles=$CYCLES audio_ms=$AUDIO_MS clients=$CLIENTS"
echo "socket=$SOCKET_PATH"
echo "out=$OUT_DIR"
echo

_get_pid() {
    launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null \
        | awk '/^\tpid = / {print $3}' \
        | head -n1
}

_get_rss_kb() {
    local pid="$1"
    [[ -n "$pid" ]] || { echo 0; return; }
    ps -o rss= -p "$pid" 2>/dev/null | tr -d ' ' || echo 0
}

_wait_for_respond() {
    # Polls `python -m stt_server status` until exit 0 or timeout.
    # Returns seconds-to-respond (to 3 decimals) or "timeout".
    local start_ns end_ns
    start_ns=$(python3 -c 'import time; print(time.monotonic_ns())')
    local deadline=$(( $(date +%s) + 15 ))
    while [[ $(date +%s) -lt $deadline ]]; do
        if "$PYTHON" -m stt_server status --socket-path "$SOCKET_PATH" \
             --timeout 2.0 >/dev/null 2>&1; then
            end_ns=$(python3 -c 'import time; print(time.monotonic_ns())')
            python3 -c "print(f'{($end_ns - $start_ns) / 1e9:.3f}')"
            return 0
        fi
        sleep 0.2
    done
    echo "timeout"
}

_err_log_line_count() {
    [[ -f "$LOG_DIR/koda-stt.err" ]] || { echo 0; return; }
    wc -l < "$LOG_DIR/koda-stt.err" | tr -d ' '
}

_scan_launchd_stderr_range() {
    # Count hazard-pattern lines in the range [start_line..end_line] of
    # koda-stt.err. Scoping by line number (not timestamp) works even for
    # Metal assertions and libc++ aborts which print without timestamps —
    # those were silently missed by the prior timestamp-based filter.
    local start_line="$1"
    local end_line="$2"
    [[ -f "$LOG_DIR/koda-stt.err" ]] || { echo 0; return; }
    awk -v s="$start_line" -v e="$end_line" '
        NR >= s && NR <= e &&
        $0 ~ /Metal|failed assertion|libc\+\+abi|terminating due to|mutex lock failed|abort|panic|segmentation fault/ { c++ }
        END { print c+0 }
    ' "$LOG_DIR/koda-stt.err"
}

_wait_for_first_ok_commit() {
    # Wait until a fresh client can complete one commit end-to-end against
    # the respawned server, and return the RSS at that point. Measuring RSS
    # before the first successful commit is misleading — on a cold respawn
    # MLX hasn't loaded yet, so we'd see a false "leak recovered" reading.
    local pid="$1"
    local deadline_s="$2"
    local deadline=$(( $(date +%s) + deadline_s ))
    while [[ $(date +%s) -lt $deadline ]]; do
        if "$PYTHON" "$REPO_ROOT/scripts/_mlx_spike_driver.py" \
             --socket "$SOCKET_PATH" --audio-ms "$AUDIO_MS" \
             --one-shot >/dev/null 2>&1; then
            _get_rss_kb "$pid"
            return 0
        fi
        sleep 0.25
    done
    echo "timeout"
    return 1
}

# Pre-flight: make sure the agent is installed.
if ! launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    echo "error: $LABEL not loaded. Run scripts/install_stt_agent.sh install." >&2
    exit 1
fi

INITIAL_PID=$(_get_pid)
# Baseline RSS after MLX is warm — comparable to the per-cycle measurement.
INITIAL_RSS=$(_wait_for_first_ok_commit "$INITIAL_PID" 30)
echo "initial: pid=$INITIAL_PID rss=${INITIAL_RSS}KB (post-warm)"
echo

FAILED=0
RESULTS=()

for cycle in $(seq 1 "$CYCLES"); do
    echo "--- cycle $cycle/$CYCLES ---"
    pid_before=$(_get_pid)
    rss_before=$(_get_rss_kb "$pid_before")
    err_start_line=$(_err_log_line_count)

    # Spawn N concurrent drivers.
    driver_pids=()
    driver_outs=()
    for i in $(seq 1 "$CLIENTS"); do
        out="$OUT_DIR/driver-${cycle}-${i}.json"
        driver_outs+=("$out")
        "$PYTHON" "$REPO_ROOT/scripts/_mlx_spike_driver.py" \
            --socket "$SOCKET_PATH" --audio-ms "$AUDIO_MS" >"$out" 2>&1 &
        driver_pids+=($!)
    done
    # Let commits build up before we kick launchd.
    sleep 1.5

    # The act under test: forced restart while drivers are hammering.
    kick_start=$(python3 -c 'import time; print(time.monotonic())')
    launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null

    # Signal drivers to stop; they'll drain current commit then print JSON.
    for p in "${driver_pids[@]}"; do
        kill -TERM "$p" 2>/dev/null || true
    done
    wait "${driver_pids[@]}" 2>/dev/null || true

    respond_s=$(_wait_for_respond)
    pid_after=$(_get_pid)
    # Measure RSS *after* the first successful commit on the respawned
    # server, so MLX is warm and the number is comparable across cycles.
    rss_after=$(_wait_for_first_ok_commit "$pid_after" 15)
    err_end_line=$(_err_log_line_count)
    err_count=$(_scan_launchd_stderr_range "$((err_start_line + 1))" "$err_end_line")

    # Aggregate driver outcomes.
    total_ok=0; total_timeout=0; total_error=0; first_err=""
    for out in "${driver_outs[@]}"; do
        # Drivers emit one JSON line followed by nothing; grab last line.
        json=$(tail -n1 "$out" 2>/dev/null || echo "{}")
        ok=$(echo "$json"       | python3 -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); print(d.get("counts",{}).get("ok",0))')
        tout=$(echo "$json"     | python3 -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); print(d.get("counts",{}).get("timeout",0))')
        errs=$(echo "$json"     | python3 -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); print(d.get("counts",{}).get("error",0))')
        fe=$(echo "$json"       | python3 -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); print(d.get("first_error") or "")')
        total_ok=$(( total_ok + ok ))
        total_timeout=$(( total_timeout + tout ))
        total_error=$(( total_error + errs ))
        [[ -z "$first_err" && -n "$fe" ]] && first_err="$fe"
    done

    # Pass criteria per cycle.
    criteria=()
    [[ "$pid_after" != "$pid_before" ]] \
        && criteria+=("pid_changed=yes") || { criteria+=("pid_changed=NO"); FAILED=1; }
    if [[ "$respond_s" == "timeout" ]]; then
        criteria+=("respond=TIMEOUT"); FAILED=1
    else
        # Target < 3 s per dev plan ThrottleInterval budget.
        awk_pass=$(python3 -c "print('yes' if $respond_s < 3.0 else 'NO')")
        criteria+=("respond=${respond_s}s(${awk_pass})")
        [[ "$awk_pass" == "NO" ]] && FAILED=1
    fi
    [[ "$err_count" == "0" ]] \
        && criteria+=("hazard_log=clean") || { criteria+=("hazard_log=$err_count"); FAILED=1; }
    [[ "$total_timeout" == "0" ]] \
        && criteria+=("client_timeouts=0") || { criteria+=("client_timeouts=$total_timeout"); FAILED=1; }

    line="cycle=$cycle pid=$pid_before->$pid_after rss=${rss_before}KB->${rss_after}KB "
    line+="ok=$total_ok err=$total_error timeout=$total_timeout "
    line+="first_err='${first_err}' ${criteria[*]}"
    echo "$line"
    RESULTS+=("$line")
    echo
done

# RSS drift check against the very first boot RSS (both post-warm).
FINAL_PID=$(_get_pid)
FINAL_RSS=$(_wait_for_first_ok_commit "$FINAL_PID" 15)
if [[ "$INITIAL_RSS" =~ ^[0-9]+$ && "$FINAL_RSS" =~ ^[0-9]+$ && "$INITIAL_RSS" -gt 0 ]]; then
    drift_pct=$(python3 -c "print(abs($FINAL_RSS - $INITIAL_RSS) / $INITIAL_RSS * 100)")
    drift_pass=$(python3 -c "print('yes' if $drift_pct <= 10 else 'NO')")
    echo "RSS drift: ${INITIAL_RSS}KB -> ${FINAL_RSS}KB (${drift_pct}%, <=10% ${drift_pass})"
    [[ "$drift_pass" == "NO" ]] && FAILED=1
else
    echo "RSS drift: skipped (initial=$INITIAL_RSS final=$FINAL_RSS)"
fi

# Summary block for pasting into the dev plan.
{
    echo "MLX teardown spike summary"
    echo "  date:    $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "  cycles:  $CYCLES (clients=$CLIENTS audio_ms=$AUDIO_MS)"
    echo "  outcome: $([[ "$FAILED" == "0" ]] && echo PASS || echo FAIL)"
    echo "  initial_rss_kb: $INITIAL_RSS"
    echo "  final_rss_kb:   $FINAL_RSS"
    echo
    for r in "${RESULTS[@]}"; do echo "  $r"; done
} | tee "$SUMMARY"

exit "$FAILED"
