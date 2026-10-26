# Operator-convenience recipes for managing the pipecat STT LaunchAgents.
#
# macOS / launchctl only (consistent with scripts/install_stt_agent.sh). This is
# the cross-agent "operate the listed servers" surface the script structurally
# lacks (it manages exactly one agent per invocation, keyed by env vars).
#
# The backend -> (label, host, port, backend-name) map in `_resolve` is a CHECKED
# MIRROR of the canonical "Per-backend port convention" table in
# docs/operations.md. A test (tests/test_justfile_recipes.py) parses that table
# and asserts this map equals it, so drift fails CI. `_resolve` prints FOUR
# fields, one per line: label, host (always 127.0.0.1), port, backend-name.
# Agents default to loopback TCP; a Unix socket is opt-in via PIPECAT_STT_SOCKET.
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
# kyutai's extra conflicts with the other MLX extras, so it lives in its own venv.
# Overridable (e.g. `just kyutai_venv=/tmp/x _ensure-extra kyutai`) so tests never
# touch the real one.
kyutai_venv := justfile_directory() / ".venv-kyutai"
# Shared venv for the non-kyutai extras; tests override it on the command line
# like kyutai_venv.
venv := justfile_directory() / ".venv"
# NOTE: stt-install / stt-uninstall do NOT decide the transport. The installer owns
# the socket-vs-TCP rule (its PIPECAT_STT_SOCKET / KODA_STT_SOCKET alias pair) and
# defaults to 127.0.0.1 plus the backend's canonical port; the recipes only strip an
# ambient PIPECAT_STT_HOST / PIPECAT_STT_PORT so those canonical values always apply.
# Do not re-implement the rule here: a second copy can drift from the installer.

# Default: show the recipe list.
default:
    @just --list

# Resolve a backend name to LABEL / HOST / PORT / BACKEND_NAME on four separate
# lines (one field per line so the reads stay bash-3.2-compatible — macOS system
# bash has no `mapfile`); fail fast on unknown. `quote()` shell-escapes the
# interpolated arg so it can never break out of the `case` and run as a command —
# the `case` arms are the allowlist, everything else exits non-zero.
_resolve backend:
    #!/usr/bin/env bash
    backend={{quote(backend)}}
    case "$backend" in
      whisper)  printf '%s\n' "pipecat.stt-server"          "127.0.0.1" "9565" "mlx" ;;
      parakeet) printf '%s\n' "pipecat.stt-server.parakeet" "127.0.0.1" "9765" "parakeet" ;;
      nemotron) printf '%s\n' "pipecat.stt-server.nemotron" "127.0.0.1" "9865" "nemotron" ;;
      kyutai)   printf '%s\n' "pipecat.stt-server.kyutai"   "127.0.0.1" "9665" "kyutai" ;;
      *) echo "error: unknown backend '$backend' (valid: whisper, parakeet, nemotron, kyutai)" >&2; exit 1 ;;
    esac

