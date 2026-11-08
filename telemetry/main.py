"""CLI entry point."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import List, Optional

import uvicorn

from . import __version__
from .api import create_app
from .config import Settings

logger = logging.getLogger("telemetry")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telemetry",
        description="Lightweight system telemetry server (FastAPI + psutil)",
    )
    parser.add_argument("--host", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, help="Bind port (default: 8000)")
    parser.add_argument(
        "--interval", type=float, help="Collection interval in seconds (default: 2)"
    )
    parser.add_argument(
        "--buffer-size", type=int, help="Ring buffer size in points (default: 3600)"
    )
    parser.add_argument(
        "--sqlite", type=Path, help="Enable SQLite persistence at this path"
    )
    parser.add_argument("--token", help="Require this bearer token for the API")
    parser.add_argument(
        "--log-level",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="Log verbosity (default: info)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
        if args.host is not None:
            settings.host = args.host
        if args.port is not None:
            if not 1 <= args.port <= 65535:
                parser.error("--port must be in 1..65535")
            settings.port = args.port
        if args.interval is not None:
            settings.interval_sec = settings.validate_interval(args.interval)
        if args.buffer_size is not None:
            if args.buffer_size < 2:
                parser.error("--buffer-size must be >= 2")
            settings.buffer_size = args.buffer_size
        if args.sqlite is not None:
            settings.sqlite_path = args.sqlite
        if args.token is not None:
            settings.auth_token = args.token
        if args.log_level is not None:
            settings.log_level = args.log_level
    except ValueError as exc:
        parser.error(str(exc))

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info(
        "Starting telemetry on %s:%s (interval=%ss, buffer=%s, sqlite=%s)",
        settings.host,
        settings.port,
        settings.interval_sec,
        settings.buffer_size,
        settings.sqlite_path or "disabled",
    )
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        workers=1,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
