# Operator-convenience recipes for managing the pipecat STT LaunchAgents.
#
# macOS / launchctl only (consistent with scripts/install_stt_agent.sh). This is
# the cross-agent "operate the listed servers" surface the script structurally
# lacks (it manages exactly one agent per invocation, keyed by env vars).
#
# `_resolve` maps a backend to (label, host, port, backend-name). The labels and
# backend names live here; the PORT is not a justfile copy: it comes from the
# installer, the one backend -> port map (`scripts/install_stt_agent.sh port
# <backend-name>`, side-effect free). The "Per-backend port convention" table in
# docs/operations.md is a CHECKED MIRROR: tests/test_justfile_recipes.py parses it
# and asserts it equals `_resolve` and the installer, so drift fails CI.
# `_resolve` prints FOUR fields, one per line: label, host (always 127.0.0.1),
# port, backend-name.
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
      whisper)  label="pipecat.stt-server";          bk="mlx" ;;
      parakeet) label="pipecat.stt-server.parakeet"; bk="parakeet" ;;
      nemotron) label="pipecat.stt-server.nemotron"; bk="nemotron" ;;
      kyutai)   label="pipecat.stt-server.kyutai";   bk="kyutai" ;;
      *) echo "error: unknown backend '$backend' (valid: whisper, parakeet, nemotron, kyutai)" >&2; exit 1 ;;
    esac
    # The real installer (not the overridable `script`, which tests point at a
    # delegation stub): its `port` subcommand prints the canonical port and calls
    # neither launchctl nor anything that writes.
    port=$("{{justfile_directory()}}/scripts/install_stt_agent.sh" port "$bk") || exit 1
    printf '%s\n' "$label" "127.0.0.1" "$port" "$bk"

# Print one field of an installed agent's plist: host, port, socket-path,
# auth-token-file (its serve flags) or interpreter (ProgramArguments[0]), exactly,
# empty when the plist does not carry it. One keyed field per call, so no recipe
# depends on the order of anything the reader prints. The plist is the source of
# truth, so stt-list never re-encodes the _resolve map and stays correct for
# custom labels/transports. scripts/read_stt_plist.py is the one reader (the
# installer calls it too); it runs under the system python3, so a pruned or
# missing venv cannot break it. `strict=1` is the reader's --strict form: a
# malformed plist exits 2 instead of reading as empty.
_plist_field plist field strict='':
    #!/usr/bin/env bash
    mode=()
    [[ -n {{quote(strict)}} ]] && mode=(--strict)
    exec python3 -I "{{justfile_directory()}}/scripts/read_stt_plist.py" "${mode[@]+"${mode[@]}"}" --field {{quote(field)}} {{quote(plist)}}

