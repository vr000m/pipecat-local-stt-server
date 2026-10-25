"""WebSocket server runtime for the local transcription service.

Lifecycle summary:

- TCP or Unix-socket listener, accepting one WebSocket per transcription
  session.
- On connect: send ``server.hello``, mint ``session_id``, send
  ``session.created``.
- Text messages parsed as JSON control events.
- Binary messages treated as raw PCM16LE audio append (V1 default path).
- ``input_audio_buffer.commit`` drains the uncommitted buffer and runs one
  backend decode to completion, streaming delta/completed events back.
- ``session.close`` drains any in-flight decode, sends ``session.closed``,
  then closes the socket. ``session.cancel`` discards uncommitted audio,
  cancels the in-flight decode, sends ``session.closed`` and closes.
- ``serve()`` installs SIGINT/SIGTERM handlers for a bounded graceful drain
  that covers both connection handlers and their decode tasks.
"""

from __future__ import annotations

import asyncio
import enum
import hmac
import json
import logging
import os
import resource
import signal
import socket
import stat as _stat
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websockets
from websockets.asyncio.server import (
    Server,
    ServerConnection,
)
from websockets.asyncio.server import (
    serve as ws_serve,
)
from websockets.asyncio.server import (
    unix_serve as ws_unix_serve,
)

from . import protocol as P
from ._peercred import peer_uid
from .backend import (
    BackendStream,
    EchoBackend,
    IdleAwareBackend,
    TranscriptionBackend,
    abort_backend,
)
from .errors import ConfigError

# OpenAI Realtime groups errors into a coarse "type" field (e.g.
# invalid_request_error, server_error, authentication_error) alongside the
# narrower "code". Map our ErrorCode enum into that taxonomy so strict
# OpenAI-shaped clients can branch on error.type without parsing strings.
_ERROR_TYPE_FOR_CODE: dict[P.ErrorCode, str] = {
    P.ErrorCode.INVALID_JSON: "invalid_request_error",
    P.ErrorCode.INVALID_EVENT: "invalid_request_error",
    P.ErrorCode.UNSUPPORTED_EVENT: "invalid_request_error",
    P.ErrorCode.INVALID_CONFIG: "invalid_request_error",
    P.ErrorCode.BUFFER_EMPTY: "invalid_request_error",
    P.ErrorCode.BUFFER_OVERFLOW: "invalid_request_error",
    P.ErrorCode.PAYLOAD_TOO_LARGE: "invalid_request_error",
    P.ErrorCode.UNAUTHORIZED: "authentication_error",
    P.ErrorCode.BACKEND_ERROR: "server_error",
    P.ErrorCode.INTERNAL_ERROR: "server_error",
}

logger = logging.getLogger("stt_server")


def _event_id() -> str:
    return f"evt_{uuid.uuid4().hex[:16]}"


def _process_peak_rss_bytes() -> int:
    # ru_maxrss units differ by platform: bytes on macOS, kibibytes on
    # Linux/BSD. Normalize so the wire value is always bytes.
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return int(rss)
    return int(rss) * 1024


def _session_id() -> str:
    return f"session_{uuid.uuid4().hex[:16]}"


def _item_id() -> str:
    return f"item_{uuid.uuid4().hex[:16]}"


def _enforce_socket_dir_secure(path: Path, trusted_root: Path) -> Path:
    """Refuse to bind unless the socket's directory chain cannot be hijacked.

    The UDS file mode (``0o600``) stops a foreign uid from ``connect()``-ing,
    but it does NOT stop a plant/swap: anyone with write on an ancestor
    directory can ``unlink()`` our socket and ``bind()`` their own at the same
    path, after which a client connects to the attacker's socket. The only
    defense is denying others write on every directory that could replace the
    socket. We therefore walk every component from the socket's bind directory
    up to AND INCLUDING ``trusted_root`` and require each to be owned by the
    running uid (``st_uid == os.geteuid()``) and not group/other-writable
    (``st_mode & 0o022 == 0``); sticky-bit directories are the explicit
    exception (the sticky bit already prevents non-owners from removing our
    inode, which is the property we need).

    Missing socket directories are created ``0o700`` and then re-``stat``-ed and
    verified rather than trusted: the ``mkdir`` mode is masked by the process
    umask, so we confirm the result instead of assuming it. A pre-existing
    directory we do not own is never silently ``chmod``/``chown``-ed — we raise
    instead, naming the offending component and condition.

    Raises ``ValueError`` on any failure; the serve entrypoint turns that into
    ``stt_server: <msg>`` + ``SystemExit(1)`` rather than a bare traceback.

    Returns the verified socket path (the literal ``path``). We verify the
    LITERAL lexical chain that clients and the LaunchAgent actually traverse —
    NOT a ``resolve()``-d one — and reject any symlink component (``lstat``).
    The kernel resolves the literal socket path at connect/bind time, so the
    directories that could replace the socket are the literal lexical ancestors.
    Resolving here would verify a *different* chain than clients use: a symlinked
    but group/other-writable lexical ancestor would be skipped, then repointed
    after startup to make clients connect to an attacker's socket while the
    server stayed bound to the safe resolved target. By rejecting symlinks the
    verified chain, the bound path, and the client-visible path are identical.
    """
    euid = os.geteuid()
    bind_dir = path.parent

    # Verify (and bind) the literal path, so we never resolve past a symlink the
    # client will still traverse. An absolute, ``..``-free path is required: with
    # no ``resolve()`` the containment check below is purely lexical, and a
    # ``..`` segment could let a path that escapes the trusted root pass it.
    if not bind_dir.is_absolute():
        raise ConfigError(
            f"socket directory {bind_dir} must be an absolute path under {trusted_root}"
        )
    if ".." in bind_dir.parts:
        raise ConfigError(
            f"socket directory {bind_dir} must not contain '..' components; "
            f"point the socket at a direct path under {trusted_root}"
        )

    # The socket MUST live under the trusted root. A path outside it (e.g. a
    # custom socket path pointing at a world-writable location) has no
    # owner-only chain we can vouch for, and walking to the filesystem root
    # would only verify root-owned system directories that we do not own. Refuse
    # rather than pretend the path is safe.
    if bind_dir != trusted_root and trusted_root not in bind_dir.parents:
        raise ConfigError(
            f"socket directory {bind_dir} is not under the trusted root "
            f"{trusted_root}; point the socket at a path under {trusted_root}"
        )

    # Create every missing component owner-only. Each is created with its own
    # ``mkdir(mode=0o700)`` rather than ``parents=True``: the latter creates
    # intermediate parents at the umask default (not ``mode``), which would
    # leave a group/other-readable directory in the chain. ``mkdir``'s mode is
    # itself umask-masked, but ``0o700`` carries no group/other bits for any
    # umask to widen, and the walk below re-stats and verifies regardless.
    to_create: list[Path] = []
    cursor = bind_dir
    while not cursor.exists():
        to_create.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    for missing in reversed(to_create):
        missing.mkdir(mode=0o700)

    component = bind_dir
    while True:
        # ``lstat`` (not ``stat``) so a symlinked component is detected, not
        # silently followed. As we walk up, every component eventually becomes
        # the final path element, so a symlink anywhere in the chain is caught.
        st = os.lstat(component)
        if _stat.S_ISLNK(st.st_mode):
            raise ConfigError(
                f"socket directory component {component} is a symlink; refusing "
                "to bind (a symlinked ancestor can be repointed by another user "
                "to hijack the client-visible socket path)"
            )
        if st.st_uid != euid:
            raise ConfigError(
                f"socket directory component {component} is owned by uid "
                f"{st.st_uid}, not the server uid {euid}; refusing to bind "
                "(another user could replace the socket at this path)"
            )
        sticky = bool(st.st_mode & 0o1000)
        if not sticky and (st.st_mode & 0o022):
            raise ConfigError(
                f"socket directory component {component} is group/other-writable "
                f"(mode {oct(st.st_mode & 0o7777)}); refusing to bind "
                "(another user could replace the socket at this path)"
            )
        if component == trusted_root:
            break
        component = component.parent

    # Bind on the literal, now-verified path: the chain clients traverse is
    # exactly the chain we vouched for (no symlinks), so verified == bound ==
    # client-visible.
    return path


def _lock_path(socket_path: str | os.PathLike) -> str:
    return f"{socket_path}.lock"


# How long shutdown waits, past the drain timeout, for cancelled session tasks
# to unwind, and then again for the listener's ``wait_closed``, before it
# proceeds regardless (what outlives either stays live work).
_CANCEL_GRACE_SECONDS = 2.0

# How long ``acquire_socket_lock`` retries a contended lock before reporting
# EADDRINUSE. A live server holds it for its whole lifetime; a preflight probe
# (``socket_lock_held``) holds it for microseconds, and must not make a
# concurrent real starter fail.
_LOCK_RETRY_SECONDS = 0.25
_LOCK_RETRY_INTERVAL = 0.01


