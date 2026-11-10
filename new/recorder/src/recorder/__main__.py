"""Recorder CLI per CONTRACT_RECORDER_STREAM appendix A.

Subcommands:
  real   — real capture (SCK loopback; optional mic) streamed to the ingest URL
  replay — fake scripted capture (integration test bed), same uplink path

Diagnostics: per-source uplink stats (sent/acked/buffered/reconnects/drops)
printed periodically; exit codes follow appendix A.5 (0/2/3/130).
"""
import argparse
import json
import signal
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .audio import CaptureSession, FakeSource
from .audio.frames import AudioFrame, SourceEvent
from .audio.sck_capture import SwiftLoopbackSource, SourceStartError
from .audio.sources import MicSource
from .uplink import SourceUplink

EXIT_OK, EXIT_ARGS, EXIT_CONN, EXIT_INTERRUPT = 0, 2, 3, 130
_SETTLE_TIMEOUT_S = 2.0


def wait_capture_drained(session, subscriber, sources, timeout=_SETTLE_TIMEOUT_S):
    """Let in-flight frames reach the uplink buffer before a clean stop."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (subscriber.empty()
                and all(len(session._pending[sid]) == 0 for sid in session.sources)):
            time.sleep(0.05)
            return
        time.sleep(0.02)
ARG_SPECS = {
    'session': lambda v: isinstance(v, str) and 6 <= len(v) <= 64 and v.replace('-', '').isalnum() and v[:1].isalnum(),
    'token': lambda v: isinstance(v, str) and 16 <= len(v) <= 128 and all(c.isalnum() or c in '-_' for c in v),
}


def add_stream_args(parser):
    parser.add_argument('--session', required=True, help='cloud session id')
    parser.add_argument('--event', required=True, help='<calendar_id>/<event_id> binding')
    parser.add_argument('--url', required=True, help='ws://<host>:8400/ingest')
    parser.add_argument('--token', required=True, help='short-lived stream token')
    parser.add_argument('--duration', type=float, help='clean stop after N seconds')


def validate_args(args):
    if not ARG_SPECS['session'](args.session):
        return 'invalid --session charset/length'
    if '/' not in args.event or any(not part for part in args.event.split('/', 1)):
        return '--event must be <calendar_id>/<event_id>'
    if not args.url.startswith('ws://') or not args.url.rstrip('/').endswith('/ingest'):
        return '--url must be a ws:// ingest endpoint'
    if not ARG_SPECS['token'](args.token):
        return 'invalid --token charset/length'
    if args.duration is not None and args.duration <= 0:
        return '--duration must be positive'
    return None


def run_recording(args, sources, out_root):
    session = CaptureSession(sources, out_root)
    uplinks = []
    for source in sources:
        uplink = SourceUplink(
            source.source_id, session_id=args.session, event=args.event,
            url=args.url, token=args.token,
            speaker=getattr(args, 'mic_speaker', None) if source.source_id == 'mic' else None)
        uplinks.append(uplink)
    subscriber = session.subscribe()
    session.start()
    for uplink in uplinks:
        uplink.start()

    stop_flag = threading.Event()

    def pump(item):
        if not isinstance(item, (AudioFrame, SourceEvent)):
            return
        for uplink in uplinks:
            if item.source_id == uplink.source_id:
                uplink.offer(item)

    def drain():
        while not stop_flag.is_set():
            try:
                item = subscriber.get(timeout=0.1)
            except Exception:
                continue
            pump(item)

    drainer = threading.Thread(target=drain, daemon=True, name='uplink-pump')
    drainer.start()

    interrupted = threading.Event()
    original = signal.getsignal(signal.SIGINT)

    def on_sigint(_sig, _frame):
        interrupted.set()
        stop_flag.set()

    signal.signal(signal.SIGINT, on_sigint)
    started = time.monotonic()
    report_at = started + 5
    try:
        while not stop_flag.is_set():
            if args.duration is not None and time.monotonic() - started >= args.duration:
                break
            if all(uplink.stopped.is_set() for uplink in uplinks):
                break
            if time.monotonic() >= report_at:
                report_at += 5
                for uplink in uplinks:
                    print(f'[uplink] {json.dumps(uplink.stats(), ensure_ascii=False)}',
                          flush=True)
            time.sleep(0.1)
    finally:
        signal.signal(signal.SIGINT, original)
        if interrupted.is_set():
            for uplink in uplinks:
                uplink.stop_interrupt()
        else:
            wait_capture_drained(session, subscriber, uplinks)
            for uplink in uplinks:
                uplink.stop_clean()
        for uplink in uplinks:
            uplink.join(timeout=15)
        stop_flag.set()
        drainer.join(timeout=2)
        summary = session.stop()
        for uplink in uplinks:
            print(f'[uplink-final] {json.dumps(uplink.stats(), ensure_ascii=False)}')
            print(f'[exit] {uplink.source_id}: code={uplink.exit_code} '
                  f'reason={uplink.exit_reason or "clean"}')
    if interrupted.is_set():
        return EXIT_INTERRUPT
    failed = [uplink for uplink in uplinks if uplink.exit_code != 0]
    if failed:
        return failed[0].exit_code
    print(f'[summary] {json.dumps(summary, ensure_ascii=False)}')
    return EXIT_OK


def sources_for_real(args, out_root):
    sources: list = [SwiftLoopbackSource('loopback')]
    if args.mic:
        sources.append(MicSource('mic'))
    return sources


def cmd_real(args):
    invalid = validate_args(args)
    if invalid:
        print(f'recorder: {invalid}', file=sys.stderr)
        return EXIT_ARGS
    out_root = Path(args.data_root) / 'recorder' / 'audio' / args.session
    sources = sources_for_real(args, out_root)
    return run_recording(args, sources, out_root)


def cmd_replay(args):
    invalid = validate_args(args)
    if invalid:
        print(f'recorder: {invalid}', file=sys.stderr)
        return EXIT_ARGS
    script = [json.loads(line) for line in Path(args.script).read_text().splitlines()
              if line.strip()]
    out_root = Path(args.data_root) / 'recorder' / 'audio' / args.session
    source = FakeSource('loopback')
    session = CaptureSession([source], out_root)
    uplink = SourceUplink('loopback', session_id=args.session, event=args.event,
                          url=args.url, token=args.token)
    subscriber = session.subscribe()
    session.start()
    uplink.start()

    stop_flag = threading.Event()

    def drain():
        while not stop_flag.is_set():
            try:
                item = subscriber.get(timeout=0.1)
            except Exception:
                continue
            uplink.offer(item)

    drainer = threading.Thread(target=drain, daemon=True)
    drainer.start()
    interrupted = threading.Event()

    def on_sigint(_sig, _frame):
        interrupted.set()
        stop_flag.set()

    signal.signal(signal.SIGINT, on_sigint)
    try:
        for row in script:
            if stop_flag.is_set():
                break
            delay = row.get('delay_ms', 500) / 1000.0
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline and not stop_flag.is_set():
                time.sleep(0.05)
            if stop_flag.is_set():
                break
            source.push_frames(1)
    finally:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        if interrupted.is_set():
            uplink.stop_interrupt()
        else:
            wait_capture_drained(session, subscriber, ['loopback'])
            uplink.stop_clean()
        uplink.join(timeout=15)
        stop_flag.set()
        drainer.join(timeout=2)
        summary = session.stop()
        print(f'[uplink-final] {json.dumps(uplink.stats(), ensure_ascii=False)}')
        print(f'[exit] loopback: code={uplink.exit_code} reason={uplink.exit_reason or "clean"}')
    if interrupted.is_set():
        return EXIT_INTERRUPT
    if uplink.exit_code != 0:
        return uplink.exit_code
    print(f'[summary] {json.dumps(summary, ensure_ascii=False)}')
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(prog='recorder',
                                     description='会参谋采集器（laptop 端）')
    parser.add_argument('--version', action='version', version=__version__)
    sub = parser.add_subparsers(dest='command', required=True)

    real = sub.add_parser('real', help='capture Feishu audio and stream to ingest')
    add_stream_args(real)
    real.add_argument('--mic', action='store_true',
                      help='enable the local microphone (default off)')
    real.add_argument('--mic-speaker', default='本机',
                      help='speaker label for mic captions (default 本机)')
    real.add_argument('--data-root', type=Path, default=Path('data'),
                      help='local retention root (default ./data)')
    real.set_defaults(handler=cmd_real)

    replay = sub.add_parser('replay', help='fake scripted capture over the same uplink')
    add_stream_args(replay)
    replay.add_argument('script', type=Path,
                        help='JSONL rows: {"delay_ms": 500, "text": "ignored"}')
    replay.add_argument('--data-root', type=Path, default=Path('data'))
    replay.set_defaults(handler=cmd_replay)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        raise SystemExit(args.handler(args))
    except SourceStartError as exc:
        print(f'recorder: loopback start failed: {exc}', file=sys.stderr)
        raise SystemExit(EXIT_CONN) from None


if __name__ == '__main__':
    main()
