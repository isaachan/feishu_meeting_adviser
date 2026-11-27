"""CLI entry point. The replay commands are added in PS5."""
from __future__ import annotations

import argparse

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='prompt-studio',
        description='Replay admin TXT meeting transcripts through a real advisor.')
    parser.add_argument('--version', action='version', version=__version__)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
