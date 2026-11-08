"""Fake recorder CLI: replay a JSONL script into the local handoff spool."""
import argparse
import json
from pathlib import Path
import signal
import sys
import threading

from . import __version__
from .replay import FakeReplay
from .script import parse_script

MAX_SCRIPT_BYTES = 1_000_000


def parser():
    root = argparse.ArgumentParser(description='Local meeting recorder (fake replay)')
    root.add_argument('--version', action='version', version=__version__)
    commands = root.add_subparsers(dest='command')
    fake = commands.add_parser('fake', help='replay a JSONL script as caption events')
    fake.add_argument('script', type=Path, help='JSONL rows: speaker, text, delay_ms')
    fake.add_argument('--session', required=True, help='advisor local session id (local:…)')
    fake.add_argument('--data-root', type=Path,
                      help='shared data root that contains recorder/ (default: repo data/)')
    fake.add_argument('--speed', type=float, default=1.0, help='replay speed multiplier')
    return root


def _read_script(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError('script must be a regular file')
    raw = path.read_bytes()
    if len(raw) > MAX_SCRIPT_BYTES:
        raise ValueError('script exceeds 1000000 bytes')
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('script must be UTF-8 encoded') from None


def main(argv=None):
    args = parser().parse_args(argv)
    if not args.command:
        parser().print_help()
        return 0
    try:
        lines = parse_script(_read_script(args.script))
        data_root = args.data_root or Path(__file__).resolve().parents[2].parent / 'data'
        replay = FakeReplay(lines, session_id=args.session, data_root=data_root,
                            speed=args.speed)
        stop = threading.Event()

        def interrupt(signum, frame):
            stop.set()

        previous = signal.signal(signal.SIGINT, interrupt)
        try:
            summary = replay.run(stop_event=stop)
        finally:
            signal.signal(signal.SIGINT, previous)
    except (ValueError, OSError) as exc:
        print('Operation stopped: ' + str(exc), file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
