# Operator-convenience recipes for managing the pipecat STT LaunchAgents.
#
# macOS / launchctl only (consistent with scripts/install_stt_agent.sh). This is
# the cross-agent "operate the listed servers" surface the script structurally
# lacks (it manages exactly one agent per invocation, keyed by env vars).
#
# The backend -> (label, socket, backend-name) map below is a CHECKED MIRROR of
# the canonical README per-ASR table ("### Per-ASR socket convention"). A test
# (tests/test_justfile_recipes.py) parses that table and asserts this map equals
# it, so drift fails CI. Only whisper's socket is an actual install_stt_agent.sh
# default; parakeet/nemotron sockets are operator-supplied env, so this map is a
# third copy and the mirror test is what keeps it honest.
#
# stt-disable vs stt-uninstall: the rendered plist sets RunAtLoad=True +
# KeepAlive=True (render_stt_plist.py). So `stt-disable` (launchctl bootout, plist
# kept) takes the agent down only until the next login — launchd reloads it from
# the on-disk plist. `stt-uninstall` removes the plist, so it stays gone. Use
# `launchctl disable` if you need cross-login suppression without removing.

set shell := ["bash", "-uc"]

# cache_dir / la_dir derive from $HOME via env_var(), which `just` evaluates when
# the recipe runs (not at parse time) — that runtime evaluation is what lets the
# tests point them at a temp HOME. Like `script` below, both are overridable on
# the command line (e.g. `just la_dir=/tmp/x stt-list`).
cache_dir := env_var('HOME') / "Library/Caches/pipecat-stt"
la_dir := env_var('HOME') / "Library/LaunchAgents"
# Overridable so tests can point install/uninstall delegation at a stub.
script := justfile_directory() / "scripts/install_stt_agent.sh"

# Default: show the recipe list.
default:
    @just --list

# Resolve a backend name to LABEL / SOCKET / BACKEND_NAME on three separate lines
# (one field per line so a socket path containing spaces survives the read in the
# callers); fail fast on unknown. `quote()` shell-escapes the interpolated arg so
# it can never break out of the `case` and run as a command — the `case` arms are
# the allowlist, everything else exits non-zero.
_resolve backend:
    #!/usr/bin/env bash
    backend={{quote(backend)}}
    case "$backend" in
      whisper)  printf '%s\n' "pipecat.stt-server" "{{cache_dir}}/stt.sock" "mlx" ;;
      parakeet) printf '%s\n' "pipecat.stt-server.parakeet" "{{cache_dir}}/parakeet.sock" "parakeet" ;;
      nemotron) printf '%s\n' "pipecat.stt-server.nemotron" "{{cache_dir}}/nemotron.sock" "nemotron" ;;
      *) echo "error: unknown backend '$backend' (valid: whisper, parakeet, nemotron)" >&2; exit 1 ;;
    esac

# List every pipecat.stt-server* agent with state, pid, and live backend.
stt-list:
    #!/usr/bin/env bash
    set -uo pipefail
    shopt -s nullglob
    uid=$(id -u)
    found=0
    for plist in "{{la_dir}}"/pipecat.stt-server*.plist; do
      found=1
      label=$(basename "$plist" .plist)
      target="gui/$uid/$label"
      # launchctl print exits non-zero when the agent is not loaded; reuse the
      # same fields the script greps (state|last exit|pid).
      if info=$(launchctl print "$target" 2>/dev/null); then
        pid=$(grep -m1 -E '^[[:space:]]*pid = ' <<<"$info" | grep -oE '[0-9]+' | head -1)
        state=$(grep -m1 -E '^[[:space:]]*state = ' <<<"$info" | sed -E 's/.*state = //')
        printf 'running  %-32s pid=%-7s state=%s\n' "$label" "${pid:-?}" "${state:-?}"
      else
        printf 'stopped  %-32s (plist present, not loaded)\n' "$label"
      fi
      # Live backend probe — canonical sockets only (a custom label's socket is
      # not derivable from its label, so it gets no socket/live line by design).
      sock=""
      case "$label" in
        pipecat.stt-server)          sock="{{cache_dir}}/stt.sock" ;;
        pipecat.stt-server.parakeet) sock="{{cache_dir}}/parakeet.sock" ;;
        pipecat.stt-server.nemotron) sock="{{cache_dir}}/nemotron.sock" ;;
      esac
      if [[ -n "$sock" ]]; then
        # Print the socket in the same ~-form onoats config.toml's `ws_socket`
        # uses, so an operator can match a config line to an agent directly
        # (note whisper's socket is `stt.sock`, not `whisper.sock`).
        printf '         socket: %s\n' "${sock/#$HOME/~}"
        # status raises SystemExit(1) on a stopped/absent socket and never prints
        # "stopped"/"unreachable" itself, so the recipe owns that display.
        if live=$(uv run python -m stt_server status --socket-path "$sock" 2>/dev/null); then
          backend=$(grep -m1 -E '^[[:space:]]*backend:' <<<"$live" | sed -E 's/.*backend:[[:space:]]*//')
          printf '         live: %s\n' "${backend:-?}"
        else
          printf '         live: stopped/unreachable\n'
        fi
      else
        printf '         socket: (custom label — not in the canonical map)\n'
      fi
    done
    if [[ "$found" -eq 0 ]]; then
      echo "no pipecat.stt-server* agents found in {{la_dir}}"
    fi
    # Deliberate: this recipe is a read-only status sweep. Per-agent probe
    # failures (stopped socket, unloaded agent) are already absorbed into display
    # lines above, so the recipe as a whole always succeeds. Any new probe added
    # below must guard its own non-zero with `|| echo …` to preserve this.
    exit 0

