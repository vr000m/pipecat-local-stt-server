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
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from .backend import EchoBackend
from .env import env_first
from .server import serve

# ``TranscriptionClient`` and ``protocol`` are imported lazily inside the
# status-subcommand helpers so the serve path (run at every launchd startup)
# doesn't pay for ``websockets.asyncio.client`` it never uses.


# Whisper repo default for ``--backend mlx``. Kept as a module constant so the
# backend-aware ``--model`` resolution and the "model unset" sentinel agree on
# one value. ``parakeet``'s default lives in ``backends/parakeet.py`` and is
# imported lazily (it would otherwise pull the parakeet module at every
# launch, defeating the lean-base invariant).
_DEFAULT_MLX_MODEL = "mlx-community/whisper-large-v3-turbo"


def _make_backend(name: str, model: str):
    if name == "echo":
        return EchoBackend()
    if name == "mlx":
        from .backends.mlx_whisper import MLXWhisperBackend

        return MLXWhisperBackend(model=model)
    if name == "parakeet":
        # Lazy import so a base install without the ``stt-server-parakeet``
        # extra still constructs ``echo``/``mlx`` backends. ``parakeet.py``
        # imports ``parakeet_mlx`` only inside ``start()`` / ``_get_model``,
        # never at module load, so this import does NOT transitively pull
        # ``parakeet_mlx`` — the missing-extra failure surfaces fast in
        # ``start()``, not here at construction.
        from .backends.parakeet import ParakeetBackend

        return ParakeetBackend(model=model)
    raise SystemExit(f"unknown backend: {name}")


def _resolve_model(backend: str, model: str | None) -> str:
    """Resolve the effective decode model id.

    An explicit ``--model`` always wins (it is passed through verbatim — the
    server-side ``--backend`` is the trust anchor; pointing a backend at a
    mismatched repo id fails fast in ``start()``/decode, and classifying a
    repo id as "whisper" vs "parakeet" would need a brittle string heuristic).
    When ``--model`` is unset the default is backend-aware: ``parakeet`` uses
    ``DEFAULT_PARAKEET_MODEL`` rather than the Whisper repo.
    """
    if model is not None:
        return model
    if backend == "parakeet":
        from .backends.parakeet import DEFAULT_PARAKEET_MODEL

        return DEFAULT_PARAKEET_MODEL
    return _DEFAULT_MLX_MODEL


def _resolve_auth_token(token_file: str | None, *, client: bool = False) -> str | None:
    # INVARIANT — do not flap. See the "Probe Auth Invariant" block in
    # docs/dev_plans/20260420-design-whisper-websocket-server.md for the
    # full history (four review cycles circled this); any change here
    # must update that block and the regression tests in
    # tests/test_stt_server.py.
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


def _cmd_serve(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    backend = _make_backend(args.backend, _resolve_model(args.backend, args.model))
    asyncio.run(
        serve(
            backend,
            socket_path=args.socket_path,
            host=args.host,
            port=args.port,
            auth_token=_resolve_auth_token(args.auth_token_file),
        )
    )


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
    if argv and not argv[0].startswith("-") and argv[0] in {"serve", "status"}:
        sub, rest = argv[0], argv[1:]
    elif top_level_help:
        sub, rest = None, argv
    else:
        sub, rest = "serve", argv

    parser = argparse.ArgumentParser(prog="stt_server")
    subparsers = parser.add_subparsers(dest="cmd")

    p_serve = subparsers.add_parser("serve", help="run the server (default)")
    _add_endpoint_flags(p_serve)
    p_serve.add_argument("--backend", choices=("echo", "mlx", "parakeet"), default="echo")
    # Default is None so ``_resolve_model`` can apply a backend-aware fallback
    # (Whisper repo for ``mlx``, ``DEFAULT_PARAKEET_MODEL`` for ``parakeet``).
    # An explicit value always wins and is passed through verbatim.
    p_serve.add_argument("--model", default=None)
    p_serve.add_argument("--log-level", default="INFO")

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
    else:
        _cmd_serve(args)


if __name__ == "__main__":
    main()
