"""``python -m stt_server`` entrypoint.

Subcommands:

- ``serve`` (default when no subcommand given) — runs the server. This is also
  the implicit behavior when the first argv looks like a flag, so existing
  invocations like ``python -m stt_server --socket-path X --backend mlx``
  keep working unchanged.
- ``status`` — connect, send ``server.status``, print the response, exit 0
  on success or 1 on failure. Useful as a preflight health probe for
  launchd keepalive scripts and for humans checking "is my server up?"
  without writing a client.
- ``prefetch`` — download a backend's model weights without serving (with
  JSON-lines progress under ``--json``), or ``--check`` offline whether they
  are already cached.

``serve --supervised`` is for a managing parent process: JSON lifecycle events
on stdout and stop-on-stdin-EOF (see ``stt_server/supervise.py``).
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import os
import sys
from pathlib import Path

from .backend import EchoBackend

# The backend registry (``backends/__init__.py``) is stdlib-only: reading it
# imports no backend module, so the serve path stays lean at every launch.
from .backends import BACKENDS, import_backend
from .backends import DEFAULT_MLX_MODEL as _DEFAULT_MLX_MODEL  # noqa: F401 (compat)
from .env import env_first
from .errors import ConfigError
from .server import serve

# ``TranscriptionClient`` and ``protocol`` are imported lazily inside the
# status-subcommand helpers so the serve path (run at every launchd startup)
# doesn't pay for ``websockets.asyncio.client`` it never uses.


def _make_backend(name: str, model: str | None):
    """Construct the ``--backend`` named ``name`` (see ``backends.BACKENDS``).

    The backend module is imported lazily, here, so a base install without an
    optional extra still constructs the others. No backend module imports its
    extra at module load (``parakeet_mlx``, ``mlx_audio``, ``moshi_mlx`` are
    imported only inside ``start()``/``_get_model``), so a missing extra
    surfaces fast in ``start()``, not at construction.
    """
    try:
        cls = import_backend(name).cls
    except ConfigError as exc:  # unknown name: the CLI's exit message
        raise SystemExit(str(exc)) from None
    if cls is None:
        return EchoBackend()
    return cls(model=model)


# Every ``--backend`` name; shared by ``serve`` and ``prefetch``.
BACKEND_CHOICES = tuple(BACKENDS)


def _resolve_model(backend: str, model: str | None) -> str | None:
    """Resolve the effective decode model id.

    An explicit ``--model`` always wins (it is passed through verbatim — the
    server-side ``--backend`` is the trust anchor; pointing a backend at a
    mismatched repo id fails fast in ``start()``/decode, and classifying a
    repo id as "whisper" vs "parakeet" would need a brittle string heuristic).
    When ``--model`` is unset the default is the backend's own
    ``BACKENDS[backend].default_model`` (e.g. ``DEFAULT_PARAKEET_MODEL`` for
    ``parakeet``); ``None`` for a backend with no model (echo) -- the registry
    is the only source of defaults.
    """
    if model is not None:
        return model
    entry = BACKENDS.get(backend)
    return entry.default_model if entry is not None else None


def _resolve_auth_token(token_file: str | None, *, client: bool = False) -> str | None:
    # INVARIANT — do not flap. Four review cycles circled this probe-auth
    # behaviour; any change here must keep the regression tests in
    # tests/test_stt_server.py green.
    #
    # A plaintext --auth-token CLI flag is intentionally unsupported: any
    # local user would be able to read the token via `ps`.
    #
    # Serve path (client=False): --auth-token-file > PIPECAT_STT_AUTH_TOKEN
    #   (canonical), then KODA_STT_AUTH_TOKEN (deprecated alias).
    # Probe path (client=True):  --auth-token-file > STT_WS_TOKEN only.
    #
    # STT_WS_TOKEN is the client-side bearer a consumer (e.g. the bot)
    # reads to authenticate against the stt_server. PIPECAT_STT_AUTH_TOKEN
    # (legacy alias KODA_STT_AUTH_TOKEN) is the server-side bearer the
    # launchd-run server expects. The probe MUST see exactly what the bot
    # sees — never the server-side secret — for two reasons:
    #   1. If the probe fell back to the server-side token it could report
    #      "ok" against a local server while the bot still 401s at
    #      startup, masking the misconfiguration we built this preflight
    #      to catch.
    #   2. If STT_WS_URI points at a remote host, that fallback would
    #      transmit the local LaunchAgent's server-side secret to the
    #      remote endpoint.
    #
    # Server-side name resolution is canonical-first via env_first:
    # PIPECAT_STT_AUTH_TOKEN wins, KODA_STT_AUTH_TOKEN is still honoured.
    # The two paths stay strictly separate — never read STT_WS_TOKEN here.
    if token_file:
        return Path(token_file).read_text(encoding="utf-8").strip() or None
    if client:
        client_val = (os.environ.get("STT_WS_TOKEN") or "").strip()
        return client_val or None
    env_val = (env_first("PIPECAT_STT_AUTH_TOKEN", "KODA_STT_AUTH_TOKEN") or "").strip()
    return env_val or None


def _load_dotenv_best_effort() -> None:
    """Load ``.env`` so ``stt_server status`` picks up the same
    ``STT_WS_*`` configuration a consumer would at startup.

    Kept optional (ImportError swallowed) so the serve path — which does
    not need dotenv — stays usable if python-dotenv is absent from a
    minimal deployment.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    # ``override=False`` so an already-exported env var always wins.
    load_dotenv(Path.cwd() / ".env", override=False)
    load_dotenv(Path.home() / ".secrets" / "ai.env", override=False)