# Wire status probe for one backend (exits with the probe's own status).
stt-status backend:
    #!/usr/bin/env bash
    set -uo pipefail
    backend={{quote(backend)}}
    resolved=$(just _resolve "$backend") || exit 1
    # One field per line (so a spaced socket path survives); three reads keep
    # this bash-3.2-compatible — macOS system bash has no `mapfile`.
    { read -r label; read -r sock; read -r bk; } <<<"$resolved"
    exec uv run python -m stt_server status --socket-path "$sock"

# Stop an agent until next login (launchctl bootout; plist kept). Idempotent.
stt-disable backend:
    #!/usr/bin/env bash
    set -uo pipefail
    backend={{quote(backend)}}
    resolved=$(just _resolve "$backend") || exit 1
    # One field per line (so a spaced socket path survives); three reads keep
    # this bash-3.2-compatible — macOS system bash has no `mapfile`.
    { read -r label; read -r sock; read -r bk; } <<<"$resolved"
    uid=$(id -u)
    if ! launchctl print "gui/$uid/$label" >/dev/null 2>&1; then
      echo "stt-disable: $label not loaded — nothing to do"
      exit 0
    fi
    # Guard explicitly: set -uo pipefail does NOT abort on a failed simple
    # command, so without this the success echo below would mask a bootout
    # failure and report exit 0 while the agent is still running.
    if ! launchctl bootout "gui/$uid/$label"; then
      echo "stt-disable: launchctl bootout failed for $label" >&2
      exit 1
    fi
    echo "stt-disable: booted out $label (plist kept; reloads at next login)."
    echo "             Use 'just stt-uninstall $backend' to remove it durably."

# Re-load + start an agent from its existing plist (no re-render).
stt-enable backend:
    #!/usr/bin/env bash
    set -uo pipefail
    backend={{quote(backend)}}
    resolved=$(just _resolve "$backend") || exit 1
    # One field per line (so a spaced socket path survives); three reads keep
    # this bash-3.2-compatible — macOS system bash has no `mapfile`.
    { read -r label; read -r sock; read -r bk; } <<<"$resolved"
    uid=$(id -u)
    plist="{{la_dir}}/$label.plist"
    if [[ ! -e "$plist" ]]; then
      echo "stt-enable: no plist at $plist — run 'just stt-install $backend' first" >&2
      exit 1
    fi
    # Self-heal a pruned venv before re-loading: if a bare `uv run`/`uv sync`
    # stripped this backend's extra since install, bootstrapping the plist would
    # just resume the crash-loop. (Skipped under PIPECAT_STT_SKIP_DEP_SYNC.)
    just _ensure-extra "$backend" || exit 1
    # Guard each state change: set -uo pipefail does NOT abort on a failed
    # simple command, so an unguarded failure would be masked by the success
    # echo (exit 0 while the agent never started).
    if ! launchctl bootstrap "gui/$uid" "$plist"; then
      echo "stt-enable: launchctl bootstrap failed for $label" >&2
      exit 1
    fi
    if ! launchctl kickstart "gui/$uid/$label"; then
      echo "stt-enable: launchctl kickstart failed for $label" >&2
      exit 1
    fi
    echo "stt-enable: bootstrapped + kickstarted $label"

