"""Command line entry: llm-harness --url http://127.0.0.1:8377."""
import argparse

from . import __version__
from .client import GatewayClient
from .repl import HarnessRepl
from .session import MeetingSession


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='llm-harness',
        description='交互式 REPL:模拟 advisor 模型客户端,向 llm-gateway 喂会议文字并观察 decision')
    parser.add_argument('--url', default='http://127.0.0.1:8377',
                        help='llm-gateway 基地址(默认 http://127.0.0.1:8377)')
    parser.add_argument('--backend', default='opencode', choices=['opencode', 'fake'],
                        help='网关推理后端(默认 opencode;fake 用于链路自检)')
    parser.add_argument('--deadline', type=int, default=90,
                        help='单次推理期限秒数(默认 90,网关上限 120)')
    parser.add_argument('--version', action='version', version=f'llm-harness {__version__}')
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    client = GatewayClient(args.url, backend=args.backend, deadline_seconds=args.deadline)
    return HarnessRepl(client, MeetingSession()).run()


if __name__ == '__main__':
    raise SystemExit(main())
