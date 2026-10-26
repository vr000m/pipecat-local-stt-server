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

---

# Upgrading from 0.4.x to 0.5.0

0.5.0 changes the default LaunchAgent transport from a Unix socket to loopback
TCP, on the ports in the [per-backend table](operations.md#per-backend-port-convention)
(whisper 9565, kyutai 9665, parakeet 9765, nemotron 9865). Parakeet and Nemotron
numbers are proposed, pending agreement with the menu bar app. Nothing changes
for the wire protocol, the import name or the Python client.

1. **Check downstream installer invocations first.** A reinstall without
   `PIPECAT_STT_SOCKET` flips that agent to TCP, so check how any consumer
   (Koda, onoats) invokes the installer before upgrading; this repo does not
   know their commands.

2. **Reinstall the agent.** `just stt-install <backend>` writes the TCP plist,
   removes the old socket (and its lock) it was replacing, and prints a note.
   The recipe always uses the canonical host and port; for a custom one run
   `PIPECAT_STT_PORT=... scripts/install_stt_agent.sh install`.

3. **Update client config** to the TCP endpoint: `STT_WS_HOST=127.0.0.1` and
   `STT_WS_PORT=<port>` instead of `STT_WS_SOCKET`. `STT_WS_URI` and
   `STT_WS_SOCKET` outrank `STT_WS_HOST`/`STT_WS_PORT`, so unset them or the
   client keeps using the old socket.
   - **onoats** (verified against onoats-bot `1a7b8c7`): the endpoint comes from
     env `STT_WS_SOCKET`/`STT_WS_HOST`/`STT_WS_PORT`/`STT_WS_URI`, else
     `config.toml` `[stt] ws_socket`/`ws_host`/`ws_port`/`ws_uri` (env wins), and
     is resolved with this repo's `resolve_endpoint_from_env`. Set `ws_host` and
     `ws_port` under `[stt]` and remove `ws_socket`, which would otherwise take
     precedence. (`onoats init` carries an existing `ws_socket` over when re-run,
     so check the file afterwards.)

4. **Env tokens are refused.** `PIPECAT_STT_AUTH_TOKEN` and
   `KODA_STT_AUTH_TOKEN` in the installer's environment now fail the install
   (they would be written into the plist). Write the token to a file and set
   `PIPECAT_STT_AUTH_TOKEN_FILE=<absolute path>`. Loopback agents run without a
   token by default, as the TTS agents do.

**Rollback / keeping a socket agent:**

```bash
PIPECAT_STT_SOCKET=<path> just stt-install <backend>
```

The socket plist is byte-identical to the 0.4.x one. Loopback TCP has no
peer-uid check; the socket keeps it.