# Local UDS peer-cred smoke: same-uid multi-connection + cross-uid 403.
# Runs an in-process server with both filesystem layers deliberately permissive
# (0711 parent + 0o666 socket) via a test-only helper replacement, so peer-cred
# is provably what rejects a foreign uid. The cross-uid leg needs a second local
# uid reachable via passwordless `sudo`; it skips cleanly (exit 0) when absent,
# while the same-uid leg still runs.
smoke-peercred:
    #!/usr/bin/env bash
    set -uo pipefail
    exec uv run python "{{justfile_directory()}}/scripts/smoke_peercred.py"

# Ensure a backend's optional Python extra is installed in .venv, additively.
# The server imports its backend lib lazily in backend.start(); a bare
# `uv run`/`uv sync` prunes optional extras, so an agent can be installed yet
# crash-loop on `ModuleNotFoundError`. `stt-install`/`stt-enable` call this so
# onboarding is self-healing. `--inexact` is load-bearing: plain
# `uv sync --extra X` prunes the OTHER backends' extras, breaking a multi-backend
# host. We probe via the venv python directly — NOT `uv run`, which would itself
# re-sync/prune before we could check. Set PIPECAT_STT_SKIP_DEP_SYNC=1 to manage
# extras yourself (the recipe tests set it so they never shell out to `uv sync`).
_ensure-extra backend:
    #!/usr/bin/env bash
    set -uo pipefail
    backend={{quote(backend)}}
    # extra == the install backend-name (the 3rd field of `_resolve`); only the
    # import-probe name differs from it. Validate before the skip check so an
    # unknown backend always errors regardless of PIPECAT_STT_SKIP_DEP_SYNC.
    case "$backend" in
      whisper)  extra="mlx";      probe="mlx_whisper" ;;
      parakeet) extra="parakeet"; probe="parakeet_mlx" ;;
      nemotron) extra="nemotron"; probe="mlx_audio" ;;
      *) echo "error: unknown backend '$backend' (valid: whisper, parakeet, nemotron)" >&2; exit 1 ;;
    esac
    if [[ -n "${PIPECAT_STT_SKIP_DEP_SYNC:-}" ]]; then
      echo "_ensure-extra: PIPECAT_STT_SKIP_DEP_SYNC set — skipping '$extra' extra check"
      exit 0
    fi
    py="{{justfile_directory()}}/.venv/bin/python"
    if [[ -x "$py" ]] && "$py" -c "import $probe" >/dev/null 2>&1; then
      echo "_ensure-extra: '$extra' extra already present ($probe importable)"
      exit 0
    fi
    echo "_ensure-extra: '$probe' missing — installing the '$extra' extra (uv sync --extra $extra --inexact)…"
    if ! uv sync --extra "$extra" --inexact; then
      echo "_ensure-extra: 'uv sync --extra $extra --inexact' failed; install the '$extra' extra manually" >&2
      exit 1
    fi
    echo "_ensure-extra: '$extra' extra ready."

# Install an agent — delegates to install_stt_agent.sh (no plist reimplementation).
# Ensures the backend's Python extra first (see _ensure-extra) so the freshly
# installed agent doesn't immediately crash-loop on a missing backend import.
stt-install backend:
    #!/usr/bin/env bash
    set -uo pipefail
    backend={{quote(backend)}}
    resolved=$(just _resolve "$backend") || exit 1
    # One field per line (so a spaced socket path survives); three reads keep
    # this bash-3.2-compatible — macOS system bash has no `mapfile`.
    { read -r label; read -r sock; read -r bk; } <<<"$resolved"
    just _ensure-extra "$backend" || exit 1
    PIPECAT_STT_LABEL="$label" PIPECAT_STT_SOCKET="$sock" PIPECAT_STT_BACKEND="$bk" \
      "{{script}}" install

# Uninstall an agent (removes the plist) — delegates to install_stt_agent.sh.
stt-uninstall backend:
    #!/usr/bin/env bash
    set -uo pipefail
    backend={{quote(backend)}}
    resolved=$(just _resolve "$backend") || exit 1
    # One field per line (so a spaced socket path survives); three reads keep
    # this bash-3.2-compatible — macOS system bash has no `mapfile`.
    { read -r label; read -r sock; read -r bk; } <<<"$resolved"
    PIPECAT_STT_LABEL="$label" PIPECAT_STT_SOCKET="$sock" PIPECAT_STT_BACKEND="$bk" \
      "{{script}}" uninstall
