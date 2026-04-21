"""``python -m stt_server`` entrypoint.

Starts the server with the ``EchoBackend`` by default. Pass ``--backend mlx``
to use ``MLXWhisperBackend`` (requires ``mlx_whisper`` installed).
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from .backend import EchoBackend
from .server import serve


def _make_backend(name: str, model: str):
    if name == "echo":
        return EchoBackend()
    if name == "mlx":
        from .backends.mlx_whisper import MLXWhisperBackend

        return MLXWhisperBackend(model=model)
    raise SystemExit(f"unknown backend: {name}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="stt_server")
    parser.add_argument("--socket-path", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--auth-token", default=None)
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
            auth_token=args.auth_token,
        )
    )


if __name__ == "__main__":
    main()
