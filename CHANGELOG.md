# Changelog

All notable changes to `pipecat-local-stt-server` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[0.4.0]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.4.0
[0.3.3]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.3.3
[0.3.2]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.3.2
[0.3.1]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.3.1
[0.3.0]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.3.0
[0.2.0]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.2.0
[0.1.2]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.1.2
[0.1.1]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.1.1
[0.1.0]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.1.0
