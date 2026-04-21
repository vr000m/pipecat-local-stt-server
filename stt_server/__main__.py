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
from .server import serve

# ``TranscriptionClient`` and ``protocol`` are imported lazily inside the
# status-subcommand helpers so the serve path (run at every launchd startup)
# doesn't pay for ``websockets.asyncio.client`` it never uses.


def _make_backend(name: str, model: str):
    if name == "echo":
        return EchoBackend()
    if name == "mlx":
        from .backends.mlx_whisper import MLXWhisperBackend

        return MLXWhisperBackend(model=model)
    raise SystemExit(f"unknown backend: {name}")


def _resolve_auth_token(token_file: str | None, *, client: bool = False) -> str | None:
    # Precedence: --auth-token-file > KODA_STT_AUTH_TOKEN env.
    # A plaintext --auth-token CLI flag is intentionally unsupported: any
    # local user would be able to read the token via `ps`.
    #
    # ``client=True`` is used by the status/probe subcommand: it also
    # consults ``STT_WS_TOKEN`` (the env name the bot reads via
    # ``bot/runtime._resolve_stt_ws_target``) so ``./koda stt status``
    # authenticates against token-protected servers the same way the bot
    # does — without forcing operators to duplicate the secret under a
    # second env name.
    if token_file:
        return Path(token_file).read_text(encoding="utf-8").strip() or None
    env_val = os.environ.get("KODA_STT_AUTH_TOKEN")
    if env_val:
        return env_val
    if client:
        alt = (os.environ.get("STT_WS_TOKEN") or "").strip()
        if alt:
            return alt
    return None


def _load_dotenv_best_effort() -> None:
    """Mirror the bot's dotenv discipline so ``stt_server status`` sees the
    same ``STT_WS_*`` configuration the bot would at startup.

    Kept optional (ImportError swallowed) so the serve path — which does
    not need dotenv — stays usable if python-dotenv is absent from a
    minimal deployment.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    # Same order and ``override=False`` semantics as bot/__main__.py and
    # bot/dual.py so an already-exported env var always wins.
    load_dotenv(Path.cwd() / ".env", override=False)
    load_dotenv(Path.home() / ".secrets" / "ai.env", override=False)


def _resolve_probe_endpoint(args: argparse.Namespace) -> dict:
    """Return the endpoint kwargs for ``TranscriptionClient`` used by the
    status probe. If the caller passed any endpoint flag explicitly, honor
    exactly that (enforcing ``uri > socket_path > host+port`` so the
    client's socket_path bias cannot mask a URI override). Otherwise load
    dotenv and read the same ``STT_WS_*`` env vars the bot resolves at
    startup — this is what makes ``./koda stt status`` report on the
    same endpoint the bot would actually connect to.
    """
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

    _load_dotenv_best_effort()
    env = os.environ
    uri = (env.get("STT_WS_URI") or "").strip() or None
    sock = (env.get("STT_WS_SOCKET") or "").strip() or None
    host = (env.get("STT_WS_HOST") or "").strip() or None
    port_raw = (env.get("STT_WS_PORT") or "").strip()
    port = int(port_raw) if port_raw else None

    if not (uri or sock or host):
        sock = env.get("STT_WS_DEFAULT_SOCKET") or os.path.expanduser(
            "~/Library/Caches/koda-stt/stt.sock"
        )
    if uri:
        sock = None
        host = None
        port = None
    elif sock:
        host = None
        port = None

    return {"uri": uri, "socket_path": sock, "host": host, "port": port}


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
    backend = _make_backend(args.backend, args.model)
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
    from .client import TranscriptionClient

    endpoint = _resolve_probe_endpoint(args)
    client = TranscriptionClient(
        **endpoint,
        auth_token=_resolve_auth_token(args.auth_token_file, client=True),
    )
    hello = await asyncio.wait_for(client.connect(), timeout=args.timeout)
    try:
        await client.status()

        # Drain until we see the server.status reply, ignoring the
        # session.updated / transcription_session.updated that may arrive
        # first if the server echoes defaults on connect.
        async def _next_status() -> dict:
            async for ev in client.events():
                if ev.get("type") == P.EVT_SERVER_STATUS:
                    return ev
            raise RuntimeError("socket closed before server.status reply")

        status = await asyncio.wait_for(_next_status(), timeout=args.timeout)
        return {"hello": hello, "status": status}
    finally:
        try:
            await client.close_session()
        except Exception:
            pass
        await client.close()


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
    p_serve.add_argument("--backend", choices=("echo", "mlx"), default="echo")
    p_serve.add_argument("--model", default="mlx-community/whisper-large-v3-turbo")
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
