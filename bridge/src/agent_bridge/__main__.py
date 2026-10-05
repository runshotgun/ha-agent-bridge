"""Entry point: `agent-bridge --config <path>`."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from aiohttp import web

from .config import load_config
from .server import build_app

DEFAULT_CONFIG = Path("~/Library/Application Support/AgentBridge/config.toml").expanduser()


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent-bridge")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config(args.config)
    web.run_app(build_app(config), host=config.host, port=config.port, print=None)


if __name__ == "__main__":
    main()
