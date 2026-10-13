"""Command line entry point; commands are added with their implementations."""

import argparse
from . import __version__


def main():
    parser = argparse.ArgumentParser(description="Feishu meeting transcript source")
    parser.add_argument("--version", action="version", version=__version__)
    parser.parse_args()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