# Warn (stderr only) when a TCP agent's port is held by a process that is not the
# agent: scripts/squat_check.py compares the lsof listener pid with the launchd pid
# (`loaded` is 1/0; `pid` empty when the job has none; `label` is for its 1-second
# re-check). Advisory: callers append `|| true`, so it never changes an exit
# status or stdout. System python3 so a pruned venv cannot break it.
_squat_check host port pid loaded label:
    #!/usr/bin/env bash
    pid={{quote(pid)}}
    exec python3 -I "{{justfile_directory()}}/scripts/squat_check.py" --host {{quote(host)}} --port {{quote(port)}} --loaded {{quote(loaded)}} ${pid:+--pid "$pid"} --label {{quote(label)}}

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
      loaded=0
      pid=
      if info=$(launchctl print "$target" 2>/dev/null); then
        loaded=1
        pid=$(grep -m1 -E '^[[:space:]]*pid = ' <<<"$info" | grep -oE '[0-9]+' | head -1)
        state=$(grep -m1 -E '^[[:space:]]*state = ' <<<"$info" | sed -E 's/.*state = //')
        printf 'running  %-32s pid=%-7s state=%s\n' "$label" "${pid:-?}" "${state:-?}"
      else
        printf 'stopped  %-32s (plist present, not loaded)\n' "$label"
      fi
      # Live backend probe: read the endpoint from the agent's OWN plist, one
      # keyed field at a time, so custom labels, custom ports and socket-mode
      # agents are all probed, and a secured agent authenticates. A malformed
      # plist reads as empty with a warning; only the first read shows it.
      host=$(just _plist_field "$plist" host)
      port=$(just _plist_field "$plist" port 2>/dev/null)
      # Paths are captured losslessly: `$(...)` drops trailing newlines, which
      # would probe a path other than the one the plist records. Append a
      # sentinel inside the substitution and strip it after.
      sock=$(just _plist_field "$plist" socket-path 2>/dev/null && printf x); sock=${sock%x}
      authfile=$(just _plist_field "$plist" auth-token-file 2>/dev/null && printf x); authfile=${authfile%x}
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
        just _squat_check "$host" "$port" "$pid" "$loaded" "$label" || true
      else
        printf '         endpoint: (no serve endpoint found in plist)\n'
        probe=()
      fi
      if [[ ${#probe[@]} -gt 0 ]]; then
        if live=$(uv run --no-sync python -m stt_server status "${probe[@]}" "${auth[@]+"${auth[@]}"}" 2>/dev/null); then
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
      exec uv run --no-sync python -m stt_server status --socket-path "$target" --timeout "$timeout"
    fi
    resolved=$(just _resolve "$target") || exit 1
    # One field per line; four reads keep this bash-3.2-compatible.
    { read -r label; read -r host; read -r port; read -r bk; } <<<"$resolved"
    plist="{{la_dir}}/$label.plist"
    auth=()
    if [[ -f "$plist" ]]; then
      # The installed plist is the source of truth for where the agent listens.
      # One keyed field per read; a malformed plist warns once and reads empty.
      p_host=$(just _plist_field "$plist" host)
      p_port=$(just _plist_field "$plist" port 2>/dev/null)
      # Lossless path captures (sentinel `x` appended inside, stripped after):
      # `$(...)` alone drops trailing newlines from a recorded path.
      p_sock=$(just _plist_field "$plist" socket-path 2>/dev/null && printf x); p_sock=${p_sock%x}
      p_auth=$(just _plist_field "$plist" auth-token-file 2>/dev/null && printf x); p_auth=${p_auth%x}
      [[ -n "$p_auth" ]] && auth=(--auth-token-file "$p_auth")
      if [[ -n "$p_sock" ]]; then
        probe=(--socket-path "$p_sock")
      elif [[ -n "$p_host" && -n "$p_port" ]]; then
        probe=(--host "$p_host" --port "$p_port")
      else
        echo "stt-status: $plist has no serve endpoint (--socket-path or --host/--port)" >&2
        exit 1
      fi
      if [[ -z "$p_sock" ]]; then
        # TCP plist: check the port's holder before the probe's exec. Same
        # read-only launchctl print as stt-list, for the pid and loaded state.
        loaded=0; pid=
        if info=$(launchctl print "gui/$(id -u)/$label" 2>/dev/null); then
          loaded=1
          pid=$(grep -m1 -E '^[[:space:]]*pid = ' <<<"$info" | grep -oE '[0-9]+' | head -1)
        fi
        just _squat_check "$p_host" "$p_port" "$pid" "$loaded" "$label" || true
      fi
    else
      probe=(--host "$host" --port "$port")
    fi
    exec uv run --no-sync python -m stt_server status "${probe[@]}" "${auth[@]+"${auth[@]}"}" --timeout "$timeout"

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
    exec uv run --no-sync python -m stt_server status "$@"

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
    # bootout returns before launchd has torn the job down (a loaded model takes
    # seconds to exit), and an stt-enable in that window fails with "Bootstrap
    # failed: 5". So wait, like the installer's wait_unloaded (30 s, its default
    # PIPECAT_STT_UNLOAD_WAIT), until launchd no longer lists the label. Still
    # loaded at the cap: say so instead of "booted out" (exit 0, as uninstall).
    waited=0
    while launchctl print "gui/$uid/$label" >/dev/null 2>&1; do
      if [[ "$waited" -ge 30 ]]; then
        echo "stt-disable: $label still loaded 30s after bootout; wait until 'launchctl print gui/$uid/$label' reports no such service before 'just stt-enable $backend'" >&2
        echo "stt-disable: bootout requested for $label, still unloading (plist kept)."
        exit 0
      fi
      sleep 1
      waited=$((waited + 1))
    done
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
    # just resume the crash-loop. The probe runs the interpreter the INSTALLED
    # plist names (its ProgramArguments[0]), never the caller's PIPECAT_STT_PYTHON:
    # that is what launchd will run. (Skipped under PIPECAT_STT_SKIP_DEP_SYNC.)
    just venv={{quote(venv)}} kyutai_venv={{quote(kyutai_venv)}} _ensure-extra "$backend" "" "$plist" || exit 1
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
    exec .venv/bin/python "{{justfile_directory()}}/scripts/smoke_peercred.py"

# The interpreter an agent installed now would run: PIPECAT_STT_PYTHON when it is
# set and non-empty, else the backend's venv python (kyutai: kyutai_venv). The one
# place the justfile picks an interpreter: stt-install probes this value and
# passes it to the installer as PIPECAT_STT_PYTHON, so the probed interpreter is
# the installed one by construction (under a `venv=` override too); the
# installer's own default applies only to a direct installer run.
_agent-python backend:
    #!/usr/bin/env bash
    set -uo pipefail
    backend={{quote(backend)}}
    case "$backend" in
      whisper)  py={{quote(venv / "bin" / "python")}} ;;
      parakeet) py={{quote(venv / "bin" / "python")}} ;;
      nemotron) py={{quote(venv / "bin" / "python")}} ;;
      kyutai)   py={{quote(kyutai_venv / "bin" / "python")}} ;;
      *) echo "error: unknown backend '$backend' (valid: whisper, parakeet, nemotron, kyutai)" >&2; exit 1 ;;
    esac
    printf '%s\n' "${PIPECAT_STT_PYTHON:-$py}"

