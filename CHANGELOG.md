# Changelog

All notable changes to `pipecat-local-stt-server` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.2.0]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.2.0
[0.1.2]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.1.2
[0.1.1]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.1.1
[0.1.0]: https://github.com/vr000m/pipecat-local-stt-server/releases/tag/v0.1.0