def _resolve_probe_endpoint(args: argparse.Namespace) -> dict:
    """Return the endpoint kwargs for ``TranscriptionClient`` used by the
    status probe. If the caller passed any endpoint flag explicitly, honor
    exactly that (enforcing ``uri > socket_path > host+port`` so the
    client's socket_path bias cannot mask a URI override). Otherwise load
    dotenv and read the ``STT_WS_*`` env vars via the shared resolver so
    this path stays in sync with every other client.
    """
    from .client import resolve_endpoint_from_env

    # Always load dotenv, even when the caller passed explicit endpoint
    # flags. The endpoint resolution below still honors those flags
    # verbatim, but auth resolution (``_resolve_auth_token``) reads
    # ``STT_WS_TOKEN`` from ``os.environ``, so without this the documented
    # "token in .env" path gets a spurious 401 whenever the operator
    # points the probe at a specific socket/host. ``override=False`` in
    # the loader means an already-exported env var still wins.
    _load_dotenv_best_effort()

    cli_uri = getattr(args, "uri", None)
    cli_sock = args.socket_path
    cli_host = args.host
    cli_port = args.port
    if cli_uri or cli_sock or cli_host or cli_port is not None:
        uri = cli_uri
        sock = None if uri else cli_sock
        host = None if (uri or sock) else cli_host
        port = None if (uri or sock) else cli_port
        return {"uri": uri, "socket_path": sock, "host": host, "port": port}

    resolved = resolve_endpoint_from_env(os.environ)
    if not (resolved["uri"] or resolved["socket_path"] or resolved["host"]):
        # Library-level fallback: only honor the explicit escape hatch.
        # No app-specific default path is baked in here — consumers that
        # want one should export ``STT_WS_DEFAULT_SOCKET`` themselves.
        default_sock = os.environ.get("STT_WS_DEFAULT_SOCKET")
        if default_sock:
            resolved["socket_path"] = default_sock
    return resolved


def _add_endpoint_flags(p: argparse.ArgumentParser, *, include_uri: bool = False) -> None:
    p.add_argument("--socket-path", default=None)
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    if include_uri:
        # ``--uri`` is only meaningful for the client-side probe; the serve
        # path builds its listener from socket-path/host+port directly.
        p.add_argument(
            "--uri",
            default=None,
            help="Full ws:// or wss:// URI (client-side override; wins over --socket-path/--host).",
        )
    p.add_argument(
        "--auth-token-file",
        default=None,
        help="Path to a file containing the auth token (whitespace-stripped).",
    )


def _preflight_listener(args: argparse.Namespace) -> None:
    """Fail in milliseconds, not after a model load, when the listener can't bind.

    ``TranscriptionServer.start()`` starts the backend BEFORE binding, so a busy
    address would otherwise surface only after a multi-GB load. For TCP this
    mirrors the real bind (``SO_REUSEADDR``, as asyncio sets on POSIX). For a
    Unix socket it refuses a path a live server is listening on; asyncio's
    ``create_unix_server`` would otherwise silently replace that socket file.
    The TOCTOU window is accepted; the real bind still reports through the same
    error classifier.
    """
    import errno
    import socket

    if args.socket_path:
        path = Path(args.socket_path).expanduser()
        from .server import socket_lock_held

        # The lock is authoritative for servers that take it: connect() cannot
        # tell a stale socket from a live one with a full backlog (macOS gives
        # ECONNREFUSED for both).
        if socket_lock_held(path):
            raise OSError(errno.EADDRINUSE, f"a server is already running for {path}")
        if path.is_socket():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                try:
                    probe.connect(str(path))
                except TimeoutError as exc:
                    raise OSError(
                        errno.EADDRINUSE, f"a server may already be listening on {path}"
                    ) from exc
                except OSError as exc:
                    if exc.errno in {errno.ECONNREFUSED, errno.ENOENT}:
                        # No lock holder (checked above) and nothing accepting:
                        # stale. start() takes the lock before replacing it.
                        return
                    if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                        raise OSError(
                            errno.EADDRINUSE,
                            f"a server may already be listening on {path}",
                        ) from exc
                    raise
            raise OSError(errno.EADDRINUSE, f"a server is already listening on {path}")
        return
    if not args.host or not args.port:
        return  # missing endpoint (ServerConfig reports it) or port 0 (ephemeral)
    for family, socktype, proto, _canon, addr in socket.getaddrinfo(
        args.host, args.port, type=socket.SOCK_STREAM
    ):
        with socket.socket(family, socktype, proto) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(addr)


