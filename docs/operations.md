# Operations and deployment

> Multi-backend operation, LaunchAgent install, health checks, and backend tuning for [pipecat-local-stt-server](../README.md). The day-to-day `just` operator recipes live in the [README](../README.md#managing-agents-with-just).

## Multi-backend operation

Each server process loads exactly **one** backend, pinned at launch via
`--backend {echo,mlx,parakeet,nemotron,kyutai}`. To run more than one ASR — for example to
A/B-benchmark Parakeet against Whisper — start a second server process on its
own endpoint: a **separate port** (the default for agents) or, for a socket
agent, a separate socket. The V1 wire protocol is unchanged; the only difference
between two ASRs from the bot's perspective is which endpoint it connects to.

### Per-backend port convention

| ASR | LaunchAgent label | Port |
|---|---|---|
| whisper (`mlx`) | `pipecat.stt-server` | 9565 |
| parakeet | `pipecat.stt-server.parakeet` | 9765 |
| nemotron | `pipecat.stt-server.nemotron` | 9865 |
| kyutai | `pipecat.stt-server.kyutai` | 9665 |

Agents bind loopback TCP (`127.0.0.1:<port>`) by default. A Unix socket is
opt-in: set `PIPECAT_STT_SOCKET` when running `just stt-install` (see
[Socket agents](#socket-agents-opt-in)). Point the bot at the agent with
`STT_WS_HOST`/`STT_WS_PORT`.

Why these numbers: the sibling TTS server uses 8665-9365 (its kokoro agent is
8765, so do not copy a TTS port into an STT example), and the macOS menu bar app
owns 9465-9665 (Apple Speech 9465 is the app's own and is not an agent).
Whisper 9565 and kyutai 9665 are the menu bar app's existing numbers; Parakeet
9765 and Nemotron 9865 were confirmed by the menu bar app side on 2026-10-26.
The menu bar app does not yet adopt an occupied port, so an agent and the
app contend for the same port if both are running.

For a TCP agent, `just stt-install` / `just stt-uninstall` always use the
canonical host and port from this table (the recipe strips an ambient
`PIPECAT_STT_HOST`/`PIPECAT_STT_PORT`; the installer owns the socket-vs-TCP rule and
applies its canonical defaults, and with `PIPECAT_STT_SOCKET`/`KODA_STT_SOCKET` set it
installs a socket agent and has no TCP settings to ignore). For a custom host or port, run the installer directly, e.g.
`PIPECAT_STT_LABEL=pipecat.stt-server.alt PIPECAT_STT_BACKEND=mlx
PIPECAT_STT_PORT=9570 scripts/install_stt_agent.sh install`. The installer
defaults the port to the backend's canonical one when `PIPECAT_STT_PORT` is
unset; the `echo` backend has no canonical port and requires one. The installer
holds the one backend-to-port map: `scripts/install_stt_agent.sh port <backend>`
(installer backend names: `mlx`, `parakeet`, `nemotron`, `kyutai`) prints it
without touching launchd or any file, the justfile asks it rather than keeping a
copy, and a test checks this table against it.
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
  directory chain (see [Trust model](#trust-model-and-socket-security-same-host-uds)).
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

### Port squat detection

Decision for [issue #17](https://github.com/vr000m/pipecat-local-stt-server/issues/17):
the trust boundary stays as described above. Loopback TCP is open to every local
user, the token file is opt-in, and token-by-default is deferred until the TTS
server moves. What this adds is a warning, not a gate.

`just stt-list` and `just stt-status <backend>` compare the pid listening on a
TCP agent's port (from `lsof`) with the pid launchd reports for that agent
(`launchctl print`; the recipes hand that answer to `scripts/squat_check.py` on
stdin, never as an argument, because it carries the job's environment block and
a plist from before 0.5.0 keeps its bearer token there, while any local user can
read a process's arguments with `ps`). They warn on stderr, after a 1-second re-check so a restart
in flight stays quiet (the re-check reads the launchd pid before and after its
`lsof` read; a pid that changed in between gives no warning), when:

- the agent is running but a different pid holds the port (the warning names the
  port, the listener pid(s) and command(s), and the launchd pid), or
- the agent is not running and something holds the port
  (`port N held by pid X; label not running`).

A listener counts when its address could block the agent's bind: either side is
a wildcard (`*`, `::`, `0.0.0.0`), the addresses are equal, or the agent's host is
`localhost` and the listener is on `127.0.0.1` or `::1` (IPv6 brackets ignored).

Stdout and exit codes do not change. Socket agents are not checked, and neither
is the canonical-port fallback used when no plist is installed. Only launchd's
own "could not find service" answer counts as not running; when `launchctl
print` fails any other way the check prints one `squat-check: cannot read the
launchd state` note and no warning, so an unreadable state is never taken as
"not loaded" (which would silence the check).

**Limit:** `lsof` run as you lists only your own processes' sockets, so a
squatter running as another user is invisible. No warning means no visible
squatter, not that the port is free.

If you see the warning:

```bash
launchctl print gui/$(id -u)/<label>      # the pid launchd thinks it runs
lsof -nP -iTCP:<port> -sTCP:LISTEN        # who actually holds the port
```

Kill the squatter, or reinstall the agent on another port.

### Token file (replaces env tokens)

`render_stt_plist.py` and `install_stt_agent.sh` now **refuse**
`PIPECAT_STT_AUTH_TOKEN` and `KODA_STT_AUTH_TOKEN` in their environment: an env
token would be written into the plist. Put the token in a file and pass its
absolute path instead:

```bash
install -d -m 700 ~/.config/pipecat-stt                # your own directory; sets mode 700
install -m 600 /dev/null ~/.config/pipecat-stt/token   # then write the token into it
PIPECAT_STT_AUTH_TOKEN_FILE=$HOME/.config/pipecat-stt/token \
  PIPECAT_STT_BACKEND=mlx scripts/install_stt_agent.sh install
```

In TCP mode the installer checks the file before writing the plist: a regular
file (not a symlink), owned by you, with no group/other permission bits and no ACL
allow entry, in a directory chain (symlinks resolved) that only you or root own and
that no one else can write to. A group-writable directory passes only when root
owns it and its group is `wheel` or `admin` (whose members can act as root anyway).
A sticky bit (`t` or `T` in `ls -ld`) is no exception, so a token file under
`/tmp` is refused: the sticky bit stops others deleting your file, but once the
name is gone (macOS empties `/tmp` at boot) anyone can create it before the agent
starts, and the server opens the path afresh at every start. ACLs count too: a directory on the path
with an ACL allow entry carrying a write-class right (`write`, `append`, `add_file`,
`add_subdirectory`, `delete`, `delete_child`, `writeattr`, `writeextattr`,
`writesecurity`, `chown`) for anyone fails the install, as does any allow entry on
the file itself; deny entries are fine. `ls -le` shows them and `chmod -N` removes
them. The plist records the resolved path, the one that was checked, so a
symlinked directory in the path you gave cannot be repointed later. The content must be what the server accepts: it reads the file as
UTF-8 and strips surrounding whitespace, and the result must be one token of
visible ASCII characters with no internal whitespace. An empty file would disable
auth, and a token with a space, tab or newline left inside it after stripping, or a
non-ASCII character, can never match an `Authorization` header, so both fail the
install. Leading and trailing whitespace, including the trailing
newline an editor or `echo` adds, is stripped and fine. A missing file would
crash-loop the agent, so the installer refuses that too. The probe (`status`)
stays lenient for a client-side token file.

**`serve --auth-token-file` enforces the same policy at every start.** All of the
rules above, file and content alike, are one implementation,
`stt_server.auth.load_token_file`, which the installer's preflight and `serve`
both run, so an installed agent never trips over them. `serve` opens the file
with `O_NOFOLLOW`, checks the descriptor it opened (a regular file owned by the
user running the server, no group/other bits, no ACL allow entry) and then walks
its resolved directory chain, and reads the token from that same descriptor. The
path must be absolute. This applies to a server you launch by hand, or one a
supervising app (Koda, the menu bar app) launches with `--auth-token-file`: a
file the installer would refuse now stops startup with exit 1 and a one-line
message naming the file, the problem and the fix, for example

```text
stt_server: auth token file '/Users/me/token' has mode 644; it must not be group/other accessible; fix: chmod 600 '/Users/me/token'
```

To fix a refused file: `chmod 600 <file>` for group/other bits; `chmod -N <file>`
(or `<dir>`) for an ACL; for a parent directory others can write (a shared
directory, `/tmp`, a group-writable folder) move the file into a directory only you
can write, e.g. `install -d -m 700 ~/.config/pipecat-stt`, and pass the new
absolute path; recreate a file someone else owns as yourself (`install -m 600
/dev/null <file>`, then write the token); pass the real file, not a symlink to it.

A reinstall never silently drops auth: if the installed plist carries a token (a
pre-0.5.0 env token or `--auth-token-file`) and a TCP install sets no
`PIPECAT_STT_AUTH_TOKEN_FILE`, the install fails. Set the token file, or run
`uninstall` first to install without a token. The installer reads the installed
plist's flags with the server's own grammar (`serve` takes full flag names only),
so `--auth-token-file /x` and `--auth-token-file=/x` both count as a token. An
installed plist the installer
cannot read (malformed, truncated, or the reader's interpreter missing: `install`
runs it under the venv python, `uninstall` under the system `python3`) also fails the install,
since it cannot tell whether that agent required a token; `uninstall` still
removes such an agent but leaves any socket in place, with a warning.

The plist then passes `--auth-token-file <path>` to the server. `just
stt-install` forwards the caller's environment, so exporting
`PIPECAT_STT_AUTH_TOKEN_FILE` before the recipe has the same effect. Clients
send the token with `STT_WS_TOKEN`.

### Socket agents (opt-in)

Set `PIPECAT_STT_SOCKET` (alias `KODA_STT_SOCKET`; empty counts as unset) to get
a Unix-socket agent. The plist is byte-identical to the pre-0.5.0 agents:

```bash
PIPECAT_STT_SOCKET="$HOME/Library/Caches/pipecat-stt/stt.sock" just stt-install whisper
```

Socket mode keeps the directory validation, `0700` socket directory, peer-uid
check and lock file described below. It ignores `PIPECAT_STT_HOST`, `PORT` and
`AUTH_TOKEN_FILE` (the installer warns on stderr). Reinstalling a previously-socket agent
without `PIPECAT_STT_SOCKET` switches it to TCP and removes the old socket; a
reinstall onto a different socket path removes the old one too. The old socket
is removed only after the new agent bootstrapped and launchd no longer lists the
old job (up to `PIPECAT_STT_UNLOAD_WAIT` seconds, default 30), and only while no
process holds its `.lock` (a server started by hand can own the path); otherwise
it is left in place with a warning. The `.lock` is never removed (see below). `uninstall` waits the same way,
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

### Single-server lock file

Each Unix socket gets a sibling lock file, `<socket>.lock` (for example
`~/Library/Caches/pipecat-stt/parakeet.sock.lock`). A server takes an exclusive
`flock` on it before loading the model, so a second server for the same socket
fails in milliseconds with `EADDRINUSE` instead of after a multi-GB load, and a
live server whose listen backlog is full is never mistaken for a stale socket.

- Mode `0600`, owned by the server user, set on the open file so the umask
  never matters. A pre-existing lock file you own that opens read-write is set
  to `0600` whatever its mode. One you own but cannot open read-write (e.g. mode
  `000` or `0400`) is reported as `EACCES` with the remedy: `chmod 600` it, or
  remove it while no server runs. One owned by another user, a symlink or a
  non-regular file (e.g. a FIFO) makes the server refuse to start.
- The kernel releases the lock when the server process exits for any reason
  (including `SIGKILL` and the immediate exit on a stop during startup), so it
  cannot go stale.
- The server never unlinks it: deleting it while a server runs, or between two
  starters, would let two servers own one socket. Leaving it behind is
  harmless. Clients never open it. The installer never unlinks it either: its
  check drops the lock when it exits, so unlinking the file afterwards would let
  a starter that just locked it run beside a later one that creates a new file.
  On a transport change and on `uninstall` of a socket agent it removes only the
  stale socket, once launchd no longer lists the old job and no process holds
  the lock. A held lock (or one it cannot check) leaves the socket in place,
  with a warning. The check and the unlink are one step under a shared lock on
  the `.lock`, so a server starting at that moment waits for it (a starter
  retries a contended lock for 250 ms) and binds only after the unlink; it never
  loses its fresh socket. With no `.lock` there, nothing is locked (the installer
  never creates one): a `.lock` that exists when the installer re-reads the
  entry just before the unlink is honoured (a held one keeps the socket), but
  one created after that re-read is not seen, so a starter in that window of a
  few syscalls can lose its fresh socket. A 0.3.x server takes no lock at all,
  and a server still holding a `.lock` inode an older installer unlinked is not
  seen either.
- `serve --supervised` checks it before binding (a held lock is reported as
  `EADDRINUSE`). The check takes a shared lock for an instant; a starter retries
  a contended lock for 250 ms, so the check never makes a real start fail. It
  opens the file non-blocking and treats anything but a plain file you own as
  "not held" (the real start then refuses it), so a planted FIFO cannot hang it.
- The lock stays held while the server still has work that can touch the
  socket or the backend:
  - **Work that outlived its grace or bound** keeps it until that work
    finishes: a session task that ignores cancellation past the shutdown
    grace, a decode thread still running on the GPU after its task was
    cancelled, `backend.close()` running past its bound (the drain timeout,
    10 s), a listener close that has not completed (a connection handler that
    will not finish keeps it open past its grace), or a startup / readiness
    step that ignored `serve`'s cancel (a stop, or `serve` itself being
    cancelled). This holds also when the shutdown itself was cancelled.
  - **A cleanup that raises** (an orphaned listener whose close failed) keeps
    it until the process exits.
  - **A failed start that left work running** keeps it the same way; that
    server stays `stopping` and is single-use.
  - **A failed start that left nothing running** releases it: the server
    returns to `new` and may be retried.

  In-process callers await `TranscriptionServer.wait_stopped()` for the lock
  to be free: `shutdown()` returning does not guarantee it.
  `python -m stt_server serve` exits the process at that point, which
  releases it. A failure in flight at that exit (for example
  `backend.close()` raising) is still printed and, under `--supervised`,
  emitted as the `error` event, with exit status 1.
- **Mixed versions during an upgrade.** The lock only protects against servers
  that take it. A server started from a release before the lock existed holds
  none, and on macOS its socket answers `ECONNREFUSED` when its listen backlog
  is full, which the preflight reads as stale: a new server could then replace
  that live server's socket. Stop old servers (`just stt-disable <backend>`, or unload their
  LaunchAgents) before starting upgraded ones on the same socket path.

A bare install binds TCP, and a client with no endpoint env resolves nothing
(the library has no built-in default), so set `STT_WS_HOST=127.0.0.1` and
`STT_WS_PORT=<port>` on the client. Selecting Parakeet is a flip of
`STT_WS_PORT` to 9765. The flip is **bot-wide** — in the dual-input bot both the
Me and Them branches connect to the same resolved endpoint, so both arms always
use the same ASR. A socket agent is selected with `STT_WS_SOCKET` instead. See
`.env.example` for the client-side configuration.

### Installing agents

> **Backend dependencies.** A backend imports its ASR library lazily at startup,
> and a bare `uv run` / `uv sync` prunes the optional extras, so an installed
> agent can crash-loop on `ModuleNotFoundError`. `just stt-install <backend>` /
> `just stt-enable <backend>` ensure the matching extra is present
> (`uv sync --extra <backend> --inexact`, additive — it won't prune other
> backends; kyutai goes to its own `.venv-kyutai`); set
> `PIPECAT_STT_SKIP_DEP_SYNC=1` to manage extras yourself. If an
> agent does crash on a missing import, the server now exits with an actionable
> `stt_server: the '<extra>' extra is not installed … run: uv sync --extra
> <extra> --inexact` instead of a bare traceback.

```bash
just stt-install whisper      # 127.0.0.1:9565
just stt-install parakeet     # 127.0.0.1:9765 — warm the model cache first (below)
just stt-install nemotron     # 127.0.0.1:9865
just stt-install kyutai       # 127.0.0.1:9665, own .venv-kyutai
just stt-list                 # agents, state, pid, live endpoint and backend
just stt-status               # no argument: same as stt-list
just stt-status parakeet      # probe the installed plist's endpoint (canonical port 9765 if no plist)
just stt-status ~/Library/Caches/pipecat-stt/stt.sock   # a path is probed as --socket-path
just stt-probe --host 127.0.0.1 --port 9565   # status probe of any endpoint
```

Warm the ~1.5 GB Hugging Face model cache for Parakeet FIRST: a cold first
launch downloads it under `KeepAlive` + `ThrottleInterval=10` and launchd may
throttle-loop the agent before the download finishes:

```bash
uv sync --extra parakeet
.venv/bin/python -m stt_server prefetch --backend parakeet
```

`scripts/install_stt_agent.sh` is parameterised by `PIPECAT_STT_LABEL` /
`PIPECAT_STT_BACKEND` / `PIPECAT_STT_HOST` / `PIPECAT_STT_PORT` /
`PIPECAT_STT_SOCKET` (the legacy `KODA_STT_*` names, where they exist, are still
honoured as deprecated aliases; `HOST`, `PORT` and `AUTH_TOKEN_FILE` have no
`KODA_` alias) so several LaunchAgents can coexist without plist or log
collisions. The script manages exactly **one** agent per invocation, identified
by `PIPECAT_STT_LABEL` — there is no registry or "all" mode. To run any
subcommand (`uninstall`/`start`/`stop`/`restart`/`status`/`logs`) against a
non-default agent you must re-export its `PIPECAT_STT_LABEL` (and
`PIPECAT_STT_BACKEND` when it is not the default); `uninstall` removes the socket
named in the *installed* plist, not the env (once the job has unloaded, as above). A default-env invocation always
targets the default `pipecat.stt-server` agent. Prefer the `just` recipes, which carry
the label for you. See the recipe in the `install_stt_agent.sh` header.

### A/B benchmark — Whisper vs Parakeet

With both agents installed and live, `scripts/benchmark_asr_ab.py`
replays a corpus of utterances through **both** servers and reports
per-utterance Word Error Rate (WER) + decode latency, plus aggregates. It is
a pure V1 client — no protocol surface added — and a one-off operator tool
(no REST counterpart, not a CI gate).

```bash
# 0.5.0 default agents are loopback TCP, so pass the URIs. --whisper-uri /
# --parakeet-uri override the socket flags.
uv run --no-sync python scripts/benchmark_asr_ab.py --corpus path/to/corpus \
    --whisper-uri ws://127.0.0.1:9565 --parakeet-uri ws://127.0.0.1:9765

# Socket agents: with no endpoint flags the script defaults to
# ~/Library/Caches/pipecat-stt/stt.sock and .../parakeet.sock
# (override with --whisper-socket / --parakeet-socket).
uv run --no-sync python scripts/benchmark_asr_ab.py --corpus path/to/corpus

# Write a full JSON report alongside the console summary.
uv run --no-sync python scripts/benchmark_asr_ab.py --corpus path/to/corpus \
    --whisper-uri ws://127.0.0.1:9565 --parakeet-uri ws://127.0.0.1:9765 \
    --json-out benchmarks/results/asr_ab.json
```

The corpus is a directory of `<stem>.wav` (16 kHz mono PCM16) + `<stem>.txt`
reference-transcript pairs, named explicitly on the command line. The
benchmark **fails fast** if only one of the two endpoints answers — it never
silently benchmarks a single ASR. The corpus is never baked into the script;
`docs/benchmarks` / `~/koda-data` JSON corpora carry real names and
financials, so the script refuses a `--corpus` under those roots unless
`--allow-pii-corpus` is passed. Use a synthetic or consented-recording corpus
and keep it outside the repo.

### Prefetch progress semantics

`python -m stt_server prefetch --json` emits `progress{repo, downloaded_bytes,
total_bytes, files_done, files_total}` events. They describe only the files this
run still has to download (the same meaning as `tts_server prefetch`), so a
supervisor can show `downloaded_bytes / total_bytes` directly:

- `total_bytes` / `files_total`: files not already in the Hugging Face cache when
  the fetch started (a fully cached repo reports `0` / `0`).
- `downloaded_bytes`: bytes on disk for those files, including partial
  `.incomplete` blobs left by an interrupted earlier run.
- `files_done`: how many of those files have finished.
- The last `progress` before `done` always has `downloaded_bytes == total_bytes`
  and `files_done == files_total`.

For the four default models, `prefetch` fetches the commit pinned in
`PINNED_REVISIONS` (`stt_server/backends/__init__.py`), so the `start`/`check`
events carry that SHA as `revision`; any other `--model` reports `revision: null`
and follows the repo's `main`. The server loads the same commit, so a fetched
snapshot is the one it serves.

The `kyutai` backend reads its weight filenames from the repo's `config.json`,
so quantized (`.q4`/`.q8`) repos fetch only the files the loader will open. Each
name must be a plain filename (no `/`, `..` or glob characters), or prefetch and
the loader refuse it.

A `prefetch` that fails with nothing network-related going wrong (an incomplete
snapshot, a corrupt or incomplete `config.json`, a local model directory that is
missing files) reports `code: ModelInvalidError`, not `ModelDownloadError`.

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
uv run --no-sync python -m stt_server status --socket-path ... --json

# Loopback TCP agent (whisper's canonical port; add --auth-token-file if set)
uv run --no-sync python -m stt_server status --host 127.0.0.1 --port 9565

# Same, through the recipe (any endpoint; an explicit endpoint is required)
just stt-probe --host 127.0.0.1 --port 9565
```

Representative text output:

```
stt_server status: ok
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

`rss` is **peak** resident set size from `resource.getrusage` — it
climbs monotonically within a process lifetime and resets on
LaunchAgent restart. Useful for leak detection (peak only grows when
a leak is actually growing), not for real-time memory monitoring.

The `server.status` reply fields, for scripting against `--json`:

| Field | Type | Meaning |
|---|---|---|
| `type` | string | `"server.status"` |
| `session_id` | string | current session id |
| `queue_depth` | int | 0 or 1 — in-flight decode tasks for this session |
| `uncommitted_bytes` | int | PCM bytes buffered but not yet committed |
| `uptime_seconds` | float | seconds since this session was created |
| `pid` | int | server process id |
| `rss_bytes` | int | peak RSS in bytes, normalized across macOS/Linux |

Use this as a preflight before starting a client, in CI smoke tests, or
from a LaunchAgent keepalive script. The existing `--socket-path`/`--host`/
`--port`/`--auth-token-file` endpoint flags work for both `serve` and
`status` subcommands.

## Smoke tests (`just smoke`)

`just smoke [--agents] [--launchd]` runs the local checks in one command and
prints one table of PASS, FAIL or SKIPPED with a reason per step; the exit status
is 1 if any step FAILED (SKIPPED never fails). It runs `.venv/bin/python
scripts/smoke.py`, never `uv run` (which prunes the backend extras).

| Step | What it runs |
| --- | --- |
| `supervised` | `scripts/smoke_supervised.py --backend echo` (see below) |
| `peercred` | `scripts/smoke_peercred.py`; SKIPPED when no second uid is reachable via passwordless `sudo` (the same-uid leg still ran) |
| `token-file` | an echo server on a spare port with a token file; a request without the token (or with a wrong one) must get `401`, with it `101`; each request goes over a connection whose server end `lsof` shows is that server's own process |
| `agents` | `--agents` only: decodes a clip through every installed `pipecat.stt-server*` agent, using the endpoint in its plist, read once (its `--port` parsed as the server parses it; installed plists only: a loaded job without a plist is not decoded; the `launchd` harness's real-agent snapshot covers those). The token is the one the agent's server uses: its `--auth-token-file`, read under the server's own token-file policy (a symlink, a FIFO, a file with group or other access, or a token the server would refuse makes that agent FAIL with nothing sent), else `PIPECAT_STT_AUTH_TOKEN`, else `KODA_STT_AUTH_TOKEN` from the plist's environment; it reaches the decode process on stdin, never on its command line. It is sent only over a connection whose server end was checked: one `launchctl print` gives the agent's pid and `runs` counter; the decode opens the connection and sends nothing until the process holding the server end of that same connection is that pid (`LOCAL_PEERPID` for a socket agent, `lsof` for a TCP agent) and a second `launchctl print` still shows the same pid and `runs`; then the token and the clip go over that connection. After the decode a fresh `launchctl print` must still show the same pid and `runs` for a PASS. A job that is not running, a server end held by another process (or not visible to `lsof`, as another user's socket is), a restart in between, or an answer that cannot be read makes that agent FAIL, and when the check refused, no token was sent. Not covered: a process running as root. Read-only: two `launchctl print` calls per agent in the runner (the identity, then the fresh one after the decode) and one in the decode (the re-read once the server end is verified), no bootout, bootstrap, kickstart or installer call. The clip is generated at run time with `say` and `afconvert` (16 kHz mono) and the transcript must contain the spoken key word; SKIPPED when there are no installed agents or `say` is absent |
| `launchd` | `--launchd` only: `scripts/smoke_launchd.py` on the spare label `pipecat.stt-server.smoke` and spare port 9998 |

`--launchd` is opt-in and macOS-only (SKIPPED elsewhere and when `CI` is set).
The harness refuses to run unless both `PIPECAT_STT_LABEL` and `KODA_STT_LABEL`
name the spare label; the runner sets both itself, so do the same when calling
`scripts/smoke_launchd.py` directly. The individual scripts below still run on
their own, but `just smoke` is the entry point.

Every step has a timeout (`scripts/smoke.py`): `supervised` 600 s, `peercred`
180 s, `token-file` 90 s; in `agents`, 60 s for each `say` / `afconvert` call
and 180 s for each decode. The `launchd` step's timeout is the harness's own
worst case, teardown included (`smoke_launchd.run_budget`, summed from the
bounds it waits on, for the installed agents: about 31 minutes with none, plus
about 2 minutes per agent). A
timed-out step gets SIGTERM, and Ctrl-C (or a SIGINT or SIGTERM sent to the
runner) sends the running step SIGINT; the step then has 180 s to clean up (the
`launchd` step: the harness's whole teardown, `smoke_launchd.teardown_budget`,
about 3 minutes plus 25 s per agent). Once the step has exited, or that grace ran out, its
whole process group is killed, before the step's own process is reaped, so the
kill can only reach that step's group. A further signal to the runner during
that grace is held until the step has stopped and then stops the run: no later
step starts, the interrupted step is FAIL, the rest are SKIPPED, and the table
still prints. A step that crashes is a FAIL row and the run goes on to the next
step.

Inside the launchd harness each installer call runs in its own process group,
with a 120 s timeout: a SIGINT or SIGTERM during an install kills the
installer's whole group before teardown starts, so teardown never runs beside a
half-finished install. An installer group that cannot be shown stopped (its
kill failed, or its leader was not reaped) is a failure that stops the
remaining checks, never a result. The teardown's uninstall is not interrupted.
The harness runs everything (the installer included) with a scrubbed
environment: `HOME`, `USER`, `TMPDIR`, the spare label and log-dir variables
and the fixed `PATH=/usr/bin:/bin:/usr/sbin:/sbin`; `BASH_ENV`, `ENV`,
exported shell functions, `PYTHON*`, `DYLD_*` and `LD_*` never reach it, and
bash is `/bin/bash`. An agent
gets 60 s to answer after each install, and the collision check polls its log
for 30 s.

The harness's proof that it left the real agents alone is a snapshot of each
one (every installed `pipecat.stt-server*` plist and every loaded such job):
from one `launchctl print` of the job, its loaded state, pid, `runs` counter
(which moves on a restart even when the pid is reused) and the endpoint the job
is serving; a digest of its plist (the plist is only the intended endpoint and
can change without a reload) and the port recorded in those same bytes (parsed
as the server parses `--port`, so `+9998` or `09998` is 9998; a value the
server would refuse is unreadable and refuses the run); and the
pid holding the job's endpoint (a socket job's `<socket>.lock`). A job with no
pid serves nothing; anything it cannot read on a running job refuses the run
before the first install, a running job whose `launchctl print` has no `runs =`
line included (the live run is what confirms launchctl prints one). The
baseline is taken before the guard (which refuses every port a real job serves
or a real plist recorded, both taken from the baseline) and compared before every check and after teardown; a difference stops
the remaining checks. A real agent that was not loaded, then loaded and unloaded
again between two of those comparisons with its plist untouched, leaves nothing
to compare. A real agent that restarts on its own during the run (a crash loop)
fails the comparison too: fix it before the run.

The token check sends its throwaway token (and a wrong one) only over
connections whose server end `lsof` shows is the spare job's pid, with the same
pid and `runs` before and after; it waits for the agent with requests that carry
no token. The collision check's "agent up" and every rollback transport are
proved the same way: their no-token request goes over a connection whose server
end is the spare job, so a listener that is not the spare cannot pass them.

Teardown removes the spare socket and its `.lock` only while it holds that
`.lock`'s `flock` itself, so no live server can own them; anything it cannot prove
is its own and unheld is left and reported. A removal that fails, or anything
still there afterwards, makes the `teardown` row FAIL. On a FAIL the harness
prints one `FAIL: launchd check failed: <check>: <detail>` line per failed row,
and `just smoke` shows those in its `launchd` row.

Only one `smoke_launchd.py` run at a time: it takes an exclusive lock on
`~/Library/Caches/pipecat-stt-smoke-launchd.lock` (in the account home, not
`$HOME`) before touching anything, and a second run is refused. The lock is
released when the run exits; the file stays.

The launchd harness uses spare port 9998 by default; when that port is busy, run
it on another spare port with `scripts/smoke_launchd.py --port <port>` (the guard
still refuses a canonical port, a port recorded by an installed or loaded agent,
and a port that is already listening). Its socket-rollback check always uses
the fixed spare socket `$HOME/Library/Caches/pipecat-stt-smoke-launchd/spare.sock`
(`PIPECAT_STT_SOCKET` is not read), so it can never be a real agent's socket.

Before it installs anything the harness refuses leftover spare state, and
prints the cleanup commands (shell-quoted, safe to paste): a loaded spare job, a
spare plist under `$HOME` or under the account home's `~/Library/LaunchAgents`
(the label is one launchd job per user, whatever `HOME` says), the spare socket
or its `.lock`, and spare logs. An installer `warning:` line fails the install
or the teardown unless it is one of three known harmless ones (socket mode
ignoring TCP settings, a dropped token for a socket agent, a retried bootstrap).

**What has been verified live.** The checks themselves were run by hand at 0.5.0
(token-file refusal and acceptance on a spare port without launchd, a decode
through each real agent, `smoke_supervised.py --backend echo`, same-uid
peer-cred). New in 0.5.1: the launchd harness (`scripts/smoke_launchd.py`) ran
live once, on 2026-10-28, against the spare label and port 9998. The port
collision (with the squat warning), the socket rollback, the token file and the
teardown all passed, each answer's server end was verified against the spare
job, and the real agents were unchanged afterwards. That run confirms that
`launchctl print` prints a `runs =` line and that `LOCAL_PEERPID` and `lsof`
identify a launchd job's server end. Still covered only by stubbed tests (stub
`launchctl`, `lsof` and `id`, a temp home): the `smoke.py` code that now
automates the 0.5.0 checks — the `token-file` child, the `--agents` decode and
the `peercred` SKIPPED mapping — and the `just smoke --launchd` wiring around
the harness. Treat those results as unverified until a live
`just smoke --agents --launchd` run; a FAIL there may be a harness bug rather
than an agent fault.

## Supervised smoke test

`scripts/smoke_supervised.py` drives `serve --supervised` the way a menubar or
other supervising app does: it spawns real server processes and checks the JSON
`ready`/`error` events, stdin-EOF and SIGTERM stops (including during warm-up),
a parent that dies, a second instance on a live socket, a stale socket after
`kill -9`, concurrent clients, and a real transcription. It is opt-in (not in CI
or pytest) and needs the backend's extra installed; run it with that
environment's interpreter:

```bash
.venv/bin/python scripts/smoke_supervised.py --backend echo
.venv/bin/python scripts/smoke_supervised.py --backend mlx \
    --model mlx-community/whisper-large-v3-turbo-q4
```

It exits 0 only if every scenario passes, and 2 (running nothing) when the
model is not cached: `prefetch` it first.

## Trust model and socket security (same-host UDS)

The Unix-domain-socket transport is hardened on the server side by two
independent measures. They are layered, not redundant:

1. **Primary filesystem boundary — owner-only ancestor chain.** Before bind,
   the server walks every directory from the socket's parent up to and
   including the trusted root (`$HOME`) and **refuses to start** unless each
   component is owned by the running uid and is not group/other-writable
   (sticky-bit directories excepted). The walk is over the **literal** socket
   path clients traverse — not a symlink-resolved one — and **rejects any
   symlink component**: resolving past a symlink would verify the target's chain
   while a foreign uid who can write a symlinked-but-writable lexical ancestor
   could repoint it after startup and hijack the path clients connect to. This
   makes the socket *un-plantable*: an
   attacker cannot `unlink()` our socket and `bind()` their own, because they
   have no write access on any directory that could replace it. On stock macOS
   the chain (`~/Library/Caches/pipecat-stt` → `~/Library/Caches` → `~/Library`
   → `~`) already satisfies this once the socket dir is `0700`. A foreign uid
   cannot even *traverse* to the socket (no `+x` → `connect()` fails `EACCES`
   at path resolution), so this layer alone closes the same-host foreign-uid
   vector at the filesystem layer.
2. **Kernel-authoritative backstop — peer-credential auth.** On every UDS
   connection the server reads the peer's uid from the kernel
   (`SO_PEERCRED` on Linux, `getpeereid(2)` on macOS) and rejects any peer
   whose `uid != server uid` with a pre-handshake `403` before the WebSocket
   handshake completes. The uid is kernel-supplied and unforgeable, so this
   holds even if the filesystem perms are ever looser than intended. It is
   *defense-in-depth behind* the filesystem boundary — not a strictly-stronger
   replacement for it. Every failure path (resolver returns `None`, resolver
   raises, missing transport socket, unsupported platform) **fails closed**
   (rejects), logging one warning.
3. **Bearer token — TCP/remote only.** For UDS the token is redundant (the
   filesystem boundary + peer-cred both dominate it), so it is kept but not
   relied on. For TCP/remote — which has neither a file-permission boundary nor
   peer credentials — the bearer token remains the trust mechanism, alongside
   the Origin check. This change does not weaken the TCP path.

**Same-uid precondition.** Peer-cred auth assumes the client and server run as
the **same uid** (the per-user LaunchAgent deployment satisfies this). A uid
mismatch is treated as **reject**, not warn-and-allow. A future deployment that
runs the server as a daemon user and the client as the logged-in user would be
correctly rejected and would need coordination — but still no client code
change.

### Socket directory permissions (`0700`) — upgrade note

`scripts/install_stt_agent.sh` validates a custom `PIPECAT_STT_SOCKET` against
the same rules the server enforces — absolute path, under `$HOME`, no symlink
component — **before** any `mkdir`/`chmod`. A path the server would reject fails
the install cleanly with no filesystem mutation. It then creates the socket's
parent directory `0700` from birth (`mkdir -m 700`) and also `chmod 700`s it,
which **self-heals** a pre-existing `0755` directory left by an older install.
Because the new startup check refuses to bind against a group/other-writable ancestor:

- **Upgrading an existing host:** re-run `install_stt_agent.sh` (it repairs the
  dir in place), or manually `chmod 700 ~/Library/Caches/pipecat-stt`.
- **Custom socket paths:** a `PIPECAT_STT_SOCKET` / `KODA_STT_SOCKET` /
  `STT_WS_SOCKET` pointing outside `$HOME` (e.g. `/tmp`, `/var`, a shared dir)
  now makes the server **refuse to start** with
  `socket directory <path> is not under the trusted root <home>`. Keep custom
  socket paths under `$HOME`.

If the server refuses to start, the error is printed as `stt_server: <message>`
on stderr (exit 1) — grep the agent's `.err` log for
`is not under the trusted root` or `is group/other-writable`.

### Cross-repo note (Koda)

Koda consumes this repo two ways: it pins the **Python client** at an immutable
git SHA, and it runs the **server** from this repo's working **checkout at
HEAD** (not a tagged/PyPI release). Consequences for this hardening:

- **No version bump gates it, and no client pin bump is needed.** The client
  library and wire protocol are unchanged, so Koda's pinned client stays valid
  (a pin bump is only required when the imported client/protocol surface
  changes). PyPI releases are irrelevant to Koda's coupling.
- **The trigger is the checkout update, not the merge.** The hardening lands on
  a Koda host the moment its server checkout is updated — decoupled from
  merging to `main` (which touches no machine until something pulls). Fold the
  coordination into that window: **re-run `install_stt_agent.sh`** so the socket
  dir is `0700`, and confirm `KODA_STT_SOCKET` (if set) stays under `$HOME`.
- Because the startup check is **fail-closed**, the "runtime change takes effect
  on checkout update" property is now a startup risk if perms/path are not
  squared away in the same step — that is the entire content of the
  coordination.

### Maintainer note — macOS `getpeereid` via `ctypes`

macOS has no `socket.SO_PEERCRED`, so the resolver in `stt_server/_peercred.py`
calls `getpeereid(2)` through `ctypes`. Two details are load-bearing and must
not be dropped: `uid_t`/`gid_t` are `c_uint32` on Darwin, and the libc function
must have explicit `argtypes`/`restype` set (a wrong width or signature can fail
*open* by comparing equal). libc is loaded with `use_errno=True`. The
`socketpair()` unit test asserting `peer_uid() == os.geteuid()` is the gate that
catches a width/signature regression — keep it.

### Verifying the boundary locally

Two local checks exercise what single-uid CI cannot:

- `just smoke-peercred` (also part of `just smoke`; `scripts/smoke_peercred.py`) — opens N concurrent
  same-uid sessions (regression guard) and, when a second local uid is reachable
  via passwordless `sudo`, drives a foreign-uid connection that must be rejected
  `403`. The cross-uid leg skips cleanly when no second uid is available.
- `scripts/verify_peercred_crossuid.py` — a stdlib-only verifier (no venv /
  `websockets`) that drives a probe as **both** the owning uid (expect `101`) and
  `nobody` (expect `403`) against one permissive socket. Same socket + perms,
  only the uid differs, so a `101`-vs-`403` split proves peer-cred — not the
  filesystem — is the discriminator. Run with `sudo -v && uv run --no-sync python
  scripts/verify_peercred_crossuid.py`. It binds under `/tmp` (world-traversable)
  and asserts every ancestor is traversable, so a filesystem-boundary failure is
  reported as inconclusive rather than masquerading as a peer-cred result.

## Whisper hallucination suppression (MLX backend)

The MLX Whisper backend forwards four decode-time knobs to
`mlx_whisper.transcribe()` to suppress the cascading-repetition failure
mode (hundreds of `subscription subscription…` lines emitted as a single
segment). Defaults match OpenAI's reference Whisper EXCEPT
`condition_on_previous_text`, which we disable: feeding the previous
chunk's emitted text back as a decoder prompt creates a self-amplifying
loop on hallucinated tokens. Bool parser accepts `1`/`true`/`yes`/`on`
(case-insensitive); anything else — including `False`, `0`, empty, or
unset — is `False`.

Each variable below is canonical (`PIPECAT_STT_*`); its legacy `KODA_STT_*`
alias is still honoured (canonical wins if both are set). For these numeric
(and the boolean) knobs precedence is *presence-based*: a present-but-empty
canonical value wins and resolves to the default rather than falling through
to a set legacy alias — so blanking the canonical reliably overrides the
alias. (String knobs like the LaunchAgent label instead skip an empty
canonical and fall through to the alias.)

| Variable (canonical) | Default | Description |
|---|---|---|
| `PIPECAT_STT_WHISPER_CONDITION_ON_PREVIOUS_TEXT` | `False` | Condition each chunk's decode on the previous chunk's text. Load-bearing — leave `False`. |
| `PIPECAT_STT_WHISPER_COMPRESSION_RATIO_THRESHOLD` | `2.4` | Flags zlib-compressible (repetitive) output as a failed segment, forces re-decode. |
| `PIPECAT_STT_WHISPER_LOGPROB_THRESHOLD` | `-1.0` | Flags low-confidence segments. |
| `PIPECAT_STT_WHISPER_NO_SPEECH_THRESHOLD` | `0.6` | Drops silence segments before they get a chance to hallucinate. |

After decode, `_decode_sync` runs a degenerate-output filter
(`stt_server.text_quality.is_degenerate`) on each segment. Segments where
the dominant case-folded unigram exceeds the ratio threshold AND the
segment has at least the minimum token count are replaced with an empty
string (and a `mlx_whisper.degenerate_dropped` warning is logged).
Defaults are calibrated against the existing transcript corpus —
p99 = 0.36, p99.5 = 0.40 — so backchannels ("yeah yeah yeah") and other
legitimate high-repetition paragraphs are not flagged.

| Variable (canonical) | Default | Description |
|---|---|---|
| `PIPECAT_STT_WHISPER_DEGENERATE_TOKEN_RATIO` | `0.40` | Drop a segment whose dominant unigram exceeds this share of all tokens. Pinned above the corpus p99.5; raise toward `0.45` first if the monitoring audit shows >1% of segments dropped. |
| `PIPECAT_STT_WHISPER_DEGENERATE_MIN_TOKENS` | `10` | Minimum token count before the ratio check fires — short utterances with one repeated word are not flagged. |

`PIPECAT_STT_WHISPER_DEGENERATE_*` are the canonical names. The earlier
`KODA_TEXT_QUALITY_DEGENERATE_TOKEN_RATIO` /
`KODA_TEXT_QUALITY_DEGENERATE_MIN_TOKENS` names, and the original
`KODA_STT_WHISPER_DEGENERATE_TOKEN_RATIO` /
`KODA_STT_WHISPER_DEGENERATE_MIN_TOKENS` names from the initial ship, are
all still honoured as deprecated backward-compat aliases (canonical wins if
several are set). New deployments should prefer the `PIPECAT_STT_*` names.

These decode-time defences were calibrated against the original transcription
corpus; a consumer's cleanup stage can pair them with a short-circuit on
degenerate input and a symmetric output guard against same-length degenerate
rewrites.