# Extract the serve endpoint from an installed agent's plist ProgramArguments.
# Emits FOUR lines — host, port, socket-path, auth-token-file — each empty when
# the flag is absent. The plist is the source of truth, so stt-list never
# re-encodes the _resolve map and stays correct for custom labels/transports.
# Reads ProgramArguments structurally via `plutil`, element by element; the flag's
# VALUE is consumed in the same step that recognises the flag (the `expect`
# latch), so a value that looks like a flag can never be mis-read as one.
_plist_endpoint plist:
    #!/usr/bin/env bash
    set -uo pipefail
    plist={{quote(plist)}}
    host=""; port=""; sock=""; authfile=""; expect=""; i=0
    while el=$(plutil -extract "ProgramArguments.$i" raw -o - "$plist" 2>/dev/null); do
      if [[ -n "$expect" ]]; then
        case "$expect" in
          --host)            host="$el" ;;
          --port)            port="$el" ;;
          --socket-path)     sock="$el" ;;
          --auth-token-file) authfile="$el" ;;
        esac
        expect=""
      elif [[ "$el" == --* ]]; then
        expect="$el"
      fi
      i=$((i + 1))
    done
    printf '%s\n' "$host" "$port" "$sock" "$authfile"

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
      # Live backend probe: read the endpoint from the agent's OWN plist (four
      # lines: host/port/socket/auth-token-file), so custom labels, custom ports
      # and socket-mode agents are all probed, and a secured agent authenticates.
      # IFS= keeps leading/trailing whitespace in a path intact (bare `read -r` trims it).
      { IFS= read -r host; IFS= read -r port; IFS= read -r sock; IFS= read -r authfile; } < <(just _plist_endpoint "$plist")
      # Empty-safe array expansion for bash 3.2 under `set -u`.
      auth=()
      [[ -n "$authfile" ]] && auth=(--auth-token-file "$authfile")
      # status raises SystemExit(1) on a stopped/absent endpoint and never prints
      # "stopped"/"unreachable" itself, so the recipe owns that display. Capture
      # only the first token after `backend:` (never a `(model: ...)` suffix).
      tilde='~'
      if [[ -n "$sock" ]]; then
        printf '         socket: %s\n' "${sock/#$HOME/$tilde}"
        probe=(--socket-path "$sock")
      elif [[ -n "$host" && -n "$port" ]]; then
        printf '         endpoint: %s:%s\n' "$host" "$port"
        probe=(--host "$host" --port "$port")
      else
        printf '         endpoint: (no serve endpoint found in plist)\n'
        probe=()
      fi
      if [[ ${#probe[@]} -gt 0 ]]; then
        if live=$(uv run python -m stt_server status "${probe[@]}" "${auth[@]+"${auth[@]}"}" 2>/dev/null); then
          backend=$(grep -m1 -E '^[[:space:]]*backend:' <<<"$live" | sed -E 's/.*backend:[[:space:]]*([^[:space:]]+).*/\1/')
          printf '         live: %s\n' "${backend:-?}"
        else
          printf '         live: stopped/unreachable\n'
        fi
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

# Wire status probe for one server (exits with the probe's own status).
#
# No target: delegates to `just stt-list`, which shows every installed agent's
# live endpoint (exit 0).
#
# A target containing `/` or naming an existing socket file is a literal
# --socket-path (probed first, so a socket file sharing a backend's name is never
# reinterpreted). NOTE: a bare socket FILENAME is resolved against this
# justfile's directory (just's cwd), not the caller's; pass a path for anything else.
#
# A bare backend name probes that agent's ACTUAL endpoint: if its plist exists at
# `la_dir/<label>.plist`, the endpoint is read from it (socket => --socket-path,
# TCP => --host/--port, plus --auth-token-file when set), so a socket agent or a
# custom-port install is probed where it really listens. Only when no plist exists
# does it fall back to the backend's canonical host/port.
stt-status target='':
    #!/usr/bin/env bash
    set -uo pipefail
    target={{quote(target)}}
    if [[ -z "$target" ]]; then
      # Forward the overridable variable stt-list reads; a nested `just` would
      # otherwise fall back to the real ~/Library/LaunchAgents.
      exec just la_dir={{quote(la_dir)}} stt-list
    fi
    # A deliberate single probe (unlike stt-list's fast sweep): a freshly installed
    # agent can take several seconds to load its model, so be forgiving.
    timeout=10
    if [[ "$target" == */* || -S "$target" ]]; then
      exec uv run python -m stt_server status --socket-path "$target" --timeout "$timeout"
    fi
    resolved=$(just _resolve "$target") || exit 1
    # One field per line; four reads keep this bash-3.2-compatible.
    { read -r label; read -r host; read -r port; read -r bk; } <<<"$resolved"
    plist="{{la_dir}}/$label.plist"
    auth=()
    if [[ -f "$plist" ]]; then
      # The installed plist is the source of truth for where the agent listens.
      { IFS= read -r p_host; IFS= read -r p_port; IFS= read -r p_sock; IFS= read -r p_auth; } < <(just _plist_endpoint "$plist")
      [[ -n "$p_auth" ]] && auth=(--auth-token-file "$p_auth")
      if [[ -n "$p_sock" ]]; then
        probe=(--socket-path "$p_sock")
      elif [[ -n "$p_host" && -n "$p_port" ]]; then
        probe=(--host "$p_host" --port "$p_port")
      else
        echo "stt-status: $plist has no serve endpoint (--socket-path or --host/--port)" >&2
        exit 1
      fi
    else
      probe=(--host "$host" --port "$port")
    fi
    exec uv run python -m stt_server status "${probe[@]}" "${auth[@]+"${auth[@]}"}" --timeout "$timeout"

# Wire status probe for any endpoint: a server a supervising app started (TCP,
# no plist, so `stt-list`/`stt-status` cannot see it) or any socket. The TCP
# agents' own endpoints are covered by `stt-status`/`stt-list`; use this for
# anything else. Flags go straight to `stt_server status`, so the endpoint rules
# live in one place: `just stt-probe --host 127.0.0.1 --port 9565 [--auth-token-file F]`,
# `just stt-probe --socket-path PATH`, `just stt-probe --uri ws://...`; add
# `--json` for machine-readable output. An explicit endpoint is required, so a
# stray STT_WS_* env var can never decide which server is probed.
[positional-arguments]
stt-probe *args:
    #!/usr/bin/env bash
    set -uo pipefail
    explicit=0
    for a in "$@"; do
      case "$a" in
        --socket-path|--socket-path=*|--host|--host=*|--port|--port=*|--uri|--uri=*) explicit=1 ;;
      esac
    done
    if [[ "$explicit" -eq 0 ]]; then
      echo "stt-probe: pass an endpoint: --socket-path PATH, --host H --port N, or --uri URI" >&2
      exit 2
    fi
    exec uv run python -m stt_server status "$@"

# Stop an agent until next login (launchctl bootout; plist kept). Idempotent.
stt-disable backend:
    #!/usr/bin/env bash
    set -uo pipefail
    backend={{quote(backend)}}
    resolved=$(just _resolve "$backend") || exit 1
    # One field per line; four reads keep this bash-3.2-compatible — macOS
    # system bash has no `mapfile`.
    { read -r label; read -r host; read -r port; read -r bk; } <<<"$resolved"
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
    # One field per line; four reads keep this bash-3.2-compatible — macOS
    # system bash has no `mapfile`.
    { read -r label; read -r host; read -r port; read -r bk; } <<<"$resolved"
    uid=$(id -u)
    plist="{{la_dir}}/$label.plist"
    if [[ ! -e "$plist" ]]; then
      echo "stt-enable: no plist at $plist — run 'just stt-install $backend' first" >&2
      exit 1
    fi
    # Self-heal a pruned venv before re-loading: if a bare `uv run`/`uv sync`
    # stripped this backend's extra since install, bootstrapping the plist would
    # just resume the crash-loop. (Skipped under PIPECAT_STT_SKIP_DEP_SYNC.)
    just venv={{quote(venv)}} kyutai_venv={{quote(kyutai_venv)}} _ensure-extra "$backend" || exit 1
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

# Ensure a backend's optional Python extra is installed in .venv, additively
# (kyutai: its own .venv-kyutai, see kyutai_venv).
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
    # import probe differs. The probe MUST match what backend.start() imports
    # (nemotron does `from mlx_audio.stt import load`, not `import mlx_audio`) —
    # a coarser `import mlx_audio` would pass for a partially-installed package
    # while the agent still crash-loops. Validate the backend before the skip
    # check so an unknown backend always errors regardless of the skip env.
    case "$backend" in
      whisper)  extra="mlx";      probe="import mlx_whisper" ;;
      parakeet) extra="parakeet"; probe="import parakeet_mlx" ;;
      nemotron) extra="nemotron"; probe="from mlx_audio.stt import load" ;;
      kyutai)   extra="kyutai";   probe="import moshi_mlx" ;;
      *) echo "error: unknown backend '$backend' (valid: whisper, parakeet, nemotron, kyutai)" >&2; exit 1 ;;
    esac
    if [[ -n "${PIPECAT_STT_SKIP_DEP_SYNC:-}" ]]; then
      echo "_ensure-extra: PIPECAT_STT_SKIP_DEP_SYNC set — skipping '$extra' extra check"
      exit 0
    fi
    # kyutai's extra conflicts with the other MLX extras, so it syncs into its own
    # venv (never .venv); the others sync additively into .venv.
    if [[ "$extra" == "kyutai" ]]; then
      venv={{quote(kyutai_venv)}}
      py="$venv/bin/python"
      sync=(env "UV_PROJECT_ENVIRONMENT=$venv" uv sync --frozen --python 3.12 --extra kyutai)
    else
      py={{quote(venv)}}"/bin/python"
      # `env -u`: never inherit an ambient UV_PROJECT_ENVIRONMENT (e.g. a shell
      # exported for kyutai) — that would sync this extra into the wrong venv.
      sync=(env -u UV_PROJECT_ENVIRONMENT uv sync --extra "$extra" --inexact)
    fi
    if [[ -x "$py" ]] && "$py" -c "$probe" >/dev/null 2>&1; then
      echo "_ensure-extra: '$extra' extra already present ($probe)"
      exit 0
    fi
    echo "_ensure-extra: '$extra' import check failed ($probe) — installing (${sync[*]})…"
    if ! "${sync[@]}"; then
      echo "_ensure-extra: '${sync[*]}' failed; install the '$extra' extra manually" >&2
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
    # One field per line; four reads keep this bash-3.2-compatible — macOS
    # system bash has no `mapfile`.
    { read -r label; read -r _host; read -r _port; read -r bk; } <<<"$resolved"
    just venv={{quote(venv)}} kyutai_venv={{quote(kyutai_venv)}} _ensure-extra "$backend" || exit 1
    # Strip an ambient HOST/PORT so the installer's canonical defaults apply; the
    # installer alone decides socket vs TCP (see the NOTE near the top).
    env -u PIPECAT_STT_HOST -u PIPECAT_STT_PORT PIPECAT_STT_LABEL="$label" PIPECAT_STT_BACKEND="$bk" \
      "{{script}}" install

# Uninstall an agent (removes the plist) — delegates to install_stt_agent.sh.
stt-uninstall backend:
    #!/usr/bin/env bash
    set -uo pipefail
    backend={{quote(backend)}}
    resolved=$(just _resolve "$backend") || exit 1
    # One field per line; four reads keep this bash-3.2-compatible — macOS
    # system bash has no `mapfile`.
    { read -r label; read -r _host; read -r _port; read -r bk; } <<<"$resolved"
    # Same ambient HOST/PORT stripping as stt-install; the installer owns the transport.
    env -u PIPECAT_STT_HOST -u PIPECAT_STT_PORT PIPECAT_STT_LABEL="$label" PIPECAT_STT_BACKEND="$bk" \
      "{{script}}" uninstall