async def _warm(backend) -> None:
    """Decode 1 s of silence so the first real commit doesn't pay for a lazy
    model load (``mlx``/``parakeet``/``nemotron`` load on first decode)."""
    stream = await backend.open_stream(language=None)
    await stream.feed(b"\x00\x00" * 16000)
    await stream.end()
    async for _ in stream.events():
        pass


async def _prepare_ready(server) -> None:
    """Warm the backend before the supervised ready event is published.

    Warm-up runs the lazy model load (and so any first download) AND a decode.
    It does not tag failures itself: each backend wraps only its fetch-and-load
    call in ``snapshot.download_phase()``, so a decoder-origin error here is not
    misreported as ``ModelDownloadError``.
    """
    await _warm(server.backend)


def _emit_ready(server) -> None:
    """Publish the sole supervised ready event after warm-up and a stop check."""
    from . import protocol as P
    from .supervise import emit

    cfg = server.config
    emit(
        {
            "event": "ready",
            "backend": server.backend.backend_name,
            "model": server.backend.model,
            "host": None if cfg.socket_path else cfg.host,
            "port": None if cfg.socket_path else (server.listening_port() or cfg.port),
            "socket_path": cfg.socket_path,
            "rate": P.AUDIO_SAMPLE_RATE_HZ,
            "pid": os.getpid(),
        }
    )


