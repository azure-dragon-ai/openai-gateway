"""openai-gateway entrypoint.

Usage:
  python main.py --config config.yaml [--host 0.0.0.0] [--port 8100]
"""
from __future__ import annotations

import argparse
import logging

import uvicorn

from gateway.config import load_config
from gateway.router import create_app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="OpenAI-compatible relay gateway")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--host", default=None, help="override server host")
    parser.add_argument("--port", type=int, default=None, help="override server port")
    args = parser.parse_args()

    cfg = load_config(args.config)
    app = create_app(cfg)
    host = args.host or cfg.server.host
    port = args.port or cfg.server.port

    logging.getLogger("openai-gateway").info(
        "starting openai-gateway on %s:%s (upstreams: %s)",
        host, port, sorted(cfg.upstreams),
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()