"""Recorder CLI: fake script replay and real audio capture."""
import argparse
import json
from pathlib import Path
import signal
import sys
import threading

from . import __version__
from .real import RealRecorder
from .replay import FakeReplay
from .script import parse_script

MAX_SCRIPT_BYTES = 1_000_000


def parser():
    root = argparse.ArgumentParser(description='Local meeting recorder')
    root.add_argument('--version', action='version', version=__version__)
    commands = root.add_subparsers(dest='command')
    fake = commands.add_parser('fake', help='replay a JSONL script as caption events')
    fake.add_argument('script', type=Path, help='JSONL rows: speaker, text, delay_ms')
    fake.add_argument('--session', required=True, help='advisor local session id (local:…)')
    fake.add_argument('--data-root', type=Path,
                      help='shared data root that contains recorder/ (default: repo data/)')
    fake.add_argument('--speed', type=float, default=1.0, help='replay speed multiplier')
    real = commands.add_parser(
        'real', help='capture Feishu audio (Mac) and transcribe into the spool')
    real.add_argument('--session', required=True, help='advisor local session id (local:…)')
    real.add_argument('--data-root', type=Path,
                      help='shared data root that contains recorder/ (default: repo data/)')
    real.add_argument('--mic', action='store_true',
                      help='enable the local microphone (manual switch; default off)')
    real.add_argument('--mic-speaker', default='本机',
                      help='speaker label for microphone captions (default 本机)')
    real.add_argument('--duration', type=float,
                      help='clean stop after N seconds (writes stream_end); '
                           'without it run until Ctrl-C (interrupt keeps the stream open)')
    real.add_argument('--stt', choices=['remote-stream', 'sherpa', 'zhipu'],
                      default='remote-stream',
                      help='STT backend: remote-stream (default; server-side '
                           'sherpa via --stt-url/LMR_STT_URL), sherpa local '
                           'streaming, or zhipu file upload')
    real.add_argument('--stt-url',
                      help='stt-server WebSocket URL, e.g. ws://host:8392 '
                           '(default: $LMR_STT_URL)')
    real.add_argument('--stt-model-dir', type=Path,
                      help='sherpa model directory (default: $LMR_STT_MODEL_DIR)')
    return root


def _default_data_root() -> Path:
    return Path(__file__).resolve().parents[2].parent / 'data'


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


def _install_interrupt(stop: threading.Event):
    def interrupt(signum, frame):
        stop.set()

    previous = signal.signal(signal.SIGINT, interrupt)
    return previous


def main(argv=None):
    args = parser().parse_args(argv)
    if not args.command:
        parser().print_help()
        return 0
    try:
        if args.command == 'fake':
            lines = parse_script(_read_script(args.script))
            data_root = args.data_root or _default_data_root()
            replay = FakeReplay(lines, session_id=args.session, data_root=data_root,
                                speed=args.speed)
            stop = threading.Event()
            previous = _install_interrupt(stop)
            try:
                summary = replay.run(stop_event=stop)
            finally:
                signal.signal(signal.SIGINT, previous)
        else:
            data_root = args.data_root or _default_data_root()
            recorder = RealRecorder(session_id=args.session, data_root=data_root,
                                    mic_enabled=args.mic,
                                    mic_speaker=args.mic_speaker,
                                    stt_mode=args.stt,
                                    stt_model_dir=args.stt_model_dir,
                                    stt_url=args.stt_url)
            stop = threading.Event()
            previous = _install_interrupt(stop)
            try:
                summary = recorder.run(stop_event=stop,
                                       duration_s=args.duration)
            finally:
                signal.signal(signal.SIGINT, previous)
    except (ValueError, OSError) as exc:
        print('Operation stopped: ' + str(exc), file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