def _cmd_serve(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.supervised:
        _run_serve(args)
        return

    # Supervised mode: stdout carries only JSON lifecycle events; logs stay on
    # stderr; stdin EOF stops the server.
    from . import supervise

    with supervise.json_channel():
        try:
            try:
                _preflight_listener(args)
            except OSError as exc:
                # Same one-line stderr message and exit 1 as a bind failure in
                # ``_run_serve`` (not the "fatal error" traceback reserved for
                # unexpected exceptions); the handler below adds the event.
                print(f"stt_server: {exc}", file=sys.stderr)
                raise SystemExit(1) from exc
            _run_serve(
                args,
                ready=_prepare_ready,
                publish_ready=_emit_ready,
                stop_on_stdin_eof=True,
            )
        except KeyboardInterrupt:
            raise
        except SystemExit as exc:
            # ``_run_serve`` already turned the failure into ``stt_server: <msg>``
            # on stderr; repeat it as an event so the supervisor can show it.
            cause = exc.__cause__
            if cause is not None:
                code, message = supervise.classify_startup_error(cause)
                supervise.emit(
                    {
                        "event": "error",
                        "code": code,
                        "message": message,
                        "exc_type": type(cause).__name__,
                    }
                )
            elif isinstance(exc.code, str):
                supervise.emit(
                    {
                        "event": "error",
                        "code": supervise.CODE_CONFIG,
                        "message": exc.code,
                        "exc_type": "SystemExit",
                    }
                )
            raise
        except Exception as exc:
            logging.getLogger("stt_server").error("stt_server: fatal error", exc_info=exc)
            code, message = supervise.classify_startup_error(exc)
            supervise.emit(
                {"event": "error", "code": code, "message": message, "exc_type": type(exc).__name__}
            )
            raise SystemExit(1) from None


def _report_fatal(exc: BaseException, *, supervised: bool) -> None:
    """Report a serve failure the way ``_run_serve`` / ``_cmd_serve`` would:
    the stderr line (a one-line message for the expected startup errors, a
    traceback otherwise) and, under ``--supervised``, the ``error`` event.

    ``serve`` calls it (``on_fatal``) right before its live-work ``os._exit``,
    which skips the handlers below."""
    if isinstance(exc, (ValueError, OSError, ImportError)):
        print(f"stt_server: {exc}", file=sys.stderr)
    else:
        logging.getLogger("stt_server").error("stt_server: fatal error", exc_info=exc)
    if supervised:
        from . import supervise

        code, message = supervise.classify_startup_error(exc)
        supervise.emit(
            {"event": "error", "code": code, "message": message, "exc_type": type(exc).__name__}
        )


def _run_serve(args: argparse.Namespace, **serve_kwargs) -> None:
    backend = _make_backend(args.backend, _resolve_model(args.backend, args.model))
    try:
        asyncio.run(
            serve(
                backend,
                socket_path=args.socket_path,
                host=args.host,
                port=args.port,
                auth_token=_resolve_auth_token(args.auth_token_file),
                # The CLI owns the process: a stop during startup/warm-up exits
                # at once rather than waiting on an uncancellable model load.
                hard_exit_on_stop=True,
                on_fatal=functools.partial(
                    _report_fatal, supervised=getattr(args, "supervised", False)
                ),
                **serve_kwargs,
            )
        )
    except (ValueError, OSError, ImportError) as exc:
        # Surface startup failures (socket-dir enforcement refusing to bind,
        # the ServerConfig.__post_init__ ValueError, bind OSErrors, and a
        # missing backend extra re-raised by backend.start()) as an actionable
        # one-line message + exit 1 rather than a bare traceback. ``ImportError``
        # (superset of ``ModuleNotFoundError``) also catches a partially-present
        # extra whose import fails on a missing sub-symbol.
        print(f"stt_server: {exc}", file=sys.stderr)
        # Explicit ``from exc``: ``_cmd_serve`` classifies the supervised error
        # event from ``__cause__``.
        raise SystemExit(1) from exc


async def _probe_status(args: argparse.Namespace) -> dict:
    from . import protocol as P
    from .client import TranscriptionClient, format_host_for_uri, is_cleartext_remote

    endpoint = _resolve_probe_endpoint(args)
    auth_token = _resolve_auth_token(args.auth_token_file, client=True)

    # Same cleartext-token guard as the bot's runtime resolver: if a bearer
    # is configured and the effective endpoint is cleartext-ws to a
    # non-loopback host, warn before opening the connection. Host+port is
    # lowered to ``ws://host:port/`` via the same formatter the client
    # uses so the check is identical regardless of config surface.
    if auth_token:
        effective_uri = endpoint.get("uri")
        if (
            not effective_uri
            and not endpoint.get("socket_path")
            and endpoint.get("host")
            and endpoint.get("port") is not None
        ):
            effective_uri = f"ws://{format_host_for_uri(endpoint['host'])}:{endpoint['port']}/"
        if effective_uri and is_cleartext_remote(effective_uri):
            print(
                f"stt_server: warning — auth token will be sent in cleartext to {effective_uri}. "
                "Use wss:// for remote hosts, or bind to loopback (127.0.0.1 / ::1 / UDS).",
                file=sys.stderr,
            )

    client = TranscriptionClient(**endpoint, auth_token=auth_token)

    async def _run() -> dict:
        hello = await client.connect()
        await client.status()

        # Drain until we see the server.status reply, ignoring the
        # session.updated / transcription_session.updated that may arrive
        # first if the server echoes defaults on connect.
        async for ev in client.events():
            if ev.get("type") == P.EVT_SERVER_STATUS:
                return {"hello": hello, "status": ev}
        raise RuntimeError("socket closed before server.status reply")

    try:
        # Single wall-clock budget for the whole probe — connect + status
        # round-trip combined — so ``--timeout`` means what ``--help`` says.
        return await asyncio.wait_for(_run(), timeout=args.timeout)
    finally:
        # Both close steps are guarded: if ``connect()`` failed before
        # assigning ``_ws`` the inner assert in close_session would raise
        # AttributeError and mask the real error; close() itself is
        # idempotent but wrap it anyway to be safe under cancellation.
        try:
            await client.close_session()
        except Exception:
            pass
        try:
            await client.close()
        except Exception:
            pass


def _cmd_status(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.WARNING)
    try:
        result = asyncio.run(_probe_status(args))
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        print(f"stt_server: not reachable ({exc})", file=sys.stderr)
        raise SystemExit(1)
    except asyncio.TimeoutError:
        print(f"stt_server: timed out after {args.timeout}s", file=sys.stderr)
        raise SystemExit(1)
    except OSError as exc:
        print(f"stt_server: socket error ({exc})", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(f"stt_server: probe failed ({exc})", file=sys.stderr)
        raise SystemExit(1)

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    hello = result["hello"]
    status = result["status"]
    caps = hello.get("capabilities", {})
    audio = hello.get("audio", {})
    print("stt_server: ok")
    print(f"  protocol_version: {hello.get('protocol_version')}")
    backend = hello.get("backend") or {}
    print(f"  backend: {backend.get('name')} (model: {backend.get('model')})")
    print(
        "  audio: {fmt} @ {rate} Hz / {ch}ch".format(
            fmt=audio.get("format"),
            rate=audio.get("rate"),
            ch=audio.get("channels"),
        )
    )
    print(
        "  capabilities: binary_audio={b} base64={s} server_vad={v}".format(
            b=caps.get("binary_audio"),
            s=caps.get("base64_audio_append"),
            v=caps.get("server_vad"),
        )
    )
    print(f"  session_id: {status.get('session_id')}")
    print(f"  queue_depth: {status.get('queue_depth')}")
    print(f"  uncommitted_bytes: {status.get('uncommitted_bytes')}")
    uptime = status.get("uptime_seconds")
    if isinstance(uptime, (int, float)):
        print(f"  session_uptime: {uptime:.1f}s")
    pid = status.get("pid")
    if pid is not None:
        print(f"  pid: {pid}")
    rss = status.get("rss_bytes")
    if isinstance(rss, (int, float)):
        print(f"  rss: {rss / (1024 * 1024):.1f}MB (peak)")


def main() -> None:
    # Accept both ``python -m stt_server <flags>`` (legacy serve path) and
    # ``python -m stt_server <subcommand> <flags>``. Detect the latter by a
    # non-flag first argv; otherwise dispatch to ``serve``. Top-level
    # ``-h``/``--help`` is NOT reinterpreted as a serve flag — that would
    # hide the ``status`` subcommand from the help text.
    argv = sys.argv[1:]
    top_level_help = argv and argv[0] in {"-h", "--help"}
    if argv and not argv[0].startswith("-") and argv[0] in {"serve", "status", "prefetch"}:
        sub, rest = argv[0], argv[1:]
    elif top_level_help:
        sub, rest = None, argv
    else:
        sub, rest = "serve", argv

    parser = argparse.ArgumentParser(prog="stt_server")
    subparsers = parser.add_subparsers(dest="cmd")

    p_serve = subparsers.add_parser("serve", help="run the server (default)")
    _add_endpoint_flags(p_serve)
    p_serve.add_argument("--backend", choices=BACKEND_CHOICES, default="echo")
    # Default is None so ``_resolve_model`` applies the backend's own default,
    # ``backends.BACKENDS[backend].default_model`` (the registry is the only
    # source; ``None`` for echo). An explicit value always wins and is passed
    # through verbatim.
    p_serve.add_argument("--model", default=None)
    p_serve.add_argument("--log-level", default="INFO")
    p_serve.add_argument(
        "--supervised",
        action="store_true",
        help=(
            "for a supervising app: print JSON lifecycle events (ready/error) on "
            "stdout and stop when stdin reaches EOF"
        ),
    )

    p_prefetch = subparsers.add_parser(
        "prefetch", help="download a backend's model weights without serving"
    )
    p_prefetch.add_argument("--backend", choices=BACKEND_CHOICES, required=True)
    p_prefetch.add_argument("--model", default=None)
    p_prefetch.add_argument(
        "--check",
        action="store_true",
        help="report whether the weights are already cached (never touches the network)",
    )
    p_prefetch.add_argument("--json", action="store_true", help="emit JSON-lines events on stdout")
    p_prefetch.add_argument("--log-level", default="WARNING")

    p_status = subparsers.add_parser(
        "status", help="probe a running server with server.status and print its reply"
    )
    _add_endpoint_flags(p_status, include_uri=True)
    p_status.add_argument(
        "--timeout", type=float, default=3.0, help="overall probe timeout in seconds"
    )
    p_status.add_argument("--json", action="store_true", help="emit raw JSON instead of text")

    if sub is None:
        # Top-level --help path: argparse prints both subcommands and exits.
        parser.parse_args(rest)
        return
    args = parser.parse_args([sub, *rest])
    if args.cmd == "status":
        _cmd_status(args)
    elif args.cmd == "prefetch":
        _cmd_prefetch(args)
    else:
        _cmd_serve(args)


def _cmd_prefetch(args: argparse.Namespace) -> None:
    """``prefetch`` subcommand: download (or ``--check``) a backend's weights."""
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from . import prefetch

    # Echo has no default model; an explicit ``--model`` reaches prefetch,
    # which refuses it (ConfigError) rather than reporting it cached.
    model = _resolve_model(args.backend, args.model)
    code = prefetch.run_prefetch(args.backend, model, check=args.check, json_mode=args.json)
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
