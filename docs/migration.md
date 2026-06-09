# Upgrading from 0.1.x to 0.2.0

> Migration guide for [pipecat-local-stt-server](../README.md).

0.2.0 renames the default runtime surface from the legacy `koda`-prefixed
namespace to a `pipecat`-namespaced default. Nothing changes for the wire
protocol or the Python import name (`stt_server`); only the LaunchAgent
label, default socket path, and default log dir/basenames move:

| | v0.1.x default | 0.2.0 default |
|---|---|---|
| LaunchAgent label | (legacy `koda`-prefixed) | `pipecat.stt-server` |
| Socket | `~/Library/Caches/`…`/stt.sock` (legacy dir) | `~/Library/Caches/pipecat-stt/stt.sock` |
| Log dir | `~/Library/Logs/`… (legacy dir) | `~/Library/Logs/pipecat-stt/` |
| Log basenames | (legacy `*-stt.{log,err}`) | `pipecat-stt.{log,err}` |

The deprecated `KODA_STT_*` environment-variable **names** are unaffected —
they remain honoured aliases (`KODA_STT_LABEL` / `KODA_STT_SOCKET` /
`KODA_STT_LOG_DIR` still override the new defaults). Only the default
*values* changed.

To upgrade an existing v0.1.x install:

1. **Re-run the installer.** `scripts/install_stt_agent.sh install` (with the
   default env) bootstraps the renamed `pipecat.stt-server` agent and
   automatically retires the legacy `koda`-prefixed agents — both the v0.1.x
   whisper and parakeet LaunchAgents — by booting them out of launchd and
   removing their `*.plist` files. This migration is idempotent: it is a no-op
   on a fresh machine and never retires the new agent. It only fires for the
   default `pipecat.stt-server` install; custom-label installs manage only
   their own selected label.

2. **Re-point pinned socket consumers.** Anything hard-coded to the old socket
   path must move to the new one. Set `STT_WS_SOCKET` to
   `~/Library/Caches/pipecat-stt/stt.sock` (or re-point a wrapper's
   `STT_WS_DEFAULT_SOCKET` fallback at the same path). The rename does **not**
   reach across to the external koda-pipecat `./koda stt` wrapper — its
   `STT_WS_DEFAULT_SOCKET` default still points at the old (v0.1.x) Caches
   socket, so re-point it or set `STT_WS_SOCKET` directly.

3. **Old dirs are left in place.** The previous socket and log directories are
   not deleted — they are simply orphaned and harmless once the new agent is
   running. Remove them by hand if you want to reclaim the space.