def acquire_socket_lock(socket_path: str | os.PathLike) -> int:
    """Take the single-server lock for ``socket_path``; return the held fd.

    ``connect()`` cannot tell a stale socket from a live one whose listen
    backlog is full (macOS returns ECONNREFUSED for both), so liveness is
    decided by an ``flock`` on ``<socket>.lock`` instead. The kernel drops the
    lock when the owning process dies, so it can never go stale; clients never
    touch it, so any number of them share one server. The lock file is left in
    place on exit (unlinking it would race a concurrent starter). Mode rule:
    the file is opened read-write; if that works and it is a regular file we
    own, any mode other than 0600 is set to 0600 with ``fchmod`` on the open fd
    (another uid able to open it could hold the lock and block every future
    server; ``fchmod`` never depends on the process umask, which with
    ``0o777`` would otherwise create a mode-000 file no later start could
    open). If the open itself is denied (e.g. mode 000 or 0400) it raises
    ``OSError(EACCES)`` naming the remedy (``chmod 600``); one we do not own,
    or not a regular file, is refused (``EACCES``). Raises
    ``OSError(EADDRINUSE)`` when another process holds it for longer than a
    brief probe would.
    """
    import errno
    import fcntl

    path = _lock_path(socket_path)
    try:
        # O_NONBLOCK: a FIFO planted at the path must not block the open.
        fd = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            0o600,
        )
    except PermissionError as exc:
        raise OSError(
            errno.EACCES,
            f"cannot open the single-server lock file {path} ({exc.strerror}); "
            "if it is yours, chmod 600 it (or remove it while no server runs)",
        ) from exc
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
            raise OSError(errno.EACCES, f"{path} is not a regular file owned by this user")
        if _stat.S_IMODE(st.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        deadline = time.monotonic() + _LOCK_RETRY_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise OSError(
                        errno.EADDRINUSE, f"a server is already running for {socket_path}"
                    ) from exc
                time.sleep(_LOCK_RETRY_INTERVAL)
    except BaseException:
        os.close(fd)
        raise
    return fd


def socket_lock_held(socket_path: str | os.PathLike) -> bool:
    """True when another process holds the lock for ``socket_path``.

    Read-only probe for preflight: never creates the lock file, never follows a
    symlink, and treats anything that is not a plain owned file as "not held"
    (``start()`` will report it properly when it tries to acquire). Probes with
    a *shared* lock: that conflicts with a server's exclusive lock (so a live
    server reads as held) but not with another probe. A shared lock still
    conflicts with a concurrent starter's exclusive one, so the probe holds it
    only for an instant and ``acquire_socket_lock`` retries briefly.
    """
    import errno
    import fcntl

    try:
        # O_NONBLOCK: preflight runs before the socket-directory trust check, so
        # the path may be in a directory another user writes; a FIFO planted
        # there must not hang the open.
        fd = os.open(
            _lock_path(socket_path),
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        )
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
            return False  # not a plain owned file: start() reports it
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            return exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


@dataclass
class ServerConfig:
    """Transport and policy configuration for ``TranscriptionServer``."""

    socket_path: str | None = None
    host: str | None = None
    port: int | None = None
    auth_token: str | None = None
    reject_browser_origins: bool = True
    max_append_bytes: int = P.MAX_APPEND_BYTES
    max_uncommitted_bytes: int = P.MAX_UNCOMMITTED_BYTES
    # Bounded write-buffer per session: if the socket's pending-send buffer
    # exceeds this many bytes, the server closes the session with
    # ``send_queue_overflow`` rather than blocking the decode loop on a slow
    # consumer.
    send_queue_high_water_bytes: int = P.SEND_QUEUE_HIGH_WATER_BYTES
    drain_timeout_seconds: float = P.SHUTDOWN_DRAIN_TIMEOUT_SECONDS
    # chmod applied to UDS after bind. 0o600 restricts connect to the owning
    # user; set to None to inherit the process umask (not recommended on
    # shared hosts — the UDS is the V1 trust boundary).
    unix_socket_mode: int | None = 0o600

    def __post_init__(self) -> None:
        if self.socket_path is None and (self.host is None or self.port is None):
            raise ConfigError("ServerConfig requires socket_path or host+port")
        if self.host is not None and self.host not in ("127.0.0.1", "::1", "localhost"):
            raise ConfigError("V1 only permits loopback binds (127.0.0.1/::1/localhost)")


@dataclass
class _SessionState:
    session_id: str
    config: dict = field(default_factory=lambda: {"turn_detection": None})
    buffer: bytearray = field(default_factory=bytearray)
    # ``asyncio.Task`` here reflects the V1 in-process topology. A future
    # worker-process backend would replace this with a cross-process handle.
    in_flight_task: asyncio.Task | None = None
    current_stream: BackendStream | None = None
    closed: bool = False
    started_monotonic: float = field(default_factory=time.monotonic)


def _log_leftover_failure(fut: asyncio.Future) -> None:
    """Done callback for work nobody awaits any more: log (and so retrieve) a
    failure instead of leaving an "exception was never retrieved"."""
    if fut.cancelled():
        return
    exc = fut.exception()
    if exc is not None:
        logger.error("stt_server: leftover task failed", exc_info=exc)


class _State(enum.Enum):
    """``TranscriptionServer`` lifecycle (see the class docstring)."""

    NEW = "new"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


# The only legal transitions; ``_transition`` refuses anything else.
_TRANSITIONS: dict[_State, frozenset[_State]] = {
    # start() begins / shutdown() before any start.
    _State.NEW: frozenset({_State.STARTING, _State.STOPPED}),
    # start() succeeded / start() failed and nothing is left running
    # (retryable) / shutdown() during start, or a failed start that left work
    # running (single-use from then on).
    _State.STARTING: frozenset({_State.RUNNING, _State.NEW, _State.STOPPING}),
    # shutdown() drains.
    _State.RUNNING: frozenset({_State.STOPPING}),
    # The last live work finished.
    _State.STOPPING: frozenset({_State.STOPPED}),
    _State.STOPPED: frozenset(),
}

# Entering one of these releases the single-server lock: ``_transition`` is the
# only place that does, and it refuses while any live work remains.
_UNLOCKED_STATES = frozenset({_State.NEW, _State.STOPPED})

# ``adopt`` is valid only while a lock is held or being released (it adds
# live work, which must never exist where nothing holds the lock).
_ADOPTABLE_STATES = frozenset({_State.STARTING, _State.RUNNING, _State.STOPPING})

# How ``_live_reasons`` names each kind of live work.
_WORK_LABELS = {
    "listener": "listener bound",
    "session": "session/decode tasks",
    "step": "start/drain steps",
    "close": "backend close",
    "bind": "listener bind",
    "listener-close": "listener close",
    "adopted": "adopted",
    "thread": "backend worker thread",
}


@dataclass
class _Work:
    """One entry of the live-work ledger: what it is, whether it outlived the
    bound its caller waited for (``stuck``: a diagnostic, shown as
    ``(N stuck)`` in ``live_work_reasons``), and the cleanup that must run when it completes,
    before it leaves the ledger (``then``: it registers any successor work
    first, via ``_hand_off``, so the lock never falls between). A cleanup that
    raises keeps its entry in the ledger for good: fail closed. ``label``: an
    ``adopt`` caller's own name for its step (``live_work_reasons``)."""

    kind: str
    stuck: bool = False
    then: Callable[[asyncio.Future], None] | None = None
    label: str | None = None


class TranscriptionServer:
    """Owns the listener and lifecycle for in-process WebSocket sessions.

    Lifecycle: one explicit state machine, ``NEW -> STARTING -> RUNNING ->
    STOPPING -> STOPPED`` (``state``), changed only through ``_transition``.

    - ``start()`` from ``NEW``: ``STARTING`` until the listener is bound, then
      ``RUNNING``. A failed or cancelled start runs the same drain as
      ``shutdown()`` (listener closed, our socket inode unlinked, sessions it
      accepted drained, a started backend ``close()``d) and then the backend's
      ``abort()``. If nothing is left running it returns to ``NEW`` (lock
      released, every per-attempt slot reset by ``_reset_attempt``), so it may
      be retried; if work outlived the drain (a session task, a backend close,
      a bind that finished after a second cancel) it moves to ``STOPPING``
      instead and the server is single-use. ``start()`` while ``RUNNING``
      returns; while ``STARTING`` raises ``RuntimeError`` (a second starter
      never touches the first one's lock).
    - **Single-use:** once ``shutdown()`` has been called (in any state, even
      ``NEW``), or a failed start left work running (``STOPPING``), the server
      never starts again: ``start()`` raises ``RuntimeError`` naming which of
      the two happened. A failed start that returned to ``NEW`` may be
      retried. Build a new ``TranscriptionServer`` (the backend may be reused)
      to serve again, after ``wait_stopped()``.
    - ``shutdown()`` is idempotent, safe before ``start()``, and safe to call
      concurrently (later callers wait for the first one's drain, including the
      backend ``close()``) or to cancel (the drain is the server's own task:
      cancelling a caller only ends that caller's wait).

    Single-server lock invariants (Unix-socket mode):

    1. The lock (``holds_socket_lock``) is never held in ``NEW`` or
       ``STOPPED``; it is taken early in ``STARTING`` and held through
       ``RUNNING`` and ``STOPPING``.
    2. "Live work" is one ledger (``_live_work``; ``_live_reasons`` reads
       only it; it is also the one registry of session / decode tasks, which
       the drain waits on and cancels): a listener bind, a bound listener,
       its close (until websockets' ``wait_closed`` returns), a session or
       decode task, an in-flight start or drain, a backend ``close()``, an
       adopted startup / readiness step, or a backend worker thread (the
       optional ``idle()`` hook: a cancelled decode task can leave its daemon
       thread behind). Every task the server creates is in it from creation.
       An entry is live until its OWN cleanup has run (``_retire``), not just
       until its future is done: that cleanup registers any successor work
       (an orphan bind's listener close) before the entry leaves, then
       re-settles the state. A cleanup that raises leaves its entry live
       (marked stuck): the lock is never released with that work untracked.
       Successor-before-predecessor is one primitive, ``_hand_off``; entries
       are added only by it and ``_track``, and dropped only by ``_end_work``.
    3. The lock is released only by ``_transition`` into ``NEW`` or
       ``STOPPED``, which refuses while any live work remains. ``STOPPING ->
       STOPPED`` happens as soon as the last live work ends.
    4. A started backend is ``close()``d on every exit from ``start()`` that
       does not reach ``RUNNING``. Every wait on someone else's code (sessions,
       the listener's ``wait_closed``, ``backend.close()``) is bounded; what
       outlives its bound stays in the ledger (marked stuck, a diagnostic
       shown in ``live_work_reasons``). ``live_work_count`` is the ledger's size: the
       lock is held while it is non-zero, and it is what ``serve``'s hard exit
       reads, so an entry of any kind keeps both.
    5. Per-attempt state (listener, bound socket inode, backend close task,
       drain task, the shutdown event) is reset in one place,
       ``_reset_attempt``, on every return to ``NEW``.

    Which signal an embedder reads (the one place this is documented):

    - To wait until the server is gone (lock released, nothing running), await
      ``wait_stopped()``. Nothing else is needed for correctness.
    - ``live_work_count`` / ``live_work_reasons``: what is still live. It is
      non-zero throughout ``running`` (the bound listener is an entry), so it
      is only meaningful after ``shutdown()``: non-zero then means leftovers
      that keep the lock (what ``serve``'s hard exit reads). A reason ending
      ``(N stuck)`` counts entries that outlived the bound their caller
      waited for: a diagnostic for logs, never a decision.
    - ``state`` / ``holds_socket_lock``: for logs and tests.
    - ``wait_closed()``: the current listener's close only (websockets'
      ``wait_closed``); not a lock or stop signal. Use ``wait_stopped()``.
    - ``adopt(task, abandoned=..., label=...)``: hand the server a task of
      yours that touches it or its backend, so the lock is kept until it ends.
      Valid only while ``can_adopt`` (``starting`` / ``running`` /
      ``stopping``); otherwise (``new`` or ``stopped``: no lock held) it
      raises ``RuntimeError``. ``serve`` uses it
      for its readiness step from creation and, ``abandoned=True``, for a
      startup or readiness step that ignored its cancel.
    """

    def __init__(self, backend: TranscriptionBackend, config: ServerConfig) -> None:
        self._backend = backend
        self._config = config
        # Set when the drain begins: sessions read it to tell a server shutdown
        # from a client close.
        self._shutdown_event = asyncio.Event()
        self._state = _State.NEW
        # The single-server lock fd: only ever held here, never handed out.
        self._lock_fd: int | None = None
        # THE live-work ledger (invariant 2): everything that can still touch
        # the socket or the backend -- a bind, the bound listener and its
        # close, session / decode tasks (from creation; no other registry), an
        # in-flight start or drain, a backend close, an adopted step, a
        # backend worker thread. The lock decision (``_live_reasons``) and
        # every public count are views of it; nothing else counts.
        self._live_work: dict[object, _Work] = {}
        # True once ``shutdown()`` was called (terminal; not per attempt).
        self._shutdown_requested = False
        # Set while the lock is free and no work runs (``new`` / ``stopped``):
        # what ``wait_stopped()`` waits for.
        self._unlocked = asyncio.Event()
        self._unlocked.set()
        self._reset_attempt()

    def _reset_attempt(self) -> None:
        """Clear every per-start slot, together (the one reset point).

        Called at construction and whenever a failed start returns to ``NEW``
        (only with no live work left), so a retry never inherits the previous
        attempt's listener, socket inode, backend close or drain."""
        # The listener this attempt bound.
        self._server: Server | None = None
        # (st_dev, st_ino) of the Unix socket file THIS attempt bound, recorded
        # right after bind so only our own inode is ever unlinked.
        self._bound_socket_id: tuple[int, int] | None = None
        # The one backend ``close()`` of this attempt (shared by every drain).
        self._close_task: asyncio.Future | None = None
        # The drain of a running server: server-owned, so cancelling one
        # ``shutdown()`` caller never abandons it; every caller waits on it.
        self._stop_task: asyncio.Task | None = None
        # The ledger itself spans attempts: ``_transition`` only reaches
        # ``NEW`` with it empty, so there is nothing to clear.
        self._shutdown_event.clear()  # a failed start's drain set it

    # --- lifecycle ---
    @property
    def state(self) -> str:
        """The lifecycle state: ``new``, ``starting``, ``running``, ``stopping``
        (draining, or waiting for leftover work) or ``stopped``."""
        return self._state.value

    @property
    def holds_socket_lock(self) -> bool:
        """Whether this server holds the single-server lock right now."""
        return self._lock_fd is not None

    def _transition(self, new: _State) -> None:
        """Change state; the ONLY place the single-server lock is released.

        Entering ``NEW`` or ``STOPPED`` releases the lock, and is refused while
        any live work remains (invariant 3), so no caller can release it early
        by forgetting a check."""
        if new not in _TRANSITIONS[self._state]:
            raise RuntimeError(
                f"stt_server: illegal lifecycle transition {self._state.value} -> {new.value}"
            )
        if new in _UNLOCKED_STATES:
            live = self._live_reasons()
            if live:
                raise RuntimeError(
                    f"stt_server: lifecycle {self._state.value} -> {new.value} "
                    f"refused while live work remains: {', '.join(live)}"
                )
        self._state = new
        if new in _UNLOCKED_STATES:
            self._release_lock()
            if new is _State.NEW:
                self._reset_attempt()  # a retry starts from a clean attempt
            self._unlocked.set()
        else:
            self._unlocked.clear()

    def _live_entries(self) -> list[_Work]:
        """Every ledger entry. An entry is live until ITS OWN cleanup has run
        (``_retire``), not merely until its future is done: a finished future
        whose done-callback is still queued has not yet registered its
        successor work (an orphan bind's listener close), so dropping it then
        would let another entry's end release the lock in between."""
        return list(self._live_work.values())

    def _sessions(self) -> set[asyncio.Future]:
        """The session / decode tasks still in the ledger (the one registry of
        them: the drain waits on and cancels exactly these)."""
        return {k for k, w in self._live_work.items() if w.kind == "session" and not k.done()}

    def _live_reasons(self) -> list[str]:
        """Everything that can still touch the socket or the backend (the one
        live-work predicate, read from the one ledger; empty means the lock
        may be released)."""
        counts: dict[tuple[str, str | None], list[int]] = {}
        for w in self._live_entries():
            n = counts.setdefault((w.kind, w.label), [0, 0])
            n[0] += 1
            n[1] += w.stuck
        reasons = []
        for (kind, own), (n, stuck) in counts.items():
            label = _WORK_LABELS.get(kind, kind)
            if own is not None:
                label = f"{label}: {own}"
            reason = label if kind == "listener" else f"{n} {label}"
            # Entries that outlived the bound their caller waited for.
            reasons.append(f"{reason} ({stuck} stuck)" if stuck else reason)
        return reasons

    def _begin_work(self) -> object:
        """Count a step of ours (a start, the drain) as live work; end it with
        ``_end_work`` -- synchronously, so the lock is released before the step
        returns to its caller."""
        token = object()
        self._hand_off(None, token, "step")
        return token

    def _end_work(self, key: object) -> None:
        """Drop ``key`` from the ledger (the one removal) and re-settle."""
        self._live_work.pop(key, None)
        self._settle()

    def _hand_off(
        self, predecessor: object | None, successor: object, kind: str, *, stuck: bool = False
    ) -> None:
        """Register ``successor`` as live work of ``kind``, THEN drop
        ``predecessor`` (``None``: nothing to drop) -- one step, so there is no
        instant with neither in the ledger. ``successor`` is a future
        (``_track``: live until it completes and its cleanup ran) or a plain
        key (a bound listener, a step token: live until ``_end_work``). If
        registering raises, the predecessor stays: fail closed."""
        if isinstance(successor, asyncio.Future):
            self._track(successor, kind, stuck=stuck)
        else:
            entry = self._live_work.setdefault(successor, _Work(kind))
            entry.stuck = entry.stuck or stuck
        if predecessor is not None and predecessor is not successor:
            self._end_work(predecessor)

    def _track(
        self,
        fut: asyncio.Future,
        kind: str,
        *,
        stuck: bool = False,
        then: Callable[[asyncio.Future], None] | None = None,
        label: str | None = None,
    ) -> None:
        """Count ``fut`` as live work of ``kind`` until it has completed AND
        its ``then`` cleanup has run (``_retire``, which re-settles the state).
        ``stuck``: it already outlived the bound its caller waited for.

        An already-done future is no live work, but a ``then`` given for one is
        never dropped: it runs now, through ``_retire`` (so a failing one still
        fails closed)."""
        if fut.done():
            if then is not None:
                self._live_work.setdefault(fut, _Work(kind)).then = then
                self._retire(fut)
            return
        entry = self._live_work.get(fut)
        if entry is None:
            self._live_work[fut] = entry = _Work(kind, label=label)
            fut.add_done_callback(self._retire)
        if then is not None:
            entry.then = then
        if stuck and not entry.stuck:
            entry.stuck = True
            # Nobody awaits it any more: log its failure.
            fut.add_done_callback(_log_leftover_failure)

    def _retire(self, fut: asyncio.Future) -> None:
        """A tracked future's ONE done-callback: run its cleanup (which
        registers any successor work), and only then drop it from the ledger
        and re-settle -- one atomic step, so no other entry's end can release
        the lock between the two.

        Fails closed: a cleanup that raises (an orphan listener whose close
        failed is still serving) leaves the entry in the ledger, marked stuck,
        so the lock stays held until the process exits (the CLI's hard exit
        sees it) instead of being released with that work untracked."""
        entry = self._live_work.get(fut)
        if entry is not None and entry.then is not None:
            then, entry.then = entry.then, None
            try:
                then(fut)
            except BaseException as exc:
                entry.stuck = True
                logger.exception(
                    "stt_server: cleanup of a finished %s failed; keeping the "
                    "single-server lock until the process exits",
                    _WORK_LABELS.get(entry.kind, entry.kind),
                )
                if not isinstance(exc, Exception):
                    raise
                return
        self._end_work(fut)

    def _linger(self, fut: asyncio.Future, kind: str) -> None:
        """``fut`` outlived the bound its caller waited for: it stays live work
        (the lock outlives it) and is marked stuck."""
        self._track(fut, kind, stuck=True)

    def adopt(self, fut: asyncio.Future, *, abandoned: bool = False, label: str = "step") -> None:
        """Hand the server a task of yours that touches it or its backend. It
        counts as live work until it finishes, so the lock is kept meanwhile
        (and a failed start that sees it ends in ``stopping``, not ``new``).

        Precondition: ``can_adopt`` (state ``starting``, ``running`` or
        ``stopping``). In ``new`` or ``stopped`` it raises ``RuntimeError`` and records nothing:
        no lock is held there, so live work would either block a lock release
        that ``shutdown()`` needs (``new``: it would be refused) or run with
        the lock already released (``stopped``).

        ``abandoned=False`` (default): you still await it (``serve``'s
        readiness step, from creation). ``abandoned=True``: you stopped
        waiting for it (``serve`` does this with a step that ignored its
        cancel); it is marked stuck (``(N stuck)`` in ``live_work_reasons``) and
        its failure is
        logged -- at once if it already failed. A future that is already done
        adds no live work. Adopting a task again only adds the stuck mark.
        ``label`` is your own short name for it (``live_work_reasons`` shows
        ``"1 adopted: <label>"``)."""
        if not isinstance(label, str) or not label.strip():
            raise ValueError("adopt(label=...) must be a non-empty string")
        if not self.can_adopt:
            raise RuntimeError(
                f"stt_server: adopt() in state {self._state.value} refused; "
                "valid only while starting, running or stopping"
            )
        if fut.done():
            if abandoned:
                _log_leftover_failure(fut)
            return
        self._track(fut, "adopted", stuck=abandoned, label=label)

    @property
    def can_adopt(self) -> bool:
        """True while ``adopt()`` is valid (a lock is held or being released:
        ``starting``, ``running``, ``stopping``). Check it instead of
        ``state`` before adopting."""
        return self._state in _ADOPTABLE_STATES

    async def wait_stopped(self) -> None:
        """Wait until the server holds no lock and runs no work: state
        ``stopped``, or ``new`` (never started, or a failed start that left
        nothing running).

        ``shutdown()`` returning does not always mean that: from ``starting``,
        or after a drain whose leftovers outlived their bounds (any live work
        left, ``live_work_count``: a session, a backend close, a backend
        worker thread), the server sits in ``stopping`` with the lock held. Await this before building a new
        server on the same socket, and before closing the event loop: the
        lock is released by a callback on this loop, so closing it with work
        still running keeps the lock until the process exits."""
        await self._unlocked.wait()

    def _settle(self) -> None:
        """``STOPPING -> STOPPED`` as soon as no live work remains."""
        if self._state is _State.STOPPING and not self._live_reasons():
            self._transition(_State.STOPPED)

    def _release_lock(self) -> None:
        fd, self._lock_fd = self._lock_fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    async def start(self) -> None:
        if self._state is _State.RUNNING:
            return
        if self._state is _State.STARTING:
            raise RuntimeError("stt_server: start() is already in progress")
        if self._state is not _State.NEW:
            cause = (
                "start() after shutdown()"
                if self._shutdown_requested
                else "start() after a failed start that left work running"
            )
            raise RuntimeError(
                f"stt_server: {cause} (state {self._state.value}); "
                "a TranscriptionServer is single-use"
            )
        self._transition(_State.STARTING)
        # An in-flight start is live work: a ``shutdown()`` landing now leaves
        # the unwind (and the lock) to this start.
        in_flight = self._begin_work()
        backend_started = False
        try:
            verified_socket = self._lock_for_start()
            await self._backend.start()
            backend_started = True
            # Checkpoint: a ``shutdown()`` that ran while the backend started
            # must not be followed by a bind (nobody would ever close it).
            self._check_still_starting()
            await self._bind_listener(verified_socket)
            # Checkpoint: ``shutdown()`` ran during the bind; the unwind below
            # closes the listener we just bound and unlinks our socket inode.
            self._check_still_starting()
            self._transition(_State.RUNNING)
        except BaseException:
            await self._unwind_failed_start(backend_started)
            raise
        finally:
            # The one removal path. In ``starting`` its re-settle is a no-op
            # (only ``stopping`` settles), so the transition below still
            # decides between ``new`` and ``stopping``.
            self._end_work(in_flight)
            if self._state is _State.STARTING:
                # Failed start, no shutdown: retryable only if nothing it
                # started is still running; otherwise wait it out, single-use.
                self._transition(_State.STOPPING if self._live_reasons() else _State.NEW)
            self._settle()
        self._log_listening()

    def _check_still_starting(self) -> None:
        if self._state is not _State.STARTING:
            raise RuntimeError("stt_server: shutdown() was called during startup")

    async def _unwind_failed_start(self, backend_started: bool) -> None:
        """Undo a start that will not reach ``RUNNING``.

        A server that never started is never shut down normally, so run the
        same drain as ``shutdown()`` (closing any listener this start bound and
        draining sessions it accepted meanwhile), ``close()`` a backend whose
        ``start()`` returned (bounded), and run the backend's cheap ``abort()``
        hook (parakeet / nemotron would otherwise leak their private temp dir
        on every failed start, e.g. a launchd crash-loop against a held lock).
        The lock is ``start()``'s ``finally`` to give back, via ``_transition``.
        """
        try:
            await self._drain(close_backend=backend_started)
        except Exception:  # noqa: BLE001 -- best-effort; the start error wins
            logger.exception("stt_server: cleanup after a failed start failed")
        finally:
            abort_backend(self._backend, logger)

    def _lock_for_start(self) -> Path | None:
        """Verify the socket directory and take the single-server lock."""
        if not self._config.socket_path:
            return None
        socket_path = Path(self._config.socket_path)
        # Trusted root for the ancestor walk: the user's home directory. The
        # default socket lives under ~/Library/Caches/pipecat-stt (see
        # scripts/install_stt_agent.sh), so on stock macOS every component
        # from the cache dir up through home is owner-owned and 0700. Enforce
        # an owner-only, non-group/other-writable chain from the socket
        # directory up to and including home BEFORE bind so the socket cannot
        # be planted/swapped by another local uid. This replaces a bare
        # mkdir that trusted the path blindly and verifies the full ancestor
        # walk, not just the immediate parent. The enforcement rejects any
        # symlink component and returns the literal verified path, so the
        # chain we vouched for is exactly the one clients traverse.
        verified_socket = _enforce_socket_dir_secure(socket_path, Path.home())
        # Single-server lock BEFORE the model load: a second instance fails
        # in milliseconds instead of after a multi-GB load, and the socket
        # file below is only ever replaced by the lock holder.
        self._lock_fd = acquire_socket_lock(verified_socket)
        return verified_socket

    async def _bind(self, coro: Awaitable[Server]) -> Server:
        """Await a listener bind; never leak the listener if cancelled mid-bind.

        The bind itself is synchronous inside ``create_server`` but is followed
        by an await, so a cancellation landing there would drop a bound,
        serving listener. Shield it. On cancel, wait for the bind and record
        its listener in ``_server`` (``start()``'s unwind closes it). If that
        wait is cancelled too, the unfinished bind stays live work, marked
        stuck (the lock is held until it finishes), and its listener is closed
        the moment it exists -- as the bind's own cleanup, before the bind
        leaves the ledger.
        """
        task = asyncio.ensure_future(self._bound(coro))
        self._track(task, "bind")  # live work from creation
        try:
            server = await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done():
                try:
                    await asyncio.wait({task})
                except asyncio.CancelledError:
                    if not task.done():
                        self._track(task, "bind", stuck=True, then=self._close_orphan_listener)
                        raise
            if not task.cancelled() and task.exception() is None:
                self._adopt_listener(task.result())
            raise
        self._adopt_listener(server)
        return server

    async def _bound(self, coro: Awaitable[Server]) -> Server:
        """Await the listener bind, then record the socket inode in the same
        step. Recording only after ``_bind`` resumes leaves a loop turn in
        which a hard exit (a stop that wins the race with startup) finds a
        bound socket whose inode is unrecorded, so ``abort_sync`` cannot prove
        it is ours and leaves the file behind."""
        server = await coro
        self._record_bound_socket()
        return server

    def _adopt_listener(self, server: Server) -> None:
        self._server = server
        self._hand_off(None, server, "listener")
        self._record_bound_socket()

    def _close_orphan_listener(self, task: asyncio.Future) -> None:
        """A bind that finished after its ``start()`` gave up on it: close it.

        Runs as the bind's ``_retire`` cleanup: the bind is still in the
        ledger, so the lock is held and the inode at the path is this bind's
        and may be unlinked. ``Server.close()`` only schedules the close of the
        listening socket, so the close itself (``wait_closed``) is registered
        as live work here, before the bind leaves the ledger: the lock is
        released only once the fd is really closed."""
        if task.cancelled() or task.exception() is not None:
            return
        self._record_bound_socket()
        self._begin_listener_close(task.result(), stuck=True)

    def _record_bound_socket(self) -> None:
        """Record the (st_dev, st_ino) of the Unix socket this process bound.

        Called right after bind, while this start holds the single-server lock,
        so the inode at the path is ours."""
        if not self._config.socket_path:
            return
        try:
            st = os.stat(self._config.socket_path)
            if _stat.S_ISSOCK(st.st_mode):
                self._bound_socket_id = (st.st_dev, st.st_ino)
        except OSError:
            pass

    def _unlink_own_socket(self) -> None:
        """Remove the Unix socket file only if it is still the inode we bound."""
        bound = self._bound_socket_id
        if bound is not None and self._config.socket_path:
            path = str(self._config.socket_path)
            try:
                st = os.lstat(path)
                if _stat.S_ISSOCK(st.st_mode) and (st.st_dev, st.st_ino) == bound:
                    os.unlink(path)
            except OSError:
                pass

    async def _bind_listener(self, verified_socket: Path | None) -> None:
        if verified_socket is not None:
            # Restrict the socket file to owner-only before bind so the UDS
            # trust boundary actually holds on multi-user hosts.
            prior_umask = os.umask(0o077)
            try:
                await self._bind(
                    ws_unix_serve(
                        self._handle_connection,
                        path=str(verified_socket),
                        max_size=self._config.max_append_bytes,
                        process_request=self._process_request,
                    )
                )
            finally:
                os.umask(prior_umask)
            if self._config.unix_socket_mode is not None:
                try:
                    os.chmod(verified_socket, self._config.unix_socket_mode)
                except OSError as exc:
                    logger.warning("stt_server: chmod on UDS failed: %s", exc)
        else:
            await self._bind(
                ws_serve(
                    self._handle_connection,
                    host=self._config.host,
                    port=self._config.port,
                    max_size=self._config.max_append_bytes,
                    process_request=self._process_request,
                )
            )

    def _log_listening(self) -> None:
        logger.info(
            "stt_server listening on %s",
            self._config.socket_path or f"{self._config.host}:{self._config.port}",
        )
        # TCP without a token accepts unauthed clients. UDS mode is protected
        # by the 0o600 file permission and the local trust boundary; TCP has
        # neither. Make the gap loud so operators notice before shipping.
        if self._config.socket_path is None and not self._config.auth_token:
            logger.warning(
                "stt_server: TCP listener is running without --auth-token; "
                "any local process that can reach %s:%s can connect. "
                "Configure PIPECAT_STT_AUTH_TOKEN (or the deprecated "
                "KODA_STT_AUTH_TOKEN, or --auth-token-file) for "
                "any non-experimental deployment.",
                self._config.host,
                self._config.port,
            )

    @property
    def sockets_bound(self) -> list:
        if self._server is None:
            return []
        return list(self._server.sockets or [])

    def listening_port(self) -> int | None:
        """For TCP mode, return the actual port (helpful with port=0)."""
        for s in self.sockets_bound:
            try:
                name = s.getsockname()
            except Exception:
                continue
            # A Unix socket's name is a path string, not a (host, port) tuple.
            if isinstance(name, tuple) and len(name) >= 2:
                return name[1]
        return None

    @property
    def backend(self) -> TranscriptionBackend:
        return self._backend

    @property
    def config(self) -> ServerConfig:
        return self._config

    async def wait_closed(self) -> None:
        """Wait for the current listener's close (websockets' ``wait_closed``).
        Listener only: not a lock or stop signal -- ``wait_stopped()`` is."""
        if self._server is not None:
            await self._server.wait_closed()

    def abort_sync(self) -> None:
        """Best-effort, synchronous cleanup for a hard process exit.

        Used by ``serve`` when a stop arrives while startup/warm-up is still
        running and the process must ``os._exit`` (model-load threads cannot be
        cancelled), which skips ``shutdown()`` and ``backend.close()``. Removes
        the Unix socket file only if it is still the inode this process bound
        (never a socket someone else created at the same path), and calls the
        backend's optional ``abort()`` hook (cheap, non-blocking: e.g. removing
        a private temp dir -- no thread joins, no Metal drain).

        The single-server lock is deliberately NOT released: the caller is about
        to ``os._exit``, and the kernel drops the lock with the process. Releasing
        it here would let a new server acquire it and start loading while this
        process (and its model-load thread) still exists.
        """
        self._unlink_own_socket()
        abort_backend(self._backend, logger)

    @property
    def live_work_count(self) -> int:
        """Entries in the live-work ledger: everything that can still touch
        the socket or the backend, of any kind. The single-server lock is held
        while this is non-zero; ``serve(hard_exit_on_stop=True)`` exits the
        process when it is non-zero after ``shutdown()``. Non-zero throughout
        ``running`` (the bound listener), so read it after ``shutdown()``."""
        return len(self._live_work)

    @property
    def live_work_reasons(self) -> list[str]:
        """``live_work_count`` broken down by kind, for logs (e.g.
        ``["listener bound", "2 session/decode tasks (1 stuck)"]``); empty
        when none. ``(N stuck)``: entries that outlived the bound their caller
        waited for (a diagnostic, never a decision)."""
        return self._live_reasons()

    @property
    def _stuck_work_count(self) -> int:
        """Diagnostic (tests): the live entries marked as having outlived the
        bound their caller waited for -- a session that swallowed or outlived
        cancellation, a backend ``close()`` past its bound, a listener close
        past its grace, an orphan bind, an adopted startup / readiness step, a
        backend worker thread behind the ``idle()`` hook, a finished entry
        whose cleanup failed. Shown publicly as ``(N stuck)`` in
        ``live_work_reasons``. Never a lock or exit decision: those read
        ``live_work_count``."""
        return sum(1 for w in self._live_entries() if w.stuck)

    async def shutdown(self) -> None:
        """Stop accepting new connections and drain active sessions.

        Drains both connection handlers and their spawned decode tasks under
        a single bounded timeout. Force-cancels anything still running so the
        process never wedges on MLX/Metal teardown, then ``close()``s the
        backend, also bounded.

        Contract (see the class docstring for the state machine):

        - Idempotent and safe in every state. Before ``start()`` it just marks
          the server ``STOPPED`` (single-use: ``start()`` then raises). During
          ``start()`` it moves the server to ``STOPPING`` and returns at once:
          the in-flight start never binds a listener or reaches ``RUNNING``; it
          unwinds (the same drain, closing a started backend) and the lock is
          released once that has finished.
        - From ``RUNNING`` the drain runs as the server's own task; every
          caller (the first, a concurrent one, a later one) waits for it,
          including the backend ``close()``. A ``close()`` failure is raised to
          the first caller only.
        - From ``RUNNING`` it returns within the drain timeout, plus two
          cancel graces (the cancelled sessions' unwind, then the listener's
          ``wait_closed``), plus the close bound (``drain_timeout_seconds``
          again) even when a session task swallows cancellation or ``close()``
          hangs. What is left running -- including any backend decode thread
          (``idle()`` hook) -- stays in the live-work ledger
          (``live_work_count``; marked stuck in ``live_work_reasons``) and the
          single-server lock stays held until the last of it finishes, so no
          new server can take over the socket while this one's work still
          runs. The CLI (``serve`` with ``hard_exit_on_stop=True``) then exits
          the process; an embedding caller owns it (``asyncio.run`` teardown
          would wait on it).
        - Returning therefore does not always mean the lock is free (from
          ``STARTING``, or with leftovers): ``await wait_stopped()`` for that.
        - Cancellation-safe: cancelling the first caller cancels the session
          tasks at once (no drain wait) but never abandons the drain: the
          backend is still closed and the lock kept until all of it ends.
        """
        state = self._state
        self._shutdown_requested = True
        if state is _State.NEW:
            self._transition(_State.STOPPED)
            return
        if state is _State.STARTING:
            self._transition(_State.STOPPING)
            return
        owner = state is _State.RUNNING
        if owner:
            self._transition(_State.STOPPING)
            self._stop_task = asyncio.ensure_future(self._drain(close_backend=True))
            self._track(self._stop_task, "step")
        stop_task = self._stop_task
        if stop_task is None:
            # STOPPING via a start that unwinds itself, or STOPPED from NEW.
            return
        if not owner:
            await asyncio.wait({stop_task})
            return
        try:
            await asyncio.shield(stop_task)
        except asyncio.CancelledError:
            if not stop_task.done():
                # Hurry the drain: cancel the sessions now. It still closes the
                # backend; its own failure is logged, since we no longer wait.
                self._cancel_sessions()
                stop_task.add_done_callback(_log_leftover_failure)
            raise

    def _cancel_sessions(self) -> set[asyncio.Future]:
        """Cancel every live session / decode task (a task already asked to
        cancel is not asked again) and keep it as lingering live work."""
        live = self._sessions()
        for t in live:
            cancelling = getattr(t, "cancelling", None)
            if cancelling is None or not cancelling():
                t.cancel()
            self._linger(t, "session")
        return live

    async def _drain(self, *, close_backend: bool) -> None:
        """The one drain, for ``shutdown()`` and a failed start alike.

        Whatever can still run when it ends -- normally, by failure or by
        cancellation -- is kept as live work, so the lock outlives it."""
        self._shutdown_event.set()
        draining = self._begin_work()
        pending = self._sessions()
        try:
            await self._drain_and_close(pending, close_backend)
        finally:
            try:
                self._keep_lock_for_leftover_work(close_backend)
            finally:
                self._end_work(draining)

    def _keep_lock_for_leftover_work(self, close_backend: bool) -> None:
        """Mark everything that can still run after the drain (normal return,
        failure or cancellation alike) as stuck live work, so the lock
        outlives it.

        Liveness is read from the ledger, never from how far the drain got: a
        cancel landing in the first drain wait leaves handlers that were never
        cancelled; cancel them now. A drain cut short before the backend
        ``close()`` still starts it (unawaited, as live work)."""
        self._cancel_sessions()
        if close_backend and self._close_task is None:
            self._close_task = asyncio.ensure_future(self._backend.close())
        if self._close_task is not None:
            self._linger(self._close_task, "close")
        if isinstance(self._backend, IdleAwareBackend):
            try:
                # A decode thread outlived its (cancelled) task: stuck work.
                self._linger(self._backend.idle(), "thread")
            except Exception:  # noqa: BLE001 -- best-effort; never skip the lock bookkeeping
                logger.exception("stt_server: backend idle() failed")
        # Nothing else to mark: a listener close past its grace and an orphan
        # bind are marked where they outlive their bound, and what holds the
        # lock (and the CLI's hard exit) is the whole ledger, any entry kind.

    def _close_listener(self) -> asyncio.Future | None:
        """Close this attempt's listener; returns its ``wait_closed``, already
        live work (or None when nothing was bound)."""
        if self._server is None:
            return None
        return self._begin_listener_close(self._server, stuck=False)

    def _begin_listener_close(self, listener: Server, *, stuck: bool) -> asyncio.Future:
        """Close ``listener`` and hand its close to the ledger.

        ``close_connections=True`` sends a close frame to every open socket so
        handler recv loops exit instead of blocking forever on an idle client.
        ``Server.close()`` only schedules the close (websockets' own task then
        closes the listening socket and waits for every connection handler,
        including handshakes still running ``process_request``), so that close
        is live work until ``wait_closed`` returns. The successor (the close)
        is registered before the predecessor (the bound listener) leaves the
        ledger: there is no instant with neither."""
        listener.close(close_connections=True)
        # Our inode only (``asyncio`` does not remove a Unix socket file on
        # close): a stale socket file would otherwise outlive the server.
        self._unlink_own_socket()
        closer = asyncio.ensure_future(listener.wait_closed())
        self._hand_off(listener, closer, "listener-close", stuck=stuck)
        return closer

    async def _drain_and_close(self, pending: set[asyncio.Future], close_backend: bool) -> None:
        closer = self._close_listener()
        if pending:
            # ``asyncio.wait`` (not ``wait_for(gather(...))``): ``wait_for``
            # waits for the cancelled gather to finish unwinding, so a task
            # that delays or swallows cancellation (the session handlers catch
            # ``CancelledError`` around in-flight decodes and then send/close)
            # would hold shutdown with no bound.
            _done, still = await asyncio.wait(pending, timeout=self._config.drain_timeout_seconds)
            if still:
                logger.warning(
                    "stt_server: drain timeout expired with %d tasks in flight", len(still)
                )
                for t in still:
                    t.cancel()
                # Give the cancellations a short, bounded window to unwind
                # before backend.close() touches shared MLX/Metal state.
                _done, stuck = await asyncio.wait(still, timeout=_CANCEL_GRACE_SECONDS)
                if stuck:
                    logger.warning(
                        "stt_server: %d tasks did not unwind within %.1fs of cancel; "
                        "closing anyway",
                        len(stuck),
                        _CANCEL_GRACE_SECONDS,
                    )
        for t in pending:
            # Retrieve outcomes, as ``gather(return_exceptions=True)`` did, so a
            # failed task is not reported as "exception never retrieved".
            if t.done() and not t.cancelled():
                t.exception()
        if closer is not None:
            # ``wait_closed`` also waits for connection handlers; bound it the
            # same way so a stuck handler cannot hold shutdown. Cancelling it
            # would only end our wait (websockets' close keeps running), so one
            # that outlives the grace stays live work, stuck, holding the lock.
            _done, stuck = await asyncio.wait({closer}, timeout=_CANCEL_GRACE_SECONDS)
            if stuck:
                self._linger(closer, "listener-close")
        if close_backend:
            await self._close_backend()

    async def _close_backend(self) -> None:
        """``backend.close()`` once, as live work, waited for at most the drain
        timeout: the protocol does not bound ``close()``, and a hung one must
        not hold shutdown (or the CLI's hard exit). One that outlives the bound
        keeps running (cancelling it mid Metal drain is worse) and lingers."""
        if self._close_task is None:
            self._close_task = asyncio.ensure_future(self._backend.close())
            self._track(self._close_task, "close")
        task = self._close_task
        _done, still = await asyncio.wait({task}, timeout=self._config.drain_timeout_seconds)
        if still:
            logger.warning(
                "stt_server: backend close() still running after %.1fs; leaving it running",
                self._config.drain_timeout_seconds,
            )
            self._linger(task, "close")
            return
        task.result()  # a close() failure is the caller's

    # --- connection handling ---
    async def _process_request(self, connection, request):
        # On a Unix-domain socket the only legitimate peer is a process owned
        # by the same effective uid as this server. Enforce that BEFORE (and
        # independently of) bearer auth so a foreign uid is rejected even when
        # no token is configured. Fail closed: any inability to resolve the
        # peer's uid is a 403, not a pass. TCP listeners cannot answer
        # SO_PEERCRED-style queries, so this gate is UDS-only.
        if self._config.socket_path is not None:
            transport = connection.transport
            if transport is None:
                # websockets 16 sets ``transport`` before ``process_request``
                # runs; guard defensively so a future/edge None fails closed
                # rather than raising AttributeError.
                logger.warning("stt_server: UDS connection has no transport; rejecting")
                return connection.respond(403, "peer not permitted\n")
            sock = transport.get_extra_info("socket")
            if sock is None:
                logger.warning("stt_server: UDS connection has no underlying socket; rejecting")
                return connection.respond(403, "peer not permitted\n")
            if sock.family != socket.AF_UNIX:
                logger.warning(
                    "stt_server: UDS connection on unexpected socket family %r; rejecting",
                    sock.family,
                )
                return connection.respond(403, "peer not permitted\n")
            try:
                uid = peer_uid(sock)
            except Exception as exc:
                logger.warning("stt_server: peer uid resolution failed (%s); rejecting", exc)
                return connection.respond(403, "peer not permitted\n")
            if uid is None or uid != os.geteuid():
                logger.warning(
                    "stt_server: rejecting UDS peer uid %r (expected %d)",
                    uid,
                    os.geteuid(),
                )
                return connection.respond(403, "peer not permitted\n")

        # Reject unexpected browser Origin headers for non-browser-focused V1,
        # and enforce optional bearer auth in one place.
        headers = request.headers
        origin = headers.get("Origin")
        if self._config.reject_browser_origins and origin:
            return connection.respond(403, "origin not permitted\n")
        if self._config.auth_token:
            provided = headers.get("Authorization", "") or ""
            expected = f"Bearer {self._config.auth_token}"
            # Constant-time compare so a loopback attacker cannot recover the
            # token byte-by-byte via response-timing signals.
            if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
                return connection.respond(401, "unauthorized\n")
        return None

    async def _handle_connection(self, ws: ServerConnection) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._track(task, "session")  # live work from creation (the one registry)
        state = _SessionState(session_id=_session_id())
        try:
            await self._send(
                ws,
                state,
                {
                    "type": P.EVT_SERVER_HELLO,
                    "event_id": _event_id(),
                    "protocol_version": P.PROTOCOL_VERSION,
                    "capabilities": {
                        "binary_audio": True,
                        "base64_audio_append": True,
                        "server_vad": False,
                    },
                    "audio": {
                        "format": P.AUDIO_FORMAT,
                        "rate": P.AUDIO_SAMPLE_RATE_HZ,
                        "channels": P.AUDIO_CHANNELS,
                    },
                    # Backend identity — lets a client verify which ASR is
                    # actually behind this socket instead of trusting the
                    # socket path. Additive optional field; old readers ignore.
                    "backend": {
                        "name": self._backend.backend_name,
                        "model": self._backend.model,
                    },
                },
            )
            session_snapshot = _session_snapshot(state)
            # Emit both the legacy name (existing clients) and the OpenAI
            # transcription-mode name so strict OpenAI clients see the event
            # they expect. Same ``session`` payload under both.
            for evt_type in (P.EVT_SESSION_CREATED, P.EVT_TRANSCRIPTION_SESSION_CREATED):
                await self._send(
                    ws,
                    state,
                    {
                        "type": evt_type,
                        "event_id": _event_id(),
                        "session": session_snapshot,
                    },
                )
            async for raw in ws:
                if state.closed:
                    break
                try:
                    if isinstance(raw, (bytes, bytearray)):
                        await self._handle_binary_audio(ws, state, bytes(raw))
                    else:
                        await self._handle_text(ws, state, raw)
                except Exception:
                    # Don't echo raw exception text (paths, tokens in
                    # nested RuntimeError(...) args, etc.) to the peer.
                    logger.exception("stt_server: error handling message")
                    await self._error(ws, state, P.ErrorCode.INTERNAL_ERROR, "internal error")
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            # The task's ``_retire`` callback drops it and re-settles.
            await self._teardown_session(ws, state)

    # --- message handlers ---
    async def _handle_text(self, ws: ServerConnection, state: _SessionState, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            await self._error(ws, state, P.ErrorCode.INVALID_JSON, str(exc))
            return
        if not isinstance(msg, dict) or "type" not in msg:
            await self._error(ws, state, P.ErrorCode.INVALID_EVENT, "missing type")
            return
        t = msg["type"]
        client_event_id = msg.get("event_id") if isinstance(msg.get("event_id"), str) else None
        if t not in P.CLIENT_EVENT_TYPES:
            await self._error(
                ws,
                state,
                P.ErrorCode.UNSUPPORTED_EVENT,
                f"unknown event: {t}",
                client_event_id=client_event_id,
            )
            return

        if t in (P.EVT_SESSION_UPDATE, P.EVT_TRANSCRIPTION_SESSION_UPDATE):
            await self._on_session_update(ws, state, msg, client_event_id)
        elif t == P.EVT_AUDIO_APPEND:
            await self._on_audio_append_json(ws, state, msg)
        elif t == P.EVT_AUDIO_COMMIT:
            await self._on_commit(ws, state, client_event_id)
        elif t == P.EVT_AUDIO_CLEAR:
            await self._on_clear(ws, state, client_event_id)
        elif t == P.EVT_SERVER_STATUS_REQ:
            await self._on_status(ws, state)
        elif t == P.EVT_SESSION_CLOSE:
            await self._on_close(ws, state)
        elif t == P.EVT_SESSION_CANCEL:
            await self._on_cancel(ws, state)

    async def _handle_binary_audio(
        self, ws: ServerConnection, state: _SessionState, data: bytes
    ) -> None:
        # Reject appends while a decode is in flight rather than silently
        # merging new audio into the next turn's buffer, which would lose the
        # commit boundary the client just signalled.
        if state.in_flight_task is not None and not state.in_flight_task.done():
            await self._error(
                ws,
                state,
                P.ErrorCode.INVALID_EVENT,
                "audio append while previous decode in flight",
            )
            return
        if len(data) > self._config.max_append_bytes:
            await self._error(
                ws, state, P.ErrorCode.PAYLOAD_TOO_LARGE, "binary audio exceeds max_append_bytes"
            )
            return
        if len(data) % P.AUDIO_SAMPLE_WIDTH_BYTES != 0:
            # PCM16 samples are 2 bytes; an odd-length append would misalign
            # every subsequent sample (silent wrong-result transcript, not a
            # crash). Reject at the boundary so the client notices.
            await self._error(
                ws,
                state,
                P.ErrorCode.INVALID_EVENT,
                f"audio length {len(data)} is not a multiple of {P.AUDIO_SAMPLE_WIDTH_BYTES}",
            )
            return
        if len(state.buffer) + len(data) > self._config.max_uncommitted_bytes:
            await self._error(
                ws,
                state,
                P.ErrorCode.BUFFER_OVERFLOW,
                "uncommitted audio exceeds per-session cap",
            )
            return
        state.buffer.extend(data)

    async def _on_session_update(
        self,
        ws: ServerConnection,
        state: _SessionState,
        msg: dict,
        client_event_id: str | None,
    ) -> None:
        session = msg.get("session") or {}
        audio_in = (session.get("audio") or {}).get("input") or {}

        # ``input_audio_format`` — accept OpenAI's enum string at session
        # root (``"pcm16"``) or the legacy nested dict. Only pcm16/16 kHz/
        # mono is on the wire regardless.
        fmt_root = session.get("input_audio_format")
        if isinstance(fmt_root, str):
            if fmt_root != P.AUDIO_FORMAT:
                await self._error(
                    ws,
                    state,
                    P.ErrorCode.INVALID_CONFIG,
                    f"only {P.AUDIO_FORMAT!r} input_audio_format is supported",
                    client_event_id=client_event_id,
                )
                return
        fmt = audio_in.get("format") or {}
        if fmt:
            if fmt.get("encoding", P.AUDIO_FORMAT) != P.AUDIO_FORMAT:
                await self._error(
                    ws,
                    state,
                    P.ErrorCode.INVALID_CONFIG,
                    "only pcm16 encoding is supported",
                    client_event_id=client_event_id,
                )
                return
            if fmt.get("rate", P.AUDIO_SAMPLE_RATE_HZ) != P.AUDIO_SAMPLE_RATE_HZ:
                await self._error(
                    ws,
                    state,
                    P.ErrorCode.INVALID_CONFIG,
                    "only 16000 Hz sample rate is supported",
                    client_event_id=client_event_id,
                )
                return
            if fmt.get("channels", P.AUDIO_CHANNELS) != P.AUDIO_CHANNELS:
                await self._error(
                    ws,
                    state,
                    P.ErrorCode.INVALID_CONFIG,
                    "only mono audio is supported",
                    client_event_id=client_event_id,
                )
                return

        # ``turn_detection`` — accept at the OpenAI session root OR nested
        # under ``session.audio.input``. Either must be null (server VAD
        # unimplemented).
        for td_container in (session, audio_in):
            if "turn_detection" in td_container:
                td = td_container["turn_detection"]
                if td is not None:
                    await self._error(
                        ws,
                        state,
                        P.ErrorCode.INVALID_CONFIG,
                        "server VAD is not implemented; use turn_detection: null",
                        client_event_id=client_event_id,
                    )
                    return
                state.config["turn_detection"] = None

        # Language may be supplied either nested (legacy) or at the session
        # root (pragmatic; OpenAI keeps it under input_audio_transcription).
        if "language" in audio_in:
            state.config["language"] = audio_in["language"]
        transcription = session.get("input_audio_transcription")
        if isinstance(transcription, dict) and "language" in transcription:
            state.config["language"] = transcription["language"]

        session_snapshot = _session_snapshot(state)
        for evt_type in (P.EVT_SESSION_UPDATED, P.EVT_TRANSCRIPTION_SESSION_UPDATED):
            await self._send(
                ws,
                state,
                {
                    "type": evt_type,
                    "event_id": _event_id(),
                    "session": session_snapshot,
                },
            )

    async def _on_audio_append_json(
        self, ws: ServerConnection, state: _SessionState, msg: dict
    ) -> None:
        import base64

        audio_b64 = msg.get("audio")
        if not isinstance(audio_b64, str):
            await self._error(
                ws, state, P.ErrorCode.INVALID_EVENT, "input_audio_buffer.append missing audio"
            )
            return
        try:
            data = base64.b64decode(audio_b64, validate=True)
        except Exception as exc:
            await self._error(
                ws, state, P.ErrorCode.INVALID_EVENT, f"audio base64 decode failed: {exc}"
            )
            return
        await self._handle_binary_audio(ws, state, data)

    async def _on_commit(
        self,
        ws: ServerConnection,
        state: _SessionState,
        client_event_id: str | None,
    ) -> None:
        if len(state.buffer) == 0:
            await self._error(
                ws,
                state,
                P.ErrorCode.BUFFER_EMPTY,
                "commit on empty buffer",
                client_event_id=client_event_id,
            )
            return
        if state.in_flight_task is not None and not state.in_flight_task.done():
            await self._error(
                ws,
                state,
                P.ErrorCode.INVALID_EVENT,
                "commit while previous decode in flight",
                client_event_id=client_event_id,
            )
            return

        audio = bytes(state.buffer)
        state.buffer.clear()
        item = _item_id()
        payload: dict[str, Any] = {
            "type": P.EVT_AUDIO_COMMITTED,
            "event_id": _event_id(),
            "item_id": item,
        }
        if client_event_id:
            # Echo the client's event_id so request/response correlation works
            # without making clients track server-minted ids.
            payload["previous_event_id"] = client_event_id
        await self._send(ws, state, payload)
        decode_task = asyncio.create_task(self._run_decode(ws, state, audio, item))
        state.in_flight_task = decode_task
        self._track(decode_task, "session")  # live work from creation (the one registry)

    async def _on_clear(
        self,
        ws: ServerConnection,
        state: _SessionState,
        client_event_id: str | None,
    ) -> None:
        # OpenAI ``input_audio_buffer.clear`` — drop uncommitted audio
        # without touching session lifecycle. Different from
        # ``session.cancel``, which also closes the session.
        if state.in_flight_task is not None and not state.in_flight_task.done():
            await self._error(
                ws,
                state,
                P.ErrorCode.INVALID_EVENT,
                "clear while decode in flight; use session.cancel to abort",
                client_event_id=client_event_id,
            )
            return
        state.buffer.clear()
        payload: dict[str, Any] = {
            "type": P.EVT_AUDIO_CLEARED,
            "event_id": _event_id(),
        }
        if client_event_id:
            payload["previous_event_id"] = client_event_id
        await self._send(ws, state, payload)

    async def _run_decode(
        self,
        ws: ServerConnection,
        state: _SessionState,
        audio: bytes,
        item_id: str,
    ) -> None:
        stream: BackendStream | None = None
        try:
            # The client ``language`` is passed through verbatim; each backend
            # translates it into its own idiom. Accepted values: ``None`` /
            # ``"auto"`` (detect) or an ISO/BCP-47 code. Per-backend handling:
            # mlx_whisper recasts ``"auto"``/blank -> ``None`` (auto-detect),
            # parakeet ignores it (model is language-pinned), nemotron forwards
            # it (``"auto"`` is a real LID prompt key; ``None`` -> its ``"auto"``
            # default). A new backend MUST decide how it maps ``"auto"``.
            stream = await self._backend.open_stream(language=state.config.get("language"))
            state.current_stream = stream
            await stream.feed(audio)
            await stream.end()
            final_text = ""
            async for ev in stream.events():
                if ev.kind == "delta":
                    await self._send(
                        ws,
                        state,
                        {
                            "type": P.EVT_TRANSCRIPT_DELTA,
                            "event_id": _event_id(),
                            "item_id": item_id,
                            "content_index": 0,
                            "delta": ev.text,
                        },
                    )
                elif ev.kind == "completed":
                    final_text = ev.text
            await self._send(
                ws,
                state,
                {
                    "type": P.EVT_TRANSCRIPT_COMPLETED,
                    "event_id": _event_id(),
                    "item_id": item_id,
                    "content_index": 0,
                    "transcript": final_text,
                },
            )
        except asyncio.CancelledError:
            if stream is not None:
                try:
                    await stream.cancel()
                except Exception:
                    pass
            raise
        except Exception:
            # Log full detail server-side; send a generic message to the
            # client (exception text can leak filesystem paths, model names,
            # or misconfigured backend secrets).
            logger.exception("stt_server: backend decode failed")
            await self._send(
                ws,
                state,
                {
                    "type": P.EVT_TRANSCRIPT_FAILED,
                    "event_id": _event_id(),
                    "item_id": item_id,
                    "content_index": 0,
                    "error": {
                        "type": _ERROR_TYPE_FOR_CODE[P.ErrorCode.BACKEND_ERROR],
                        "code": P.ErrorCode.BACKEND_ERROR.value,
                        "message": "backend decode failed",
                    },
                },
                force=True,
            )
        finally:
            state.current_stream = None

    async def _on_status(self, ws: ServerConnection, state: _SessionState) -> None:
        await self._send(
            ws,
            state,
            {
                "type": P.EVT_SERVER_STATUS,
                "event_id": _event_id(),
                "session_id": state.session_id,
                # Backend identity, mirrored from server.hello — so a status
                # probe alone is enough to confirm which ASR is running.
                "backend": {
                    "name": self._backend.backend_name,
                    "model": self._backend.model,
                },
                "queue_depth": 1 if state.in_flight_task and not state.in_flight_task.done() else 0,
                "uncommitted_bytes": len(state.buffer),
                "uptime_seconds": time.monotonic() - state.started_monotonic,
                "pid": os.getpid(),
                # Peak RSS from getrusage — stdlib, no psutil dep. Sufficient
                # for soak leak-detection (peak only climbs when a leak is
                # actually growing). ru_maxrss is bytes on macOS, kibibytes
                # on Linux/BSD — normalize to bytes so the wire contract is
                # platform-independent.
                "rss_bytes": _process_peak_rss_bytes(),
            },
        )

    async def _on_close(self, ws: ServerConnection, state: _SessionState) -> None:
        if state.closed:
            return
        if state.in_flight_task and not state.in_flight_task.done():
            # If shutdown() is already running it owns the drain budget; don't
            # double-spend it here or a client sending session.close during
            # shutdown doubles the worst-case wait. Cancel immediately and
            # let shutdown's drain clean up.
            if self._shutdown_event.is_set():
                state.in_flight_task.cancel()
            else:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(state.in_flight_task),
                        timeout=self._config.drain_timeout_seconds,
                    )
                except TimeoutError:
                    # Force-cancel and wait for unwind so the decode can't
                    # outlive the session.
                    state.in_flight_task.cancel()
                    try:
                        await state.in_flight_task
                    except (asyncio.CancelledError, Exception):
                        pass
        state.closed = True
        await self._send(
            ws,
            state,
            {
                "type": P.EVT_SESSION_CLOSED,
                "event_id": _event_id(),
                "session_id": state.session_id,
                "reason": "client_close",
            },
            force=True,
        )
        await ws.close()

    async def _on_cancel(self, ws: ServerConnection, state: _SessionState) -> None:
        if state.closed:
            return
        state.buffer.clear()
        if state.current_stream is not None:
            try:
                await state.current_stream.cancel()
            except Exception:
                pass
        if state.in_flight_task and not state.in_flight_task.done():
            state.in_flight_task.cancel()
            try:
                await state.in_flight_task
            except (asyncio.CancelledError, Exception):
                pass
        state.closed = True
        await self._send(
            ws,
            state,
            {
                "type": P.EVT_SESSION_CLOSED,
                "event_id": _event_id(),
                "session_id": state.session_id,
                "reason": "client_cancel",
            },
            force=True,
        )
        await ws.close()

    async def _teardown_session(self, ws: ServerConnection, state: _SessionState) -> None:
        if state.in_flight_task and not state.in_flight_task.done():
            state.in_flight_task.cancel()
            try:
                await state.in_flight_task
            except (asyncio.CancelledError, Exception):
                pass
        state.closed = True

    # --- send helpers ---
    async def _send(
        self,
        ws: ServerConnection,
        state: _SessionState,
        payload: dict,
        *,
        force: bool = False,
    ) -> None:
        # Backpressure: if the socket's pending write buffer is larger than
        # the per-session high-water mark, the consumer isn't keeping up. We
        # surface an explicit error and close the socket rather than block
        # the decode loop indefinitely. ``force=True`` bypasses the check for
        # terminal events (session.closed, error) so teardown can still flush.
        if not force and not state.closed:
            pending = _pending_write_bytes(ws)
            if pending is not None and pending > self._config.send_queue_high_water_bytes:
                logger.warning(
                    "stt_server: send-queue overflow (%d bytes pending), closing session %s",
                    pending,
                    state.session_id,
                )
                state.closed = True
                try:
                    await ws.close(code=1011, reason="send_queue_overflow")
                except Exception:
                    pass
                return
        try:
            await ws.send(json.dumps(payload))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def _error(
        self,
        ws: ServerConnection,
        state: _SessionState,
        code: P.ErrorCode,
        message: str,
        *,
        item_id: str | None = None,
        client_event_id: str | None = None,
    ) -> None:
        error_obj: dict[str, Any] = {
            "type": _ERROR_TYPE_FOR_CODE.get(code, "server_error"),
            "code": code.value,
            "message": message,
        }
        if client_event_id:
            error_obj["event_id"] = client_event_id
        payload: dict[str, Any] = {
            "type": P.EVT_ERROR,
            "event_id": _event_id(),
            "error": error_obj,
        }
        if item_id:
            payload["item_id"] = item_id
        await self._send(ws, state, payload, force=True)


def _session_snapshot(state: _SessionState) -> dict[str, Any]:
    """Build a ``session`` payload that names both legacy nested fields
    and the OpenAI transcription-mode flat fields, so clients of either
    shape get the values they expect."""
    return {
        "id": state.session_id,
        "object": "realtime.transcription_session",
        "type": "transcription",
        "input_audio_format": P.AUDIO_FORMAT,
        "turn_detection": state.config.get("turn_detection"),
        "input_audio_transcription": {"language": state.config.get("language")},
        "audio": {
            "input": {
                "format": {
                    "encoding": P.AUDIO_FORMAT,
                    "rate": P.AUDIO_SAMPLE_RATE_HZ,
                    "channels": P.AUDIO_CHANNELS,
                },
                "turn_detection": state.config.get("turn_detection"),
                "language": state.config.get("language"),
            }
        },
    }


def _pending_write_bytes(ws: ServerConnection) -> int | None:
    """Best-effort read of the socket's pending outbound byte count.

    websockets 15.x exposes the underlying asyncio transport; we look up the
    write-buffer size through it. Returns None if we cannot determine it,
    which disables the backpressure guard for that send.
    """
    try:
        transport = ws.transport  # type: ignore[attr-defined]
    except AttributeError:
        return None
    if transport is None:
        return None
    try:
        return transport.get_write_buffer_size()
    except Exception:
        return None


async def serve(
    backend: TranscriptionBackend | None = None,
    *,
    socket_path: str | None = None,
    host: str | None = None,
    port: int | None = None,
    auth_token: str | None = None,
    install_signal_handlers: bool = True,
    ready: Callable[[TranscriptionServer], Awaitable[None]] | None = None,
    publish_ready: Callable[[TranscriptionServer], None] | None = None,
    stop_on_stdin_eof: bool = False,
    hard_exit_on_stop: bool = False,
    on_fatal: Callable[[BaseException], None] | None = None,
) -> None:
    """Start the server, wait for shutdown signal, then drain and exit.

    The public entrypoint behind ``python -m stt_server``; tests drive
    ``TranscriptionServer`` directly.

    ``ready`` performs asynchronous readiness preparation such as model warm-up;
    ``publish_ready`` performs the final synchronous notification after a stop
    check. ``stop_on_stdin_eof`` (``serve --supervised``) treats stdin EOF like
    SIGTERM. The SIGINT / SIGTERM handlers ``install_signal_handlers`` adds
    are removed on every return or raise from ``serve`` (a failed start
    included), restoring the default action.

    A stop (signal, or stdin EOF) that arrives while startup or readiness
    preparation is pending:

    - ``hard_exit_on_stop=False`` (default, for in-process callers): the pending
      step is cancelled and awaited for at most the drain timeout
      (``ServerConfig.drain_timeout_seconds``), then ``serve`` returns after the
      normal ``shutdown()``. A step that outlives that bound can no longer bind
      a listener or mark the server started (see ``shutdown()``); a model load
      already running in a worker thread keeps running until it finishes
      (threads cannot be cancelled). Such leftovers are the caller's.
    - ``hard_exit_on_stop=True`` (the CLI, which owns its process): best-effort
      cleanup (``abort_sync``) and an immediate ``os._exit(0)``, so SIGTERM
      during a multi-GB load is honoured at once. Hugging Face downloads resume
      from their ``.incomplete`` blobs next time.

    After a normal ``shutdown()``, any live work left (``live_work_count``:
    e.g. session tasks that swallowed cancellation) would make
    ``asyncio.run``'s teardown wait on it without a bound. With ``hard_exit_on_stop=True`` the process exits there
    (``os._exit``: 0 after a requested stop, 1 if ``serve`` was failing); with
    ``False`` they are the caller's, and the single-server lock stays held
    until they finish. ``os._exit`` skips the caller's exception handlers, so a
    failure propagating at that point is handed to ``on_fatal`` first (the CLI
    passes its own reporter: stderr message and, under ``--supervised``, the
    ``error`` event); without ``on_fatal`` it is logged. ``on_fatal`` without
    ``hard_exit_on_stop=True`` would never be called, so it is rejected
    (``ConfigError``). When ``shutdown()`` also fails after ``serve`` was
    already failing, the first failure is the one raised (and reported to
    ``on_fatal``); the shutdown failure is logged.

    Every exit from ``serve`` -- a stop, a cancellation of ``serve`` itself,
    a failure -- that finds a startup or readiness step still pending cancels
    it and waits for it at most the drain timeout before ``shutdown()``; one
    that outlives that is handed to the server (``TranscriptionServer.adopt``):
    it keeps the lock until it ends (``live_work_count``; marked stuck in
    ``live_work_reasons``). The readiness step is live work from creation
    (``adopt(task, label=...)``). A cancel of ``serve`` landing while that
    bounded wait runs does not start a second one. ``serve`` uses only the
    server's public API, so an embedder can write the same loop.
    After ``shutdown()``, ``hard_exit_on_stop=True`` exits on any live work
    left (``live_work_count``), of any kind.
    """
    try:
        if on_fatal is not None and not hard_exit_on_stop:
            # It is only ever called right before the hard exit; accepting it
            # alone would silently drop the caller's error reporting.
            raise ConfigError("serve(on_fatal=...) requires hard_exit_on_stop=True")
        cfg = ServerConfig(socket_path=socket_path, host=host, port=port, auth_token=auth_token)
    except BaseException:
        # No server will ever ``start()``/``close()`` this backend.
        if backend is not None:
            abort_backend(backend, logger)
        raise
    server = TranscriptionServer(backend or EchoBackend(), cfg)
    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    def _request_stop() -> None:
        if not stop.done():
            stop.set_result(None)

    def _exit_process(code: int, *, abort: bool, failure: BaseException | None) -> None:
        """The one hard exit. ``abort``: run ``abort_sync`` first (a stop
        during a step: ``shutdown()`` never ran). ``failure``: the exception
        ``os._exit`` would swallow, handed to ``on_fatal`` (else logged)."""
        if abort:
            server.abort_sync()
        if failure is not None:
            _report_before_hard_exit(failure, on_fatal)
        logging.shutdown()
        os._exit(code)

    # The startup / readiness step in flight (and its stage), for every exit.
    pending: list[tuple[asyncio.Future, str]] = []

    async def _abandon_step(task: asyncio.Future, stage: str) -> None:
        """Cancel a pending step and wait for it, bounded (one that swallows
        cancellation cannot hold the exit). One still running may touch the
        backend: the server keeps it as live work, holding the lock until it
        ends, and logs its failure -- even if this wait is itself cancelled."""
        # Settled from here on, whatever happens to this wait: a cancel of
        # ``serve`` landing during it must not start a second full wait in
        # ``serve``'s ``finally``.
        pending.clear()
        task.cancel()
        try:
            await asyncio.wait({task}, timeout=cfg.drain_timeout_seconds)
        finally:
            if task.done():
                if not task.cancelled():
                    task.exception()  # retrieved; we are stopping regardless
            elif server.can_adopt:
                server.adopt(task, abandoned=True, label=stage)
            # Else only a startup step that never ran its first step is pending
            # (``start()`` leaves ``new`` / ``stopped`` synchronously):
            # cancelled, it ends touching nothing, and no lock is held to keep.

    async def _run_until_stop(coro: Awaitable[None], stage: str, *, track: bool = False) -> bool:
        """Await ``coro`` unless a stop arrives first; True when it completed
        and no stop is pending. ``track``: the step is live work from creation
        (readiness; ``start()`` keeps its own in-flight entry)."""
        task = asyncio.ensure_future(coro)
        if track:
            server.adopt(task, label=stage)
        pending[:] = [(task, stage)]
        await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
        if stop.done() and not task.done():
            if hard_exit_on_stop:
                logger.warning("stt_server: stop requested during %s; exiting", stage)
                _exit_process(0, abort=True, failure=None)
            logger.info("stt_server: stop requested during %s; cancelling it", stage)
            await _abandon_step(task, stage)
            return False
        await task
        return not stop.done()

    # The signals ``serve`` captured: released on every exit from ``serve``
    # (the outermost ``finally``), so a returned or failed ``serve`` never
    # leaves SIGINT / SIGTERM resolving a stop nobody awaits.
    captured: list[signal.Signals] = []
    failure: BaseException | None = None
    try:
        if install_signal_handlers:
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, _request_stop)
                except NotImplementedError:
                    continue
                captured.append(sig)
        if stop_on_stdin_eof:
            from .supervise import watch_stdin_eof

            watch_stdin_eof(loop, _request_stop)
        # Race startup against stop in every mode: the signal handlers above are
        # installed before ``start()``, so awaiting it bare would defer a
        # SIGTERM/SIGINT until the backend import finished (``main`` installed
        # them after ``start()``, leaving the default terminate-now action).
        if not await _run_until_stop(server.start(), "startup"):
            return
        if ready is not None and not await _run_until_stop(
            ready(server), "readiness preparation", track=True
        ):
            return
        if publish_ready is not None:
            publish_ready(server)
        await stop
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            try:
                # Every exit (a cancelled ``serve`` included) settles a step
                # still in flight the same way a stop does, before the drain.
                if pending and not pending[0][0].done():
                    task, stage = pending[0]
                    logger.info("stt_server: serve exiting during %s; cancelling it", stage)
                    await _abandon_step(task, stage)
            finally:
                await server.shutdown()
        except BaseException as exc:
            if failure is None:
                failure = exc
                raise
            # The primary failure keeps propagating (and is what ``on_fatal``
            # / the caller classifies); the cleanup failure is only logged.
            logger.error("stt_server: shutdown after a failure also failed", exc_info=exc)
        finally:
            if hard_exit_on_stop and server.live_work_count:
                logger.warning(
                    "stt_server: %d pieces of work outlived shutdown (%s); exiting",
                    server.live_work_count,
                    ", ".join(server.live_work_reasons),
                )
                _exit_process(1 if failure is not None else 0, abort=False, failure=failure)
            for sig in captured:
                loop.remove_signal_handler(sig)


def _report_before_hard_exit(
    exc: BaseException, on_fatal: Callable[[BaseException], None] | None
) -> None:
    """Surface the failure ``os._exit`` is about to swallow (best-effort)."""
    try:
        if on_fatal is not None and isinstance(exc, Exception):
            on_fatal(exc)
        else:
            logger.error("stt_server: fatal error", exc_info=exc)
    except Exception:  # noqa: BLE001 -- we are exiting regardless
        logger.exception("stt_server: reporting the fatal error failed")
