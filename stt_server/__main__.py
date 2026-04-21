"""``python -m stt_server`` entrypoint.

Starts the server with the ``EchoBackend`` by default. Pass ``--backend mlx``
to use ``MLXWhisperBackend`` (requires ``mlx_whisper`` installed).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from .backend import EchoBackend
from .server import serve


def _make_backend(name: str, model: str):
    if name == "echo":
        return EchoBackend()
    if name == "mlx":
        from .backends.mlx_whisper import MLXWhisperBackend

        return MLXWhisperBackend(model=model)
    raise SystemExit(f"unknown backend: {name}")


def _resolve_auth_token(cli_value: str | None, token_file: str | None) -> str | None:
    # Precedence: --auth-token-file > KODA_STT_AUTH_TOKEN env > --auth-token (ps-visible, deprecated).
    if token_file:
        return Path(token_file).read_text(encoding="utf-8").strip() or None
    env_val = os.environ.get("KODA_STT_AUTH_TOKEN")
    if env_val:
        return env_val
    return cli_value


def main() -> None:
    parser = argparse.ArgumentParser(prog="stt_server")
    parser.add_argument("--socket-path", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--auth-token",
        default=None,
        help=(
            "Auth token (DEPRECATED: visible to any local user via ps). "
            "Prefer KODA_STT_AUTH_TOKEN env or --auth-token-file."
        ),
    )
    parser.add_argument(
        "--auth-token-file",
        default=None,
        help="Path to a file containing the auth token (whitespace-stripped).",
    )
    parser.add_argument("--backend", choices=("echo", "mlx"), default="echo")
    parser.add_argument("--model", default="mlx-community/whisper-large-v3-turbo")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

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
            auth_token=_resolve_auth_token(args.auth_token, args.auth_token_file),
        )
    )


if __name__ == "__main__":
    main()
