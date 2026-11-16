# Transport security

Access rules, token files, and socket ownership for
[pipecat-local-stt-server](../README.md). For installation and day-to-day
commands, see [Operations](operations.md).

- [Token files](#token-file-replaces-env-tokens)
- [Port squat detection](#port-squat-detection)
- [Socket lock files](#single-server-lock-file)
- [Unix socket trust model](#trust-model-and-socket-security-same-host-uds)
- [Local boundary checks](#verifying-the-boundary-locally)

Loopback TCP has no peer-uid check. A token authenticates clients to the
server; it does not prove the server's identity to clients. Unix sockets
restrict access to the server's uid and validate the directory path.

## Token file (replaces env tokens)

`render_stt_plist.py` and `install_stt_agent.sh` refuse
`PIPECAT_STT_AUTH_TOKEN` and `KODA_STT_AUTH_TOKEN` in their environment: an env
token would be written into the plist. Put the token in a file and pass its
absolute path instead:

```bash
install -d -m 700 ~/.config/pipecat-stt                # your own directory; sets mode 700
install -m 600 /dev/null ~/.config/pipecat-stt/token
```

Write your token into that file, then install the agent:

```bash
PIPECAT_STT_AUTH_TOKEN_FILE=$HOME/.config/pipecat-stt/token \
  PIPECAT_STT_BACKEND=mlx scripts/install_stt_agent.sh install
```

An empty file fails validation.

### File and directory requirements

The installer and server both use `stt_server.auth.load_token_file`. The path
must be absolute and meet these rules:

- The token is a regular file, not a symlink, owned by the server user. It has
  no group/other permission bits and no ACL allow entries. Use mode `0600`.
- Every directory in the resolved path is owned by you or root and cannot be
  written by others. A group-writable directory is allowed only when root owns
  it and its group is `wheel` or `admin`.
- Sticky directories such as `/tmp` are refused. Once a file there disappears,
  another user could create a replacement before the next server start.
- Directory ACL allow entries must not grant any write-class right: `write`,
  `append`, `add_file`, `add_subdirectory`, `delete`, `delete_child`,
  `writeattr`, `writeextattr`, `writesecurity`, or `chown`. Deny entries are fine.

The plist records the resolved path, so repointing a symlinked directory later
cannot change which file the server opens. `ls -le` shows ACLs; `chmod -N`
removes them.

### Token content

The file must exist and contain one UTF-8 token made of visible ASCII
characters. Surrounding whitespace, including a final newline, is stripped.
An empty token, internal whitespace, or non-ASCII characters fail validation.
The client-side `status` probe is more lenient about its token file.

### Startup validation and repairs

`serve --auth-token-file` repeats validation at every start. It opens the file
with `O_NOFOLLOW`, checks the opened descriptor and resolved directory chain,
and reads from that same descriptor. A file changed after installation can
therefore stop the next startup.

The same rules apply to foreground servers and servers launched by a
supervising app. A refused file stops startup with exit 1 and a message naming
the path, problem, and repair:

```text
stt_server: auth token file '/Users/me/token' has mode 644; it must not be group/other accessible; fix: chmod 600 '/Users/me/token'
```

| Problem | Repair |
|---|---|
| Group/other permission bits | `chmod 600 <file>` |
| ACL allow entries | Inspect with `ls -le`; remove the offending file or directory ACL with `chmod -N` |
| Writable parent directory, including `/tmp` | Move the file under a directory only you can write, such as `~/.config/pipecat-stt`, and pass its absolute path |
| File owned by another user | Create your own mode-`0600` file and write the token into it |
| Symlink file | Pass the real file's path |

### Reinstalling an authenticated agent

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

## Port squat detection

`just stt-list` and `just stt-status` warn when another visible process holds
an agent's TCP port. This is a diagnostic warning, not server authentication.
Token files remain opt-in; see [Loopback TCP caveats](operations.md#loopback-tcp-caveats).

`just stt-list` and `just stt-status <backend>` compare the pid listening on a
TCP agent's port (from `lsof`) with the pid launchd reports for that agent
(`launchctl print`; the recipes hand that answer to `scripts/squat_check.py` on
stdin, never as an argument, because it carries the job's environment block and
a plist from before 0.5.0 keeps its bearer token there, while any local user can
read a process's arguments with `ps`). They warn on stderr, after a 1-second re-check so a restart
in flight stays quiet (the re-check reads the launchd pid and `runs` counter before and after its
`lsof` read; a pid or `runs` that changed in between, as when the job started,
listened and exited between two reads with no pid, or an answer with no `runs =`
line, gives no warning but a "cannot tell" note), when:

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

Identify the process before stopping it, or reinstall the agent on another port.

## Single-server lock file

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

## Trust model and socket security (same-host UDS)

Unix sockets use directory permissions and kernel peer credentials to restrict
access. TCP uses a separate authentication mechanism:

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
   *defence in depth behind* the filesystem boundary — not a strictly-stronger
   replacement for it. Every failure path (resolver returns `None`, resolver
   raises, missing transport socket, unsupported platform) **fails closed**
   (rejects), logging one warning.
3. **Bearer token — loopback TCP.** For UDS the token is redundant (the
   filesystem boundary + peer-cred both dominate it), so it is kept but not
   relied on. For loopback TCP — which has neither a file-permission boundary nor
   peer credentials — the bearer token remains the trust mechanism, alongside
   the Origin check. This change does not weaken the TCP path.

**Same-uid precondition.** Peer-cred auth assumes the client and server run as
the **same uid** (the per-user LaunchAgent deployment satisfies this). A uid
mismatch is treated as **reject**, not warn-and-allow. A future deployment that
runs the server as a daemon user and the client as the logged-in user would be
correctly rejected and would need coordination — but still no client code
change.

### Socket directory permissions

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

### macOS peer credentials

macOS has no `socket.SO_PEERCRED`, so the resolver in `stt_server/_peercred.py`
calls `getpeereid(2)` through `ctypes`. Keep both ABI details explicit: `uid_t`/`gid_t` are `c_uint32` on Darwin, and the libc function
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
