"""Entry point: python -m gui (or the gui console script)."""
import argparse
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(prog='gui',
                                     description='会参谋采集客户端（GUI）')
    parser.parse_args(argv)
    from .app import run
    return run(argv)


if __name__ == '__main__':
    sys.exit(main())