# Ensure a backend's optional Python extra is installed in .venv, additively
# (kyutai: its own .venv-kyutai, see kyutai_venv).
# The server imports its backend lib lazily in backend.start(); a bare
# `uv run`/`uv sync` prunes optional extras, so an agent can be installed yet
# crash-loop on `ModuleNotFoundError`. `stt-install`/`stt-enable` call this so
# onboarding is self-healing. `--inexact` is load-bearing: plain
# `uv sync --extra X` prunes the OTHER backends' extras, breaking a multi-backend
# host. We probe via the venv python directly — NOT `uv run`, which would itself
# re-sync/prune before we could check.
# It probes the interpreter the agent runs, and only a venv python can be fixed by
# a sync (any other interpreter fails with the remedy):
#   * `plist` given (stt-enable): ProgramArguments[0] of that installed plist, read
#     with the plist reader's --strict form; the caller's env plays no part;
#   * else `python` given (stt-install): that interpreter, from _agent-python;
#   * else (a direct call): _agent-python's rule (PIPECAT_STT_PYTHON, else the venv).
# Set PIPECAT_STT_SKIP_DEP_SYNC=1 to manage
# extras yourself (the recipe tests set it so they never shell out to `uv sync`).
_ensure-extra backend python='' plist='':
    #!/usr/bin/env bash
    set -uo pipefail
    backend={{quote(backend)}}
    # extra == the install backend-name (the 4th field of `_resolve`); only the
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
    # venv (never .venv); the others sync additively into .venv. default_py is the
    # interpreter that sync fills, the only one a sync can fix.
    if [[ "$extra" == "kyutai" ]]; then
      venv={{quote(kyutai_venv)}}
      default_py="$venv/bin/python"
      sync=(env "UV_PROJECT_ENVIRONMENT=$venv" uv sync --frozen --python 3.12 --extra kyutai)
    else
      default_py={{quote(venv)}}"/bin/python"
      # `env -u`: never inherit an ambient UV_PROJECT_ENVIRONMENT (e.g. a shell
      # exported for kyutai) — that would sync this extra into the wrong venv.
      sync=(env -u UV_PROJECT_ENVIRONMENT uv sync --extra "$extra" --inexact)
    fi
    plist={{quote(plist)}}
    python={{quote(python)}}
    if [[ -n "$plist" ]]; then
      # Lossless (sentinel `x`): the interpreter probed is the one recorded.
      if ! py=$(just _plist_field "$plist" interpreter 1 && printf x); then
        echo "_ensure-extra: cannot read the interpreter from $plist; fix or reinstall it ('just stt-install $backend')" >&2
        exit 1
      fi
      py=${py%x}
      if [[ -z "$py" ]]; then
        echo "_ensure-extra: $plist names no interpreter (no ProgramArguments); reinstall it ('just stt-install $backend')" >&2
        exit 1
      fi
      remedy="the installed agent runs $py (ProgramArguments[0] of $plist); install the '$extra' extra into that interpreter, or reinstall the agent (stt-install $backend)"
    else
      if [[ -n "$python" ]]; then
        py="$python"
      elif ! py=$(just venv={{quote(venv)}} kyutai_venv={{quote(kyutai_venv)}} _agent-python "$backend" && printf x); then
        exit 1
      else
        # _agent-python ends its output with one newline; strip the sentinel and
        # exactly that one, nothing the path itself carries.
        py=${py%x}; py=${py%$'\n'}
      fi
      remedy="PIPECAT_STT_PYTHON=$py cannot run '$probe'; install the '$extra' extra into that interpreter, or unset PIPECAT_STT_PYTHON to use $default_py"
    fi
    if [[ -x "$py" ]] && "$py" -c "$probe" >/dev/null 2>&1; then
      echo "_ensure-extra: '$extra' extra already present ($probe, $py)"
      exit 0
    fi
    if [[ "$py" != "$default_py" ]]; then
      echo "_ensure-extra: $remedy" >&2
      exit 1
    fi
    echo "_ensure-extra: '$extra' import check failed ($probe) — installing (${sync[*]})…"
    if ! "${sync[@]}"; then
      echo "_ensure-extra: '${sync[*]}' failed; install the '$extra' extra manually" >&2
      exit 1
    fi
    # A sync that exits 0 has not necessarily made the import work (a wrong
    # interpreter in the venv, a broken wheel, a partial install): only the
    # probe decides "ready", or the agent would be installed to crash-loop.
    if ! { [[ -x "$py" ]] && "$py" -c "$probe" >/dev/null 2>&1; }; then
      echo "_ensure-extra: '${sync[*]}' succeeded but '$probe' still fails under $py; install the '$extra' extra manually" >&2
      exit 1
    fi
    echo "_ensure-extra: '$extra' extra ready."

