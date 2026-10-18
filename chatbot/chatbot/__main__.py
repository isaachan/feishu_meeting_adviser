"""CLI: python -m chatbot --context context --backend opencode."""

import argparse
import logging
from pathlib import Path

from .backend import OpenCodeBackend
from .bot import ChatBot
from .config import load_settings
from .context import read_context


def main() -> None:
    parser = argparse.ArgumentParser(description="飞书聊天机器人")
    parser.add_argument("--context", required=True, type=Path, help="LLM 背景资料目录")
    parser.add_argument("--backend", required=True, choices=["opencode"], help="对话 agent 后端")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    context_dir = args.context.resolve(strict=True)
    read_context(context_dir, settings.max_context_bytes)
    ChatBot(settings, context_dir, OpenCodeBackend(settings)).start()


if __name__ == "__main__":
    main()
