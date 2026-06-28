# Changelog

All notable changes to `pipecat-local-stt-server` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.3] - 2026-06-27

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
  excepted), creating missing dirs `0700`. This makes the socket un-plantable
  (no foreign uid can `unlink`+`bind` a replacement).
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
  install-shell umask default) and self-heals a pre-existing `0755` dir.
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

[0.3.1]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.3.1
[0.3.0]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.3.0
[0.2.0]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.2.0
[0.1.2]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.1.2
[0.1.1]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.1.1
[0.1.0]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.1.0
