# Operations and deployment

Install and manage persistent macOS LaunchAgents for
[pipecat-local-stt-server](../README.md). For a foreground server, start with
the [README](../README.md). Choose a model in [Backends and models](backends.md).

- [Install an agent](#installing-agents)
- [Enable, disable, or remove an agent](#everyday-commands)
- [Choose an endpoint](#per-backend-port-convention)
- [Check server health](#checking-server-health)
- [Transport security](security.md)
- [Testing and benchmarks](testing.md)

## Installing agents

Run these commands from the repository checkout on macOS with `uv` and
`just` installed. Each agent runs at login and restarts if its process exits.
Warm the model cache before installing a production agent:

```bash
uv sync --extra parakeet --inexact
uv run --no-sync python -m stt_server prefetch --backend parakeet
just stt-install parakeet
```

`just stt-install` and `just stt-enable` ensure the selected backend's extra
is present without pruning other extras. Kyutai uses a separate environment;
see [Kyutai](#kyutai-agent-and-its-own-venv). Set
`PIPECAT_STT_SKIP_DEP_SYNC=1` only if you manage dependencies yourself.
A bare `uv sync` or `uv run` can remove extras, so use `uv run --no-sync`
for commands in an existing environment.

Replace `parakeet` with `whisper`, `nemotron`, `kyutai`, or `moonshine` to
install another backend. The [port table](#per-backend-port-convention) lists
their endpoints. Each install starts that agent.

A cold start can spend minutes downloading weights. Prefetch first so a
launch failure does not repeat that work under launchd's restart policy. See
[Prefetching and cache checks](backends.md#prefetching-and-cache-checks) for
progress events, local model paths, and pinned revisions.

### Custom agents

The installer manages one agent per invocation, identified by
`PIPECAT_STT_LABEL`. A default invocation targets `pipecat.stt-server`.
Set a distinct label and backend for a custom agent, then reuse those settings
for `uninstall`, `start`, `stop`, `restart`, `status`, or `logs`.
The `just` recipes carry the canonical labels for you.

Configure the installer with `PIPECAT_STT_LABEL`, `PIPECAT_STT_BACKEND`,
`PIPECAT_STT_HOST`, `PIPECAT_STT_PORT`, and `PIPECAT_STT_SOCKET`.
Legacy `KODA_STT_*` aliases remain supported where they exist; `HOST`, `PORT`,
and `AUTH_TOKEN_FILE` have no `KODA_` alias. Uninstall reads the socket path
from the installed plist, not the environment, and waits for the old job to
unload before cleanup. See [Socket agents](#socket-agents-opt-in).

## Everyday commands

| Task | Command |
|---|---|
| List agents, pids, and live endpoints | `just stt-list` |
| Probe one agent | `just stt-status parakeet` |
| Probe a socket path | `just stt-status ~/Library/Caches/pipecat-stt/stt.sock` |
| Stop until the next login; keep the plist | `just stt-disable parakeet` |
| Start an installed agent | `just stt-enable parakeet` |
| Remove the agent and its plist | `just stt-uninstall parakeet` |

`stt-disable` does not persist across login: launchd reloads the saved plist.
Use `stt-uninstall` to remove it, or `launchctl disable` for persistent
suppression while keeping the plist. Check the installed endpoint with
`stt-list` after changing transport or port.

## Multi-backend operation

Each process loads one backend, selected at launch with `--backend`. To run
Whisper and Parakeet together, start two processes on separate ports or socket
paths. Clients use the same wire protocol for both; they select the backend
by choosing its endpoint.

### Per-backend port convention

| ASR | LaunchAgent label | Port |
|---|---|---|
| whisper (`mlx`) | `pipecat.stt-server` | 9565 |
| parakeet | `pipecat.stt-server.parakeet` | 9765 |
| nemotron | `pipecat.stt-server.nemotron` | 9865 |
| kyutai | `pipecat.stt-server.kyutai` | 9665 |
| moonshine | `pipecat.stt-server.moonshine` | 9965 |

Agents bind loopback TCP (`127.0.0.1:<port>`) by default. A Unix socket is
opt-in: set `PIPECAT_STT_SOCKET` when running `just stt-install` (see
[Socket agents](#socket-agents-opt-in)). Set `STT_WS_HOST=127.0.0.1` and `STT_WS_PORT=<port>` on the client.
The client library has no built-in endpoint default. For a socket agent, set
`STT_WS_SOCKET` instead.

Use a different endpoint for any second server, including one launched by a
menu bar app. Two processes cannot own the same listening port.

`just stt-install` and `just stt-uninstall` use the canonical host and port,
ignoring ambient `PIPECAT_STT_HOST` and `PIPECAT_STT_PORT`. Setting
`PIPECAT_STT_SOCKET` (or its legacy alias `KODA_STT_SOCKET`) selects a socket
agent instead.

For a custom TCP endpoint, call the installer with a distinct label:

```bash
PIPECAT_STT_LABEL=pipecat.stt-server.alt PIPECAT_STT_BACKEND=mlx \
  PIPECAT_STT_PORT=9570 scripts/install_stt_agent.sh install
```

The installer defaults to the backend's canonical port when none is set.
`echo` has no canonical port and requires one. The executable port map lives
in `scripts/install_stt_agent.sh`: `port <backend>` prints it without changing
launchd or files. The justfile reads that map, and tests check this table
against it.

`just stt-status <backend>` probes the endpoint in the installed plist (socket
or TCP, with its `--auth-token-file`) and falls back to the canonical port only
when no plist exists; `just stt-list` shows each agent's live endpoint from its
plist. Until an agent is reinstalled, both still show and probe its old socket.

### Kyutai agent and its own venv

`just stt-install kyutai` works like the other backends, with one difference:
kyutai's extra conflicts with the other MLX extras, so `_ensure-extra` syncs it
into a separate `.venv-kyutai` (`UV_PROJECT_ENVIRONMENT=.venv-kyutai uv sync
--frozen --python 3.12 --extra kyutai`), never the shared `.venv`. It needs Python 3.12.
The kyutai agent runs `.venv-kyutai/bin/python` (override with
`PIPECAT_STT_PYTHON`). `just stt-install` picks the interpreter once
(`PIPECAT_STT_PYTHON` when set, else the backend's venv python, honouring a
`venv=`/`kyutai_venv=` override), checks the backend's extra under it and passes
that same interpreter to the installer as `PIPECAT_STT_PYTHON`, so the plist
records the interpreter that was checked. `just stt-enable` checks the
interpreter the installed plist runs (its `ProgramArguments[0]`) and ignores
`PIPECAT_STT_PYTHON` in your environment, so there is nothing to re-export. Only
the venv python can be fixed by a sync; for any other interpreter a missing extra
fails the recipe with the remedy (install the extra into it, or reinstall the
agent).

### Loopback TCP caveats

- **Loopback TCP has no peer-uid check**: any local process (any uid) can
  connect to the port. Unix sockets keep the peer-uid check and the owner-only
  directory chain (see [Trust model](security.md#trust-model-and-socket-security-same-host-uds)).
  A TCP agent has no token by default; add one with `--auth-token-file` if that
  matters on a shared host.
- **A local process that binds the port first can impersonate the server.**
  `--auth-token-file` authenticates clients to the server; it does not
  authenticate the server to clients, so a client that connects to a squatted
  port sends its audio to whatever answers.
- **A taken port makes the agent restart-loop.** If the port is already in use
  the server exits with `EADDRINUSE`, and launchd (`KeepAlive`,
  `ThrottleInterval: 10`) restarts the agent every 10 seconds until the port is
  free. Read `~/Library/Logs/pipecat-stt/<label with dots as dashes>.err`
  (for example `pipecat-stt-server-parakeet.err`; the default whisper agent logs
  to `pipecat-stt.err`) and free the port or reinstall on another one.

### Socket agents (opt-in)

Set `PIPECAT_STT_SOCKET` (alias `KODA_STT_SOCKET`; empty counts as unset) to get
a Unix-socket agent. The plist is byte-identical to the pre-0.5.0 agents:

```bash
PIPECAT_STT_SOCKET="$HOME/Library/Caches/pipecat-stt/stt.sock" just stt-install whisper
```

Socket mode keeps the directory validation, `0700` socket directory, peer-uid
check and lock file described in [Transport security](security.md). It ignores `PIPECAT_STT_HOST`, `PORT` and
`AUTH_TOKEN_FILE` (the installer warns on stderr). Reinstalling a previously-socket agent
without `PIPECAT_STT_SOCKET` switches it to TCP and removes the old socket; a
reinstall onto a different socket path removes the old one too. The old socket
is removed only after the new agent bootstrapped and launchd no longer lists the
old job (up to `PIPECAT_STT_UNLOAD_WAIT` seconds, default 30), and only while no
process holds its `.lock` (a server started by hand can own the path); otherwise
it is left in place with a warning. The `.lock` is never removed (see [Lock files](security.md#single-server-lock-file)). `uninstall` waits the same way,
for a TCP agent too (its server can own the port until it exits): it prints
`uninstalled: <label>` only once launchd no longer lists the job, and otherwise a
warning and `uninstall incomplete (still loaded): <label>` (exit 0 either way).
When a wait times out, no later run can find the old socket (its plist is gone or
replaced), so the warning prints the command to remove it once the old server has
exited (`lsof -- <socket>.lock` lists no process): `test -S <socket> && ! test -L
<socket> && test -O <socket> && rm -f -- <socket>`, with the path shell-quoted. It
is printed only for a path the installer would clean itself (absolute, no `..`,
under `$HOME`, no symlinked directory); for any other recorded path the warning
says it looks unsafe and offers no command.

### Model cache warm-up

`prefetch --json` exposes byte/file progress, and `prefetch --check` inspects
the cache without using the network. The event schema, pinned-revision rules,
local-path validation, and backend-specific constraints are canonical in
[Prefetching and cache checks](backends.md#prefetching-and-cache-checks).
The server never deletes a healthy cached model; the one exception is a
same-size Moonshine file whose CRC32C does not match, which `prefetch` removes
and downloads again. For Moonshine, `prefetch --check --json` reports each
model's directory and size; see
[Removing models](backends.md#removing-models) before cleaning the cache.

## Checking server health

The server answers a `server.status` wire event with its current session
state (queue depth, uncommitted bytes, uptime) and process health (pid,
peak RSS), and, on connect, replies with a `server.hello` carrying
protocol version, audio format, and capabilities. Both `server.hello` and
`server.status` also carry a `backend` object — `{"name": ..., "model": ...}` —
naming the ASR actually behind the endpoint, so a client can verify it rather
than trust the socket path or port. The `status` subcommand wraps that round-trip:

```bash
# Text output (exit 0 on success, 1 on not-reachable/timeout/error)
uv run --no-sync python -m stt_server status --socket-path ~/Library/Caches/pipecat-stt/stt.sock

# Raw JSON for scripting / monitoring
uv run --no-sync python -m stt_server status \
  --socket-path ~/Library/Caches/pipecat-stt/stt.sock --json

# Loopback TCP agent (whisper's canonical port; add --auth-token-file if set)
uv run --no-sync python -m stt_server status --host 127.0.0.1 --port 9565

# Same, through the recipe (any endpoint; an explicit endpoint is required)
just stt-probe --host 127.0.0.1 --port 9565
```

Representative text output:

```text
stt_server status: ok
  backend: parakeet (model: mlx-community/parakeet-tdt-0.6b-v3)
  protocol: 0.1
  audio: pcm16 @ 16000 Hz / 1ch
  capabilities: binary_audio=True base64=True server_vad=False
  session_id: session_abc123
  queue_depth: 0
  uncommitted_bytes: 0
  session_uptime: 0.1s
  pid: 12345
  rss: 1800.3MB (peak)
```

`rss` is peak resident set size from `resource.getrusage`, not current memory
use. It only rises within a process lifetime and resets when the agent restarts;
a higher peak alone does not prove a leak.

The `server.status` reply fields, for scripting against `--json`:

| Field | Type | Meaning |
|---|---|---|
| `type` | string | `"server.status"` |
| `session_id` | string | current session id |
| `queue_depth` | int | 0 or 1 — in-flight decode tasks for this session |
| `uncommitted_bytes` | int | PCM bytes buffered but not yet committed |
| `uptime_seconds` | float | seconds since this session was created |
| `pid` | int | server process id |
| `rss_bytes` | int | peak RSS in bytes, normalised across macOS/Linux |
| `backend` | object | active backend `name` and `model` |

Use this as a preflight before starting a client, in CI smoke tests, or
from a LaunchAgent keepalive script. The existing `--socket-path`/`--host`/
`--port`/`--auth-token-file` endpoint flags work for both `serve` and
`status` subcommands.

## Detailed references

The following sections retain the previous guide's link targets. Follow each
link for the full contract.

### Port squat detection

See [Port squat detection](security.md#port-squat-detection) for warnings,
identity checks, and their limits.

### Token file (replaces env tokens)

See [Token files](security.md#token-file-replaces-env-tokens) for setup,
permissions, content validation, and authenticated reinstalls.

### Single-server lock file

Never delete a live server's `.lock`. See [Lock files](security.md#single-server-lock-file)
for ownership, cleanup, and mixed-version upgrade rules.

### A/B benchmark — Whisper vs Parakeet

See [A/B benchmarks](testing.md#ab-benchmark-whisper-vs-parakeet).

## Smoke tests (`just smoke`)

See [Smoke tests](testing.md#smoke-tests-just-smoke) for `just smoke`, optional
agent decoding, and the launchd harness.

## Supervised smoke test

See [Supervised smoke tests](testing.md#supervised-smoke-test).

## Trust model and socket security (same-host UDS)

See [Unix socket security](security.md#trust-model-and-socket-security-same-host-uds).

### Socket directory permissions (`0700`) — upgrade note

See [Socket directory permissions](security.md#socket-directory-permissions).

### Cross-repo note (Koda)

See [Koda checkout updates](security.md#cross-repo-note-koda).

### Maintainer note — macOS `getpeereid` via `ctypes`

See [macOS peer credentials](security.md#macos-peer-credentials).

### Verifying the boundary locally

See [Local security checks](security.md#verifying-the-boundary-locally).

## Backend-specific tuning

See [Backends and models](backends.md) for defaults, model revisions, and
Whisper hallucination suppression.
