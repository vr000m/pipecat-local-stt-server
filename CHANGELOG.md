# Changelog

All notable changes to `pipecat-local-stt-server` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.1] - 2026-10-29

### Added

- **Port squat detection** (issue #17). `just stt-list` and `just stt-status
  <backend>` warn on stderr when a TCP agent's port is held by a process other
  than the agent (listener pid vs launchd pid, confirmed after 1 second), or by
  any process while the agent is not running. Advisory only: stdout and exit
  codes are unchanged. A `launchctl print` failure other than "could not find
  service" prints a "cannot read the launchd state" note instead of passing as
  not running. `scripts/squat_check.py` does the check, reading the `launchctl
  print` answer from stdin so a legacy plist's token never appears in `ps`;
  `lsof` shows only your own processes' sockets. See `docs/operations.md`.

- **launchd verification harness** (opt-in). `scripts/smoke_launchd.py` runs
  the checks 0.5.0 covered only with stubs against real launchd, on a spare
  label (`pipecat.stt-server.smoke`) and port (9998) that no real agent shares:
  the port-collision restart loop (with a squat warning), the TCP-to-socket-and-
  back rollback, and a token file through launchd. It refuses any other label or
  a canonical or busy port, tears the spare agent down on every exit (removing
  the spare socket only under its own `.lock` flock, and failing on any cleanup
  it could not do), and compares each real agent's loaded job (pid, restart
  count, served endpoint), plist and listener with a baseline before every check
  and after teardown; a field it cannot read refuses the run (a real agent's
  `--port` is parsed as the server parses it, so `+9998` is 9998). Every
  answer that proves the spare is up goes over a connection whose server end
  is the spare job; the harness and its installer run with a scrubbed
  environment and fixed `PATH`; its runner timeout is the harness's own
  worst-case budget, teardown included. The shared
  `assert_token_gate` in `scripts/_smoke_lib.py` checks that a request without
  the token gets 401 and with it is accepted, sending the token only over a
  connection whose server end is the expected process. Run live three times
  on 2026-10-28 against the spare label and port, the last (on the closing-pass
  code) with all five rows PASS, then through `just smoke --agents --launchd`
  on 2026-10-29 on the round-8 head (`launchd` PASS; every `agents` row
  SKIPPED, no installed agent's job loaded); later fixes are covered by
  stubbed tests.

- **Unified `just smoke [--agents] [--launchd]`**. `scripts/smoke.py` runs the
  supervised (echo), peer-cred and token-file checks and prints one table of
  PASS, FAIL or SKIPPED with a reason; the exit status is 1 on any FAIL.
  `--agents` decodes a clip (generated with `say`/`afconvert`) through each
  installed agent from its plist endpoint, read-only, with the agent's token
  (its token file, or a legacy `PIPECAT_STT_AUTH_TOKEN` / `KODA_STT_AUTH_TOKEN`
  in the plist; the file read under the server's own token-file policy)
  passed on stdin. The token is sent only over a connection whose
  server end is the agent's own launchd process (same pid and restart count
  before and after); a foreign or unidentifiable listener gets nothing. An
  installed agent whose job is not loaded in launchd (servers run by another
  app) is SKIPPED, not decoded; an unreadable `launchctl print` answer FAILs.
  `--launchd` adds the launchd harness (macOS only, opt-in, never in CI). The
  `--agents` decode runs the production client's handshake over the verified
  socket and is tested end to end against a fake agent, but not yet live.

### Changed

- README and `docs/operations.md`: one `just smoke` section replaces the scattered
  smoke instructions, and the remaining `uv run python` examples use `uv run
  --no-sync python` so they cannot prune the backend extras.
- `just smoke-peercred` and `scripts/smoke_test_parakeet.py` run with
  `.venv/bin/python` instead of plain `uv run`, which pruned the backend extras.
- `scripts/smoke_supervised.py` sends each server's stderr to a temp file
  instead of an unread pipe, so a chatty server can no longer stall a scenario.

## [0.5.0] - 2026-10-27

> **UPGRADE NOTE.** The default LaunchAgent transport changed from a Unix socket
> to loopback TCP. Reinstalling an agent (`just stt-install <backend>`) flips it
> to TCP; set `PIPECAT_STT_SOCKET` to keep a socket agent
> (`PIPECAT_STT_SOCKET=<path> just stt-install <backend>`). Point clients at
> `STT_WS_HOST`/`STT_WS_PORT`. `PIPECAT_STT_AUTH_TOKEN` / `KODA_STT_AUTH_TOKEN`
> are now refused; use `PIPECAT_STT_AUTH_TOKEN_FILE`. A TCP reinstall of an
> agent whose installed plist carried a token exits 1 unless
> `PIPECAT_STT_AUTH_TOKEN_FILE` is set (run `uninstall` first to drop auth).
> Check any downstream
> installer invocations first. See `docs/migration.md`.

### Added

- **Kyutai LaunchAgent** (`just stt-install kyutai`, port 9665) with its own
  `.venv-kyutai` (`uv sync --frozen --python 3.12 --extra kyutai`), since its extra
  conflicts with the other MLX extras.
- **`just stt-probe`**: a wire status probe for any endpoint (`--host/--port`,
  `--socket-path`, `--uri`), including servers a supervising app started.
- `PIPECAT_STT_HOST`, `PIPECAT_STT_PORT` and `PIPECAT_STT_AUTH_TOKEN_FILE` for
  the installer; it passes them to `render_stt_plist.py` as `HOST`, `PORT` and
  `AUTH_TOKEN_FILE`.
- `PIPECAT_STT_PYTHON`: installer interpreter override (default
  `.venv/bin/python`, or `.venv-kyutai/bin/python` for kyutai). `just
  stt-install` checks the backend extra under it when it is set (and fails rather
  than sync the venv), so a custom interpreter without the extra no longer passes
  the check and then crash-loops; it passes the interpreter it checked to the
  installer as `PIPECAT_STT_PYTHON`, so the plist records the checked interpreter
  by construction, under a `venv=` override too. `just stt-enable` checks the
  interpreter the installed plist runs (`ProgramArguments[0]`, read through the
  plist reader) and ignores the caller's `PIPECAT_STT_PYTHON`.
- Canonical per-backend loopback ports: whisper 9565, kyutai 9665, parakeet 9765,
  nemotron 9865 (parakeet and nemotron confirmed with the menu bar app side on
  2026-10-26). The installer holds the only copy of this map;
  `scripts/install_stt_agent.sh port <backend>` prints a backend's port without
  calling `launchctl` or touching any file, and the justfile's `_resolve` asks it
  instead of keeping its own.

### Changed

- **Agents default to loopback TCP** instead of a Unix socket. `PIPECAT_STT_SOCKET`
  (or `KODA_STT_SOCKET`), when non-empty, is an explicit opt-in to a socket agent
  whose plist is byte-identical to before. Loopback TCP has no peer-uid check.
- `just stt-status` with no argument runs `just stt-list`. `just stt-status
  <backend>` probes the endpoint (socket or TCP, plus `--auth-token-file`) in the
  installed plist, falling back to the backend's canonical host and port only
  when no plist exists; a path is probed as `--socket-path`. `just stt-list`
  reads each agent's live endpoint from its plist.
- `stt-install`/`stt-uninstall` always use the canonical host and port for a TCP
  agent; use the installer script directly for a custom one. The recipes strip an
  ambient `PIPECAT_STT_HOST`/`PIPECAT_STT_PORT` and leave the transport to the
  installer, so the socket rollback (`PIPECAT_STT_SOCKET` set) no longer warns
  about ignored TCP settings and the socket-vs-TCP rule lives in one place.
- **`PIPECAT_STT_AUTH_TOKEN` / `KODA_STT_AUTH_TOKEN` get the same check as the token
  file.** A value with internal whitespace or non-ASCII characters now fails
  startup instead of running with a token no client can send.
- `just stt-list`, `stt-status` and `stt-probe` run `uv run --no-sync`, so a probe can
  no longer prune an agent's backend extras.
- The installer and the justfile read an installed plist through one helper,
  `scripts/read_stt_plist.py` (stdlib `plistlib`), instead of two `plutil` loops.
  A plist it cannot read (truncated, garbage, wrong shape) or a failed reader
  (its interpreter missing) now fails an install instead of counting as "no auth", and
  makes `uninstall` leave any socket in place; `stt-list` shows blank fields and a
  one-line warning for it instead of a traceback. The reader answers one field by
  name (`--field host|port|socket-path|auth-token-file|interpreter`, the last being
  `ProgramArguments[0]`), so no caller splits its output by line position; the
  earlier four positional lines are gone (a call without `--field` is a usage
  error, exit 2), and a newline inside a plist value can no longer shift one field
  into another.
- **`serve` takes each flag by its full name only** (argparse `allow_abbrev=False`):
  `--auth-token /x` or `--sock /x` now fails at startup instead of being read as
  `--auth-token-file` / `--socket-path`. `--flag=value` still works. The plist
  reader follows the same grammar: it reads `--auth-token-file=/x` (and the `=`
  form of `--host`, `--port`, `--socket-path`) and no longer takes the element after
  `--supervised` as its value, so a hand-edited plist that requires a token can no
  longer read as "no token" and let a reinstall drop auth.
- The installer's token-file check reads macOS ACLs: any ACL allow entry on the
  token file, or an allow entry with a write-class right (`write`, `add_file`,
  `delete_child`, `writesecurity`, ...) on a directory of its resolved path, fails
  the install; deny entries pass. A sticky bit exempts no directory: a token
  file under `/tmp` (root `1777`) or any group- or other-writable sticky
  directory outside the `wheel`/`admin` rule is refused, since anyone who can
  write there can create the name once it is gone (macOS empties `/tmp` at boot)
  and the server re-opens the path at every start.
- `install` runs the plist reader and the socket cleanup's lock step under its venv
  python (it needs no system `python3`, also when it retires an old socket); `uninstall` keeps `python3`. The endpoint read (`read_stt_plist.py
  --strict --field socket-path`) fails on a malformed plist by itself, so it no longer relies on the
  `--has-auth` read having run first.
- `uninstall` waits for launchd to drop the job for a TCP agent too, and prints
  `uninstalled: <label>` only once it is gone; a job still loaded after
  `PIPECAT_STT_UNLOAD_WAIT` gets a warning and `uninstall incomplete (still
  loaded)` instead (exit status unchanged, 0). When that wait times out on a
  socket agent (on `uninstall`, or on an install that changes the socket path),
  the warning prints the command that removes the stale socket by hand, instead
  of advising a re-run that could no longer find it.
- Socket cleanup on a transport change or `uninstall` leaves the socket in
  place, with a warning, while any process holds its `.lock` (a server started
  by hand can own the recorded path). The installer never removes the `.lock`
  itself, as the server never does: unlinking it after the check could let a
  starter that just locked the old file run beside a later one that creates a
  new file at the same path.
- The installer's token-file check refuses a path outside the plist allowlist
  before it creates any directory.
- The installer's raw-token refusal tests only the token's length, so running it
  under `bash -x` no longer prints a `PIPECAT_STT_AUTH_TOKEN` /
  `KODA_STT_AUTH_TOKEN` value to stderr.
- **Loopback TCP without a token logs INFO, not a warning.** That is the default
  agent setup, and non-loopback binds are refused, so there was nothing to warn
  about. The message names `--auth-token-file`, not the env token.
- **Breaking for hand or supervised launches: `serve --auth-token-file` enforces
  the token-file policy at every start**, the one the installer already applied:
  the path must be absolute; the file is opened with `O_NOFOLLOW` (a symlink is
  refused) and the opened descriptor must be a regular file owned by the user
  running the server with no group/other permission bits and no ACL allow
  entry; every directory of its resolved path must be owned by that user or root
  and writable by nobody else (by mode, or by a write-class ACL allow entry;
  group write only on a root-owned `wheel`/`admin` directory; a sticky bit
  exempts nothing, so `/tmp` is refused). A refused file stops startup (exit 1)
  with a message naming the file, the problem and the fix (`chmod 600 <file>`,
  `chmod -N` for an ACL, or move it out of a shared directory into one only you
  can write, e.g. `~/.config/pipecat-stt`). The policy is one Python
  implementation, `stt_server.auth.load_token_file`; the installer's preflight
  now calls it instead of its own bash checks, so installer and server cannot
  disagree. An installed agent is unaffected (its file passed the same check);
  a `0644` token file, a relative path or one under `/tmp` passed to `serve` by
  hand or by a supervising app now fails. The probe (`status`) is unchanged.
- **`serve --auth-token-file` is strict.** An empty or whitespace-only file, a
  token with internal whitespace (a space, tab or newline inside it; a trailing
  newline is stripped and fine) or a non-ASCII character, or a
  non-UTF-8 file now stops the server at startup instead of silently running
  without auth (an empty token disabled it) or never matching any client. The
  probe (`status`) stays lenient. The installer runs the same check
  (`stt_server.auth.load_token_file`) before writing the plist, and now also
  requires the token file's parent directories (symlinks resolved) to belong to
  you or root and not be writable by others (group write only on a root-owned
  directory of group `wheel` or `admin`; a sticky bit is no exception). The plist records the
  resolved path, the one that was checked.
- **The installer refuses to drop auth on reinstall.** If the installed plist
  carries a token (a pre-0.5.0 `PIPECAT_STT_AUTH_TOKEN`/`KODA_STT_AUTH_TOKEN` in
  `EnvironmentVariables`, or `--auth-token-file`) and a TCP install sets no
  `PIPECAT_STT_AUTH_TOKEN_FILE`, the install fails instead of leaving an
  unauthenticated listener. Set the token file, or `uninstall` first to install
  without one.
- **`PIPECAT_STT_AUTH_TOKEN_FILE` is validated** in TCP mode: a regular file, not a
  symlink, owned by you, no group/other permission bits, and non-empty (an empty
  file would silently disable auth).
- `PIPECAT_STT_UNLOAD_WAIT` must be an integer from 0 to 86400 (leading zeros are
  read as decimal, so `08` is 8 seconds); anything else, including values large
  enough to overflow bash arithmetic, fails before any `launchctl` call. `PIPECAT_STT_BACKEND` is checked against the
  renderer's backend names before any directory is created (`whisper` is the
  justfile name; the installer name is `mlx`).
- The default weights of every Hub-backed backend (Whisper
  `mlx-community/whisper-large-v3-turbo`, Kyutai `kyutai/stt-1b-en_fr-mlx`,
  Parakeet `mlx-community/parakeet-tdt-0.6b-v3`, Nemotron
  `mlx-community/nemotron-3.5-asr-streaming-0.6b`) are pinned to a Hub commit
  (`backends.PINNED_REVISIONS`), for serving and `prefetch`. Any other
  `--model` still follows the repo's `main`. The pins match the snapshots
  already cached on current installs.

### Removed

- **Breaking:** `PIPECAT_STT_AUTH_TOKEN` and `KODA_STT_AUTH_TOKEN` are refused by
  the installer and renderer (an env token would land in the plist). Use
  `PIPECAT_STT_AUTH_TOKEN_FILE`.

### Fixed

- **Old socket cleanup waits for the old server.** The installer removes the old
  socket only after the new agent bootstrapped and launchd no longer lists the
  old job; `uninstall` waits for the job to unload first, since the old server
  owns its socket until it exits. A failed bootstrap now leaves the old socket in
  place. The `.lock` beside it is left in place in every case. A reinstall from one socket path to
  another also retires the old socket (it was orphaned before).
- `just stt-status` (no argument) forwards a `la_dir=` override to `stt-list`, and
  `stt-install`/`stt-enable` forward `venv=`/`kyutai_venv=` to `_ensure-extra`.
- **Reinstalling over a socket agent no longer risks deleting the wrong file.** The installer removes the old socket only when the installed plist names an absolute path under `$HOME` without `..` or a symlinked directory component and the socket is a real socket (not a symlink), owner-checked; anything else is left in place with a warning. It never removes the `.lock`. The renderer's validators use `fullmatch`, so a trailing newline is rejected.
- **Reinstalling a running agent could leave it unloaded** (`Bootstrap failed: 5:
  Input/output error`). `launchctl bootout` returns before launchd has finished
  tearing the old job down (a loaded ASR model takes seconds to exit), and the
  installer bootstrapped straight into that window. The installer now waits
  (up to `PIPECAT_STT_UNLOAD_WAIT` seconds, default 30) until the label is gone
  and retries the bootstrap once. Existed before 0.5.0 (same bootout/bootstrap
  sequence); reproduced on a nemotron reinstall.
- **Socket cleanup no longer races a starting server.** The installer checked
  the old socket's `.lock` and released it before unlinking the socket, so a
  server that took the lock and bound in between lost its fresh socket. The lock
  check, the socket checks (a real socket, not a symlink, owned by you) and the
  unlink now run in one step while the installer holds a shared lock on the
  `.lock`; a starter waits for it (its 250 ms lock retry) and binds after.
- The manual socket-removal command printed after a timed-out wait applies the
  installer's own path guards: it is offered only for an absolute, `..`-free
  path under `$HOME` with no symlinked directory (otherwise the warning says the
  recorded path looks unsafe), and it removes the path only if it is still a
  socket, not a symlink, and owned by you (`test -S P && ! test -L P && test -O P
  && rm -f -- P`).
- `_ensure-extra` (run by `stt-install`/`stt-enable`) probes the interpreter
  the agent will run: for `stt-install`, `PIPECAT_STT_PYTHON` when it is set and
  non-empty (the installer's override), else the venv, and that interpreter is
  the one installed; for `stt-enable`, the
  installed plist's `ProgramArguments[0]` (a plist with no interpreter, or one the
  reader cannot parse, fails before any bootstrap). A failed probe of an
  interpreter outside the venv fails the recipe with the remedy instead of
  syncing the venv, which could not fix it.
- `_ensure-extra` re-runs the import probe after a successful `uv sync` and fails
  if it still fails, instead of reporting the extra ready and letting the agent
  crash-loop.
- `just stt-disable` waits (up to 30 s) until launchd no longer lists the agent
  before it reports `booted out`, so a `just stt-enable` straight after it no
  longer fails with `Bootstrap failed: 5: Input/output error` while the old job
  tears down. Still listed at the cap, it warns and reports the bootout as still
  unloading (exit 0).
- The installer and the `just` recipes capture paths losslessly. `$(...)` drops
  trailing newlines, so a token file or socket path ending in one was validated
  or probed as one string and recorded or acted on as another (a plist socket
  path `<sock>\n` was cleaned up as `<sock>`). Captures now keep every byte, and
  a token-file or socket path containing a control character (newline, tab,
  escape) is refused with a `fix:` line before anything is created.
- Kyutai: a cancelled stream (barge-in) now stops its decode at the next 80 ms
  frame, so the next client no longer waits for the abandoned decode to finish.

## [0.4.0] - 2026-10-25

### Added

- **`docs/architecture.md`**: the server lifecycle state machine, the single-server lock, stop during startup, and the supervisor contract.
- **`scripts/smoke_supervised.py`**: an opt-in end-to-end smoke test that drives `serve --supervised` the way a menubar app does (events, stops, parent death, second instance, stale socket, concurrent clients). See `docs/operations.md`.
- **Single-server lock for Unix sockets.** `start()` takes an `flock` on
  `<socket>.lock` (mode `0600`, next to the socket) before the model loads, and
  the supervised preflight treats a held lock as a live server. `connect()`
  alone cannot tell a stale socket from a live one with a full listen backlog
  (macOS returns `ECONNREFUSED` for both). The kernel drops the lock when the
  process dies, so it cannot go stale; clients never touch it, so any number
  share one server. The lock file is left in place on exit. A pre-existing lock
  file we own that opens read-write is set to `0600` whatever its mode; one we
  cannot open (e.g. mode `000`) is refused with `EACCES` naming the remedy
  (`chmod 600`); one owned by another user, or not a regular file, is refused. The preflight probe takes a shared lock and a
  starter retries a contended lock for 250 ms, so a probe never makes a
  concurrent real start fail.
- `ModelInvalidError` supervised/prefetch error code: the model snapshot is
  present but unusable (incomplete, corrupt or incomplete `config.json`, an
  incomplete local model directory), from `prefetch` and from the `kyutai`
  loader in `serve`. The code list is per repo (`tts_server` has
  `SocketPathConflict` and no `ModelInvalidError`); supervisors should treat an
  unknown code as `BackendStartError`. See README "Error codes (per repo)".
- `backends.BACKENDS` registry: CLI choices, construction and the prefetch
  module map derive from one table. `SnapshotSpec` moved to the stdlib-only
  `stt_server.snapshot` (still importable from `stt_server.prefetch`); the
  optional backend hooks `abort()` (`AbortableBackend`, now exported) and
  `idle()` (`IdleAwareBackend`) and module-level `prefetch_specs()` are
  documented in `stt_server/backend.py`. `backends.import_backend()` is the one
  lookup-and-import and returns the entry, module and class. README has an
  "Adding a backend" checklist.
- `TranscriptionServer.state` (`new`/`starting`/`running`/`stopping`/`stopped`),
  `holds_socket_lock`, and `wait_stopped()`: waits until the lock is free and
  no work runs (`stopped`, or `new` after a failed start). `shutdown()`
  returning does not always mean that (from `starting`, or with leftover work
  past its bounds); await `wait_stopped()` before starting a new server on the
  same socket or closing the event loop.
- **`serve --supervised`** for a managing parent process: stdout carries only
  JSON lines (`ready` once listening and warm, with the real port; or `error`
  with a stable `code` such as `EADDRINUSE`, `ModuleNotFoundError`,
  `ModelDownloadError`), a busy port or live socket is reported before the
  model loads, and stdin EOF stops the server like SIGTERM. Same channel as
  `tts_server serve --supervised`; the `code` list is per repo (see above).
- **`prefetch` subcommand**: downloads what a backend loads (with JSON-lines
  byte progress under `--json`), and `--check` reports whether the weights are
  cached without touching the network. Each backend module exports
  `prefetch_specs(model)`. Progress is measured on disk, so it works across
  `huggingface_hub` versions.
- **`kyutai` backend** (extra `kyutai`, `moshi_mlx==0.3.0`; default model
  `kyutai/stt-1b-en_fr-mlx`, English + French, CC-BY-4.0). The extra is declared
  conflicting with `mlx` / `parakeet` / `nemotron` (moshi_mlx pins `mlx<0.27`,
  `huggingface-hub<0.29`), so the lockfile keeps their newer pins.
- `TranscriptionServer.adopt(task, abandoned=..., label=...)`: registers a
  task of yours as live work, keeping the single-server lock until it ends.
- `TranscriptionServer.can_adopt`: whether `adopt()` is valid now (`starting`,
  `running` or `stopping`); otherwise `adopt()` raises `RuntimeError`.
- `TranscriptionServer.live_work_count`: size of the live-work ledger; the lock
  is held while it is non-zero (read it after `shutdown()`).
- `TranscriptionServer.live_work_reasons`: `live_work_count` by kind, for logs;
  `(N stuck)` marks entries that outlived the bound their caller waited for.
- `TranscriptionServer.backend` and `TranscriptionServer.config`: read-only.

### Changed

- `serve(..., hard_exit_on_stop=False)` is the default: a stop during
  startup/warm-up now cancels the pending step (waiting at most the drain
  timeout) and shuts down normally for in-process callers, which previously got
  the immediate `os._exit`. The CLI passes `True` to keep the immediate exit.
- `ConfigError` covers only explicit configuration rejections: a bare
  `ValueError` (for example from a model library during warm-up) is now
  `BackendStartError`, not `ConfigError`. `ConfigError` (a `ValueError`
  subclass: endpoint, socket directory, unknown backend, malformed Hub repo id,
  unsupported local model path) lives in the stdlib-only `stt_server.errors`
  and is exported as `stt_server.ConfigError`; `supervise.ConfigError` remains
  as a re-export.
- `--model` defaults come only from `backends.BACKENDS[...].default_model`.
- `TranscriptionServer` is **single-use**: after `shutdown()` (in any state,
  including before `start()`), or once a failed start left work running
  (`stopping`), `start()` raises `RuntimeError` naming which of the two
  happened; build a new server to serve again. A failed or cancelled `start()` returns the server to
  `new` and may be retried, unless work it started outlived its cleanup (a
  session, a backend `close()`, a bind finishing after a second cancel): then
  it stays `stopping`, holding the lock until that work ends, and is
  single-use. `start()` while another `start()` is in flight raises
  `RuntimeError` instead of racing it for the lock.
- `serve(on_fatal=...)` without `hard_exit_on_stop=True` raises `ConfigError`
  (it would never be called).
- `stt_server.IdleAwareBackend` is exported alongside `AbortableBackend`.
- `backends.import_backend()` raises `ConfigError` for an unknown name (was
  `KeyError`) and returns a `LoadedBackend` (`entry`, `module`, `cls`).

### Fixed

- A stop during startup or warm-up (`python -m stt_server serve`, with or without
  `--supervised`) still exits immediately but now removes the Unix socket file it
  bound (only that inode) and the parakeet/nemotron private temp dir, via
  `TranscriptionServer.abort_sync()` and an optional backend `abort()` hook. The
  single-server lock is held until the process exits.
- A failed `start()` (lock held, socket-directory refusal, bind failure, missing
  extra) runs the backend's `abort()`, so parakeet/nemotron no longer leak a
  private temp dir per failed start.
- `shutdown()` is bounded even when a session task delays or swallows
  cancellation or `backend.close()` hangs: the drain timeout, plus two 2 s
  cancel graces (the cancelled sessions' unwind, then the listener's
  `wait_closed`), plus the backend `close()` bounded by the drain timeout
  again. It releases the single-server lock even when draining or
  `backend.close()` fails.
- A decode thread that fails to start no longer leaves the in-flight count
  raised (which made every later `close()` wait out its drain window).
- `kyutai` weight filenames from `config.json` must be plain filenames (no path
  separators, `..` or glob characters) before they are fetched or matched.
- `prefetch --json` progress is consistent: `total_bytes` / `files_total` count
  only files not yet cached (as in `tts_server prefetch`), `downloaded_bytes`
  includes partial `.incomplete` blobs from an interrupted run, `files_done` is a
  real count, and a final progress event reaches the total before `done`.
- `kyutai` prefetch and `--check` take the weight filenames from the repo's
  `config.json` (`mimi_name`, `moshi_name`, `tokenizer_name`), so quantized
  (`.q4`/`.q8`) repos neither fail the completeness check nor download every
  `*.safetensors`.
- A listener-preflight error (e.g. `ECONNRESET`/`ECONNABORTED` from the Unix
  socket probe) is reported as `BackendStartError`, not `ModelDownloadError`. A
  bare `ConnectionError`/`TimeoutError` is a download failure only when raised by
  a backend's model fetch-and-load call or a `prefetch` download, or when a
  network library raised it (a decode during warm-up is not a download). A
  malformed Hub repo id (`HFValidationError`) is `ConfigError`, not a download
  failure.
- `prefetch --check` reports a cached `config.json` that is valid JSON but not
  an object (e.g. `null`) as `cached: false` instead of crashing.
- `shutdown()` wins over an in-flight `start()`: once shutdown begins, a start
  still running (e.g. one that outlived `serve`'s bounded cancel) never binds a
  listener or marks the server started; it closes anything it bound and
  releases the single-server lock itself. A cancel landing mid-bind no longer
  drops a serving listener.
- Session tasks that swallow cancellation past the shutdown grace keep the
  single-server lock until they finish (they are live work,
  `TranscriptionServer.live_work_count`), and the CLI exits the process
  instead of hanging in `asyncio.run` teardown.
  That exit no longer swallows a failure in flight (e.g. `backend.close()`
  raising): it is printed and, under `--supervised`, emitted as the `error`
  event, with exit status 1.
- The start/shutdown lifecycle is one explicit state machine with the lock kept
  on the server: a concurrent `start()` can no longer release the active
  starter's lock; a `shutdown()` cancelled mid-drain cancels the sessions it
  had not cancelled and keeps the lock until they finish; the lock also waits
  for a backend decode thread that outlived its cancelled task (the `idle()`
  hook, implemented by every Metal backend); a `shutdown()` during `start()`
  now `close()`s a backend whose `start()` had completed; `shutdown()` removes
  the Unix socket file it bound (only that inode).
- The single-server lock is released in one place only (the transition to
  `new`/`stopped`), refused while anything is still live: a bound listener, a
  session or decode task, a start, drain or backend `close()` in flight, or a
  backend decode thread. As a result: a failed start drains the sessions it
  accepted before giving the lock back; a second cancel mid-bind can no longer
  orphan a listener; `backend.close()` is bounded by the drain timeout (a hung
  close keeps the lock as live work, so the CLI exits); a
  cancelled `shutdown()` no longer abandons `backend.close()` and later
  callers wait for it; and `serve` keeps a startup/readiness step that ignored
  its cancel as live work (lock held until it ends, its failure logged).
- The single-server lock is held while any live work remains: a bind that
  finished after a second cancel, its listener's close, the listener's
  `wait_closed` (which lingers past its grace instead of being cancelled), a
  backend decode thread, and session / decode tasks from creation. An
  orphaned listener keeps the lock until its socket has really closed. Work
  stays live until its own cleanup has run, so other work ending in the same
  loop tick cannot release the lock before an orphan bind's listener is
  closed and unlinked. A failed start that returns to `new` resets every
  per-attempt slot, so a retry closes its own backend and never acts on the
  first attempt's socket inode.
- The lock fails closed on every exit path: work whose own cleanup raises (an
  orphaned listener whose `close()` failed, still serving) stays live and
  keeps the lock, and a cleanup given for an already-finished future runs at
  once. Cancelling `serve` itself settles a pending startup / readiness step
  the way a stop does (cancel, bounded wait, keep one that outlives it as
  live work) instead of leaving it running while the backend closes and the
  lock is released. The lock and the CLI's hard exit both read
  `live_work_count`, so live work of any kind triggers the hard exit; the
  stuck mark in `live_work_reasons` is a diagnostic only.
- `prefetch --check --backend mlx` counts a snapshot complete only with the
  files `mlx_whisper` opens (`config.json` and `weights.safetensors` or
  `weights.npz`); a `weights.*` glob accepted e.g. `weights.txt`.
  Alternatives in `SnapshotSpec.required` are a tuple entry, not an `a|b`
  string. A Kyutai `config.json` weight filename containing `|` (e.g.
  `model.q8.safetensors|config.json`) is refused: `prefetch --check` reports
  `cached: false` and a fetch fails with `ModelInvalidError` ("config.json
  names an invalid file"), instead of passing on `config.json` alone.
- `serve` uses only `TranscriptionServer`'s public API (`adopt()`,
  `can_adopt`, `live_work_count`, `live_work_reasons`), so an embedder can
  write the same loop; which signal to read is documented once, in the class
  docstring. `adopt()` in `new` or `stopped` raises `RuntimeError` instead of
  making `shutdown()` fail or running work after the lock is released. A
  cancel of `serve` during a stop's bounded wait on a startup / readiness
  step does not start a second full drain-timeout wait.
- `serve` removes the SIGINT / SIGTERM handlers it installed on every return
  or raise, so after a failed start the process no longer survives SIGTERM.
- `prefetch --backend echo --model X` fails with `ConfigError` (exit 1)
  instead of ignoring the model and reporting it cached.
- `serve`: when `shutdown()` also fails after `serve` was already failing, the
  first failure is raised (and handed to `on_fatal`); the shutdown failure is
  logged instead of replacing it.
- `prefetch --model '~user/...'` for a user with no home directory is a missing
  local model (`cached: false`, or `ModelInvalidError` on a fetch) instead of a
  `BackendStartError` crash.
- `prefetch --check` reports a malformed Hub repo id as `ConfigError` (exit 1),
  as a fetch does, instead of `cached: false`.
- The `<socket>.lock` mode no longer depends on the umask (a `0777` umask made
  an unopenable mode-000 file); an unopenable existing lock reports `EACCES`
  with the remedy. The preflight probe opens it non-blocking and reads a FIFO,
  non-regular or foreign-owned file as "not held" instead of hanging or
  reporting a spurious running server.
- `TranscriptionServer.listening_port()` returns `None` for a Unix socket
  instead of a character of its path.

### Upgrade notes

- Consumers that pin `pipecat-local-stt-server>=0.3.3,<0.4` (for example
  onoats-bot) must widen the pin to take this release. They use only
  `TranscriptionClient` and `stt_server.protocol`, which are unchanged.
- Code that embeds `stt_server.server.serve()` or `TranscriptionServer` should
  read the Changed section: the server is single-use after `shutdown()`,
  `hard_exit_on_stop` defaults to `False`, and `import_backend()` raises
  `ConfigError` for an unknown name.
- A second `serve` for a Unix socket a live server owns now exits with
  `EADDRINUSE` before loading a model; it never replaces the running server.
- The `kyutai` extra conflicts with `mlx`, `parakeet` and `nemotron`: install it
  in its own environment (`uv sync --extra kyutai`), and it needs Python 3.12.
- See [docs/architecture.md](docs/architecture.md) for the lifecycle and the
  supervisor contract.

## [0.3.3] - 2026-06-28

Server-side hardening of the same-host UDS trust boundary. No new user-facing
features; clients connect unchanged (no protocol or client-API change).

### Added

- **Server-side UDS peer-credential authentication.** The server rejects any
  connection whose kernel-reported peer uid `!= os.geteuid()` with a
  pre-handshake HTTP `403` (`peer not permitted`), before the WebSocket
  handshake. Uses macOS `getpeereid(2)` (via `ctypes`) / Linux `SO_PEERCRED`;
  every resolver failure path fails closed (rejects). UDS only — TCP keeps the
  Origin + bearer-token checks. No client change required.
- **Socket-directory ancestor enforcement.** Before bind, the server walks every
  directory from the socket's parent up to and including `$HOME` and refuses to
  start unless each is owner-owned and not group/other-writable (sticky-bit dirs
  excepted), creating missing dirs `0700`. The walk is over the **literal** path
  clients traverse and **rejects any symlink component** (a symlinked, writable
  lexical ancestor would otherwise be repointable post-startup to hijack the
  client-visible socket). This makes the socket un-plantable (no foreign uid can
  `unlink`+`bind` a replacement).
- **Backend-extra onboarding.** `just stt-install` / `stt-enable` now ensure the
  selected backend's optional extra is installed (`uv sync --extra <X>
  --inexact`) so a freshly installed agent doesn't crash-loop on a missing
  import; `PIPECAT_STT_SKIP_DEP_SYNC=1` opts out.
- `scripts/smoke_peercred.py` + `just smoke-peercred`: a local cross-uid /
  multi-connection peer-cred smoke (cross-uid leg skips cleanly without a second
  local uid).
- `scripts/verify_peercred_crossuid.py`: a stdlib-only cross-uid verifier that
  drives a probe as both the owning uid and `nobody` against one permissive
  socket — proving peer-cred (not the filesystem) is the discriminator. Needs no
  venv/`websockets`, so it works with `nobody` where the smoke's `sudo` path
  can't.

### Changed

- Pinned `websockets` to `>=16,<17` (was `>=13`; the `_process_request`
  handshake/transport contract depends on the v16 API). Applies to every extra,
  including `client`. See Upgrade notes for the consumer-conflict caveat.
- `scripts/install_stt_agent.sh` creates the socket directory `0700` (was the
  install-shell umask default) and self-heals a pre-existing `0755` dir. It now
  validates a custom `PIPECAT_STT_SOCKET` against the same rules the server
  enforces (absolute, under `$HOME`, no symlink component) **before** any
  `mkdir`/`chmod`, so a path the server would reject fails the install cleanly
  with no filesystem mutation instead of tightening the directory and then
  crash-looping the agent.
- Startup failures — socket-dir enforcement, the `ServerConfig` `ValueError`,
  bind `OSError`s, and a missing backend extra — surface as
  `stt_server: <msg>` + exit 1 instead of a bare traceback.

### Security

- Closes the same-host UDS plant/swap and foreign-uid-connect vectors: the
  owner-only ancestor chain is the primary filesystem boundary and peer-cred is
  the kernel-authoritative backstop. The bearer token is retained for TCP/remote
  (which has neither boundary). See [`docs/operations.md`](docs/operations.md).

### Documentation

- **Split the 593-line README into a focused top page + `docs/`.** The README now
  keeps the preamble, V1 scope, running the server, the `just` operator recipes,
  and backend/model selection; the rest moved verbatim to
  [`docs/operations.md`](docs/operations.md) (multi-backend operation, two-agent
  install, A/B benchmark, health checks, Whisper hallucination knobs),
  [`docs/integration.md`](docs/integration.md) (client usage, Pipecat integration,
  Koda reference consumer), [`docs/protocol.md`](docs/protocol.md) (wire protocol),
  and [`docs/migration.md`](docs/migration.md) (0.1.x → 0.2.0 upgrade). A
  Documentation index links them; all cross-references were repointed. No content
  was dropped.
- Documented the same-host UDS trust model and the same-uid precondition in
  `docs/operations.md`; added a pre-handshake connection-rejection table to
  `docs/protocol.md`.

### Upgrade notes

- **May require action on existing hosts.** Because the server now refuses to
  start against a group/other-writable socket-dir ancestor, an existing `0755`
  socket directory — or a custom `STT_WS_SOCKET` / `KODA_STT_SOCKET` pointing
  outside `$HOME` — will block startup. Re-run `scripts/install_stt_agent.sh`
  (self-heals the dir to `0700`), or `chmod 700` the dir / move the socket under
  `$HOME`. Koda hosts run the server from the checkout, so fold this into the
  next checkout update; no client pin bump is needed.
- **`websockets>=16` may conflict for library consumers.** The floor moved from
  `>=13` to `>=16,<17` across every extra (`client` included), so an environment
  that resolves an older `websockets` (or another package capping it `<16`) will
  hit a dependency conflict when installing this version. The wire protocol and
  `stt_server.client` API are unchanged — only the dependency floor moved — so
  this is a packaging bump, not a behavioural one, and stays a patch release
  (`0.3.3`).

## [0.3.2] - 2026-06-08

### Changed

- **Nemotron is now a clean PyPI extra (`uv sync --extra nemotron`), no longer a
  git-pinned dev group.** `mlx-audio>=0.4.4` is the first published release
  carrying Nemotron STT support ([Blaizzy/mlx-audio#774](https://github.com/Blaizzy/mlx-audio/pull/774),
  merged 2026-06-05), so the backend's dependency moved from a `[dependency-groups]`
  git-pin (a direct-URL dep PyPI forbids in published extras) to a versioned
  `[project.optional-dependencies]` entry alongside `mlx` and `parakeet`. Install
  with `uv sync --extra nemotron` (the old `--group nemotron` no longer exists);
  `pip install "pipecat-local-stt-server[nemotron]"` now resolves from PyPI.

### Documentation

- **Document running the server on loopback TCP (`localhost:port`).** Added a
  `uv run` prerequisite note to the "Running the server" section (bare `python -m
  stt_server` fails with `ModuleNotFoundError: No module named 'websockets'`), a
  minimal no-token loopback-TCP example (auth is warn-only on loopback), and a
  "Ports" note: there is no default port (`--port` is required; `--port 0`
  lets the OS assign one), and V1 rejects non-loopback binds. No code changes.

### Added

- **`justfile` operator recipes for managing the STT LaunchAgents.** New macOS
  recipes — `just stt-list`, `stt-status <backend>`, `stt-enable <backend>`,
  `stt-disable <backend>`, `stt-install <backend>`, `stt-uninstall <backend>` —
  give a cross-agent "operate the listed servers" surface that
  `scripts/install_stt_agent.sh` (one agent per invocation) structurally lacks.
  Backends are `whisper` / `parakeet` / `nemotron`, mapped to the labels and
  sockets in the README per-ASR table (a test asserts the map mirrors the
  table). `stt-disable` boots the agent out but keeps the plist (reloads at next
  login); `stt-uninstall` removes the plist durably. Install/uninstall delegate
  to `scripts/install_stt_agent.sh` rather than reimplementing plist rendering.

### Fixed

- **`just stt-disable` / `stt-enable` now propagate `launchctl` failures.** A
  failed `launchctl bootout` / `bootstrap` / `kickstart` was previously masked by
  the recipe's success `echo` (exit 0 reported while the agent was still running
  or never started). Each state change is now guarded and surfaces a non-zero
  exit with an error on stderr; `stt-enable` skips `kickstart` once `bootstrap`
  fails.

## [0.3.1] - 2026-06-06

### Fixed

- **`--backend mlx` (Whisper) now accepts `language="auto"`.** A client
  `language` of `"auto"` (or blank) is recast to `None` server-side in the
  whisper backend (`mlx_whisper._normalize_language`) so Whisper performs its
  own auto-detection, instead of raising `ValueError: Unsupported language:
  auto` and surfacing a `transcript.failed`. This makes `"auto"` a uniform
  "auto-detect" sentinel across all backends — whisper auto-detects, parakeet
  ignores `language` (model is language-pinned), and nemotron treats `"auto"`
  as its language-ID prompt. The recast is localized to the whisper backend
  (not server-generic) to avoid coupling the sentinel to nemotron's
  `DEFAULT_NEMOTRON_LANGUAGE`. Real language codes (`"en"`, `"es-ES"`) pass
  through unchanged.

## [0.3.0] - 2026-06-05

### Added

- **Nemotron 3.5 ASR backend** (`stt_server/backends/nemotron.py`), selected
  via `--backend nemotron` and installed with `uv sync --group nemotron`.
  Default model `mlx-community/nemotron-3.5-asr-streaming-0.6b`. Like Parakeet,
  it decodes from a temp WAV holding raw utterance audio (PII), written to a
  per-process private `0o700` directory and unlinked after decode.

### Notes

- **Packaging — Option 1 landed: a git-pinned `[dependency-groups]` dev group,
  not a published extra.** The backend requires Nemotron STT support from
  `mlx-audio`, which only merged in PR #774 and is not yet in any published
  `mlx-audio` PyPI release. A direct-URL (`@ git+…`) dependency is forbidden in
  a published wheel's `Requires-Dist` (PyPI rejects direct-URL deps in extra
  metadata), so shipping a `nemotron` *extra* would block 0.3.0 from PyPI
  entirely. PEP 735 dependency groups are never emitted into wheel/sdist
  metadata, so `uv sync --group nemotron` installs the git-pinned backend
  locally while the published 0.3.0 stays PyPI-installable. PyPI-installability
  was verified by confirming the built wheel's `METADATA` carries **no**
  `mlx-audio` direct-URL in `Requires-Dist`. Promote to a versioned `nemotron`
  extra once `mlx-audio` publishes a release containing #774.

## [0.2.0] - 2026-05-30

### Changed (BREAKING)

- **The default runtime surface is renamed from the legacy `koda`-prefixed
  namespace to a `pipecat`-namespaced default.** A fresh install now uses the
  LaunchAgent label `koda.stt-server` → `pipecat.stt-server` (and the
  two-agent variant `koda.stt-server.parakeet` → `pipecat.stt-server.parakeet`),
  the default socket `~/Library/Caches/koda-stt/stt.sock` →
  `~/Library/Caches/pipecat-stt/stt.sock`, the default log dir
  `~/Library/Logs/koda-stt/` → `~/Library/Logs/pipecat-stt/`, and the log
  basenames `koda-stt.{log,err}` → `pipecat-stt.{log,err}`. These were
  *live, correct defaults*, so this is a genuine breaking change for any
  existing v0.1.x install.
- **`scripts/install_stt_agent.sh install` now migrates v0.1.x installs.**
  When run with the default label, it boots out and removes **both** legacy
  agents (`koda.stt-server` and `koda.stt-server.parakeet`) before
  bootstrapping the renamed `pipecat.stt-server` agent, so a v0.1.x machine is
  not left double-running an old and a new agent. The migration is idempotent
  (a no-op on a fresh machine), never retires the new agent, and does not touch
  unrelated custom-label installs. The old socket and log directories are left
  in place (orphaned, harmless).
- Consumers pinned to the old socket path must re-point to the new
  `~/Library/Caches/pipecat-stt/stt.sock` — set `STT_WS_SOCKET` (or re-point a
  wrapper's `STT_WS_DEFAULT_SOCKET` fallback). The rename does not reach across
  to the external koda-pipecat `./koda stt` wrapper. See the "Upgrading from
  0.1.x to 0.2.0" section in the README.

### Notes

- The deprecated `KODA_STT_*` environment-variable **names** are untouched and
  remain honoured aliases: `KODA_STT_LABEL` / `KODA_STT_SOCKET` /
  `KODA_STT_LOG_DIR` still override the new `pipecat`-namespaced defaults. Only
  the default *values* changed; the names-vs-values distinction is unchanged.
- The `koda.stt-server → koda-stt` log-basename mapping is retained as an
  explicit backward-compat shim alongside the new
  `pipecat.stt-server → pipecat-stt` mapping, so an explicit legacy-label
  render still produces the legacy basenames.

## [0.1.2] - 2026-05-30

### Documentation

- Remove dangling references to the private `koda-pipecat` monorepo that do
  not exist in this standalone repo. Renamed the stale `shared/text_quality`
  / `shared.classifier` module paths to their `stt_server.*` equivalents, and
  stripped dead `docs/dev_plans/*` links, the `scripts/calibrate_degenerate_threshold.py`
  reference, and an internal plan id from the README and the
  `stt_server` / `scripts` docstrings and comments. Substantive guidance
  (thresholds, rationale, calibration intent) is preserved. No code changes.

## [0.1.1] - 2026-05-30

### Changed

- **`env_float_first` / `env_int_first` now resolve by presence, not
  non-emptiness** — matching `env_bool_first`. A present-but-empty canonical
  `PIPECAT_STT_*` value now wins and resolves to the default, instead of
  silently falling through to a set legacy `KODA_*` alias. This makes the
  canonical-first precedence rule uniform across bool/float/int knobs. Parsing
  is delegated to `env_float` / `env_int` so coercion is single-sourced.
  Affects the `PIPECAT_STT_WHISPER_*` decode and degenerate-detection knobs.

### Internal

- `scripts/render_stt_plist.py` now imports `env_first` from `stt_server.env`
  instead of carrying a duplicate `_env_first`; the resolver is single-sourced.
- `stt_server/text_quality.py` resolves its thresholds through
  `env_float_first` / `env_int_first` (gaining the invalid-value warning that
  the prior inline parse swallowed) instead of `env_first` + inline `float()`.
- `scripts/render_stt_plist.py` guards its `stt_server` import: a hand-run with
  the wrong interpreter now exits with an actionable "use the project venv"
  hint instead of an opaque `ImportError` traceback.

## [0.1.0] - 2026-05-30

First public release: a standalone local WebSocket transcription (STT) server,
client, and pluggable ASR backends, extracted (history-preserving) from the
private `koda-pipecat` monorepo. Distribution name `pipecat-local-stt-server`,
import name `stt_server`.

### Added

- **`PIPECAT_STT_*` environment-variable namespace** as the canonical
  configuration surface, resolved canonical-first (the `PIPECAT_STT_*` name
  wins when set):
  - `PIPECAT_STT_AUTH_TOKEN` — server-side bearer for the serve path.
  - `PIPECAT_STT_LABEL`, `PIPECAT_STT_SOCKET`, `PIPECAT_STT_BACKEND`,
    `PIPECAT_STT_MODEL`, `PIPECAT_STT_LOG_DIR` — LaunchAgent install /
    plist-render parameters (`scripts/install_stt_agent.sh`,
    `scripts/render_stt_plist.py`, `scripts/mlx_teardown_spike.sh`).
  - `PIPECAT_STT_WHISPER_CONDITION_ON_PREVIOUS_TEXT`,
    `PIPECAT_STT_WHISPER_COMPRESSION_RATIO_THRESHOLD`,
    `PIPECAT_STT_WHISPER_LOGPROB_THRESHOLD`,
    `PIPECAT_STT_WHISPER_NO_SPEECH_THRESHOLD` — MLX Whisper decode /
    hallucination-suppression knobs.
  - `PIPECAT_STT_WHISPER_DEGENERATE_TOKEN_RATIO`,
    `PIPECAT_STT_WHISPER_DEGENERATE_MIN_TOKENS` — degenerate-output filter
    thresholds.
- `env_bool_first` / `env_float_first` / `env_int_first` helpers in
  `stt_server/env.py` for canonical-then-alias resolution.
- PyPI packaging metadata: authors, `[project.urls]` (Homepage / Repository /
  Issues), trove classifiers (BSD-2-Clause, Python 3.12 / 3.13, macOS,
  speech / AI topics), and keywords.
- `stt_server/examples/pipecat_stt_service.py` — a runnable Pipecat
  `SegmentedSTTService` subclass (`LocalWebSocketSTTService`) wiring
  `TranscriptionClient` into a pipeline, plus README sections on choosing a
  backend/model (Whisper + Parakeet) and the Pipecat integration. The example
  imports `pipecat`, which remains an optional, non-declared dependency.

### Deprecated

- All `KODA_STT_*` environment-variable names (and the
  `KODA_TEXT_QUALITY_DEGENERATE_*` names) are **deprecated but still honoured**
  as backward-compatible aliases. Precedence is canonical-first: a
  `PIPECAT_STT_*` name wins when set, otherwise the legacy `KODA_*` name is
  used. No existing `KODA_STT_*` deployment breaks. New deployments should
  prefer the `PIPECAT_STT_*` names; the aliases may be removed in a future
  major release.

### Notes

- `STT_WS_TOKEN` (the client-side / probe bearer) is unchanged and remains
  strictly separate from the server-side `PIPECAT_STT_AUTH_TOKEN`.
- Wire protocol is unchanged: `PROTOCOL_VERSION == "0.1"`; the `server.hello`
  and `server.status` shapes are stable.

[0.5.1]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.5.1
[0.5.0]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.5.0
[0.4.0]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.4.0
[0.3.3]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.3.3
[0.3.2]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.3.2
[0.3.1]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.3.1
[0.3.0]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.3.0
[0.2.0]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.2.0
[0.1.2]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.1.2
[0.1.1]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.1.1
[0.1.0]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.1.0