# Install an agent — delegates to install_stt_agent.sh (no plist reimplementation).
# Ensures the backend's Python extra first (see _ensure-extra) so the freshly
# installed agent doesn't immediately crash-loop on a missing backend import. The
# interpreter it probes (_agent-python) is passed to the installer as
# PIPECAT_STT_PYTHON, so the plist records exactly the interpreter that was checked.
stt-install backend:
    #!/usr/bin/env bash
    set -uo pipefail
    backend={{quote(backend)}}
    resolved=$(just _resolve "$backend") || exit 1
    # One field per line; four reads keep this bash-3.2-compatible — macOS
    # system bash has no `mapfile`.
    { read -r label; read -r _host; read -r _port; read -r bk; } <<<"$resolved"
    py=$(just venv={{quote(venv)}} kyutai_venv={{quote(kyutai_venv)}} _agent-python "$backend" && printf x) || exit 1
    py=${py%x}; py=${py%$'\n'}
    just venv={{quote(venv)}} kyutai_venv={{quote(kyutai_venv)}} _ensure-extra "$backend" "$py" || exit 1
    # Strip an ambient HOST/PORT so the installer's canonical defaults apply; the
    # installer alone decides socket vs TCP (see the NOTE near the top).
    env -u PIPECAT_STT_HOST -u PIPECAT_STT_PORT PIPECAT_STT_LABEL="$label" PIPECAT_STT_BACKEND="$bk" \
      PIPECAT_STT_PYTHON="$py" "{{script}}" install

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
