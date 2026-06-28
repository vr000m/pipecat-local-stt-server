# Operations and deployment

> Multi-backend operation, LaunchAgent install, health checks, and backend tuning for [pipecat-local-stt-server](../README.md). The day-to-day `just` operator recipes live in the [README](../README.md#managing-agents-with-just).

## Multi-backend operation

Each server process loads exactly **one** backend, pinned at launch via
`--backend {echo,mlx,parakeet,nemotron}`. To run more than one ASR — for example to
A/B-benchmark Parakeet against Whisper — start a second server process on a
**separate socket**. The V1 wire protocol is unchanged; the only difference
between two ASRs from the bot's perspective is which socket it connects to.

### Per-ASR socket convention

| ASR | LaunchAgent label | Socket | Bot selection |
|---|---|---|---|
| whisper (`mlx`) | `pipecat.stt-server` | `~/Library/Caches/pipecat-stt/stt.sock` | leave `STT_WS_SOCKET` unset |
| parakeet | `pipecat.stt-server.parakeet` | `~/Library/Caches/pipecat-stt/parakeet.sock` | set `STT_WS_SOCKET` to the parakeet socket |
| nemotron | `pipecat.stt-server.nemotron` | `~/Library/Caches/pipecat-stt/nemotron.sock` | set `STT_WS_SOCKET` to the nemotron socket |

Whisper uses the default label and socket, so a bot-side default of
`~/Library/Caches/pipecat-stt/stt.sock` resolves to it with no `.env`
change. Selecting Parakeet is a one-env-var flip: point
`STT_WS_SOCKET` at `.../parakeet.sock`. The flip is **bot-wide** — in the
dual-input bot both the Me and Them branches connect to the same resolved
endpoint, so both arms always use the same ASR. See `.env.example` for the
client-side configuration.

### Two-agent install

> **Backend dependencies.** A backend imports its ASR library lazily at startup,
> and a bare `uv run` / `uv sync` prunes the optional extras, so an installed
> agent can crash-loop on `ModuleNotFoundError`. `just stt-install <backend>` /
> `just stt-enable <backend>` ensure the matching extra is present
> (`uv sync --extra <backend> --inexact`, additive — it won't prune other
> backends); set `PIPECAT_STT_SKIP_DEP_SYNC=1` to manage extras yourself. If an
> agent does crash on a missing import, the server now exits with an actionable
> `stt_server: the '<extra>' extra is not installed … run: uv sync --extra
> <extra> --inexact` instead of a bare traceback.

`scripts/install_stt_agent.sh` is parameterised by `PIPECAT_STT_LABEL` /
`PIPECAT_STT_SOCKET` / `PIPECAT_STT_BACKEND` (the legacy `KODA_STT_*` names
are still honoured as deprecated aliases) so two LaunchAgents can coexist
without plist or log collisions:

```bash
# 1. Whisper agent — default env keeps the legacy label + socket.
scripts/install_stt_agent.sh install

# 2. Parakeet agent — distinct label, socket and backend.
#    Warm the ~1.5 GB Hugging Face model cache FIRST: a cold first launch
#    downloads it under KeepAlive + ThrottleInterval=10 and launchd may
#    throttle-loop the agent before the download finishes.
uv sync --extra parakeet
.venv/bin/python -c 'import parakeet_mlx; parakeet_mlx.from_pretrained("mlx-community/parakeet-tdt-0.6b-v3")'
PIPECAT_STT_LABEL=pipecat.stt-server.parakeet \
  PIPECAT_STT_SOCKET="$HOME/Library/Caches/pipecat-stt/parakeet.sock" \
  PIPECAT_STT_BACKEND=parakeet \
  scripts/install_stt_agent.sh install
```

The script manages exactly **one** agent per invocation, identified by
`PIPECAT_STT_LABEL` (+ its socket) — there is no registry or "all" mode. To run
any subcommand (`uninstall`/`start`/`stop`/`restart`/`status`/`logs`) against
the Parakeet agent you must re-export its `PIPECAT_STT_LABEL` and
`PIPECAT_STT_SOCKET` (legacy `KODA_STT_*` aliases still work); a default-env
invocation always targets the default `pipecat.stt-server` agent. See the recipe
in the `install_stt_agent.sh` header.

### A/B benchmark — Whisper vs Parakeet

With both agents installed and socket-live, `scripts/benchmark_asr_ab.py`
replays a corpus of utterances through **both** servers and reports
per-utterance Word Error Rate (WER) + decode latency, plus aggregates. It is
a pure V1 client — no protocol surface added — and a one-off operator tool
(no REST counterpart, not a CI gate).

```bash
# Default endpoints: whisper on stt.sock, parakeet on parakeet.sock.
uv run python scripts/benchmark_asr_ab.py --corpus path/to/corpus

# Write a full JSON report alongside the console summary.
uv run python scripts/benchmark_asr_ab.py --corpus path/to/corpus \
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


## Checking server health

The server answers a `server.status` wire event with its current session
state (queue depth, uncommitted bytes, uptime) and process health (pid,
peak RSS), and, on connect, replies with a `server.hello` carrying
protocol version, audio format, and capabilities. Both `server.hello` and
`server.status` also carry a `backend` object — `{"name": ..., "model": ...}` —
naming the ASR actually behind the socket, so a client can verify it rather
than trust the socket path. The `status` subcommand wraps that round-trip:

```bash
# Text output (exit 0 on success, 1 on not-reachable/timeout/error)
uv run python -m stt_server status --socket-path ~/Library/Caches/pipecat-stt/stt.sock

# Raw JSON for scripting / monitoring
uv run python -m stt_server status --socket-path ... --json

# Loopback TCP with bearer token
uv run python -m stt_server status --host 127.0.0.1 --port 8765 \
    --auth-token-file /path/to/token
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

- `just smoke-peercred` (`scripts/smoke_peercred.py`) — opens N concurrent
  same-uid sessions (regression guard) and, when a second local uid is reachable
  via passwordless `sudo`, drives a foreign-uid connection that must be rejected
  `403`. The cross-uid leg skips cleanly when no second uid is available.
- `scripts/verify_peercred_crossuid.py` — a stdlib-only verifier (no venv /
  `websockets`) that drives a probe as **both** the owning uid (expect `101`) and
  `nobody` (expect `403`) against one permissive socket. Same socket + perms,
  only the uid differs, so a `101`-vs-`403` split proves peer-cred — not the
  filesystem — is the discriminator. Run with `sudo -v && uv run python
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
